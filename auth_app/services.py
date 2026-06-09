# Responsibility: Pure business-logic helpers for OAuth auth flows — origin validation,
# redirect-URI allowlisting, CDS credential verification, session TTL checks,
# and session credential storage.

import base64
import json
import logging
import time
from typing import Any, Optional
from urllib.parse import parse_qs, urlsplit

from django.conf import settings
from django.http import HttpRequest, JsonResponse
import newrelic.agent
import requests

from mcp_app.nr_utils import add_attrs

logger = logging.getLogger(__name__)


def get_session_credentials(session) -> Optional[dict]:
    """Return the credentials dict stored in the session, or None if absent."""
    raw = session.get("credentials")
    if isinstance(raw, dict):
        return raw
    return None


def set_session_credentials(session, credentials: dict) -> None:
    """Store credentials in the session."""
    session["credentials"] = credentials


def check_session_ttl(session) -> bool:
    """Return True if the session has exceeded its original TTL.


    Django's rolling SESSION_SAVE_EVERY_REQUEST is intentionally disabled so
    these stored values are the authoritative expiry source.
    """
    ttl_seconds = session.get("session_ttl_seconds", -1)
    if ttl_seconds <= 0: 
        return False
    created_at_ts = session.get("session_created_at")
    if not created_at_ts:
        return False
    try:
        import time as _time
        deadline_ts = int(created_at_ts) + int(ttl_seconds)
        return _time.time() > deadline_ts
    except (ValueError, TypeError):
        return False


def check_origin(request: HttpRequest) -> Optional[JsonResponse]:
    """Return None if the Origin header is acceptable; return a 403 JsonResponse otherwise.

    Desktop MCP clients (Claude Desktop, Cursor) do not send an Origin header because
    they are not browsers — those are unconditionally allowed. When an Origin IS present
    (web-based Claude clients), it must appear in settings.OAUTH_ALLOWED_ORIGINS.
    """
    origin: str = request.META.get("HTTP_ORIGIN", "").rstrip("/")
    if not origin:
        return None

    allowed: set[str] = set(getattr(settings, "OAUTH_ALLOWED_ORIGINS", [
        # Anthropic / Claude (web)
        "https://claude.ai",
        "https://api.claude.ai",
        # OpenAI / ChatGPT (web)
        "https://chatgpt.com",
        "https://chat.openai.com",
        "https://platform.openai.com",
        # Google Gemini (web)
        "https://gemini.google.com",
        "https://aistudio.google.com",
        # Microsoft Copilot (web)
        "https://copilot.microsoft.com",
        "https://www.bing.com",
    ]))
    allowed.add(settings.BASE_URL.rstrip("/"))  # always allow same-origin

    if origin in allowed:
        return None

    logger.warning("OAuth: blocked request from disallowed origin=%r", origin)
    return JsonResponse(
        {"error": "invalid_origin", "error_description": "Origin not allowed"},
        status=403,
    )


_LOOPBACK_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})


def is_loopback_redirect_uri(uri: str) -> bool:
    """Return True for http://localhost:<port>/... or http://127.0.0.1:<port>/... URIs.

    Native/desktop OAuth clients (RFC 8252 §7.3) bind an ephemeral local port at
    launch and can't be allowlisted by exact string match — the authorization
    server must accept any port for these.
    """
    try:
        parts = urlsplit(uri)
    except ValueError:
        return False
    return parts.scheme == "http" and parts.hostname in _LOOPBACK_HOSTS


def is_registrable_redirect_uri(uri: str) -> bool:
    """Return True for redirect URIs acceptable at dynamic client registration.

    Per RFC 7591 / OAuth 2.1, registration is open to any client — the server
    doesn't pre-approve specific apps by URL. The only requirement is transport
    security: either HTTPS (web/mobile callbacks) or a loopback address (native
    apps per RFC 8252 §7.3, which can't use HTTPS for an ephemeral local port).
    Plain http:// to a non-loopback host is rejected as it would leak the
    authorization code over an insecure channel.
    """
    try:
        parts = urlsplit(uri)
    except ValueError:
        return False
    if parts.scheme == "https" and parts.hostname:
        return True
    return is_loopback_redirect_uri(uri)


def redirect_uris_match(requested: str, registered: str) -> bool:
    """Return True when redirect URIs match exactly, or both are loopback URIs
    that differ only by port.

    RFC 8252 §7.3: the authorization server MUST allow any port to be specified
    at request time for loopback redirect URIs, since native apps obtain an
    ephemeral port from the OS when they start listening.
    """
    if requested == registered:
        return True
    if not (is_loopback_redirect_uri(requested) and is_loopback_redirect_uri(registered)):
        return False
    req, reg = urlsplit(requested), urlsplit(registered)
    return (req.scheme, req.hostname, req.path) == (reg.scheme, reg.hostname, reg.path)


def parse_oauth_token_body(request: HttpRequest) -> tuple[Optional[dict[str, Any]], Optional[JsonResponse]]:
    """Parse /oauth/token request body from JSON or application/x-www-form-urlencoded."""
    content_type = (request.content_type or "").split(";", 1)[0].strip().lower()

    if content_type == "application/json":
        try:
            body = json.loads(request.body)
        except json.JSONDecodeError:
            return None, JsonResponse({"error": "invalid_request"}, status=400)
        if not isinstance(body, dict):
            return None, JsonResponse({"error": "invalid_request"}, status=400)
        return body, None

    if content_type == "application/x-www-form-urlencoded":
        if request.POST:
            return request.POST.dict(), None
        try:
            parsed = parse_qs(request.body.decode("utf-8"), keep_blank_values=True)
        except UnicodeDecodeError:
            return None, JsonResponse({"error": "invalid_request"}, status=400)
        return {key: values[0] if values else "" for key, values in parsed.items()}, None

    return None, JsonResponse(
        {
            "error": "invalid_request",
            "error_description": "Content-Type must be application/x-www-form-urlencoded or application/json",
        },
        status=400,
    )


@newrelic.agent.function_trace(name="validate_cds_auth", group="Auth")
def validate_cds_credentials(
    publisher_id: str,
    api_key: str,
    api_secret: str,
) -> tuple[bool, int]:
    """Call the Publive CDS API to verify credentials; return (is_valid, http_status).
    Raises requests.RequestException if the CDS is unreachable — callers must handle it.
    Records latency and HTTP status as New Relic custom attributes on the current transaction.
    """
    token: str = base64.b64encode(f"{api_key}:{api_secret}".encode()).decode()
    t0: float = time.perf_counter()
    # Validate against a documented, always-present CDS endpoint. `/posts/?limit=1`
    # is the smallest authenticated GET that exists for every publisher. 
    # Use the same env-configurable base URL as the CDS client (CDS_BASE_URL).
    base = settings.CDS_BASE_URL.format(publisher_id=publisher_id)
    resp = requests.get(
        f"{base}/posts/",
        params={"limit": 1},
        headers={"Authorization": f"Basic {token}"},
        timeout=10,
    )
    latency_ms: float = round((time.perf_counter() - t0) * 1000, 2)
    add_attrs([
        ("auth.cds_validation_status", resp.status_code),
        ("auth.cds_validation_ms", latency_ms),
    ])
    logger.info(
        "CDS validation: publisher=%s status=%d latency_ms=%.2f",
        publisher_id, resp.status_code, latency_ms,
    )
    # Only a 2xx means the credentials are genuinely valid. 
    return 200 <= resp.status_code < 300, resp.status_code
