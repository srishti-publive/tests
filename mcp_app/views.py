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

logger = logging.getLogger(__name__)

# session_id → (Queue, credentials)  (shared across threads; single gunicorn worker required)
_sessions: dict[str, tuple[queue.Queue, dict]] = {}
_sessions_lock = threading.Lock()

# session_id → {"tool_count": int, "error_count": int}
# Tracks per-SSE-session stats for MCPSessionSummary event and session_tool_seq attribute.
_session_stats: dict[str, dict] = {}
_session_stats_lock = threading.Lock()

_PROTOCOL_VERSION = "2024-11-05"
_SESSION_PROTOCOL_KEY = "mcp_protocol_version"

# MCPPrompt event sampling: emit at most this many events per minute per process.
# Prevents hitting NR's 3000 custom-events/min limit under heavy load (50 threads).
_PROMPT_EVENT_MAX_PER_MIN = 1000
_prompt_event_count = 0
_prompt_event_window_start = time.monotonic()
_prompt_event_lock = threading.Lock()

# Maximum SSE message queue depth per session.  Without a bound a slow client
# leaks unbounded memory.  Override via MCP_QUEUE_MAXSIZE env var.
import os as _os
_MCP_QUEUE_MAXSIZE = int(_os.environ.get("MCP_QUEUE_MAXSIZE", "100"))

def _should_emit_prompt_event() -> bool:
    """Return True if we are under the per-minute prompt-event budget."""
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
# Maps the lower-cased first token of User-Agent to a human-readable name.
# Without this, dashboards show raw UA fragments like "python-requests" or
# "claude" instead of "Python Requests Client" or "Claude Desktop".
_CLIENT_NAME_MAP: dict[str, str] = {
    "claude":            "Claude Desktop",
    "cursor":            "Cursor",
    "anthropic":         "Anthropic SDK",
    "python-requests":   "Python Requests Client",
    "python-httpx":      "Python HTTPX Client",
    "mcp":               "MCP Python SDK",
    "node":              "Node.js MCP Client",
    "go-http-client":    "Go MCP Client",
    "axios":             "Axios (JS)",
    "openai":            "OpenAI SDK",
}


def _classify_tool_error(exc) -> str:
    """Map an exception from call_tool to a standard error.category string.

    Categories align with the ones set by cds_client._cds_error_category so
    NRQL can use a single FACET across all layers.
    """
    http_status = getattr(getattr(exc, "response", None), "status_code", None)
    exc_lower = str(exc).lower()
    if http_status == 408 or "timeout" in exc_lower or "timed out" in exc_lower:
        return "timeout"
    if http_status == 401:
        return "auth_error"
    if http_status and 400 <= http_status < 500:
        return "client_error"
    if http_status and 500 <= http_status < 600:
        return "upstream_error"
    return "system_error"


@newrelic.agent.function_trace(name="get_credentials", group="Auth")
def _get_credentials(request):
    """Resolve credentials from Bearer token (DB lookup) or session cookie.

    Wrapped in a function trace so the OAuthToken DB query is visible in the
    APM waterfall — previously this latency was invisible.
    """
    auth_header = request.META.get("HTTP_AUTHORIZATION", "")
    if auth_header.startswith("Bearer "):
        token_value = auth_header[len("Bearer "):]
        try:
            from auth_app.models import OAuthToken
            oauth_token = OAuthToken.objects.get(token=token_value)
            if oauth_token.expires_at >= timezone.now():
                return oauth_token.credentials
        except Exception:
            pass
    return request.session.get("credentials")


