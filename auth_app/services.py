# Responsibility: Pure business-logic helpers for OAuth auth flows — origin validation,
# redirect-URI allowlisting, and CDS credential verification. No HTTP routing here.
import logging
import time
from typing import Optional

import newrelic.agent
import requests
from django.conf import settings
from django.http import HttpRequest, JsonResponse

from mcp_app.nr_utils import add_attrs
from mcp_app.utils import CDS_BASE_URL, make_basic_token

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


def validate_redirect_uris(uris: list[str]) -> bool:
    """Return True only when every redirect URI starts with an allowed prefix.

    The allowlist is read from settings.OAUTH_ALLOWED_REDIRECT_PREFIXES so it
    can be extended without a code change.
    """
    prefixes: list[str] = getattr(settings, "OAUTH_ALLOWED_REDIRECT_PREFIXES", [
        "https://claude.ai/",
        "http://localhost:",
        "http://127.0.0.1:",
    ])
    return all(
        any(uri.startswith(p) for p in prefixes)
        for uri in uris
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
    token: str = make_basic_token(api_key, api_secret)
    t0: float = time.perf_counter()
    resp = requests.get(
        CDS_BASE_URL.format(publisher_id=publisher_id) + "/publisher-data/",
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
