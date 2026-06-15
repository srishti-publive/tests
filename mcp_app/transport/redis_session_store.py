"""Redis-backed replacement for the in-process `_sse_sessions` dict.

This is the piece that actually removes the "exactly one gunicorn worker"
constraint (see docs/deployment.md, docs/mcp-protocol.md): `GET /mcp` (open) and
`POST /mcp/message` (route) can now land on different processes/replicas and
still find the session, because the registry lives in Redis rather than a
worker's memory.

`credentials` holds live CDS/CMS API secrets ({publisherId, apiKey, apiSecret}),
stored as plain JSON — matching how they are stored in Postgres (credential
encryption was deliberately removed; see git history). Lock down Redis network
access accordingly (Railway private networking / requirepass).
"""
import json
import logging
from typing import Optional

from mcp_app.redis_client import get_redis_client

logger = logging.getLogger(__name__)

# Safety-net TTL: refreshed on every message so long-lived sessions never expire
# mid-use, while sessions that die without a clean close (process crash, network
# partition) are reaped automatically — the in-process dict had no such concern
# because it died with the process. Comfortably longer than any plausible session.
_SESSION_TTL_SECONDS = 24 * 3600

# Set of currently-open session IDs, maintained alongside the per-session keys —
# gives an O(1) cluster-wide active-session count (SCARD) for the
# Custom/MCP/active_sessions metric and the SSE admission gate, without
# SCAN/KEYS over the keyspace.
_ACTIVE_SESSIONS_SET = "mcp:active_sessions"


def _session_key(session_id: str) -> str:
    return f"mcp:session:{session_id}"


def register_session(session_id: str, credentials: dict) -> int:
    """Register a newly opened SSE session. Returns the cluster-wide active count
    (including this session) — the admission gate in sse.py rejects and rolls back
    when this exceeds the cap."""
    client  = get_redis_client()
    payload = json.dumps({
        "credentials": credentials or {},
    })
    with client.pipeline() as pipe:
        pipe.set(_session_key(session_id), payload, ex=_SESSION_TTL_SECONDS)
        pipe.sadd(_ACTIVE_SESSIONS_SET, session_id)
        pipe.scard(_ACTIVE_SESSIONS_SET)
        _, _, active_count = pipe.execute()
    return active_count


def get_session(session_id: str) -> Optional[dict]:
    """Look up a session's credentials dict from any process. Returns None if absent,
    expired, or unparseable (forces a reconnect).

    Tolerates legacy payloads that still carry a `token_expires_at` key (written
    before it was removed) — only `credentials` is read, so the extra key is ignored
    until the entry ages out via TTL."""
    client = get_redis_client()
    raw    = client.get(_session_key(session_id))
    if raw is None:
        return None

    try:
        data        = json.loads(raw)
        credentials = data["credentials"]
    except (ValueError, KeyError):
        logger.error(
            "redis_session_store: unparseable session entry, treating as absent: session=%s",
            session_id,
        )
        return None

    # Refresh TTL on access so active sessions never expire mid-use (mirrors the
    # in-process dict's lifetime, which was bounded only by the process lifetime).
    client.expire(_session_key(session_id), _SESSION_TTL_SECONDS)
    return credentials


def close_session(session_id: str) -> int:
    """Remove a session on close. Returns the cluster-wide active count remaining."""
    client = get_redis_client()
    with client.pipeline() as pipe:
        pipe.delete(_session_key(session_id))
        pipe.srem(_ACTIVE_SESSIONS_SET, session_id)
        pipe.scard(_ACTIVE_SESSIONS_SET)
        _, _, active_count = pipe.execute()
    return active_count
