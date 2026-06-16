"""Database-backed SSE session registry.

`GET /mcp` (open) and `POST /mcp/message` (route) can land on different
processes/replicas and still find the session, because the registry lives in the
shared database.

Liveness is tracked by the `last_seen` heartbeat, refreshed on every keepalive
(`touch_session`). A session that dies without a clean close (TCP reset, worker
kill, redeploy) stops being refreshed and goes stale after `SESSION_TTL_SECONDS`;
stale rows no longer count against the admission gate and are pruned lazily on the
next `register_session`. This prevents orphaned rows from permanently consuming
the (small) `MCP_MAX_SSE_SESSIONS` budget and locking out every new stream.

`credentials` holds live CDS/CMS API secrets ({publisherId, apiKey, apiSecret}),
stored as plain JSON — matching how they're stored in Postgres elsewhere
(credential encryption was deliberately removed; see git history).
"""
import logging
import os
from typing import Optional

from django.db import transaction
from django.utils import timezone

from mcp_app.models import SSESession

logger = logging.getLogger(__name__)

# A session is "live" if its heartbeat landed within this window. Must comfortably
# exceed the SSE keepalive interval (25 s) so a healthy idle stream is never pruned
# — 3 missed keepalives before a row is considered dead.
SESSION_TTL_SECONDS = int(os.environ.get("MCP_SSE_SESSION_TTL_SECONDS", "90"))


def _stale_before():
    return timezone.now() - timezone.timedelta(seconds=SESSION_TTL_SECONDS)


def register_session(session_id: str, credentials: dict) -> int:
    """Register a newly opened SSE session. Returns the cluster-wide *live* active
    count (including this session) — the admission gate in sse.py rejects and rolls
    back when this exceeds the cap. Stale rows (no heartbeat within the TTL) are
    pruned first, then the create + count run in one transaction so the gate sees a
    consistent total."""
    with transaction.atomic():
        deleted, _ = SSESession.objects.filter(last_seen__lt=_stale_before()).delete()
        if deleted:
            logger.info("Pruned %d stale SSE session row(s) (TTL=%ds)", deleted, SESSION_TTL_SECONDS)
        SSESession.objects.update_or_create(
            session_id=session_id,
            defaults={"credentials": credentials or {}, "last_seen": timezone.now()},
        )
        return SSESession.objects.filter(last_seen__gte=_stale_before()).count()


def touch_session(session_id: str) -> None:
    """Refresh a session's heartbeat so it stays live. Called on each SSE keepalive
    and outbound message. Best-effort: a missing row (already closed) is a no-op."""
    SSESession.objects.filter(session_id=session_id).update(last_seen=timezone.now())


def get_session(session_id: str) -> Optional[dict]:
    """Look up a session's credentials dict from any process. Returns None if
    absent (forces a reconnect)."""
    row = SSESession.objects.filter(session_id=session_id).first()
    return row.credentials if row is not None else None


def close_session(session_id: str) -> int:
    """Remove a session on close. Returns the cluster-wide live active count remaining."""
    with transaction.atomic():
        SSESession.objects.filter(session_id=session_id).delete()
        return SSESession.objects.filter(last_seen__gte=_stale_before()).count()
