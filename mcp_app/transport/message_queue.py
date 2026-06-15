"""Database-backed per-session message queue (was a Redis list + BLPOP).

`handle_sse_message` (on whichever process received the POST) inserts the JSON-RPC
response; the `event_stream` generator (on whichever process holds the GET stream
— possibly a different one) consumes it. This is the half of the externalization
that lets GET and POST land on different processes.

A database has no blocking-pop primitive, so `pop_message` polls every
`_POLL_INTERVAL` seconds up to its timeout — replacing Redis `BLPOP(timeout)`.
Each poll grabs the oldest row with `select_for_update(skip_locked=True)` and
deletes it in the same transaction, so two workers never deliver the same message
(on Postgres; `select_for_update` is a harmless no-op on SQLite, where local dev
is single-process anyway).

Timestamps use timezone-aware DB datetimes, so producer/consumer can be different
processes — unlike `time.perf_counter`, whose reference point is per-process.
"""
import logging
import time
from datetime import timedelta
from typing import Optional

from django.db import transaction
from django.utils import timezone

from mcp_app.models import SSEMessage

logger = logging.getLogger(__name__)

# Worst-case added latency per delivered message; balances responsiveness against
# DB query rate (the admission gate caps concurrent SSE sessions, so this stays low).
_POLL_INTERVAL = 0.25


def push_message(session_id: str, message: dict, maxsize: int, timeout: float = 5.0) -> bool:
    """Enqueue a message for delivery, waiting up to `timeout` seconds for room if
    the queue is at `maxsize`. Returns False on persistent overflow — the caller
    logs it and records `Custom/MCP/queue_overflow_count`. The depth check is not
    atomic with the insert, so concurrent producers can land a couple of items over
    `maxsize`: a soft cap, the same tolerance the in-process bounded queue had."""
    deadline = time.monotonic() + timeout
    backoff  = 0.05
    while True:
        if SSEMessage.objects.filter(session_id=session_id).count() < maxsize:
            SSEMessage.objects.create(session_id=session_id, payload=message)
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(backoff)
        backoff = min(backoff * 2, 1.0)


def pop_message(session_id: str, timeout: float) -> Optional[tuple]:
    """Wait up to `timeout` seconds for the next message, polling the queue table.
    Returns `(queue_wait_ms, message)`, or None on timeout (→ caller emits a
    keepalive). Each attempt atomically claims and deletes the oldest row so a
    message is delivered at most once."""
    deadline = time.monotonic() + timeout
    while True:
        with transaction.atomic():
            row = (
                SSEMessage.objects
                .select_for_update(skip_locked=True)
                .filter(session_id=session_id)
                .order_by("id")
                .first()
            )
            if row is not None:
                wait_ms = round((timezone.now() - row.enqueued_at).total_seconds() * 1000, 2)
                message = row.payload
                row.delete()
                return wait_ms, message

        if time.monotonic() >= deadline:
            return None
        time.sleep(_POLL_INTERVAL)


def delete_queue(session_id: str) -> None:
    """Drop the queue on session close."""
    SSEMessage.objects.filter(session_id=session_id).delete()


def queue_depth(session_id: str) -> int:
    """Current queue length — for the `mcp.session_queue_depth` attribute /
    `Custom/MCP/session_queue_depth` metric."""
    return SSEMessage.objects.filter(session_id=session_id).count()
