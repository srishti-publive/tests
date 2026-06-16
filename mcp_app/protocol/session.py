"""Session ID derivation and MCPPrompt event rate-limiting."""
import hashlib
import time
import uuid

from django.core.cache import cache

SESSION_PROTOCOL_KEY = "mcp_protocol_version"
_SESSION_PROTOCOL_KEY = SESSION_PROTOCOL_KEY  # backward-compat alias

# MCPPrompt event rate limit: at most this many per minute, cluster-wide. Backed
# by a fixed-window counter in the shared Django cache (DatabaseCache, key = current
# minute bucket) so the budget is shared across every worker/replica — a per-process
# counter would silently become `limit × process_count` once the app scales
# horizontally, defeating its purpose as a New-Relic cost-control gate.
_PROMPT_EVENT_MAX_PER_MIN = 1000
_PROMPT_EVENT_KEY_TTL     = 120  # > window length, so a bucket always self-expires


def derive_session_id(request) -> str:
    """Return a stable session identifier for this request.

    Priority:
    1. Django session key  (browser / session-cookie clients)
    2. SHA-256 prefix of Bearer token  (OAuth clients — same token → same ID across requests)
    3. Transient UUID  (unauthenticated or sessionless probes)
    """
    key = getattr(request.session, "session_key", None)
    if key:
        return key

    auth_header = request.META.get("HTTP_AUTHORIZATION", "")
    if auth_header.startswith("Bearer "):
        token = auth_header[len("Bearer "):]
        return "oauth-" + hashlib.sha256(token.encode()).hexdigest()[:16]

    return "anon-" + uuid.uuid4().hex[:8]


def should_emit_prompt_event() -> bool:
    """Return True when under the per-minute, cluster-wide MCPPrompt event budget.

    Fixed window keyed by the current UTC minute. `cache.add` seeds the bucket with
    a generous TTL (2x the window, so a bucket always self-expires) only on the
    first call; `cache.incr` then bumps the shared (DatabaseCache) counter. This
    read-modify-write isn't strictly atomic across workers, so the count can drift
    by a few under heavy concurrency — acceptable for a coarse
    cost-control gate that errs toward emitting, not dropping.
    """
    bucket = int(time.time() // 60)
    key    = f"mcp:prompt_events:{bucket}"
    # add() is a no-op if the key already exists, so only the first caller sets the TTL.
    cache.add(key, 0, timeout=_PROMPT_EVENT_KEY_TTL)
    try:
        count = cache.incr(key)
    except ValueError:
        # Bucket evicted/expired between add() and incr(); treat as a fresh window.
        cache.add(key, 1, timeout=_PROMPT_EVENT_KEY_TTL)
        count = 1
    return count <= _PROMPT_EVENT_MAX_PER_MIN
