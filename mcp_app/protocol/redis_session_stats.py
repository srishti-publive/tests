"""Redis-backed replacement for the in-process `session_stats` dict.

Stores per-SSE-session telemetry (tool counts, timing, rate-limit buckets) in a
Redis hash keyed by session_id, plus a separate list for `tool_sequence`. This
makes the data visible to — and atomically updatable from — any worker process,
which is what removes the "must be exactly one gunicorn worker" constraint.

Counters use HINCRBY/HINCRBYFLOAT, which are atomic on the Redis side — this
actually *simplifies* the old lock-protected read-modify-write in dispatch.py
into a single round trip with no lock needed.

Field values are stored as strings (Redis hash values are always bytes/str);
`get_stats`/`get_timeline` coerce them back to int/float/None on read.
"""
import time
from typing import Optional

from mcp_app.redis_client import get_redis_client

# Generous TTL: refreshed on every write so long-running sessions never expire
# mid-use, while abandoned sessions (opened, never closed cleanly) are reaped
# automatically instead of accumulating forever — the in-process dict had no
# such concern because it died with the process.
_STATS_TTL_SECONDS = 24 * 3600

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

# Sentinel stored for fields that are conceptually `None` (Redis hashes can't
# hold Python None) — `last_tool_end_perf` and `client_name` start this way.
_NONE = ""


def _stats_key(session_id: str) -> str:
    return f"mcp:session_stats:{session_id}"


def _seq_key(session_id: str) -> str:
    return f"mcp:session_stats:{session_id}:tool_sequence"


def _coerce(field: str, value):
    if value is None or value == _NONE:
        return None
    if field in _FLOAT_FIELDS:
        return float(value)
    if field in _INT_FIELDS:
        return int(value)
    return value


def init_stats(session_id: str, session_trace_id: str) -> None:
    """Create the stats hash for a newly opened SSE session (mirrors the dict literal
    previously assigned at sse.py:84-98)."""
    client = get_redis_client()
    key    = _stats_key(session_id)
    with client.pipeline() as pipe:
        pipe.delete(key, _seq_key(session_id))  # defensive: clear any stale leftovers
        pipe.hset(key, mapping={
            "tool_count":                    0,
            "error_count":                   0,
            "degraded_count":                0,
            "session_start_time":            time.perf_counter(),
            "total_tool_duration_ms":        0.0,
            "total_estimated_input_tokens":  0,
            "total_estimated_output_tokens": 0,
            "last_tool_end_perf":            _NONE,
            "client_name":                   _NONE,
            "session_trace_id":              session_trace_id,
            "create_op_count":               0,
            "update_delete_op_count":        0,
        })
        pipe.expire(key, _STATS_TTL_SECONDS)
        pipe.execute()


def increment(session_id: str, field: str, by=1) -> Optional[float]:
    """Atomically increment a counter field; returns the new value, or None if the
    session has no stats entry (mirrors the `if stats is not None` guard at every
    call site in dispatch.py/sse.py — HTTP stateless sessions never call init_stats,
    so this correctly no-ops for them too)."""
    client = get_redis_client()
    key    = _stats_key(session_id)
    if not client.exists(key):
        return None
    new_value = (
        client.hincrbyfloat(key, field, by) if field in _FLOAT_FIELDS
        else client.hincrby(key, field, by)
    )
    client.expire(key, _STATS_TTL_SECONDS)
    return _coerce(field, new_value)


def set_field(session_id: str, field: str, value) -> bool:
    """Set a scalar field (e.g. `last_tool_end_perf`) only if the session exists.
    Returns whether the write happened."""
    client = get_redis_client()
    key    = _stats_key(session_id)
    if not client.exists(key):
        return False
    client.hset(key, field, _NONE if value is None else value)
    client.expire(key, _STATS_TTL_SECONDS)
    return True


def append_tool_sequence(session_id: str, tool_name: str) -> None:
    """RPUSH onto the per-session tool_sequence list (kept as its own key rather
    than growing inside the hash — Redis hash fields aren't meant to hold lists)."""
    client = get_redis_client()
    key    = _seq_key(session_id)
    client.rpush(key, tool_name)
    client.expire(key, _STATS_TTL_SECONDS)


def get_timeline_and_set_client_name(session_id: str, user_agent: Optional[str]) -> dict:
    """Read `session_start_time`/`last_tool_end_perf`/`session_trace_id`, and set
    `client_name` if it's still unset and a user agent is available — the compound
    read-then-maybe-write at dispatch.py:289-300.

    Not wrapped in a Lua script: the only race is two concurrent first-tool-calls
    both writing the *same* `client_name` value (the User-Agent for that session) —
    harmless, so a plain HMGET + conditional HSET is fine here.
    """
    client = get_redis_client()
    key    = _stats_key(session_id)
    if not client.exists(key):
        # HTTP stateless sessions never call init_stats — mirror the old
        # `if stats is not None` guard so we don't create a phantom hash.
        return {"session_start_time": None, "last_tool_end_perf": None, "session_trace_id": ""}

    fields = ["session_start_time", "last_tool_end_perf", "client_name", "session_trace_id"]
    raw    = client.hmget(key, fields)
    result = dict(zip(fields, raw))

    if not result.get("client_name") and user_agent is not None:
        client.hset(key, "client_name", user_agent)
        client.expire(key, _STATS_TTL_SECONDS)

    return {
        "session_start_time": _coerce("session_start_time", result.get("session_start_time")),
        "last_tool_end_perf": _coerce("last_tool_end_perf", result.get("last_tool_end_perf")),
        "session_trace_id":   result.get("session_trace_id") or "",
    }


def get_field(session_id: str, field: str):
    """Read a single field (returns None if the session or field is absent)."""
    client = get_redis_client()
    raw = client.hget(_stats_key(session_id), field)
    return _coerce(field, raw)


def pop_stats(session_id: str) -> dict:
    """Snapshot and delete all stats for a session — the close-time roll-up that
    feeds MCPSessionSummary/SSESessionClose/MCPSessionAbandoned (mirrors
    `session_stats.pop(session_id, {})` at sse.py:151). Returns {} if absent."""
    client   = get_redis_client()
    key      = _stats_key(session_id)
    seq_key  = _seq_key(session_id)

    with client.pipeline() as pipe:
        pipe.hgetall(key)
        pipe.lrange(seq_key, 0, -1)
        pipe.delete(key, seq_key)
        raw, tool_sequence, _ = pipe.execute()

    if not raw:
        return {}

    return {
        "tool_count":                    _coerce("tool_count", raw.get("tool_count")) or 0,
        "error_count":                   _coerce("error_count", raw.get("error_count")) or 0,
        "degraded_count":                _coerce("degraded_count", raw.get("degraded_count")) or 0,
        "total_tool_duration_ms":        _coerce("total_tool_duration_ms", raw.get("total_tool_duration_ms")) or 0.0,
        "total_estimated_input_tokens":  _coerce("total_estimated_input_tokens", raw.get("total_estimated_input_tokens")) or 0,
        "total_estimated_output_tokens": _coerce("total_estimated_output_tokens", raw.get("total_estimated_output_tokens")) or 0,
        "client_name":                   raw.get("client_name") or None,
        "session_trace_id":              raw.get("session_trace_id") or "",
        "tool_sequence":                 tool_sequence,
    }
