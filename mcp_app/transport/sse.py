"""SSE (Server-Sent Events) transport for the legacy MCP 2024-11-05 protocol.

Handles:
  GET /mcp  → open_sse_connection()   opens a long-lived SSE stream
  POST /mcp/message  → handle_sse_message()  routes client messages to the open session
"""
import json
import logging
import os
import queue
import threading
import time
import uuid

from django.http import HttpResponse, JsonResponse, StreamingHttpResponse
import newrelic.agent

from mcp_app.nr_utils import (
    SERVER_ENV,
    SERVER_VERSION,
    add_attrs,
    get_linking_metadata,
    record_event,
    record_metric,
    set_txn_name,
    suppress_apdex,
    suppress_trace,
)
from mcp_app.protocol.auth import identify_mcp_client
from mcp_app.protocol.dispatch import dispatch_jsonrpc
from mcp_app.protocol.session import SESSION_PROTOCOL_KEY
from mcp_app.protocol.session_store import (
    get_field,
    increment,
    init_stats,
    pop_stats,
)

logger = logging.getLogger(__name__)

_MCP_QUEUE_MAXSIZE = int(os.environ.get("MCP_QUEUE_MAXSIZE", "100"))

# In-process session registry: session_id → {credentials, token_expires_at, queue}
_sse_sessions: dict = {}
_sessions_lock = threading.Lock()


def _add_session_protocol_attrs(request) -> None:
    if request is None:
        return
    protocol_version = request.session.get(SESSION_PROTOCOL_KEY)
    if protocol_version:
        add_attrs([("mcp.protocol_version", protocol_version)])


# ── session registry helpers ──────────────────────────────────────────────────

def register_session(session_id: str, credentials: dict, token_expires_at) -> int:
    with _sessions_lock:
        _sse_sessions[session_id] = {
            "credentials":      credentials or {},
            "token_expires_at": token_expires_at,
            "queue":            queue.Queue(maxsize=_MCP_QUEUE_MAXSIZE),
        }
        return len(_sse_sessions)


def get_session(session_id: str):
    with _sessions_lock:
        entry = _sse_sessions.get(session_id)
    if entry is None:
        return None
    return entry["credentials"], entry["token_expires_at"]


def close_session(session_id: str) -> int:
    with _sessions_lock:
        _sse_sessions.pop(session_id, None)
        return len(_sse_sessions)


def _get_queue(session_id: str):
    with _sessions_lock:
        entry = _sse_sessions.get(session_id)
    return entry["queue"] if entry else None


def push_message(session_id: str, message: dict, timeout: float = 5.0) -> bool:
    q = _get_queue(session_id)
    if q is None:
        return False
    try:
        q.put(message, block=True, timeout=timeout)
        return True
    except queue.Full:
        return False


def pop_message(session_id: str, timeout: float):
    q = _get_queue(session_id)
    if q is None:
        return None
    t0 = time.time()
    try:
        msg = q.get(block=True, timeout=timeout)
        wait_ms = round((time.time() - t0) * 1000, 2)
        return wait_ms, msg
    except queue.Empty:
        return None


def queue_depth(session_id: str) -> int:
    q = _get_queue(session_id)
    return q.qsize() if q else 0


# ── SSE connection ────────────────────────────────────────────────────────────