def _get_session_id(request) -> str:
    """Return a stable session identifier for this request.

    Priority:
    1. Django session key  — used by browser / session-cookie clients
    2. SHA-256 prefix of Bearer token — used by OAuth clients (Claude AI, Cursor, etc.)
       Same token across multiple HTTP requests in one conversation → same session ID.
    3. Transient UUID — guarantees logs always have a non-empty session value even
       for unauthenticated or sessionless requests (e.g. preflight probes).
    """
    key = getattr(request.session, "session_key", None)
    if key:
        return key

    auth_header = request.META.get("HTTP_AUTHORIZATION", "")
    if auth_header.startswith("Bearer "):
        token = auth_header[len("Bearer "):]
        return "oauth-" + hashlib.sha256(token.encode()).hexdigest()[:16]

    # No session cookie and no Bearer token — assign a transient ID so that
    # every log line has a non-empty session= field and errors remain correlatable.
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
    client_name = "unknown"
    client_version = "unknown"
    match = re.match(r"^([^\s/]+)/([^\s]+)", ua)
    if match:
        raw_name = match.group(1).lower()
        client_version = match.group(2)
        # Normalise known clients; fall back to original casing when unknown.
        client_name = _CLIENT_NAME_MAP.get(raw_name, match.group(1))
    elif ua and ua != "unknown":
        raw_name = (ua.split()[0] if ua.split() else ua).lower()
        client_name = _CLIENT_NAME_MAP.get(raw_name, ua.split()[0] if ua.split() else ua)
    return client_name, client_version


def _add_mcp_client_attrs(request):
    client_name, client_version = _mcp_client_identity(request)
    add_attrs([
        ("mcp.client_name", client_name),
        ("mcp.client_version", client_version),
    ])


def _add_session_protocol_attrs(request):
    if request is None:
        return
    protocol_version = request.session.get(_SESSION_PROTOCOL_KEY)
    if protocol_version:
        add_attrs([("mcp.protocol_version", protocol_version)])


# ── Known-but-unimplemented MCP methods ───────────────────────────────────────
# These are valid MCP protocol methods that this server intentionally does not
# implement (we declared only "tools" in capabilities).  Compliant clients may
# still probe for them.  We return spec-correct -32601 at DEBUG level so they
# don't pollute the WARNING log or fire MCPUnknownMethod custom events.

_UNIMPLEMENTED_METHODS: frozenset[str] = frozenset({
    "sampling/createMessage",   # server→client; if client sends it, refuse cleanly
    "roots/list",               # filesystem roots — not applicable to this API server
    "resources/list",           # resource capability not declared
    "resources/read",
    "resources/subscribe",
    "resources/unsubscribe",
    "prompts/list",             # prompt-template capability not declared
    "prompts/get",
    "completion/complete",      # completion capability not declared
    "logging/setLevel",         # logging capability not declared
})


# ── JSON-RPC helpers ──────────────────────────────────────────────────────────

def _ok(id_, result):
    return {"jsonrpc": "2.0", "id": id_, "result": result}


def _err(id_, code, message):
    return {"jsonrpc": "2.0", "id": id_, "error": {"code": code, "message": message}}


