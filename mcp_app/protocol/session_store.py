"""Per-session telemetry store — Redis-backed so it's shared across worker processes.

Re-exports `mcp_app.protocol.redis_session_stats` under the names `dispatch.py`/
`sse.py` already import, keeping their diffs to call-site mechanics rather than
import churn. Previously an in-process dict (`session_stats`) guarded by a
`threading.Lock`; that pinned the app to exactly one gunicorn worker (see
docs/deployment.md). The Redis hash this now wraps lets any worker read/update
any session's stats, with atomic HINCRBY replacing the old lock-protected
read-modify-write for counters.
"""
from mcp_app.protocol.redis_session_stats import (  # noqa: F401 — re-exported for callers
    append_tool_sequence,
    get_field,
    get_timeline_and_set_client_name,
    increment,
    init_stats,
    pop_stats,
    set_field,
)
