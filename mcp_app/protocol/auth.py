"""Credential resolution and unauthorized response helpers for MCP requests."""
import hashlib
import logging
import re

from django.conf import settings
from django.http import JsonResponse
from django.utils import timezone

logger = logging.getLogger(__name__)

_CLIENT_NAME_MAP: dict[str, str] = {
    "claude":          "Claude Desktop",
    "cursor":          "Cursor",
    "anthropic":       "Anthropic SDK",
    "python-requests": "Python Requests Client",
    "python-httpx":    "Python HTTPX Client",
    "mcp":             "MCP Python SDK",
    "node":            "Node.js MCP Client",
    "go-http-client":  "Go MCP Client",
    "axios":           "Axios (JS)",
    "openai":          "OpenAI SDK",
}


def resolve_credentials(request):
    """Return (credentials_dict, token_expires_at) from Bearer token or session cookie.

    Bearer lookup takes priority.  Returns (None, None) when no valid auth is found.
    Raises on unexpected DB errors rather than silently swallowing them.
    """
    auth_header = request.META.get("HTTP_AUTHORIZATION", "")
    if auth_header.startswith("Bearer "):
        token_value = auth_header[len("Bearer "):].strip()
        try:
            from auth_app.models import OAuthToken
            oauth_token = OAuthToken.objects.get(token=token_value)
            if oauth_token.expires_at >= timezone.now():
                return oauth_token.credentials, oauth_token.expires_at
        except Exception as exc:  # noqa: BLE001
            # ImportError / OAuthToken.DoesNotExist are expected; anything else is not.
            from auth_app.models import OAuthToken as _OT  # re-import for isinstance check
            if not isinstance(exc, _OT.DoesNotExist):
                logger.error("resolve_credentials: unexpected token lookup failure", exc_info=True)
                raise
    return request.session.get("credentials"), None


def build_unauthorized_response(request):
    """Return a 401 JSON response with the RFC 6750 WWW-Authenticate challenge."""
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


def identify_mcp_client(request) -> tuple[str, str]:
    """Return (client_name, client_version) parsed from the User-Agent header."""
    ua    = request.META.get("HTTP_USER_AGENT", "unknown")
    name  = "unknown"
    ver   = "unknown"
    match = re.match(r"^([^\s/]+)/([^\s]+)", ua)
    if match:
        raw  = match.group(1).lower()
        ver  = match.group(2)
        name = _CLIENT_NAME_MAP.get(raw, match.group(1))
    elif ua and ua != "unknown":
        raw  = (ua.split()[0] if ua.split() else ua).lower()
        name = _CLIENT_NAME_MAP.get(raw, ua.split()[0] if ua.split() else ua)
    return name, ver