def _dispatch(body, credentials, request=None, session_id=None):
    method = body.get("method", "")
    id_    = body.get("id")

    if request is not None:
        _add_mcp_client_attrs(request)
        _add_session_protocol_attrs(request)

    if id_ is None:
        logger.debug("MCP notification received: method=%s (no response required)", method)
        return None  # notification — no response

    if method == "initialize":
        set_txn_name("MCP/initialize", group="MCP")
        add_attrs([("mcp.protocol_version", _PROTOCOL_VERSION)])
        if request is not None:
            request.session[_SESSION_PROTOCOL_KEY] = _PROTOCOL_VERSION
        logger.info("MCP initialize: session=%s protocol=%s", session_id, _PROTOCOL_VERSION)
        return _ok(id_, {
            "protocolVersion": _PROTOCOL_VERSION,
            "capabilities": {"tools": {}},
            "serverInfo": {"name": "publive-cds", "version": "1.0.0"},
        })

    if method == "tools/list":
        set_txn_name("MCP/tools_list", group="MCP")
        logger.debug("MCP tools/list: session=%s tool_count=%d", session_id, len(TOOLS))
        return _ok(id_, {"tools": TOOLS})

    if method == "tools/call":
        params = body.get("params", {})
        name   = params.get("name", "")
        prompt_id, prompt_text, prompt_source, args = extract_prompt_for_tool_call(
            request, body, params
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
            # Budget exceeded: still set transaction attrs, skip the custom event.
            # Emit a metric so dashboards can show "how often is NR prompt observability
            # degraded?" — a rising drop rate means the rate limit needs tuning.
            add_attrs([
                ("mcp.prompt_id", prompt_id),
                ("mcp.prompt_text", prompt_text),
                ("mcp.prompt_source", prompt_source),
                ("mcp.session_id", session_id or ""),
                ("mcp.tool_name", name),
            ])
            record_metric("Custom/MCP/prompt_event_dropped_count", 1)
            logger.warning(
                "MCPPrompt event dropped (rate limit): tool=%s session=%s", name, session_id
            )

        # Capture tool input args as a dedicated attribute — always set regardless
        # of prompt_source so the Tool Call List NRQL has a reliable input field.
        tool_input = json.dumps(args, ensure_ascii=False, default=str)[:500] if args else ""
        add_attrs([
            ("mcp.tool_name", name),
            ("mcp.tool_input", tool_input),
        ])

        # Per-tool and global invocation counters — emitted before execution so they
        # record on every outcome: success, degraded, and error.
        record_metric(f"Custom/Tool/{name}/call_count", 1)
        record_metric("Custom/MCP/tool_call_count", 1)
        logger.debug(
            "NR metric emitted: Custom/MCP/tool_call_count tool=%s session=%s",
            name, session_id,
        )

        # ── Session timeline attributes ───────────────────────────────────────
        # Read session start time and last-tool-end timestamp atomically.
        # start_offset_ms: how far into the session this tool was called.
        # ai_think_time_ms: gap between previous tool response and this call
        #   (proxy for AI processing time between tool calls).
        _start_offset_ms = None
        _ai_think_time_ms = None
        _prompt_input_tokens = max(1, len(prompt_text) // 4)
        # session_trace_id for cross-referencing MCPToolError/MCPToolDegraded events
        # back to the session they belong to — without it these events are orphaned.
        _session_trace_id_for_events = ""

        with _session_stats_lock:
            _disp_stats = _session_stats.get(session_id or "")
            if _disp_stats is not None:
                _sess_start = _disp_stats.get("session_start_time")
                _last_end   = _disp_stats.get("last_tool_end_perf")
                if _sess_start is not None:
                    _start_offset_ms = round((time.perf_counter() - _sess_start) * 1000, 2)
                if _last_end is not None:
                    _ai_think_time_ms = round((time.perf_counter() - _last_end) * 1000, 2)
                # Capture AI client name on the first tool call that has a User-Agent.
                if _disp_stats.get("client_name") is None and request is not None:
                    _disp_stats["client_name"] = request.META.get("HTTP_USER_AGENT", "unknown")[:200]
                _session_trace_id_for_events = _disp_stats.get("session_trace_id", "")

        if _start_offset_ms is not None:
            add_attrs([("mcp.tool_start_offset_ms", _start_offset_ms)])
        if _ai_think_time_ms is not None:
            add_attrs([("mcp.ai_think_time_ms", _ai_think_time_ms)])

        t0 = time.perf_counter()
        try:
            result = call_tool(credentials, name, args)
            duration_ms = round((time.perf_counter() - t0) * 1000, 2)
            set_txn_name(f"MCP/{name}", group="MCP")
            output_text = json.dumps(result, indent=2) if result else ""
            result_size = len(output_text)

            # Output token estimate — mirrors input estimate in prompt_capture.py.
            # Gives a full token cost picture per tool call (input + output).
            output_tokens = max(1, result_size // 4)
            add_attrs([("mcp.estimated_output_tokens", output_tokens)])

            # Collect trace linking data for event ↔ APM trace correlation.
            _linking = get_linking_metadata()
            _trace_id = _linking.get("trace.id", "")
            _span_id  = _linking.get("span.id", "")

            # Degraded response detection: 7 tools return {"error": "..."} dicts on
            # partial failures (upstream_timeout, invalid_input, not_found, not_configured,
            # auth_expired).  These complete without raising, so they land here looking
            # like successes.  Distinguish them explicitly so NR dashboards can show
            # "clean success", "degraded", and "error" as separate series.
            degraded_reason = result.get("error") if isinstance(result, dict) else None
            is_degraded = bool(degraded_reason)

            # Update session-level accumulators regardless of degraded/success.
            with _session_stats_lock:
                _upd = _session_stats.get(session_id or "")
                if _upd is not None:
                    _upd["total_tool_duration_ms"] = _upd.get("total_tool_duration_ms", 0.0) + duration_ms
                    _upd["total_estimated_input_tokens"]  = _upd.get("total_estimated_input_tokens", 0)  + _prompt_input_tokens
                    _upd["total_estimated_output_tokens"] = _upd.get("total_estimated_output_tokens", 0) + output_tokens
                    _upd["last_tool_end_perf"] = time.perf_counter()
                    # Ordered list of tool names for session replay via MCPSessionSummary.
                    _upd.setdefault("tool_sequence", []).append(name)
                    if is_degraded:
                        _upd["degraded_count"] = _upd.get("degraded_count", 0) + 1

            if is_degraded:
                add_attrs([
                    ("mcp.tool_result_status", "degraded"),
                    ("mcp.tool_is_degraded", True),
                    ("mcp.tool_is_error", False),
                    ("mcp.degraded_reason", degraded_reason),
                    ("mcp.tool_args_count", len(args) if args else 0),
                    ("mcp.tool_response_size", result_size),
                    ("mcp.tool_duration_ms", duration_ms),
                    ("mcp.tool_output_preview", output_text[:500]),
                    ("mcp.tool_output_char_count", result_size),
                ])
                record_metric(f"Custom/Tool/{name}/degraded_count", 1)
                record_metric("Custom/MCP/tool_degraded_count", 1)
                record_metric(f"Custom/Tool/{name}/duration_ms", duration_ms)
                logger.debug(
                    "NR metric emitted: Custom/MCP/tool_degraded_count tool=%s reason=%s",
                    name, degraded_reason,
                )
                record_event("MCPToolDegraded", {
                    "tool_name": name,
                    "publisher_id": (credentials or {}).get("publisherId", "unknown"),
                    "degraded_reason": degraded_reason,
                    "session_id": session_id or "",
                    "session_trace_id": _session_trace_id_for_events,
                    "prompt_id": prompt_id,
                    "duration_ms": duration_ms,
                    "tool_input": tool_input,
                    "trace_id": _trace_id,
                    "span_id": _span_id,
                    "tool_start_offset_ms": _start_offset_ms or 0,
                    "ai_think_time_ms": _ai_think_time_ms or 0,
                    "env": SERVER_ENV,
                    "server_version": SERVER_VERSION,
                })
                logger.warning(
                    "MCP tools/call degraded: tool=%s reason=%s duration_ms=%.2f",
                    name, degraded_reason, duration_ms,
                )
            else:
                add_attrs([
                    ("mcp.tool_result_status", "success"),
                    ("mcp.tool_is_error", False),
                    ("mcp.tool_args_count", len(args) if args else 0),
                    ("mcp.tool_response_size", result_size),
                    ("mcp.tool_duration_ms", duration_ms),
                    ("mcp.tool_output_preview", output_text[:500]),
                    ("mcp.tool_output_char_count", result_size),
                ])
                record_metric(f"Custom/Tool/{name}/duration_ms", duration_ms)
                record_metric("Custom/MCP/tool_success_count", 1)
                logger.info(
                    "MCP tools/call success: tool=%s duration_ms=%.2f response_size=%d",
                    name, duration_ms, result_size,
                )
                logger.debug(
                    "NR metric emitted: Custom/MCP/tool_success_count tool=%s", name
                )

            return _ok(id_, {"content": [{"type": "text", "text": output_text}]})
        except Exception as exc:
            duration_ms = round((time.perf_counter() - t0) * 1000, 2)
            set_txn_name(f"MCP/{name}", group="MCP")
            error_category = _classify_tool_error(exc)

            _linking = get_linking_metadata()
            _trace_id = _linking.get("trace.id", "")
            _span_id  = _linking.get("span.id", "")

            add_attrs([
                ("mcp.tool_result_status", "error"),
                ("mcp.tool_is_error", True),
                ("mcp.tool_args_count", len(args) if args else 0),
                ("mcp.tool_response_size", 0),
                ("mcp.tool_duration_ms", duration_ms),
                ("mcp.tool_output_preview", str(exc)[:500]),
                ("mcp.error_category", error_category),
            ])

            # Update session accumulators — errors still consume server time.
            with _session_stats_lock:
                _upd = _session_stats.get(session_id or "")
                if _upd is not None:
                    _upd["total_tool_duration_ms"] = _upd.get("total_tool_duration_ms", 0.0) + duration_ms
                    _upd["total_estimated_input_tokens"] = _upd.get("total_estimated_input_tokens", 0) + _prompt_input_tokens
                    _upd["last_tool_end_perf"] = time.perf_counter()
                    # Record tool in sequence even on error so replay is complete.
                    _upd.setdefault("tool_sequence", []).append(name)

            record_metric("Custom/MCP/tool_error_count", 1)
            record_metric(f"Custom/Tool/{name}/error_count", 1)
            # Duration metric on error path — enables p95/p99 latency for failed
            # calls separately from successful ones (timeout failures pull p99 up).
            record_metric(f"Custom/Tool/{name}/error_duration_ms", duration_ms)
            logger.debug(
                "NR metric emitted: Custom/MCP/tool_error_count tool=%s category=%s",
                name, error_category,
            )
            record_event("MCPToolError", {
                "tool_name": name,
                "publisher_id": (credentials or {}).get("publisherId", "unknown"),
                "error_type": type(exc).__name__,
                "error_message": str(exc)[:500],
                "error_category": error_category,
                "session_id": session_id or "",
                "session_trace_id": _session_trace_id_for_events,
                "prompt_id": prompt_id,
                "prompt_text": prompt_text[:500],
                "duration_ms": duration_ms,
                "tool_input": tool_input,
                "trace_id": _trace_id,
                "span_id": _span_id,
                "tool_start_offset_ms": _start_offset_ms or 0,
                "ai_think_time_ms": _ai_think_time_ms or 0,
                "env": SERVER_ENV,
                "server_version": SERVER_VERSION,
            })
            logger.error(
                "MCP tools/call error: tool=%s session=%s error_category=%s error=%s duration_ms=%.2f",
                name, session_id, error_category, exc, duration_ms, exc_info=True,
            )
            return _ok(id_, {"content": [{"type": "text", "text": f"Error: {exc}"}], "isError": True})

    if method == "ping":
        set_txn_name("MCP/ping", group="MCP")
        logger.debug("MCP ping: session=%s", session_id)
        return _ok(id_, {})

    # Known-but-unimplemented methods: spec-compliant -32601, no WARNING noise.
    # These come from compliant clients probing capabilities we didn't declare.
    if method in _UNIMPLEMENTED_METHODS:
        logger.debug(
            "MCP method not implemented (expected): method=%s session=%s", method, session_id
        )
        add_attrs([("mcp.jsonrpc_error_code", -32601)])
        return _err(id_, -32601, f"Method not found: {method}")

    # Truly unknown method — record as observable event so unexpected clients are visible
    logger.warning("MCP unknown method: method=%s session=%s jsonrpc_id=%s", method, session_id, id_)
    add_attrs([
        ("mcp.jsonrpc_error_code", -32601),
        ("mcp.unknown_method", method),
    ])
    record_event("MCPUnknownMethod", {
        "method": method,
        "session_id": session_id or "",
        "jsonrpc_id": str(id_) if id_ is not None else "",
        "env": SERVER_ENV,
        "server_version": SERVER_VERSION,
    })
    return _err(id_, -32601, f"Method not found: {method}")


# ── Views ─────────────────────────────────────────────────────────────────────

@newrelic.agent.function_trace(name="health_check", group="Transport")
def health_check(request):
    """Root health check — returns service name, version, and liveness status.

    Suppressed from New Relic Apdex and slow-transaction traces so frequent
    uptime probes don't skew latency metrics.
    """
    newrelic.agent.suppress_apdex_metric()
    newrelic.agent.suppress_transaction_trace()
    return JsonResponse({
        "status": "ok",
        "service": "publive-cds-mcp",
        "version": "1.0.0",
        "protocol": _PROTOCOL_VERSION,
    })


@csrf_exempt
@newrelic.agent.function_trace(name="mcp_endpoint", group="Transport")
def mcp_endpoint(request):
    credentials = _get_credentials(request)
    if not credentials:
        logger.warning(
            "MCP authentication failed: method=%s",
            request.method,
        )
        add_attrs([
            ("mcp.authenticated", False),
        ])
        return _unauth(request)

    _add_mcp_client_attrs(request)

    if request.method == "GET":
        # Legacy SSE transport
        session_id = str(uuid.uuid4())
        set_txn_name("Transport/SSE", group="Transport")
        # SSE sessions are long-lived (minutes to hours).  Suppress Apdex and
        # slow-transaction traces so they don't pollute the APM overview page.
        # All meaningful telemetry is captured via custom events and metrics.
        suppress_apdex()
        suppress_trace()
        active_threads = threading.active_count()
        publisher_id = (credentials or {}).get("publisherId", "unknown")
        add_attrs([
            ("mcp.transport", "sse"),
            ("mcp.session_id", session_id),
            ("mcp.thread_active_count", active_threads),
        ])
        _add_session_protocol_attrs(request)

        logger.info(
            "SSE session open: session=%s publisher=%s active_threads=%d",
            session_id, publisher_id, active_threads,
        )
        # Register session BEFORE emitting SSESessionOpen so active_sessions_on_open
        # is defined.  (Previous ordering caused a NameError at runtime.)
        # Bounded queue: prevents a slow/disconnected client from leaking unbounded
        # memory.  MCP_QUEUE_MAXSIZE env var (default 100) controls the ceiling.
        msg_queue: queue.Queue = queue.Queue(maxsize=_MCP_QUEUE_MAXSIZE)
        with _sessions_lock:
            _sessions[session_id] = (msg_queue, credentials)
            active_sessions_on_open = len(_sessions)
        # Capture the SSE-open trace.id so it can be stamped on every subsequent
        # mcp_message transaction for this session.  Allows:
        #   SELECT * FROM Transaction WHERE mcp.session_trace_id = 'X'
        # to retrieve all transactions in the session even though they are separate
        # HTTP requests with separate trace.ids.
        session_open_linking = get_linking_metadata()
        session_trace_id = session_open_linking.get("trace.id", "")

        with _session_stats_lock:
            _session_stats[session_id] = {
                "tool_count": 0,
                "error_count": 0,
                "degraded_count": 0,
                # Wall-clock anchor for mcp.tool_start_offset_ms per tool call.
                "session_start_time": time.perf_counter(),
                # Cumulative tool execution time — compare to MCPSessionSummary.duration_ms
                # to see what fraction of the session was active server work vs AI think time.
                "total_tool_duration_ms": 0.0,
                # Per-session token cost proxies (input + output, chars ÷ 4).
                "total_estimated_input_tokens": 0,
                "total_estimated_output_tokens": 0,
                # Timestamp of last tool response enqueue; used to compute AI think time
                # between tool calls (last_tool_end_perf → next tool start = AI latency).
                "last_tool_end_perf": None,
                # AI client identity — stored on first tool call with a User-Agent.
                "client_name": None,
                # Stable session-level trace anchor for cross-transaction NRQL joins.
                "session_trace_id": session_trace_id,
                # Ordered list of tool names called this session — for MCPSessionSummary
                # replay and "what did this AI session actually do?" debugging.
                "tool_sequence": [],
            }

        newrelic.agent.record_custom_metric("Custom/MCP/active_sessions", active_sessions_on_open)
        add_attrs([
            ("mcp.active_sessions", active_sessions_on_open),
            ("mcp.session_trace_id", session_trace_id),
        ])

        # SSESessionOpen does NOT need a current_transaction() guard —
        # record_event() works at application level without an active transaction.
        record_event("SSESessionOpen", {
            "session_id": session_id,
            "publisher_id": publisher_id,
            "active_threads": active_threads,
            "active_sessions": active_sessions_on_open,
            "trace_id": session_trace_id,
            "span_id": session_open_linking.get("span.id", ""),
            "env": SERVER_ENV,
            "server_version": SERVER_VERSION,
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
                        # Queue wait time: how long the response sat in the queue
                        # before the SSE generator consumed it.  High values mean
                        # the client is not reading the stream fast enough.
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

                tool_call_count  = stats.get("tool_count", 0)
                tool_error_count = stats.get("error_count", 0)
                tool_degraded_count    = stats.get("degraded_count", 0)
                total_tool_duration_ms = round(stats.get("total_tool_duration_ms", 0.0), 2)
                total_input_tokens     = stats.get("total_estimated_input_tokens", 0)
                total_output_tokens    = stats.get("total_estimated_output_tokens", 0)
                session_client_name    = stats.get("client_name") or "unknown"
                session_trace_id       = stats.get("session_trace_id", "")
                # Compact ordered string: "list_posts,get_post,get_category" (max 500 chars)
                # Enables session replay and "what did this AI session do?" debugging.
                tool_sequence_str = ",".join(stats.get("tool_sequence", []))[:500]

                # Derived: how much of the session wall time was active server work?
                # The rest (duration_ms - total_tool_duration_ms) is AI think time + network.
                server_work_pct = (
                    round(total_tool_duration_ms / duration_ms * 100, 1)
                    if duration_ms > 0 else 0.0
                )

                newrelic.agent.record_custom_metric("Custom/MCP/active_sessions", active_sessions_on_close)

                # MCPSessionAbandoned: fires when a session opened but no tools were called.
                # Captures misconfigured clients, auth probes, and network blips that never
                # complete the MCP handshake → tool call flow.
                if tool_call_count == 0:
                    record_metric("Custom/MCP/session_abandon_count", 1)
                    record_event("MCPSessionAbandoned", {
                        "session_id": session_id,
                        "publisher_id": publisher_id,
                        "duration_ms": duration_ms,
                        "session_client_name": session_client_name,
                        "session_trace_id": session_trace_id,
                        "env": SERVER_ENV,
                        "server_version": SERVER_VERSION,
                    })
                    logger.info(
                        "SSE session abandoned (0 tool calls): session=%s publisher=%s duration_ms=%.2f",
                        session_id, publisher_id, duration_ms,
                    )

                # MCPSessionSummary: session-level rollup for per-session dashboards.
                # Fires at close so it captures complete tool call and error counts.
                record_event("MCPSessionSummary", {
                    "session_id": session_id,
                    "publisher_id": publisher_id,
                    "duration_ms": duration_ms,
                    "tool_call_count": tool_call_count,
                    "tool_error_count": tool_error_count,
                    "tool_degraded_count": tool_degraded_count,
                    "total_tool_duration_ms": total_tool_duration_ms,
                    "total_estimated_input_tokens": total_input_tokens,
                    "total_estimated_output_tokens": total_output_tokens,
                    "total_estimated_tokens": total_input_tokens + total_output_tokens,
                    "server_work_pct": server_work_pct,
                    "session_client_name": session_client_name,
                    "session_trace_id": session_trace_id,
                    "active_sessions_remaining": active_sessions_on_close,
                    # Ordered tool call sequence — enables session replay without
                    # joining N separate Transaction records.
                    "tool_sequence": tool_sequence_str,
                    "env": SERVER_ENV,
                    "server_version": SERVER_VERSION,
                })
                # Use record_event() (no current_transaction() guard)
                # so this fires even after the WSGI transaction context has shifted.
                record_event("SSESessionClose", {
                    "session_id": session_id,
                    "publisher_id": publisher_id,
                    "duration_ms": duration_ms,
                    "tool_call_count": tool_call_count,
                    "tool_error_count": tool_error_count,
                    "tool_degraded_count": tool_degraded_count,
                    "total_tool_duration_ms": total_tool_duration_ms,
                    "session_trace_id": session_trace_id,
                    "env": SERVER_ENV,
                    "server_version": SERVER_VERSION,
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

    if request.method == "POST":
        # Streamable HTTP transport (MCP 2025-11-25)
        request_size = len(request.body)
        active_threads = threading.active_count()
        session_id = _get_session_id(request)
        add_attrs([
            ("mcp.transport", "http"),
            ("mcp.session_id", session_id),
            ("mcp.thread_active_count", active_threads),
            ("mcp.request_size_bytes", request_size),
        ])
        _add_session_protocol_attrs(request)
        # Custom metric for thread saturation monitoring
        newrelic.agent.record_custom_metric("Custom/MCP/active_threads", active_threads)

        try:
            body = json.loads(request.body)
        except json.JSONDecodeError:
            logger.warning("MCP POST invalid JSON: size=%d", request_size)
            return JsonResponse({"error": "Invalid JSON"}, status=400)

        try:
            if isinstance(body, list):
                logger.debug("MCP batch request: count=%d session=%s", len(body), session_id)
                responses = [
                    r for r in (_dispatch(msg, credentials, request, session_id) for msg in body)
                    if r is not None
                ]
                return JsonResponse(responses, safe=False) if responses else HttpResponse(status=202)

            response = _dispatch(body, credentials, request, session_id)
            if response is None:
                return HttpResponse(status=202)
            return JsonResponse(response)
        except Exception as exc:
            logger.error("MCP transport error: session=%s", session_id, exc_info=True)
            notice_err(exc, [("error.layer", "transport")])
            raise

    return HttpResponse(status=405)


@csrf_exempt
@newrelic.agent.function_trace(name="mcp_message", group="Transport")
def mcp_message(request):
    """Legacy SSE transport: POST /mcp/message?sessionId=<id>"""
    if request.method != "POST":
        return HttpResponse(status=405)

    raw_sid = request.GET.get("sessionId", "")
    # Guard against a missing or empty sessionId query-param so logs never show session=
    session_id = raw_sid if raw_sid else "anon-" + uuid.uuid4().hex[:8]
    active_threads = threading.active_count()
    add_attrs([
        ("mcp.session_id", session_id),
        ("mcp.thread_active_count", active_threads),
        ("mcp.request_size_bytes", len(request.body)),
    ])
    _add_mcp_client_attrs(request)
    _add_session_protocol_attrs(request)

    with _sessions_lock:
        session_entry = _sessions.get(session_id)

    if session_entry is None:
        logger.warning("mcp_message: no active SSE session: session_id=%s", session_id)
        add_attrs([("mcp.sse_session_missing", True)])
        # Metric + event: makes cross-worker routing failures (session on worker A,
        # mcp_message arriving at worker B) quantifiable in dashboards.
        record_metric("Custom/MCP/sse_session_missing_count", 1)
        record_event("MCPSessionMissing", {
            "session_id": session_id,
            "env": SERVER_ENV,
            "server_version": SERVER_VERSION,
        })
        return JsonResponse({"error": "No active MCP session."}, status=400)

    msg_queue, credentials = session_entry

    # Stamp the session-level trace anchor on every mcp_message transaction so
    # you can find all transactions in a session with:
    #   SELECT * FROM Transaction WHERE mcp.session_trace_id = '<session_trace_id>'
    # even though each request has its own trace.id.
    with _session_stats_lock:
        _msg_stats = _session_stats.get(session_id) or {}
    _session_trace_id = _msg_stats.get("session_trace_id", "")
    if _session_trace_id:
        add_attrs([("mcp.session_trace_id", _session_trace_id)])

    try:
        body = json.loads(request.body)
    except json.JSONDecodeError:
        logger.warning("mcp_message invalid JSON: session=%s", session_id)
        return JsonResponse({"error": "Invalid JSON"}, status=400)

    # ── Session-level tool sequence tracking ─────────────────────────────────
    # For tools/call messages, record the sequence number within this session
    # so conversations can be replayed in exact order.
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
        response_msg = _dispatch(body, credentials, request, session_id)

        # Track tool errors at session level for MCPSessionSummary
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
            # Enqueue as (enqueue_timestamp, message) so event_stream() can measure
            # how long the message waited before being consumed (queue wait time).
            # block=True, timeout=30 s: wait briefly for the client to drain before
            # declaring overflow.  Dropping a tool response would confuse the AI client,
            # so we prefer a short wait over silent loss.
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
            # Queue depth: a growing queue means the client isn't draining the SSE
            # stream fast enough.  Alert when this stays consistently > 5.
            queue_depth = msg_queue.qsize()
            add_attrs([("mcp.session_queue_depth", queue_depth)])
            newrelic.agent.record_custom_metric("Custom/MCP/session_queue_depth", queue_depth)

        return JsonResponse({"ok": True})
    except Exception as exc:
        logger.error("mcp_message transport error: session=%s", session_id, exc_info=True)
        notice_err(exc, [("error.layer", "transport")])
        raise
