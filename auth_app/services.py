# Responsibility: Pure business-logic helpers for OAuth auth flows — origin validation,
# redirect-URI allowlisting, and CDS credential verification. No HTTP routing here.
import base64
import json
import logging
import time
from typing import Any, Optional
from urllib.parse import parse_qs

import newrelic.agent
import requests
from django.conf import settings
from django.http import HttpRequest, JsonResponse

from mcp_app.nr_utils import add_attrs

logger = logging.getLogger(__name__)


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
        "https://claude.ai",
        "https://api.claude.ai",
    ]))
    allowed.add(settings.BASE_URL.rstrip("/"))  # always allow same-origin

    if origin in allowed:
        return None

    logger.warning("OAuth: blocked request from disallowed origin=%r", origin)
    return JsonResponse(
        {"error": "invalid_origin", "error_description": "Origin not allowed"},
        status=403,
    )


def get_allowed_redirect_uris() -> set[str]:
    """Return the exact redirect URIs permitted at dynamic client registration."""
    uris: list[str] = list(getattr(settings, "OAUTH_ALLOWED_REDIRECT_URIS", []))
    return set(uris)


def validate_redirect_uris(uris: list[str]) -> bool:
    """Return True only when every redirect URI exactly matches an allowed URI."""
    if not uris:
        return True
    allowed = get_allowed_redirect_uris()
    return all(uri in allowed for uri in uris)


def redirect_uri_is_registered(redirect_uri: str, registered_uris: list[str]) -> bool:
    """Return True when redirect_uri exactly matches a client-registered URI."""
    return redirect_uri in registered_uris


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
    resp = requests.get(
        f"https://cds-beta.thepublive.com/publisher/{publisher_id}/publisher-data/",
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
    return resp.status_code not in (401, 403), resp.status_code
