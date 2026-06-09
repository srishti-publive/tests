"""Per-session telemetry store — in-process dict guarded by a threading.Lock."""
import threading
import time
from typing import Optional

_lock: threading.Lock = threading.Lock()
_stats: dict = {}


def init_stats(session_id: str, session_trace_id: str) -> None:
    with _lock:
        _stats[session_id] = {
            "tool_count":                    0,
            "error_count":                   0,
            "degraded_count":                0,
            "session_start_time":            time.perf_counter(),
            "total_tool_duration_ms":        0.0,
            "total_estimated_input_tokens":  0,
            "total_estimated_output_tokens": 0,
            "last_tool_end_perf":            None,
            "client_name":                   None,
            "session_trace_id":              session_trace_id,
            "create_op_count":               0,
            "update_delete_op_count":        0,
            "tool_sequence":                 [],
        }


def increment(session_id: str, field: str, by=1) -> Optional[float]:
    with _lock:
        entry = _stats.get(session_id)
        if entry is None:
            return None
        entry[field] = (entry.get(field) or 0) + by
        return entry[field]


def set_field(session_id: str, field: str, value) -> bool:
    with _lock:
        entry = _stats.get(session_id)
        if entry is None:
            return False
        entry[field] = value
        return True


def append_tool_sequence(session_id: str, tool_name: str) -> None:
    with _lock:
        entry = _stats.get(session_id)
        if entry is not None:
            entry["tool_sequence"].append(tool_name)


def get_timeline_and_set_client_name(session_id: str, user_agent: Optional[str]) -> dict:
    with _lock:
        entry = _stats.get(session_id)
        if entry is None:
            return {"session_start_time": None, "last_tool_end_perf": None, "session_trace_id": ""}
        if not entry.get("client_name") and user_agent is not None:
            entry["client_name"] = user_agent
        return {
            "session_start_time": entry.get("session_start_time"),
            "last_tool_end_perf": entry.get("last_tool_end_perf"),
            "session_trace_id":   entry.get("session_trace_id") or "",
        }


def get_field(session_id: str, field: str):
    with _lock:
        entry = _stats.get(session_id)
        if entry is None:
            return None
        return entry.get(field)


def pop_stats(session_id: str) -> dict:
    with _lock:
        entry = _stats.pop(session_id, None)
    if not entry:
        return {}
    return {
        "tool_count":                    entry.get("tool_count")                    or 0,
        "error_count":                   entry.get("error_count")                   or 0,
        "degraded_count":                entry.get("degraded_count")                or 0,
        "total_tool_duration_ms":        entry.get("total_tool_duration_ms")        or 0.0,
        "total_estimated_input_tokens":  entry.get("total_estimated_input_tokens")  or 0,
        "total_estimated_output_tokens": entry.get("total_estimated_output_tokens") or 0,
        "client_name":                   entry.get("client_name"),
        "session_trace_id":              entry.get("session_trace_id") or "",
        "tool_sequence":                 entry.get("tool_sequence") or [],
    }
