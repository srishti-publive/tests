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

from .nr_utils import add_attrs, notice_err, record_event, set_txn_name
from .prompt_capture import extract_prompt_for_tool_call, record_prompt_observability
from .tools import TOOLS, call_tool

logger = logging.getLogger(__name__)

# session_id → (Queue, credentials)  (shared across threads; single gunicorn worker required)
_sessions: dict[str, tuple[queue.Queue, dict]] = {}
_sessions_lock = threading.Lock()

_PROTOCOL_VERSION = "2024-11-05"
_SESSION_PROTOCOL_KEY = "mcp_protocol_version"

# MCPPrompt event sampling: emit at most this many events per minute per process.
# Prevents hitting NR's 3000 custom-events/min limit under heavy load (50 threads).
_PROMPT_EVENT_MAX_PER_MIN = 1000
_prompt_event_count = 0
_prompt_event_window_start = time.monotonic()
_prompt_event_lock = threading.Lock()

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


def _get_credentials(request):
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
    3. Empty string fallback (should not happen after auth passes)
    """
    key = getattr(request.session, "session_key", None)
    if key:
        return key

    auth_header = request.META.get("HTTP_AUTHORIZATION", "")
    if auth_header.startswith("Bearer "):
        token = auth_header[len("Bearer "):]
        return "oauth-" + hashlib.sha256(token.encode()).hexdigest()[:16]

    return ""


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
    client_name = ua
    client_version = "unknown"
    match = re.match(r"^([^\s/]+)/([^\s]+)", ua)
    if match:
        client_name = match.group(1)
        client_version = match.group(2)
    elif ua and ua != "unknown":
        client_name = ua.split()[0] if ua.split() else ua
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
            # Budget exceeded: still set transaction attrs, skip the custom event
            add_attrs([
                ("mcp.prompt_id", prompt_id),
                ("mcp.prompt_text", prompt_text),
                ("mcp.prompt_source", prompt_source),
                ("mcp.session_id", session_id or ""),
                ("mcp.tool_name", name),
            ])
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

        t0 = time.perf_counter()
        try:
            result = call_tool(credentials, name, args)
            duration_ms = round((time.perf_counter() - t0) * 1000, 2)
            set_txn_name(f"MCP/{name}", group="MCP")
            output_text = json.dumps(result, indent=2) if result else ""
            result_size = len(output_text)
            add_attrs([
                ("mcp.tool_result_status", "success"),
                ("mcp.tool_is_error", False),
                ("mcp.tool_args_count", len(args) if args else 0),
                ("mcp.tool_response_size", result_size),
                ("mcp.tool_duration_ms", duration_ms),
                ("mcp.tool_output_preview", output_text[:500]),
            ])
            # Custom metric for per-tool latency and throughput (SLO-ready)
            newrelic.agent.record_custom_metric(f"Custom/Tool/{name}/duration_ms", duration_ms)
            newrelic.agent.record_custom_metric("Custom/MCP/tool_call_count", 1)
            logger.info(
                "MCP tools/call success: tool=%s duration_ms=%.2f response_size=%d",
                name, duration_ms, result_size,
            )
            return _ok(id_, {"content": [{"type": "text", "text": output_text}]})
        except Exception as exc:
            duration_ms = round((time.perf_counter() - t0) * 1000, 2)
            set_txn_name(f"MCP/{name}", group="MCP")
            add_attrs([
                ("mcp.tool_result_status", "error"),
                ("mcp.tool_is_error", True),
                ("mcp.tool_args_count", len(args) if args else 0),
                ("mcp.tool_response_size", 0),
                ("mcp.tool_duration_ms", duration_ms),
                ("mcp.tool_output_preview", str(exc)[:500]),
            ])
            newrelic.agent.record_custom_metric("Custom/MCP/tool_error_count", 1)
            record_event("MCPToolError", {
                "tool_name": name,
                "publisher_id": (credentials or {}).get("publisherId", "unknown"),
                "error_type": type(exc).__name__,
                "error_message": str(exc)[:500],
                "session_id": session_id or "",
                "prompt_id": prompt_id,
                "prompt_text": prompt_text[:500],
                "duration_ms": duration_ms,
                "tool_input": tool_input,
            })
            logger.error(
                "MCP tools/call error: tool=%s session=%s error=%s duration_ms=%.2f",
                name, session_id, exc, duration_ms, exc_info=True,
            )
            return _ok(id_, {"content": [{"type": "text", "text": f"Error: {exc}"}], "isError": True})

    if method == "ping":
        set_txn_name("MCP/ping", group="MCP")
        logger.debug("MCP ping: session=%s", session_id)
        return _ok(id_, {})

    # Unknown method — record as observable event so bad clients are visible
    logger.warning("MCP unknown method: method=%s session=%s jsonrpc_id=%s", method, session_id, id_)
    add_attrs([
        ("mcp.jsonrpc_error_code", -32601),
        ("mcp.unknown_method", method),
    ])
    record_event("MCPUnknownMethod", {
        "method": method,
        "session_id": session_id or "",
        "jsonrpc_id": str(id_) if id_ is not None else "",
    })
    return _err(id_, -32601, f"Method not found: {method}")


# ── Views ─────────────────────────────────────────────────────────────────────

@csrf_exempt
@newrelic.agent.function_trace(name="mcp_endpoint", group="Transport")
def mcp_endpoint(request):
    credentials = _get_credentials(request)
    if not credentials:
        logger.warning("MCP unauthenticated request: method=%s", request.method)
        return _unauth(request)

    _add_mcp_client_attrs(request)

    if request.method == "GET":
        # Legacy SSE transport
        session_id = str(uuid.uuid4())
        set_txn_name("Transport/SSE", group="Transport")
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
        # SSESessionOpen does NOT need a current_transaction() guard —
        # record_event() works at application level without an active transaction.
        record_event("SSESessionOpen", {
            "session_id": session_id,
            "publisher_id": publisher_id,
            "active_threads": active_threads,
        })

        msg_queue: queue.Queue = queue.Queue()
        with _sessions_lock:
            _sessions[session_id] = (msg_queue, credentials)

        base_url = getattr(settings, "BASE_URL", "http://localhost:8000").rstrip("/")
        post_url = f"{base_url}/mcp/message?sessionId={session_id}"
        stream_t0 = time.perf_counter()

        def event_stream():
            yield f"event: endpoint\ndata: {post_url}\n\n"
            try:
                while True:
                    try:
                        msg = msg_queue.get(timeout=25)
                        if msg is None:
                            break
                        yield f"event: message\ndata: {json.dumps(msg)}\n\n"
                    except queue.Empty:
                        yield ": keepalive\n\n"
            finally:
                duration_ms = round((time.perf_counter() - stream_t0) * 1000, 2)
                with _sessions_lock:
                    _sessions.pop(session_id, None)
                # CRITICAL FIX: use record_event() (no current_transaction() guard)
                # so this fires even after the WSGI transaction context has shifted.
                record_event("SSESessionClose", {
                    "session_id": session_id,
                    "publisher_id": publisher_id,
                    "duration_ms": duration_ms,
                })
                logger.info(
                    "SSE session close: session=%s publisher=%s duration_ms=%.2f",
                    session_id, publisher_id, duration_ms,
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

    session_id = request.GET.get("sessionId", "")
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
        return JsonResponse({"error": "No active MCP session."}, status=400)

    msg_queue, credentials = session_entry

    try:
        body = json.loads(request.body)
    except json.JSONDecodeError:
        logger.warning("mcp_message invalid JSON: session=%s", session_id)
        return JsonResponse({"error": "Invalid JSON"}, status=400)

    try:
        response_msg = _dispatch(body, credentials, request, session_id)
        if response_msg is not None:
            msg_queue.put(response_msg)
        return JsonResponse({"ok": True})
    except Exception as exc:
        logger.error("mcp_message transport error: session=%s", session_id, exc_info=True)
        notice_err(exc, [("error.layer", "transport")])
        raise
