"""Credential resolution and unauthorized-response helpers for MCP requests.

resolve_credentials() returns a 3-tuple:
    (credentials_dict | None, token_expires_at | None, error_code | None)

error_code is one of the typed reason codes below — never a generic string.
"""
import hashlib
import logging
import re
from datetime import datetime, timedelta
from typing import Optional

from django.conf import settings
from django.http import JsonResponse
from django.utils import timezone

logger = logging.getLogger(__name__)

# Typed 401 reason codes — every 401 response carries exactly one of these.
SESSION_EXPIRED     = "SESSION_EXPIRED"
CLIENT_BLOCKED      = "CLIENT_BLOCKED"
INVALID_CLIENT_ID   = "INVALID_CLIENT_ID"

_ERROR_DESCRIPTIONS: dict[str, str] = {
    SESSION_EXPIRED:   "Your session has expired. Please log in again.",
    CLIENT_BLOCKED:    "This AI client has been blocked by an administrator. Contact the server admin.",
    INVALID_CLIENT_ID: "Unknown client ID. Please re-register at /ai/register.",
}

# UUID v4 format — used to route bearer tokens to the AIClient table instead
# of the OAuthToken table.  OAuthTokens are secrets.token_urlsafe(32) strings
# and will never match this pattern.
_UUID_V4_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$",
    re.IGNORECASE,
)

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

    Routing:
    • Bearer UUID v4  →  AIClient table  →  CLIENT_BLOCKED | INVALID_CLIENT_ID | credentials
    • Bearer other    →  OAuthToken table (existing PKCE flow)
    • No Bearer       →  Django session with server-side TTL check  →  SESSION_EXPIRED | credentials

    The last_seen_at update for AIClients happens here — exactly one place.
    """
    auth_header: str = request.META.get("HTTP_AUTHORIZATION", "")
    if auth_header.startswith("Bearer "):
        token_value: str = auth_header[len("Bearer "):].strip()
        if _UUID_V4_RE.match(token_value):
            return _resolve_ai_client(token_value)
        return _resolve_oauth_token(token_value)

    return _resolve_session(request)


# ── Internal resolvers ────────────────────────────────────────────────────────

def _resolve_oauth_token(token_value: str):
    """Resolve an OAuthToken bearer (existing PKCE flow).

    Expired or unknown tokens fall through to None so callers can
    fall back to session auth — preserving pre-existing behaviour.
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


def _resolve_ai_client(client_id_str: str):
    """Resolve an AIClient bearer (UUID v4 direct-registration flow).

    Unlike OAuthTokens, AIClient lookups are DEFINITIVE:
    • Not found  →  INVALID_CLIENT_ID  (no session fallback)
    • Blocked    →  CLIENT_BLOCKED     (no session fallback)
    • Active     →  stored credentials (may be None if registered without them)

    last_seen_at is updated here — the single authoritative place.
    """
    try:
        from auth_app.models import AIClient
        ai_client = AIClient.objects.get(client_id=client_id_str)

        # Update last_seen_at on every successful lookup — single place.
        ai_client.last_seen_at = timezone.now()
        ai_client.save(update_fields=["last_seen_at"])

        if ai_client.status == AIClient.STATUS_BLOCKED:
            return None, None, CLIENT_BLOCKED

        return ai_client.credentials, None, None

    except Exception as exc:  # noqa: BLE001
        from auth_app.models import AIClient as _AC
        if not isinstance(exc, _AC.DoesNotExist):
            logger.error("resolve_credentials: unexpected AIClient lookup failure", exc_info=True)
            raise
    return None, None, INVALID_CLIENT_ID


def _resolve_session(request):
    """Resolve session credentials with server-side absolute TTL enforcement.

    Relies on session_created_at + session_ttl_seconds stored at login time.
    SESSION_SAVE_EVERY_REQUEST is disabled so these values are never silently
    rolled forward — a "90 day" session expires 90 days after login, not after
    the last request.
    """
    credentials = request.session.get("credentials")
    if not credentials:
        return None, None, None

    from auth_app.services import check_session_ttl
    if check_session_ttl(request.session):
        # flush() exists on real Django session backends; guard for test dicts.
        if hasattr(request.session, "flush"):
            request.session.flush()
        return None, None, SESSION_EXPIRED

    return credentials, None, None


# ── Response helpers ──────────────────────────────────────────────────────────

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
