"""WSGI entry points — thin routing layer only.

Each view delegates immediately to the appropriate protocol or transport module.
No business logic lives here.
"""
import logging

import newrelic.agent
from django.http import HttpResponse, JsonResponse
from django.views.decorators.csrf import csrf_exempt

from .nr_utils           import suppress_apdex, suppress_trace
from .protocol.auth      import build_unauthorized_response, identify_mcp_client, resolve_credentials
from .protocol.dispatch  import PROTOCOL_VERSION
from .transport.http     import handle_http_request
from .transport.sse      import handle_sse_message, open_sse_connection

logger = logging.getLogger(__name__)


@newrelic.agent.function_trace(name="health_check", group="Transport")
def health_check(request):
    """Root health-check endpoint — returns service identity and liveness."""
    suppress_apdex()
    suppress_trace()
    return JsonResponse({
        "status":   "ok",
        "service":  "publive-cds-mcp",
        "version":  "1.0.0",
        "protocol": PROTOCOL_VERSION,
    })


@csrf_exempt
@newrelic.agent.function_trace(name="mcp_endpoint", group="Transport")
def mcp_endpoint(request):
    """MCP protocol entry point — authenticates then routes to the correct transport."""
    has_bearer = request.META.get("HTTP_AUTHORIZATION", "").startswith("Bearer ")
    client_name, _ = identify_mcp_client(request)
    logger.info(
        "MCP request: method=%s has_bearer=%s ua=%s",
        request.method,
        has_bearer,
        request.META.get("HTTP_USER_AGENT", "unknown")[:80],
    )

    credentials, token_expires_at, error_code = resolve_credentials(request)
    if error_code or not credentials:
        logger.warning(
            "MCP authentication failed: method=%s has_bearer=%s error_code=%s",
            request.method, has_bearer, error_code,
        )
        return build_unauthorized_response(request, error_code=error_code)

    logger.info("MCP authenticated: method=%s", request.method)

    if request.method == "GET":
        return open_sse_connection(request, credentials, token_expires_at)

    if request.method == "POST":
        return handle_http_request(request, credentials, token_expires_at)

    return HttpResponse(status=405)


@csrf_exempt
@newrelic.agent.function_trace(name="mcp_message", group="Transport")
def mcp_message(request):
    """Legacy SSE transport: POST /mcp/message?sessionId=<id>"""
    return handle_sse_message(request)
