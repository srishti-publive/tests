"""Streamable HTTP transport for MCP (stateless POST /mcp)."""
import json
import logging
import threading

from django.http import HttpResponse, JsonResponse
import newrelic.agent

from mcp_app.nr_utils import add_attrs, notice_err
from mcp_app.protocol.dispatch import dispatch_jsonrpc
from mcp_app.protocol.session import SESSION_PROTOCOL_KEY, derive_session_id

logger = logging.getLogger(__name__)


def _add_session_protocol_attrs(request) -> None:
    if request is None:
        return
    protocol_version = request.session.get(SESSION_PROTOCOL_KEY)
    if protocol_version:
        add_attrs([("mcp.protocol_version", protocol_version)])


@newrelic.agent.function_trace(name="handle_http_request", group="Transport")
def handle_http_request(request, credentials: dict, token_expires_at) -> HttpResponse:
    """Process a single stateless POST /mcp request (Streamable HTTP transport)."""
    request_size   = len(request.body)
    active_threads = threading.active_count()
    session_id     = derive_session_id(request)

    add_attrs([
        ("mcp.transport",           "http"),
        ("mcp.session_id",          session_id),
        ("mcp.thread_active_count", active_threads),
        ("mcp.request_size_bytes",  request_size),
    ])
    _add_session_protocol_attrs(request)
    newrelic.agent.record_custom_metric("Custom/MCP/active_threads", active_threads)

    try:
        body = json.loads(request.body)
    except json.JSONDecodeError:
        logger.warning("handle_http_request: invalid JSON: size=%d", request_size)
        return JsonResponse({"error": "Invalid JSON"}, status=400)

    try:
        if isinstance(body, list):
            logger.debug("MCP batch request: count=%d session=%s", len(body), session_id)
            responses = [
                r for r in (
                    dispatch_jsonrpc(msg, credentials, request, session_id, token_expires_at)
                    for msg in body
                )
                if r is not None
            ]
            return JsonResponse(responses, safe=False) if responses else HttpResponse(status=202)

        response = dispatch_jsonrpc(body, credentials, request, session_id, token_expires_at)
        if response is None:
            return HttpResponse(status=202)
        return JsonResponse(response)

    except Exception as exc:
        logger.error("handle_http_request transport error: session=%s", session_id, exc_info=True)
        notice_err(exc, [("error.layer", "transport")])
        raise
