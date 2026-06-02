import hashlib
import json
import logging
import queue
import re
import threading
import time
import uuid

import newrelic.agent
from django.conf import settings
from django.http import HttpResponse, StreamingHttpResponse, JsonResponse
from django.utils import timezone
from django.views.decorators.csrf import csrf_exempt

from .nr_utils import (
    SERVER_ENV, SERVER_VERSION,
    add_attrs, get_linking_metadata, notice_err, record_event, record_metric,
    set_txn_name, suppress_apdex, suppress_trace,
)
from .prompt_capture import extract_prompt_for_tool_call, record_prompt_observability
from .tools import TOOLS, call_tool
from .cms_tools import CMS_TOOLS, call_cms_tool
from .utils import extract_bearer_token

logger = logging.getLogger(__name__)

# session_id → (Queue, credentials, token_expires_at)
_sessions: dict[str, tuple[queue.Queue, dict, object]] = {}
_sessions_lock = threading.Lock()

# session_id → {"tool_count": int, "error_count": int, ...}
_session_stats: dict[str, dict] = {}
_session_stats_lock = threading.Lock()

_PROTOCOL_VERSION    = "2024-11-05"
_SESSION_PROTOCOL_KEY = "mcp_protocol_version"

# MCPPrompt event sampling — at most this many per minute per process.
_PROMPT_EVENT_MAX_PER_MIN = 1000
_prompt_event_count        = 0
_prompt_event_window_start = time.monotonic()
_prompt_event_lock         = threading.Lock()

import os as _os
_MCP_QUEUE_MAXSIZE = int(_os.environ.get("MCP_QUEUE_MAXSIZE", "100"))


def _should_emit_prompt_event() -> bool:
    global _prompt_event_count, _prompt_event_window_start
    now = time.monotonic()
    with _prompt_event_lock:
        if now - _prompt_event_window_start >= 60.0:
            _prompt_event_count = 0
            _prompt_event_window_start = now
        if _prompt_event_count < _PROMPT_EVENT_MAX_PER_MIN:
            _prompt_event_count += 1
            return True
    return False


# ── Client name normalisation ─────────────────────────────────────────────────
_CLIENT_NAME_MAP: dict[str, str] = {
    "claude":           "Claude Desktop",
    "cursor":           "Cursor",
    "anthropic":        "Anthropic SDK",
    "python-requests":  "Python Requests Client",
    "python-httpx":     "Python HTTPX Client",
    "mcp":              "MCP Python SDK",
    "node":             "Node.js MCP Client",
    "go-http-client":   "Go MCP Client",
    "axios":            "Axios (JS)",
    "openai":           "OpenAI SDK",
}


# ── Known-but-unimplemented MCP methods ───────────────────────────────────────
_UNIMPLEMENTED_METHODS: frozenset[str] = frozenset({
    "sampling/createMessage",
    "roots/list",
    "resources/list", "resources/read", "resources/subscribe", "resources/unsubscribe",
    "prompts/list", "prompts/get",
    "completion/complete",
    "logging/setLevel",
})


# ── JSON-RPC helpers ──────────────────────────────────────────────────────────

def _ok(id_, result):
    return {"jsonrpc": "2.0", "id": id_, "result": result}

def _err(id_, code, message):
    return {"jsonrpc": "2.0", "id": id_, "error": {"code": code, "message": message}}

def _bad_json_response() -> JsonResponse:
    """Shared 400 response for malformed JSON bodies — used by both transports."""
    return JsonResponse({"error": "Invalid JSON"}, status=400)


# ── Auth helpers ──────────────────────────────────────────────────────────────

@newrelic.agent.function_trace(name="get_credentials", group="Auth")
def _get_credentials(request):
    """Resolve credentials from Bearer token (DB lookup) or session cookie."""
    token = extract_bearer_token(request)
    if token:
        try:
            from auth_app.models import OAuthToken
            oauth_token = OAuthToken.objects.get(token=token)
            if oauth_token.expires_at >= timezone.now():
                return oauth_token.credentials, oauth_token.expires_at
        except Exception:
            pass
    return request.session.get("credentials"), None


