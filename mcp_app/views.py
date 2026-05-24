import json
import queue
import threading
import time
import uuid

import newrelic.agent
from django.conf import settings
from django.http import HttpResponse, StreamingHttpResponse, JsonResponse
from django.utils import timezone
from django.views.decorators.csrf import csrf_exempt

from .tools import TOOLS, call_tool
from .langfuse_tracing import start_mcp_trace, record_tool_call

# session_id → (Queue, credentials)  (shared across threads; single gunicorn worker required)
_sessions: dict[str, tuple[queue.Queue, dict]] = {}
_sessions_lock = threading.Lock()

_PROTOCOL_VERSION = "2024-11-05"


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


# ── JSON-RPC helpers ──────────────────────────────────────────────────────────

def _ok(id_, result):
    return {"jsonrpc": "2.0", "id": id_, "result": result}


def _err(id_, code, message):
    return {"jsonrpc": "2.0", "id": id_, "error": {"code": code, "message": message}}


def _dispatch(body, credentials):
    method = body.get("method", "")
    id_    = body.get("id")

    if id_ is None:
        return None  # notification — no response

    if method == "initialize":
        publisher_id = (credentials or {}).get("publisherId", "unknown")
        start_mcp_trace(publisher_id)
        return _ok(id_, {
            "protocolVersion": _PROTOCOL_VERSION,
            "capabilities": {"tools": {}},
            "serverInfo": {"name": "publive-cds", "version": "1.0.0"},
        })

    if method == "tools/list":
        return _ok(id_, {"tools": TOOLS})

    if method == "tools/call":
        params       = body.get("params", {})
        name         = params.get("name", "")
        args         = dict(params.get("arguments") or {})
        publisher_id = (credentials or {}).get("publisherId", "unknown")

        newrelic.agent.add_custom_attributes([
            ("publisher_id", publisher_id),
            ("tool_name", name),
        ])

        t0 = time.perf_counter()
        try:
            result      = call_tool(credentials, name, args)
            duration_ms = round((time.perf_counter() - t0) * 1000, 2)
            record_tool_call(publisher_id, name, args, result, None, duration_ms)
            return _ok(id_, {"content": [{"type": "text", "text": json.dumps(result, indent=2)}]})
        except Exception as exc:
            duration_ms = round((time.perf_counter() - t0) * 1000, 2)
            record_tool_call(publisher_id, name, args, None, str(exc), duration_ms)
            return _ok(id_, {"content": [{"type": "text", "text": f"Error: {exc}"}], "isError": True})

    if method == "ping":
        return _ok(id_, {})

    return _err(id_, -32601, f"Method not found: {method}")


# ── Views ─────────────────────────────────────────────────────────────────────

@csrf_exempt
def mcp_endpoint(request):
    credentials = _get_credentials(request)
    if not credentials:
        return _unauth(request)

    if request.method == "GET":
        # Legacy SSE transport
        session_id = str(uuid.uuid4())
        msg_queue: queue.Queue = queue.Queue()
        with _sessions_lock:
            _sessions[session_id] = (msg_queue, credentials)

        base_url = getattr(settings, "BASE_URL", "http://localhost:8000").rstrip("/")
        post_url = f"{base_url}/mcp/message?sessionId={session_id}"

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
                with _sessions_lock:
                    _sessions.pop(session_id, None)

        resp = StreamingHttpResponse(event_stream(), content_type="text/event-stream")
        resp["Cache-Control"]     = "no-cache"
        resp["X-Accel-Buffering"] = "no"
        return resp

    if request.method == "POST":
        # Streamable HTTP transport (MCP 2025-11-25)
        try:
            body = json.loads(request.body)
        except json.JSONDecodeError:
            return JsonResponse({"error": "Invalid JSON"}, status=400)

        if isinstance(body, list):
            responses = [r for r in (_dispatch(msg, credentials) for msg in body) if r is not None]
            return JsonResponse(responses, safe=False) if responses else HttpResponse(status=202)

        response = _dispatch(body, credentials)
        if response is None:
            return HttpResponse(status=202)
        return JsonResponse(response)

    return HttpResponse(status=405)


@csrf_exempt
def mcp_message(request):
    """Legacy SSE transport: POST /mcp/message?sessionId=<id>"""
    if request.method != "POST":
        return HttpResponse(status=405)

    session_id = request.GET.get("sessionId", "")
    with _sessions_lock:
        session_entry = _sessions.get(session_id)

    if session_entry is None:
        return JsonResponse({"error": "No active MCP session."}, status=400)

    msg_queue, credentials = session_entry

    try:
        body = json.loads(request.body)
    except json.JSONDecodeError:
        return JsonResponse({"error": "Invalid JSON"}, status=400)

    response_msg = _dispatch(body, credentials)
    if response_msg is not None:
        msg_queue.put(response_msg)

    return JsonResponse({"ok": True})
