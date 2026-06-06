# Responsibility: Pure business-logic helpers for OAuth auth flows — origin validation,
# redirect-URI allowlisting, CDS credential verification, session TTL checks,
# and session credential encryption/decryption.
import base64
import json
import logging
import time
from typing import Any, Optional
from urllib.parse import parse_qs

from django.conf import settings
from django.http import HttpRequest, JsonResponse
import newrelic.agent
import requests

from mcp_app.nr_utils import add_attrs

logger = logging.getLogger(__name__)


def get_session_credentials(session) -> Optional[dict]:
    """Decrypt and return the credentials dict stored in the session.

    Handles both the legacy format (plain dict, stored before Fix D was deployed)
    and the new format (Fernet-encrypted string). Returns None if absent or corrupted.
    """
    raw = session.get("credentials")
    if raw is None:
        return None
    if isinstance(raw, dict):
        # Legacy session — plaintext dict written before encryption was enabled
        return raw
    if isinstance(raw, str):
        try:
            from .crypto import InvalidToken, decrypt_json
            return decrypt_json(raw)
        except (InvalidToken, json.JSONDecodeError):
            logger.warning("get_session_credentials: failed to decrypt session credentials — treating as expired")
            return None
    return None


def set_session_credentials(session, credentials: dict) -> None:
    """Encrypt and store credentials in the session."""
    from .crypto import encrypt_json
    session["credentials"] = encrypt_json(credentials)


def check_session_ttl(session) -> bool:
    """Return True if the session has exceeded its original TTL.

    ttl_seconds == -1  → "Always" session, never expires.
    ttl_seconds == 0   → browser-session; expiry is browser-controlled.
    ttl_seconds  > 0   → absolute deadline from session_created_at.

    session_created_at is stored as a Unix integer timestamp (int(timezone.now().timestamp()))
    to avoid Python 3.9 fromisoformat limitations with timezone-aware ISO strings.

    Django's rolling SESSION_SAVE_EVERY_REQUEST is intentionally disabled so
    these stored values are the authoritative expiry source.
    """
    ttl_seconds = session.get("session_ttl_seconds", -1)
    if ttl_seconds <= 0:  # always (-1) or browser-session (0) → not expired here
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
    # Validate against a documented, always-present CDS endpoint. `/posts/?limit=1`
    # is the smallest authenticated GET that exists for every publisher. The old
    # `/publisher-data/` path is not a real route (the API answers it with
    # 400 "Unknown Endpoint Path"), so it could never return a 2xx.
    resp = requests.get(
        f"https://cds.thepublive.com/publisher/{publisher_id}/posts/",
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
    # Only a 2xx means the credentials are genuinely valid. The previous check
    # (status_code not in (401, 403)) wrongly treated 400 and 5xx as success,
    # letting bad credentials through to authorization-code issuance.
    return 200 <= resp.status_code < 300, resp.status_code
