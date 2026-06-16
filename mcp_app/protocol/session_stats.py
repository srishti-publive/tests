"""Database-backed per-SSE-session telemetry.

Stores tool counts, timing, token estimates, and rate-limit buckets in the
`SessionStats` row, plus an ordered `SessionToolEvent` list for `tool_sequence`.
Living in the shared database lets any worker read/update any session's stats.
Counters are incremented atomically at the DB level via F() expressions.

Only SSE sessions call `init_stats`; HTTP stateless sessions never do, so every
function here no-ops (returns None/False/{}) when no row exists — mirroring the
`if stats is not None` guard at the call sites.

Rows live until `pop_stats` deletes them at session close — there is no expiry/TTL.
"""
import time
from typing import Optional

from django.db import transaction
from django.db.models import F

from mcp_app.models import SessionStats, SessionToolEvent

_FLOAT_FIELDS = frozenset({
    "session_start_time",
    "total_tool_duration_ms",
    "last_tool_end_perf",
})
_INT_FIELDS = frozenset({
    "tool_count", "error_count", "degraded_count",
    "total_estimated_input_tokens", "total_estimated_output_tokens",
    "create_op_count", "update_delete_op_count",
})
# Fields incrementable via increment(); guards against typo'd attribute names.
_COUNTER_FIELDS = _FLOAT_FIELDS | _INT_FIELDS


def init_stats(session_id: str, session_trace_id: str) -> None:
    """Create the stats row for a newly opened SSE session, clearing any stale
    leftovers for this session id first."""
    with transaction.atomic():
        SessionToolEvent.objects.filter(session_id=session_id).delete()
        SessionStats.objects.update_or_create(
            session_id=session_id,
            defaults={
                "tool_count": 0,
                "error_count": 0,
                "degraded_count": 0,
                "session_start_time": time.perf_counter(),
                "total_tool_duration_ms": 0.0,
                "total_estimated_input_tokens": 0,
                "total_estimated_output_tokens": 0,
                "last_tool_end_perf": None,
                "client_name": None,
                "session_trace_id": session_trace_id,
                "create_op_count": 0,
                "update_delete_op_count": 0,
            },
        )


def increment(session_id: str, field: str, by=1) -> Optional[float]:
    """Atomically increment a counter field; returns the new value, or None if the
    session has no stats row (HTTP stateless sessions never call init_stats, so
    this correctly no-ops for them too)."""
    if field not in _COUNTER_FIELDS:
        raise ValueError(f"increment: unknown counter field {field!r}")
    updated = SessionStats.objects.filter(session_id=session_id).update(
        **{field: F(field) + by}
    )
    if not updated:
        return None
    return SessionStats.objects.filter(session_id=session_id).values_list(field, flat=True).first()


def set_field(session_id: str, field: str, value) -> bool:
    """Set a scalar field (e.g. `last_tool_end_perf`) only if the session exists.
    Returns whether the write happened."""
    updated = SessionStats.objects.filter(session_id=session_id).update(
        **{field: value}
    )
    return bool(updated)


def append_tool_sequence(session_id: str, tool_name: str) -> None:
    """Append to the per-session ordered tool-call sequence."""
    SessionToolEvent.objects.create(session_id=session_id, tool_name=tool_name)


def get_timeline_and_set_client_name(session_id: str, user_agent: Optional[str]) -> dict:
    """Read `session_start_time`/`last_tool_end_perf`/`session_trace_id`, and set
    `client_name` if it's still unset and a user agent is available.

    The only race is two concurrent first-tool-calls both writing the *same*
    `client_name` (the User-Agent for that session) — harmless, so no lock needed.
    """
    row = (
        SessionStats.objects
        .filter(session_id=session_id)
        .values("session_start_time", "last_tool_end_perf", "client_name", "session_trace_id")
        .first()
    )
    if row is None:
        # HTTP stateless sessions never call init_stats — don't create a phantom row.
        return {"session_start_time": None, "last_tool_end_perf": None, "session_trace_id": ""}

    if not row.get("client_name") and user_agent is not None:
        SessionStats.objects.filter(session_id=session_id).update(
            client_name=user_agent
        )

    return {
        "session_start_time": row.get("session_start_time"),
        "last_tool_end_perf": row.get("last_tool_end_perf"),
        "session_trace_id":   row.get("session_trace_id") or "",
    }


def get_field(session_id: str, field: str):
    """Read a single field (returns None if the session or field is absent)."""
    return SessionStats.objects.filter(session_id=session_id).values_list(field, flat=True).first()


def pop_stats(session_id: str) -> dict:
    """Snapshot and delete all stats for a session — the close-time roll-up that
    feeds MCPSessionSummary/SSESessionClose/MCPSessionAbandoned. Returns {} if absent."""
    with transaction.atomic():
        row = SessionStats.objects.filter(session_id=session_id).first()
        if row is None:
            return {}
        tool_sequence = list(
            SessionToolEvent.objects
            .filter(session_id=session_id)
            .order_by("id")
            .values_list("tool_name", flat=True)
        )
        snapshot = {
            "tool_count":                    row.tool_count or 0,
            "error_count":                   row.error_count or 0,
            "degraded_count":                row.degraded_count or 0,
            "total_tool_duration_ms":        row.total_tool_duration_ms or 0.0,
            "total_estimated_input_tokens":  row.total_estimated_input_tokens or 0,
            "total_estimated_output_tokens": row.total_estimated_output_tokens or 0,
            "client_name":                   row.client_name or None,
            "session_trace_id":              row.session_trace_id or "",
            "tool_sequence":                 tool_sequence,
        }
        SessionToolEvent.objects.filter(session_id=session_id).delete()
        row.delete()
        return snapshot