def _get_session_id(request) -> str:
    """Return a stable session identifier for this request."""
    key = getattr(request.session, "session_key", None)
    if key:
        return key
    token = extract_bearer_token(request)
    if token:
        return "oauth-" + hashlib.sha256(token.encode()).hexdigest()[:16]
    return "anon-" + uuid.uuid4().hex[:8]


def _unauth(request):
    base_url = getattr(settings, "BASE_URL", "http://localhost:8000").rstrip("/")
    resp = JsonResponse(
        {"error": "Not authenticated", "authUrl": f"{base_url}/connect"},
        status=401,
    )
    resp["WWW-Authenticate"] = (
        f'Bearer realm="{base_url}",'
        f' resource_metadata="{base_url}/.well-known/oauth-protected-resource"'
    )
    return resp


def _mcp_client_identity(request):
    ua = request.META.get("HTTP_USER_AGENT", "unknown")
    client_name    = "unknown"
    client_version = "unknown"
    match = re.match(r"^([^\s/]+)/([^\s]+)", ua)
    if match:
        raw_name       = match.group(1).lower()
        client_version = match.group(2)
        client_name    = _CLIENT_NAME_MAP.get(raw_name, match.group(1))
    elif ua and ua != "unknown":
        raw_name    = (ua.split()[0] if ua.split() else ua).lower()
        client_name = _CLIENT_NAME_MAP.get(raw_name, ua.split()[0] if ua.split() else ua)
    return client_name, client_version


def _add_mcp_client_attrs(request):
    client_name, client_version = _mcp_client_identity(request)
    add_attrs([
        ("mcp.client_name",    client_name),
        ("mcp.client_version", client_version),
    ])


def _add_session_protocol_attrs(request):
    if request is None:
        return
    protocol_version = request.session.get(_SESSION_PROTOCOL_KEY)
    if protocol_version:
        add_attrs([("mcp.protocol_version", protocol_version)])


# ── Session accumulator ───────────────────────────────────────────────────────

def _update_session_stats(
    session_id: str,
    *,
    duration_ms: float,
    input_tokens: int,
    output_tokens: int = 0,
    tool_name: str,
    is_degraded: bool = False,
) -> None:
    """Update per-session accumulators for a completed tool call (success or error).

    Previously the same lock/lookup/update block was copy-pasted in both the
    success path and the error path of _handle_tool_call, with the error path
    missing the output_tokens update.  One function ensures both paths are identical.
    """
    with _session_stats_lock:
        stats = _session_stats.get(session_id or "")
        if stats is None:
            return
        stats["total_tool_duration_ms"] = stats.get("total_tool_duration_ms", 0.0) + duration_ms
        stats["total_estimated_input_tokens"] = (
            stats.get("total_estimated_input_tokens", 0) + input_tokens
        )
        if output_tokens:
            stats["total_estimated_output_tokens"] = (
                stats.get("total_estimated_output_tokens", 0) + output_tokens
            )
        stats["last_tool_end_perf"] = time.perf_counter()
        stats.setdefault("tool_sequence", []).append(tool_name)
        if is_degraded:
            stats["degraded_count"] = stats.get("degraded_count", 0) + 1


# ── Tool call handler (extracted from _dispatch) ──────────────────────────────

