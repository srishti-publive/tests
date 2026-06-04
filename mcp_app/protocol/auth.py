"""Credential resolution and unauthorized-response helpers for MCP requests.

resolve_credentials() returns a 3-tuple:
    (credentials_dict | None, token_expires_at | None, error_code | None)

error_code is one of the typed reason codes below — never a generic string.
"""
import logging
import re
from typing import Optional

from django.conf import settings
from django.http import JsonResponse
from django.utils import timezone

logger = logging.getLogger(__name__)

# Typed 401 reason codes.
SESSION_EXPIRED = "SESSION_EXPIRED"

_ERROR_DESCRIPTIONS: dict[str, str] = {
    SESSION_EXPIRED: "Your session has expired. Please log in again.",
}

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
    """Return (credentials_dict, token_expires_at, error_code) from Bearer token or session.

    Bearer present  →  OAuthToken table (PKCE flow)
    No Bearer       →  Django session with server-side TTL check → SESSION_EXPIRED | credentials
    """
    auth_header: str = request.META.get("HTTP_AUTHORIZATION", "")
    if auth_header.startswith("Bearer "):
        token_value: str = auth_header[len("Bearer "):].strip()
        return _resolve_oauth_token(token_value)

    return _resolve_session(request)


def _resolve_oauth_token(token_value: str):
    """Resolve an OAuthToken bearer (PKCE flow).

    Expired or unknown tokens return (None, None, None) — no session fallback.
    """
    try:
        from auth_app.models import OAuthToken
        oauth_token = OAuthToken.objects.get(token=token_value)
        if oauth_token.expires_at >= timezone.now():
            return oauth_token.credentials, oauth_token.expires_at, None
    except Exception as exc:  # noqa: BLE001
        from auth_app.models import OAuthToken as _OT
        if not isinstance(exc, _OT.DoesNotExist):
            logger.error("resolve_credentials: unexpected OAuthToken lookup failure", exc_info=True)
            raise
    return None, None, None


def _resolve_session(request):
    """Resolve session credentials with server-side absolute TTL enforcement."""
    credentials = request.session.get("credentials")
    if not credentials:
        return None, None, None

    from auth_app.services import check_session_ttl
    if check_session_ttl(request.session):
        if hasattr(request.session, "flush"):
            request.session.flush()
        return None, None, SESSION_EXPIRED

    return credentials, None, None


def build_unauthorized_response(request, error_code: Optional[str] = None) -> JsonResponse:
    """Return a 401 JSON response with RFC 6750 WWW-Authenticate and a typed reason code."""
    base_url: str = getattr(settings, "BASE_URL", "http://localhost:8000").rstrip("/")

    body: dict = {"authUrl": f"{base_url}/connect"}
    if error_code and error_code in _ERROR_DESCRIPTIONS:
        body["error"] = error_code
        body["error_description"] = _ERROR_DESCRIPTIONS[error_code]
    else:
        body["error"] = "Not authenticated"

    resp = JsonResponse(body, status=401)
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
