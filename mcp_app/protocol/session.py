"""Session ID derivation and MCPPrompt event rate-limiting."""
import hashlib
import threading
import time
import uuid

SESSION_PROTOCOL_KEY = "mcp_protocol_version"
_SESSION_PROTOCOL_KEY = SESSION_PROTOCOL_KEY  # backward-compat alias

_PROMPT_EVENT_MAX_PER_MIN = 1000

_rate_lock    = threading.Lock()
_rate_bucket  = 0   # current minute bucket
_rate_count   = 0   # events in that bucket


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
    """Return True when under the per-minute MCPPrompt event budget."""
    global _rate_bucket, _rate_count
    bucket = int(time.time() // 60)
    with _rate_lock:
        if bucket != _rate_bucket:
            _rate_bucket = bucket
            _rate_count  = 0
        _rate_count += 1
        return _rate_count <= _PROMPT_EVENT_MAX_PER_MIN