def _handle_tool_call(id_, params, credentials, request, session_id, token_expires_at):
    """Execute a tools/call request and return a JSON-RPC response object.

    Extracted from _dispatch so that method-routing logic and tool-execution
    logic have a clean boundary.  All prompt observability, rate limiting,
    session timeline tracking, and NR instrumentation lives here.
    """
    name = params.get("name", "")
    prompt_id, prompt_text, prompt_source, args = extract_prompt_for_tool_call(
        request, {}, params
    )

    logger.info(
        "MCP tools/call: tool=%s session=%s prompt_source=%s args_count=%d",
        name, session_id, prompt_source, len(args) if args else 0,
    )

    if _should_emit_prompt_event():
        record_prompt_observability(
            prompt_id=prompt_id,
            prompt_text=prompt_text,
            prompt_source=prompt_source,
            session_id=session_id or "",
            tool_name=name,
            jsonrpc_id=id_,
            request=request,
            credentials=credentials,
        )
    else:
        add_attrs([
            ("mcp.prompt_id",     prompt_id),
            ("mcp.prompt_text",   prompt_text),
            ("mcp.prompt_source", prompt_source),
            ("mcp.session_id",    session_id or ""),
            ("mcp.tool_name",     name),
        ])
        record_metric("Custom/MCP/prompt_event_dropped_count", 1)
        logger.warning("MCPPrompt event dropped (rate limit): tool=%s session=%s", name, session_id)

    tool_input = json.dumps(args, ensure_ascii=False, default=str)[:500] if args else ""
    add_attrs([("mcp.tool_name", name), ("mcp.tool_input", tool_input)])

    record_metric(f"Custom/Tool/{name}/call_count", 1)
    record_metric("Custom/MCP/tool_call_count", 1)

    # ── Session timeline attributes ───────────────────────────────────────────
    _start_offset_ms          = None
    _ai_think_time_ms         = None
    _prompt_input_tokens      = max(1, len(prompt_text) // 4)
    _session_trace_id         = ""

    with _session_stats_lock:
        _disp_stats = _session_stats.get(session_id or "")
        if _disp_stats is not None:
            _sess_start = _disp_stats.get("session_start_time")
            _last_end   = _disp_stats.get("last_tool_end_perf")
            if _sess_start is not None:
                _start_offset_ms = round((time.perf_counter() - _sess_start) * 1000, 2)
            if _last_end is not None:
                _ai_think_time_ms = round((time.perf_counter() - _last_end) * 1000, 2)
            if _disp_stats.get("client_name") is None and request is not None:
                _disp_stats["client_name"] = request.META.get("HTTP_USER_AGENT", "unknown")[:200]
            _session_trace_id = _disp_stats.get("session_trace_id", "")

    if _start_offset_ms is not None:
        add_attrs([("mcp.tool_start_offset_ms", _start_offset_ms)])
    if _ai_think_time_ms is not None:
        add_attrs([("mcp.ai_think_time_ms", _ai_think_time_ms)])

    # ── CMS write-op rate limit ───────────────────────────────────────────────
    _is_cms_write = (
        name.startswith("cms_")
        and not name.startswith("cms_list_")
        and not name.startswith("cms_get_")
        and not name.startswith("validate_")
    )
    if _is_cms_write:
        with _session_stats_lock:
            _write_stats = _session_stats.get(session_id or "")
            if _write_stats is not None:
                _write_stats["write_op_count"] += 1
                _write_op_count = _write_stats["write_op_count"]
            else:
                _write_op_count = 0
        if _write_op_count > 50:
            return _ok(id_, {"content": [{"type": "text", "text": json.dumps({
                "error_type": "rate_limit",
                "message": (
                    "Write operation limit (50) reached for this session. "
                    "Start a new session to continue making changes."
                ),
                "retryable": False,
            })}]})

    # ── Collect trace-linking metadata once — shared by success and error paths ──
    # Previously called twice (once in each branch), paying the function-call
    # overhead twice and risking drift if a new key was only added in one branch.
    _linking   = get_linking_metadata()
    _trace_id  = _linking.get("trace.id", "")
    _span_id   = _linking.get("span.id", "")

    t0 = time.perf_counter()
    try:
        if name.startswith("cms_") or name.startswith("validate_"):
            result = call_cms_tool(credentials, name, args)
        else:
            result = call_tool(credentials, name, args)

        duration_ms  = round((time.perf_counter() - t0) * 1000, 2)
        set_txn_name(f"MCP/{name}", group="MCP")
        output_text  = json.dumps(result, indent=2) if result else ""
        result_size  = len(output_text)
        output_tokens = max(1, result_size // 4)
        add_attrs([("mcp.estimated_output_tokens", output_tokens)])

        # Degraded-response detection: tool returned an error dict without raising.
        if isinstance(result, dict):
            degraded_reason = result.get("error") or result.get("error_type")
        else:
            degraded_reason = None
        is_degraded = bool(degraded_reason)

        _update_session_stats(
            session_id,
            duration_ms=duration_ms,
            input_tokens=_prompt_input_tokens,
            output_tokens=output_tokens,
            tool_name=name,
            is_degraded=is_degraded,
        )

        # Build common NR attrs shared by both success and degraded paths,
        # append path-specific attrs, then call add_attrs once.
        _common_result_attrs = [
            ("mcp.tool_is_error",        False),
            ("mcp.tool_args_count",      len(args) if args else 0),
            ("mcp.tool_response_size",   result_size),
            ("mcp.tool_duration_ms",     duration_ms),
            ("mcp.tool_output_preview",  output_text[:500]),
            ("mcp.tool_output_char_count", result_size),
        ]

        if is_degraded:
            add_attrs(_common_result_attrs + [
                ("mcp.tool_result_status", "degraded"),
                ("mcp.tool_is_degraded",   True),
                ("mcp.degraded_reason",    degraded_reason),
            ])
            record_metric(f"Custom/Tool/{name}/degraded_count", 1)
            record_metric("Custom/MCP/tool_degraded_count", 1)
            record_metric(f"Custom/Tool/{name}/duration_ms", duration_ms)
            record_event("MCPToolDegraded", {
                "tool_name":            name,
                "publisher_id":         (credentials or {}).get("publisherId", "unknown"),
                "degraded_reason":      degraded_reason,
                "session_id":           session_id or "",
                "session_trace_id":     _session_trace_id,
                "prompt_id":            prompt_id,
                "duration_ms":          duration_ms,
                "tool_input":           tool_input,
                "trace_id":             _trace_id,
                "span_id":              _span_id,
                "tool_start_offset_ms": _start_offset_ms or 0,
                "ai_think_time_ms":     _ai_think_time_ms or 0,
                "env":                  SERVER_ENV,
                "server_version":       SERVER_VERSION,
            })
            logger.warning(
                "MCP tools/call degraded: tool=%s reason=%s duration_ms=%.2f",
                name, degraded_reason, duration_ms,
            )
        else:
            add_attrs(_common_result_attrs + [
                ("mcp.tool_result_status", "success"),
            ])
            record_metric(f"Custom/Tool/{name}/duration_ms", duration_ms)
            record_metric("Custom/MCP/tool_success_count", 1)
            logger.info(
                "MCP tools/call success: tool=%s duration_ms=%.2f response_size=%d",
                name, duration_ms, result_size,
            )

        return _ok(id_, {"content": [{"type": "text", "text": output_text}]})

    except Exception as exc:
        duration_ms = round((time.perf_counter() - t0) * 1000, 2)
        set_txn_name(f"MCP/{name}", group="MCP")

        from .utils import classify_error_category
        error_category = classify_error_category(exc)

        add_attrs([
            ("mcp.tool_result_status", "error"),
            ("mcp.tool_is_error",      True),
            ("mcp.tool_args_count",    len(args) if args else 0),
            ("mcp.tool_response_size", 0),
            ("mcp.tool_duration_ms",   duration_ms),
            ("mcp.tool_output_preview", str(exc)[:500]),
            ("mcp.error_category",     error_category),
        ])

        _update_session_stats(
            session_id,
            duration_ms=duration_ms,
            input_tokens=_prompt_input_tokens,
            tool_name=name,
        )

        record_metric("Custom/MCP/tool_error_count", 1)
        record_metric(f"Custom/Tool/{name}/error_count", 1)
        record_metric(f"Custom/Tool/{name}/error_duration_ms", duration_ms)
        record_event("MCPToolError", {
            "tool_name":            name,
            "publisher_id":         (credentials or {}).get("publisherId", "unknown"),
            "error_type":           type(exc).__name__,
            "error_message":        str(exc)[:500],
            "error_category":       error_category,
            "session_id":           session_id or "",
            "session_trace_id":     _session_trace_id,
            "prompt_id":            prompt_id,
            "prompt_text":          prompt_text[:500],
            "duration_ms":          duration_ms,
            "tool_input":           tool_input,
            "trace_id":             _trace_id,
            "span_id":              _span_id,
            "tool_start_offset_ms": _start_offset_ms or 0,
            "ai_think_time_ms":     _ai_think_time_ms or 0,
            "env":                  SERVER_ENV,
            "server_version":       SERVER_VERSION,
        })
        logger.error(
            "MCP tools/call error: tool=%s session=%s error_category=%s error=%s duration_ms=%.2f",
            name, session_id, error_category, exc, duration_ms, exc_info=True,
        )
        return _ok(id_, {"content": [{"type": "text", "text": f"Error: {exc}"}], "isError": True})


# ── JSON-RPC dispatcher ───────────────────────────────────────────────────────

def _dispatch(body, credentials, request=None, session_id=None, token_expires_at=None):
    """Route a single JSON-RPC message to the correct handler.

    Responsibilities kept here: NR client attrs, method routing, notification
    short-circuit, spec-compliant error codes for unknown/unimplemented methods.

    Tool-call complexity (prompt observability, rate limiting, session stats,
    NR metrics, event emission) lives entirely in _handle_tool_call.
    """
    method = body.get("method", "")
    id_    = body.get("id")

    if request is not None:
        _add_mcp_client_attrs(request)
        _add_session_protocol_attrs(request)

    if id_ is None:
        logger.debug("MCP notification received: method=%s (no response required)", method)
        return None  # notification — no response required

    if method == "initialize":
        set_txn_name("MCP/initialize", group="MCP")
        add_attrs([("mcp.protocol_version", _PROTOCOL_VERSION)])
        if request is not None:
            request.session[_SESSION_PROTOCOL_KEY] = _PROTOCOL_VERSION
        logger.info("MCP initialize: session=%s protocol=%s", session_id, _PROTOCOL_VERSION)
        result = {
            "protocolVersion": _PROTOCOL_VERSION,
            "capabilities":    {"tools": {}},
            "serverInfo":      {"name": "publive-cds", "version": "1.0.0"},
        }
        if token_expires_at is not None:
            result["tokenExpiresAt"] = token_expires_at.isoformat()
        return _ok(id_, result)

    if method == "tools/list":
        set_txn_name("MCP/tools_list", group="MCP")
        all_tools = TOOLS + CMS_TOOLS
        logger.debug("MCP tools/list: session=%s tool_count=%d", session_id, len(all_tools))
        return _ok(id_, {"tools": all_tools})

    if method == "tools/call":
        return _handle_tool_call(
            id_, body.get("params", {}), credentials, request, session_id, token_expires_at
        )

    if method == "ping":
        set_txn_name("MCP/ping", group="MCP")
        logger.debug("MCP ping: session=%s", session_id)
        return _ok(id_, {})

    if method in _UNIMPLEMENTED_METHODS:
        logger.debug("MCP method not implemented (expected): method=%s session=%s", method, session_id)
        add_attrs([("mcp.jsonrpc_error_code", -32601)])
        return _err(id_, -32601, f"Method not found: {method}")

    logger.warning("MCP unknown method: method=%s session=%s jsonrpc_id=%s", method, session_id, id_)
    add_attrs([("mcp.jsonrpc_error_code", -32601), ("mcp.unknown_method", method)])
    record_event("MCPUnknownMethod", {
        "method":     method,
        "session_id": session_id or "",
        "jsonrpc_id": str(id_) if id_ is not None else "",
        "env":        SERVER_ENV,
        "server_version": SERVER_VERSION,
    })
    return _err(id_, -32601, f"Method not found: {method}")


# ── Transport handlers (extracted from mcp_endpoint) ─────────────────────────

def _run_sse_session(request, credentials, token_expires_at):
    """Open an SSE session: register it, stream events, tear down on close.

    Extracted from mcp_endpoint so that SSE transport logic and HTTP transport
    logic are not interleaved in a single function.
    """
    session_id     = str(uuid.uuid4())
    active_threads = threading.active_count()
    publisher_id   = (credentials or {}).get("publisherId", "unknown")

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
    with _sessions_lock:
        _sessions[session_id] = (msg_queue, credentials, token_expires_at)
        active_sessions_on_open = len(_sessions)

    session_open_linking = get_linking_metadata()
    session_trace_id     = session_open_linking.get("trace.id", "")

    with _session_stats_lock:
        _session_stats[session_id] = {
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

    newrelic.agent.record_custom_metric("Custom/MCP/active_sessions", active_sessions_on_open)
    add_attrs([
        ("mcp.active_sessions",  active_sessions_on_open),
        ("mcp.session_trace_id", session_trace_id),
    ])

    record_event("SSESessionOpen", {
        "session_id":      session_id,
        "publisher_id":    publisher_id,
        "active_threads":  active_threads,
        "active_sessions": active_sessions_on_open,
        "trace_id":        session_trace_id,
        "span_id":         session_open_linking.get("span.id", ""),
        "env":             SERVER_ENV,
        "server_version":  SERVER_VERSION,
    })

    base_url = getattr(settings, "BASE_URL", "http://localhost:8000").rstrip("/")
    post_url = f"{base_url}/mcp/message?sessionId={session_id}"
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
            duration_ms = round((time.perf_counter() - stream_t0) * 1000, 2)
            with _sessions_lock:
                _sessions.pop(session_id, None)
                active_sessions_on_close = len(_sessions)
            with _session_stats_lock:
                stats = _session_stats.pop(session_id, {})

            tool_call_count        = stats.get("tool_count", 0)
            tool_error_count       = stats.get("error_count", 0)
            tool_degraded_count    = stats.get("degraded_count", 0)
            total_tool_duration_ms = round(stats.get("total_tool_duration_ms", 0.0), 2)
            total_input_tokens     = stats.get("total_estimated_input_tokens", 0)
            total_output_tokens    = stats.get("total_estimated_output_tokens", 0)
            session_client_name    = stats.get("client_name") or "unknown"
            session_trace_id_      = stats.get("session_trace_id", "")
            tool_sequence_str      = ",".join(stats.get("tool_sequence", []))[:500]

            server_work_pct = (
                round(total_tool_duration_ms / duration_ms * 100, 1)
                if duration_ms > 0 else 0.0
            )

            newrelic.agent.record_custom_metric("Custom/MCP/active_sessions", active_sessions_on_close)

            if tool_call_count == 0:
                record_metric("Custom/MCP/session_abandon_count", 1)
                record_event("MCPSessionAbandoned", {
                    "session_id":          session_id,
                    "publisher_id":        publisher_id,
                    "duration_ms":         duration_ms,
                    "session_client_name": session_client_name,
                    "session_trace_id":    session_trace_id_,
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
                "tool_call_count":               tool_call_count,
                "tool_error_count":              tool_error_count,
                "tool_degraded_count":           tool_degraded_count,
                "total_tool_duration_ms":        total_tool_duration_ms,
                "total_estimated_input_tokens":  total_input_tokens,
                "total_estimated_output_tokens": total_output_tokens,
                "total_estimated_tokens":        total_input_tokens + total_output_tokens,
                "server_work_pct":               server_work_pct,
                "session_client_name":           session_client_name,
                "session_trace_id":              session_trace_id_,
                "active_sessions_remaining":     active_sessions_on_close,
                "tool_sequence":                 tool_sequence_str,
                "env":                           SERVER_ENV,
                "server_version":                SERVER_VERSION,
            })
            record_event("SSESessionClose", {
                "session_id":           session_id,
                "publisher_id":         publisher_id,
                "duration_ms":          duration_ms,
                "tool_call_count":      tool_call_count,
                "tool_error_count":     tool_error_count,
                "tool_degraded_count":  tool_degraded_count,
                "total_tool_duration_ms": total_tool_duration_ms,
                "session_trace_id":     session_trace_id_,
                "env":                  SERVER_ENV,
                "server_version":       SERVER_VERSION,
            })
            logger.info(
                "SSE session close: session=%s publisher=%s duration_ms=%.2f "
                "tool_calls=%d tool_errors=%d",
                session_id, publisher_id, duration_ms, tool_call_count, tool_error_count,
            )

    resp = StreamingHttpResponse(event_stream(), content_type="text/event-stream")
    resp["Cache-Control"]     = "no-cache"
    resp["X-Accel-Buffering"] = "no"
    return resp


def _run_http_post(request, credentials, token_expires_at):
    """Handle the Streamable HTTP (POST) transport.

    Extracted from mcp_endpoint so SSE and HTTP paths are separate functions
    rather than two deeply-nested branches inside one view.
    """
    request_size   = len(request.body)
    active_threads = threading.active_count()
    session_id     = _get_session_id(request)

    add_attrs([
        ("mcp.transport",            "http"),
        ("mcp.session_id",           session_id),
        ("mcp.thread_active_count",  active_threads),
        ("mcp.request_size_bytes",   request_size),
    ])
    _add_session_protocol_attrs(request)
    newrelic.agent.record_custom_metric("Custom/MCP/active_threads", active_threads)

    try:
        body = json.loads(request.body)
    except json.JSONDecodeError:
        logger.warning("MCP POST invalid JSON: size=%d", request_size)
        return _bad_json_response()

    try:
        if isinstance(body, list):
            logger.debug("MCP batch request: count=%d session=%s", len(body), session_id)
            responses = [
                r for r in (
                    _dispatch(msg, credentials, request, session_id, token_expires_at)
                    for msg in body
                )
                if r is not None
            ]
            return JsonResponse(responses, safe=False) if responses else HttpResponse(status=202)

        response = _dispatch(body, credentials, request, session_id, token_expires_at)
        if response is None:
            return HttpResponse(status=202)
        return JsonResponse(response)
    except Exception as exc:
        logger.error("MCP transport error: session=%s", session_id, exc_info=True)
        notice_err(exc, [("error.layer", "transport")])
        raise


# ── Views ─────────────────────────────────────────────────────────────────────

@newrelic.agent.function_trace(name="health_check", group="Transport")
def health_check(request):
    newrelic.agent.suppress_apdex_metric()
    newrelic.agent.suppress_transaction_trace()
    return JsonResponse({
        "status":   "ok",
        "service":  "publive-cds-mcp",
        "version":  "1.0.0",
        "protocol": _PROTOCOL_VERSION,
    })


@csrf_exempt
@newrelic.agent.function_trace(name="mcp_endpoint", group="Transport")
def mcp_endpoint(request):
    """Authenticate then route to the correct transport handler.

    Previously contained both the SSE and HTTP transport implementations
    as deeply-nested branches.  Each transport now lives in its own function
    (_run_sse_session / _run_http_post).
    """
    credentials, token_expires_at = _get_credentials(request)
    if not credentials:
        logger.warning("MCP authentication failed: method=%s", request.method)
        add_attrs([("mcp.authenticated", False)])
        return _unauth(request)

    _add_mcp_client_attrs(request)

    if request.method == "GET":
        return _run_sse_session(request, credentials, token_expires_at)
    if request.method == "POST":
        return _run_http_post(request, credentials, token_expires_at)
    return HttpResponse(status=405)


@csrf_exempt
@newrelic.agent.function_trace(name="mcp_message", group="Transport")
def mcp_message(request):
    """Legacy SSE transport: POST /mcp/message?sessionId=<id>"""
    if request.method != "POST":
        return HttpResponse(status=405)

    raw_sid    = request.GET.get("sessionId", "")
    session_id = raw_sid if raw_sid else "anon-" + uuid.uuid4().hex[:8]
    active_threads = threading.active_count()
    add_attrs([
        ("mcp.session_id",          session_id),
        ("mcp.thread_active_count", active_threads),
        ("mcp.request_size_bytes",  len(request.body)),
    ])
    _add_mcp_client_attrs(request)
    _add_session_protocol_attrs(request)

    with _sessions_lock:
        session_entry = _sessions.get(session_id)

    if session_entry is None:
        logger.warning("mcp_message: no active SSE session: session_id=%s", session_id)
        add_attrs([("mcp.sse_session_missing", True)])
        record_metric("Custom/MCP/sse_session_missing_count", 1)
        record_event("MCPSessionMissing", {
            "session_id":   session_id,
            "env":          SERVER_ENV,
            "server_version": SERVER_VERSION,
        })
        return JsonResponse({"error": "No active MCP session."}, status=400)

    msg_queue, credentials, token_expires_at = session_entry

    with _session_stats_lock:
        _msg_stats = _session_stats.get(session_id) or {}
    _session_trace_id = _msg_stats.get("session_trace_id", "")
    if _session_trace_id:
        add_attrs([("mcp.session_trace_id", _session_trace_id)])

    try:
        body = json.loads(request.body)
    except json.JSONDecodeError:
        logger.warning("mcp_message invalid JSON: session=%s", session_id)
        return _bad_json_response()

    body_method = body.get("method", "") if isinstance(body, dict) else ""
    if body_method == "tools/call":
        with _session_stats_lock:
            stats = _session_stats.get(session_id)
            if stats is not None:
                stats["tool_count"] += 1
                seq = stats["tool_count"]
            else:
                seq = 0
        if seq:
            add_attrs([("mcp.session_tool_seq", seq)])

    try:
        response_msg = _dispatch(body, credentials, request, session_id, token_expires_at)

        if (
            body_method == "tools/call"
            and isinstance(response_msg, dict)
            and isinstance(response_msg.get("result"), dict)
            and response_msg["result"].get("isError")
        ):
            with _session_stats_lock:
                stats = _session_stats.get(session_id)
                if stats is not None:
                    stats["error_count"] += 1

        if response_msg is not None:
            try:
                msg_queue.put((time.perf_counter(), response_msg), block=True, timeout=30.0)
            except queue.Full:
                record_metric("Custom/MCP/queue_overflow_count", 1)
                add_attrs([("mcp.queue_overflow", True)])
                logger.error(
                    "MCP SSE queue full (maxsize=%d) after 30 s: session=%s — "
                    "response dropped; client is not draining the SSE stream",
                    _MCP_QUEUE_MAXSIZE, session_id,
                )
                return JsonResponse({"ok": True})
            queue_depth = msg_queue.qsize()
            add_attrs([("mcp.session_queue_depth", queue_depth)])
            newrelic.agent.record_custom_metric("Custom/MCP/session_queue_depth", queue_depth)

        return JsonResponse({"ok": True})
    except Exception as exc:
        logger.error("mcp_message transport error: session=%s", session_id, exc_info=True)
        notice_err(exc, [("error.layer", "transport")])
        raise
