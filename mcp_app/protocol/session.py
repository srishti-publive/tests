"""Session ID derivation and MCPPrompt event rate-limiting."""
import hashlib
import time
import uuid

from mcp_app.redis_client import get_redis_client

SESSION_PROTOCOL_KEY = "mcp_protocol_version"
_SESSION_PROTOCOL_KEY = SESSION_PROTOCOL_KEY  # backward-compat alias

# MCPPrompt event rate limit: at most this many per minute, cluster-wide. Backed
# by a Redis fixed-window counter (key = current minute bucket, INCR + EXPIRE) so
# the budget is shared across every worker/replica — a per-process counter would
# silently become `limit × process_count` once the app scales horizontally,
# defeating its purpose as a New-Relic cost-control gate.
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

    Fixed window keyed by the current UTC minute: INCR is atomic on the Redis
    side, so concurrent callers across every process/replica still get a single
    consistent count with no lock. EXPIRE is set only by whichever caller first
    creates the bucket (count == 1), and is generous (2x the window) so a
    slow-to-arrive request near a window boundary can't race the key's deletion.
    """
    client = get_redis_client()
    bucket = int(time.time() // 60)
    key    = f"mcp:prompt_events:{bucket}"
    count  = client.incr(key)
    if count == 1:
        client.expire(key, _PROMPT_EVENT_KEY_TTL)
    return count <= _PROMPT_EVENT_MAX_PER_MIN