def open_sse_connection(request, credentials: dict, token_expires_at):
    """Open a long-lived SSE session; stream messages to the client until disconnect."""
    session_id       = str(uuid.uuid4())
    publisher_id     = (credentials or {}).get("publisherId", "unknown")
    active_threads   = threading.active_count()
    client_name, _   = identify_mcp_client(request)

    set_txn_name("Transport/SSE", group="Transport")
    suppress_apdex()
    suppress_trace()

    add_attrs([
        ("mcp.transport",            "sse"),
        ("mcp.session_id",           session_id),
        ("mcp.thread_active_count",  active_threads),
    ])
    _add_session_protocol_attrs(request)

    logger.info(
        "SSE session open: session=%s publisher=%s active_threads=%d",
        session_id, publisher_id, active_threads,
    )

    active_on_open = register_session(session_id, credentials, token_expires_at)

    open_linking     = get_linking_metadata()
    session_trace_id = open_linking.get("trace.id", "")

    init_stats(session_id, session_trace_id)

    newrelic.agent.record_custom_metric("Custom/MCP/active_sessions", active_on_open)
    add_attrs([("mcp.active_sessions", active_on_open), ("mcp.session_trace_id", session_trace_id)])

    record_event("SSESessionOpen", {
        "session_id":     session_id,
        "publisher_id":   publisher_id,
        "active_threads": active_threads,
        "active_sessions": active_on_open,
        "trace_id":       session_trace_id,
        "span_id":        open_linking.get("span.id", ""),
        "env":            SERVER_ENV,
        "server_version": SERVER_VERSION,
    })

    from django.conf import settings
    base_url  = getattr(settings, "BASE_URL", "http://localhost:8000").rstrip("/")
    post_url  = f"{base_url}/mcp/message?sessionId={session_id}"
    stream_t0 = time.perf_counter()

    def event_stream():
        yield f"event: endpoint\ndata: {post_url}\n\n"
        try:
            while True:
                popped = pop_message(session_id, timeout=25)
                if popped is None:
                    yield ": keepalive\n\n"
                    continue
                wait_ms, msg = popped
                newrelic.agent.record_custom_metric("Custom/MCP/queue_wait_ms", wait_ms)
                yield f"event: message\ndata: {json.dumps(msg)}\n\n"
        finally:
            _close_sse_session(session_id, publisher_id, stream_t0)

    resp = StreamingHttpResponse(event_stream(), content_type="text/event-stream")
    resp["Cache-Control"]     = "no-cache"
    resp["X-Accel-Buffering"] = "no"
    return resp


def _close_sse_session(session_id: str, publisher_id: str, stream_t0: float) -> None:
    """Tear down session state and emit close-event observability."""
    duration_ms = round((time.perf_counter() - stream_t0) * 1000, 2)

    active_on_close = close_session(session_id)

    stats = pop_stats(session_id)

    tool_count              = stats.get("tool_count",                    0)
    tool_error_count        = stats.get("error_count",                   0)
    tool_degraded_count     = stats.get("degraded_count",                0)
    total_tool_ms           = round(stats.get("total_tool_duration_ms",  0.0), 2)
    total_input_tokens      = stats.get("total_estimated_input_tokens",  0)
    total_output_tokens     = stats.get("total_estimated_output_tokens", 0)
    session_client_name     = stats.get("client_name")  or "unknown"
    session_trace_id        = stats.get("session_trace_id", "")
    tool_sequence_str       = ",".join(stats.get("tool_sequence", []))[:500]
    server_work_pct         = round(total_tool_ms / duration_ms * 100, 1) if duration_ms > 0 else 0.0

    newrelic.agent.record_custom_metric("Custom/MCP/active_sessions", active_on_close)

    if tool_count == 0:
        record_metric("Custom/MCP/session_abandon_count", 1)
        record_event("MCPSessionAbandoned", {
            "session_id":          session_id,
            "publisher_id":        publisher_id,
            "duration_ms":         duration_ms,
            "session_client_name": session_client_name,
            "session_trace_id":    session_trace_id,
            "env":                 SERVER_ENV,
            "server_version":      SERVER_VERSION,
        })
        logger.info(
            "SSE session abandoned (0 tool calls): session=%s publisher=%s duration_ms=%.2f",
            session_id, publisher_id, duration_ms,
        )

    record_event("MCPSessionSummary", {
        "session_id":                    session_id,
        "publisher_id":                  publisher_id,
        "duration_ms":                   duration_ms,
        "tool_call_count":               tool_count,
        "tool_error_count":              tool_error_count,
        "tool_degraded_count":           tool_degraded_count,
        "total_tool_duration_ms":        total_tool_ms,
        "total_estimated_input_tokens":  total_input_tokens,
        "total_estimated_output_tokens": total_output_tokens,
        "total_estimated_tokens":        total_input_tokens + total_output_tokens,
        "server_work_pct":               server_work_pct,
        "session_client_name":           session_client_name,
        "session_trace_id":              session_trace_id,
        "active_sessions_remaining":     active_on_close,
        "tool_sequence":                 tool_sequence_str,
        "env":                           SERVER_ENV,
        "server_version":                SERVER_VERSION,
    })
    record_event("SSESessionClose", {
        "session_id":             session_id,
        "publisher_id":           publisher_id,
        "duration_ms":            duration_ms,
        "tool_call_count":        tool_count,
        "tool_error_count":       tool_error_count,
        "tool_degraded_count":    tool_degraded_count,
        "total_tool_duration_ms": total_tool_ms,
        "session_trace_id":       session_trace_id,
        "env":                    SERVER_ENV,
        "server_version":         SERVER_VERSION,
    })
    logger.info(
        "SSE session close: session=%s publisher=%s duration_ms=%.2f tool_calls=%d tool_errors=%d",
        session_id, publisher_id, duration_ms, tool_count, tool_error_count,
    )


