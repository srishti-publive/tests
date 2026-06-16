"""MCP view router — authenticates every request then delegates to the right transport view."""
import logging

from django.http import HttpResponse
from django.views.decorators.csrf import csrf_exempt
import newrelic.agent

from mcp_app.protocol.auth import build_unauthorized_response, identify_mcp_client, resolve_credentials
from mcp_app.views.http import http_mcp

logger = logging.getLogger(__name__)


@csrf_exempt
@newrelic.agent.function_trace(name="mcp_endpoint", group="Transport")
def mcp_endpoint(request):
    """Single entry point for POST /mcp (Streamable HTTP).
    Resolves credentials once, then hands off to the transport-specific view."""
    has_bearer = request.META.get("HTTP_AUTHORIZATION", "").startswith("Bearer ")
    identify_mcp_client(request)
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

    if request.method == "POST":
        return http_mcp(request, credentials, token_expires_at)

    return HttpResponse(status=405)
