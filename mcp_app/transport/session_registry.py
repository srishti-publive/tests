"""Database-backed SSE session registry (was Redis `mcp:session:*`).

`GET /mcp` (open) and `POST /mcp/message` (route) can land on different
processes/replicas and still find the session, because the registry lives in the
shared database. Replaces the former in-process `_sse_sessions` dict and its
Redis successor.

Rows live until `close_session` deletes them — there is no expiry/TTL. (A session
that dies without a clean close leaves a row behind; clear it manually if needed.)

`credentials` holds live CDS/CMS API secrets ({publisherId, apiKey, apiSecret}),
stored as plain JSON — matching how they're stored in Postgres elsewhere
(credential encryption was deliberately removed; see git history).
"""
import logging
from typing import Optional

from django.db import transaction

from mcp_app.models import SSESession

logger = logging.getLogger(__name__)


def register_session(session_id: str, credentials: dict) -> int:
    """Register a newly opened SSE session. Returns the cluster-wide active count
    (including this session) — the admission gate in sse.py rejects and rolls back
    when this exceeds the cap. The create + count run in one transaction so the gate
    sees a consistent total."""
    with transaction.atomic():
        SSESession.objects.update_or_create(
            session_id=session_id,
            defaults={"credentials": credentials or {}},
        )
        return SSESession.objects.count()


def get_session(session_id: str) -> Optional[dict]:
    """Look up a session's credentials dict from any process. Returns None if
    absent (forces a reconnect)."""
    row = SSESession.objects.filter(session_id=session_id).first()
    return row.credentials if row is not None else None


def close_session(session_id: str) -> int:
    """Remove a session on close. Returns the cluster-wide active count remaining."""
    with transaction.atomic():
        SSESession.objects.filter(session_id=session_id).delete()
        return SSESession.objects.count()