@newrelic.agent.function_trace(name="handle_sse_message", group="Transport")
def handle_sse_message(request) -> HttpResponse:
    """Handle POST /mcp/message — route the client's JSON-RPC body through the open SSE session."""
    if request.method != "POST":
        return HttpResponse(status=405)

    raw_sid    = request.GET.get("sessionId", "")
    session_id = raw_sid if raw_sid else "anon-" + uuid.uuid4().hex[:8]
    active_threads = threading.active_count()

    add_attrs([
        ("mcp.session_id",           session_id),
        ("mcp.thread_active_count",  active_threads),
        ("mcp.request_size_bytes",   len(request.body)),
    ])
    _add_session_protocol_attrs(request)

    client_name, client_version = identify_mcp_client(request)
    add_attrs([("mcp.client_name", client_name), ("mcp.client_version", client_version)])

    session_entry = get_session(session_id)

    if session_entry is None:
        logger.warning("handle_sse_message: no active SSE session: session_id=%s", session_id)
        add_attrs([("mcp.sse_session_missing", True)])
        record_metric("Custom/MCP/sse_session_missing_count", 1)
        record_event("MCPSessionMissing", {
            "session_id":     session_id,
            "env":            SERVER_ENV,
            "server_version": SERVER_VERSION,
        })
        return JsonResponse({"error": "No active MCP session."}, status=400)

    credentials, token_expires_at = session_entry

    session_trace_id = get_field(session_id, "session_trace_id") or ""
    if session_trace_id:
        add_attrs([("mcp.session_trace_id", session_trace_id)])

    try:
        body = json.loads(request.body)
    except json.JSONDecodeError:
        logger.warning("handle_sse_message: invalid JSON: session=%s", session_id)
        return JsonResponse({"error": "Invalid JSON"}, status=400)

    body_method = body.get("method", "") if isinstance(body, dict) else ""
    if body_method == "tools/call":
        seq = increment(session_id, "tool_count") or 0
        if seq:
            add_attrs([("mcp.session_tool_seq", seq)])

    try:
        response_msg = dispatch_jsonrpc(body, credentials, request, session_id, token_expires_at)

        if (
            body_method == "tools/call"
            and isinstance(response_msg, dict)
            and isinstance(response_msg.get("result"), dict)
            and response_msg["result"].get("isError")
        ):
            increment(session_id, "error_count")

        if response_msg is not None:
            # 5 s cap: with few worker threads, one thread blocked on a dead
            # client's full queue is a large share of total server capacity.
            ok = push_message(session_id, response_msg, timeout=5.0)
            if not ok:
                record_metric("Custom/MCP/queue_overflow_count", 1)
                add_attrs([("mcp.queue_overflow", True)])
                logger.error(
                    "MCP SSE queue full (maxsize=%d) after 5 s: session=%s — response dropped",
                    _MCP_QUEUE_MAXSIZE, session_id,
                )
                return JsonResponse({"ok": True})
            depth = queue_depth(session_id)
            add_attrs([("mcp.session_queue_depth", depth)])
            newrelic.agent.record_custom_metric("Custom/MCP/session_queue_depth", depth)

        return JsonResponse({"ok": True})
    except Exception:
        logger.error("handle_sse_message transport error: session=%s", session_id, exc_info=True)
        raise
