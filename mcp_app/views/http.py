import logging

from django.http import JsonResponse
import newrelic.agent

from mcp_app.transport.http import handle_http_request

logger = logging.getLogger(__name__)


def http_mcp(request, credentials: dict):
    """POST /mcp — stateless Streamable HTTP transport. Called by the router after auth."""
    content_type = request.META.get("CONTENT_TYPE", "")
    if "application/json" not in content_type:
        logger.warning(
            "MCP POST rejected: invalid Content-Type=%r ua=%s",
            content_type,
            request.META.get("HTTP_USER_AGENT", "unknown")[:80],
        )
        return JsonResponse(
            {
                "error": "unsupported_media_type",
                "error_description": "Content-Type must be application/json",
            },
            status=415,
        )
    return handle_http_request(request, credentials)
