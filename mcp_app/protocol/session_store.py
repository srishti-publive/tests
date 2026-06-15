"""Per-session telemetry store — database-backed so it's shared across worker processes.

Re-exports `mcp_app.protocol.session_stats` under the names `dispatch.py`/`sse.py`
already import, keeping their diffs to call-site mechanics rather than import
churn. Previously an in-process dict (`_stats`) guarded by a `threading.Lock`
(then a Redis hash); both are now the `SessionStats`/`SessionToolEvent` models,
which let any worker read/update any session's stats, with atomic F() increments
replacing the old lock-protected read-modify-write for counters.
"""
from mcp_app.protocol.session_stats import (  # noqa: F401 — re-exported for callers
    append_tool_sequence,
    get_field,
    get_timeline_and_set_client_name,
    increment,
    init_stats,
    pop_stats,
    set_field,
)
