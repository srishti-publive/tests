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
from mcp_app.protocol.session_store import session_stats, session_stats_lock

logger = logging.getLogger(__name__)

# SSE message queues: session_id → (Queue, credentials, token_expires_at)
_sse_sessions: dict[str, tuple[queue.Queue, dict, object]] = {}
_sse_sessions_lock = threading.Lock()

_MCP_QUEUE_MAXSIZE = int(os.environ.get("MCP_QUEUE_MAXSIZE", "100"))


def _add_session_protocol_attrs(request) -> None:
    if request is None:
        return
    protocol_version = request.session.get(SESSION_PROTOCOL_KEY)
    if protocol_version:
        add_attrs([("mcp.protocol_version", protocol_version)])


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

    msg_queue: queue.Queue = queue.Queue(maxsize=_MCP_QUEUE_MAXSIZE)

    with _sse_sessions_lock:
        _sse_sessions[session_id] = (msg_queue, credentials, token_expires_at)
        active_on_open            = len(_sse_sessions)

    open_linking     = get_linking_metadata()
    session_trace_id = open_linking.get("trace.id", "")

    with session_stats_lock:
        session_stats[session_id] = {
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
            "tool_sequence":                 [],
            "write_op_count":                0,
        }

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
                try:
                    entry = msg_queue.get(timeout=25)
                    if entry is None:
                        break
                    enqueue_t, msg = entry
                    wait_ms = round((time.perf_counter() - enqueue_t) * 1000, 2)
                    newrelic.agent.record_custom_metric("Custom/MCP/queue_wait_ms", wait_ms)
                    yield f"event: message\ndata: {json.dumps(msg)}\n\n"
                except queue.Empty:
                    yield ": keepalive\n\n"
        finally:
            _close_sse_session(session_id, publisher_id, stream_t0)

    resp = StreamingHttpResponse(event_stream(), content_type="text/event-stream")
    resp["Cache-Control"]     = "no-cache"
    resp["X-Accel-Buffering"] = "no"
    return resp


def _close_sse_session(session_id: str, publisher_id: str, stream_t0: float) -> None:
    """Tear down session state and emit close-event observability."""
    duration_ms = round((time.perf_counter() - stream_t0) * 1000, 2)

    with _sse_sessions_lock:
        _sse_sessions.pop(session_id, None)
        active_on_close = len(_sse_sessions)

    with session_stats_lock:
        stats = session_stats.pop(session_id, {})

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

    with _sse_sessions_lock:
        session_entry = _sse_sessions.get(session_id)

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

    msg_queue, credentials, token_expires_at = session_entry

    with session_stats_lock:
        msg_stats = session_stats.get(session_id) or {}
    session_trace_id = msg_stats.get("session_trace_id", "")
    if session_trace_id:
        add_attrs([("mcp.session_trace_id", session_trace_id)])

    try:
        body = json.loads(request.body)
    except json.JSONDecodeError:
        logger.warning("handle_sse_message: invalid JSON: session=%s", session_id)
        return JsonResponse({"error": "Invalid JSON"}, status=400)

    body_method = body.get("method", "") if isinstance(body, dict) else ""
    if body_method == "tools/call":
        with session_stats_lock:
            stats = session_stats.get(session_id)
            if stats is not None:
                stats["tool_count"] += 1
                seq = stats["tool_count"]
            else:
                seq = 0
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
            with session_stats_lock:
                stats = session_stats.get(session_id)
                if stats is not None:
                    stats["error_count"] += 1

        if response_msg is not None:
            try:
                msg_queue.put((time.perf_counter(), response_msg), block=True, timeout=30.0)
            except queue.Full:
                record_metric("Custom/MCP/queue_overflow_count", 1)
                add_attrs([("mcp.queue_overflow", True)])
                logger.error(
                    "MCP SSE queue full (maxsize=%d) after 30 s: session=%s — response dropped",
                    _MCP_QUEUE_MAXSIZE, session_id,
                )
                return JsonResponse({"ok": True})
            queue_depth = msg_queue.qsize()
            add_attrs([("mcp.session_queue_depth", queue_depth)])
            newrelic.agent.record_custom_metric("Custom/MCP/session_queue_depth", queue_depth)

        return JsonResponse({"ok": True})
    except Exception:
        logger.error("handle_sse_message transport error: session=%s", session_id, exc_info=True)
        raise
