# Responsibility: HTTP view handlers for OAuth 2.0 PKCE flow and session-based auth.
import base64
from datetime import timedelta
import hashlib
import json
import logging
import secrets
from typing import Optional
from urllib.parse import urlencode

from django.conf import settings
from django.db import transaction
from django.http import HttpRequest, HttpResponse, JsonResponse
from django.shortcuts import redirect, render
from django.utils import timezone
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_http_methods
import newrelic.agent
import requests

from mcp_app.nr_utils import add_attrs, notice_err, record_metric, set_txn_name
from mcp_app.protocol.auth import build_unauthorized_response, resolve_credentials

from .models import OAuthClient, OAuthCode, OAuthToken
from .services import (
    check_origin,
    check_session_ttl,
    get_session_credentials,
    is_registrable_redirect_uri,
    parse_oauth_token_body,
    redirect_uris_match,
    set_session_credentials,
    validate_cds_credentials,
)

logger = logging.getLogger(__name__)


# ── OAuth discovery ───────────────────────────────────────────────────────────

def oauth_protected_resource(request: HttpRequest, resource_path: str = "") -> JsonResponse:
    """Serve the OAuth 2.0 protected-resource metadata document (RFC 9728)."""
    base_url = settings.BASE_URL.rstrip("/")
    return JsonResponse({
        "resource": f"{base_url}/mcp",
        "authorization_servers": [base_url],
    })


def oauth_server_metadata(request: HttpRequest) -> JsonResponse:
    """Serve the OAuth 2.0 / OpenID Connect authorization server metadata document."""
    base_url = settings.BASE_URL.rstrip("/")
    return JsonResponse({
        "issuer": base_url,
        "authorization_endpoint": f"{base_url}/authorize",
        "token_endpoint": f"{base_url}/token",
        "revocation_endpoint": f"{base_url}/revoke",
        "registration_endpoint": f"{base_url}/register",
        "userinfo_endpoint": f"{base_url}/userinfo",
        "response_types_supported": ["code"],
        "grant_types_supported": ["authorization_code", "refresh_token"],
        "code_challenge_methods_supported": ["S256"],
        "token_endpoint_auth_methods_supported": ["none"],
        "revocation_endpoint_auth_methods_supported": ["none"],
        "scopes_supported": ["read", "write"],
    })


# ── Dynamic client registration ───────────────────────────────────────────────

@csrf_exempt
@require_http_methods(["POST"])
@newrelic.agent.function_trace(name="oauth_register", group="Auth")
def oauth_register(request: HttpRequest) -> JsonResponse:
    """Register a new OAuth 2.0 client dynamically (one redirect_uri per client)."""
    set_txn_name("Auth/oauth_register", group="Auth")

    origin_err: Optional[JsonResponse] = check_origin(request)
    if origin_err:
        return origin_err

    try:
        body = json.loads(request.body)
    except json.JSONDecodeError:
        body = {}

    # Accept either redirect_uris (list, legacy) or redirect_uri (string).
    raw_uris = body.get("redirect_uris", [])
    if isinstance(raw_uris, list) and raw_uris:
        redirect_uri: str = raw_uris[0]
    else:
        redirect_uri = str(body.get("redirect_uri", "")).strip()

    if redirect_uri and not is_registrable_redirect_uri(redirect_uri):
        add_attrs([("auth.flow", "oauth_register"), ("auth.result", "failure")])
        logger.warning("OAuth register: insecure redirect_uri=%s", redirect_uri)
        return JsonResponse(
            {
                "error": "invalid_redirect_uri",
                "error_description": "redirect_uri must use https:// or be a loopback address (http://localhost or http://127.0.0.1)",
            },
            status=400,
        )

    # os.urandom under the hood
    client_id: str = secrets.token_urlsafe(24)

    try:
        OAuthClient.objects.create(
            client_id=client_id,
            redirect_uri=redirect_uri,
        )
        add_attrs([
            ("auth.flow", "oauth_register"),
            ("auth.client_id", client_id),
            ("auth.result", "success"),
        ])
        record_metric("Custom/Auth/client_registered_count", 1)
        logger.info("OAuth client registered: client_id=%s redirect_uri=%s", client_id, redirect_uri)
        return JsonResponse({
            "client_id": client_id,
            "client_id_issued_at": int(timezone.now().timestamp()),
            "redirect_uris": [redirect_uri] if redirect_uri else [],
        }, status=201)
    except Exception as exc:
        add_attrs([("auth.flow", "oauth_register"), ("auth.result", "failure")])
        notice_err(exc, [("error.layer", "auth")])
        logger.error("OAuth client registration failed", exc_info=True)
        raise


# ── Authorization endpoint ────────────────────────────────────────────────────

_IMPLICIT_RESPONSE_TYPES = frozenset({"token", "id_token token", "code token", "code id_token token"})


def _oauth_authorize_error(
    request: HttpRequest,
    *,
    error: str,
    description: str,
    status: int = 400,
) -> HttpResponse:
    """Return an OAuth error for browser-based authorize requests."""
    if request.method == "GET":
        return render(request, "authorize.html", {
            "client_id": request.GET.get("client_id", ""),
            "redirect_uri": request.GET.get("redirect_uri", ""),
            "state": request.GET.get("state", ""),
            "code_challenge": request.GET.get("code_challenge", ""),
            "code_challenge_method": request.GET.get("code_challenge_method", "S256"),
            "error": description,
        }, status=status)
    return JsonResponse({"error": error, "error_description": description}, status=status)


def _validate_authorize_request(
    client_id: Optional[str],
    redirect_uri: str,
    response_type: str,
) -> Optional[tuple[str, str]]:
    """Return (error, description) when the authorize request is invalid; else None."""
    if response_type in _IMPLICIT_RESPONSE_TYPES:
        return "unsupported_response_type", "Implicit grant is not supported"
    if response_type != "code":
        return "unsupported_response_type", "Only response_type=code is supported"

    if not client_id:
        return "invalid_request", "client_id is required"
    try:
        oauth_client = OAuthClient.objects.get(client_id=client_id)
    except OAuthClient.DoesNotExist:
        # Clients like Claude Desktop cache their client_id and may present one
        # this server has no record of (e.g. after a DB wipe, or an ID minted by
        # the client platform itself). Auto-registering here is security-equivalent
        # to the open /register endpoint: PKCE still binds the auth code, the
        # redirect URI must pass the same https/loopback rules, and the user
        # still has to submit valid Publive credentials.
        if not redirect_uri or not is_registrable_redirect_uri(redirect_uri):
            return "invalid_client", "Unknown client_id and no acceptable redirect_uri to auto-register."
        oauth_client = OAuthClient.objects.create(client_id=client_id, redirect_uri=redirect_uri)
        logger.info(
            "OAuth authorize: auto-registered unknown client_id=%s redirect_uri=%s",
            client_id, redirect_uri,
        )

    registered_uri: str = oauth_client.redirect_uri or ""
    if not registered_uri:
        return "invalid_request", "Client has no registered redirect URI"
    if not redirect_uri:
        return "invalid_request", "redirect_uri is required"
    if not redirect_uris_match(redirect_uri, registered_uri):
        return "invalid_request", "redirect_uri does not match the registered value"

    return None


@require_http_methods(["GET", "POST"])
@newrelic.agent.function_trace(name="oauth_authorize", group="Auth")
def oauth_authorize(request: HttpRequest) -> HttpResponse:
    """Show the authorization form (GET) or process credential submission and issue an auth code (POST)."""
    if request.method == "GET":
        response_type: str = request.GET.get("response_type", "code")
        client_id: str = request.GET.get("client_id", "")
        redirect_uri: str = request.GET.get("redirect_uri", "")
        auth_err = _validate_authorize_request(client_id or None, redirect_uri, response_type)
        if auth_err:
            error_code, description = auth_err
            return _oauth_authorize_error(
                request, error=error_code, description=description,
            )
        return render(request, "authorize.html", {
            "client_id": client_id,
            "redirect_uri": redirect_uri,
            "state": request.GET.get("state", ""),
            "code_challenge": request.GET.get("code_challenge", ""),
            "code_challenge_method": request.GET.get("code_challenge_method", "S256"),
        })

    set_txn_name("Auth/pkce_authorize", group="Auth")
    client_id: Optional[str] = request.POST.get("client_id")
    add_attrs([
        ("auth.flow", "oauth_pkce"),
        ("auth.client_id", client_id),
    ])

    publisher_id: str = request.POST.get("publisherId", "").strip()
    api_key: str = request.POST.get("apiKey", "").strip()
    api_secret: str = request.POST.get("apiSecret", "").strip()
    redirect_uri: str = request.POST.get("redirect_uri", "")
    state: str = request.POST.get("state", "")
    code_challenge: str = request.POST.get("code_challenge", "")
    code_challenge_method: str = request.POST.get("code_challenge_method", "S256")

    ctx: dict = {
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "state": state,
        "code_challenge": code_challenge,
        "code_challenge_method": code_challenge_method,
        "publisherId": publisher_id,
        "apiKey": api_key,
    }

    try:
        auth_err = _validate_authorize_request(client_id, redirect_uri, "code")
        if auth_err:
            error_code, description = auth_err
            add_attrs([
                ("auth.result", "failure"),
                ("auth.failure_reason", error_code),
            ])
            ctx["error"] = description
            return render(request, "authorize.html", ctx)

        if not all([publisher_id, api_key, api_secret]):
            add_attrs([("auth.result", "failure"), ("auth.failure_reason", "missing_params")])
            record_metric("Custom/Auth/auth_failure_count", 1)
            logger.warning("OAuth authorize: missing params client=%s", client_id)
            ctx["error"] = "All fields are required."
            return render(request, "authorize.html", ctx)

        try:
            ok, status_code = validate_cds_credentials(publisher_id, api_key, api_secret)
        except requests.RequestException as exc:
            add_attrs([("auth.result", "failure"), ("auth.failure_reason", "cds_auth_failed")])
            record_metric("Custom/Auth/auth_failure_count", 1)
            logger.error(
                "OAuth authorize: CDS unreachable publisher=%s client=%s",
                publisher_id, client_id, exc_info=True,
            )
            ctx["error"] = f"Could not reach Publive API: {exc}"
            return render(request, "authorize.html", ctx)

        if not ok:
            add_attrs([("auth.result", "failure"), ("auth.failure_reason", "cds_auth_failed")])
            record_metric("Custom/Auth/auth_failure_count", 1)
            logger.warning(
                "OAuth authorize: invalid CDS credentials publisher=%s client=%s status=%d",
                publisher_id, client_id, status_code,
            )
            ctx["error"] = f"Invalid credentials (HTTP {status_code}). Check your Publisher ID, API Key, and API Secret."
            return render(request, "authorize.html", ctx)

        code: str = secrets.token_urlsafe(32)
        OAuthCode.objects.create(
            code=code,
            client_id=client_id,
            redirect_uri=redirect_uri,
            #  code_challenge = base64url(SHA256(code_verifier))
            code_challenge=code_challenge,
            credentials={"publisherId": publisher_id, "apiKey": api_key, "apiSecret": api_secret},
            expires_at=timezone.now() + timedelta(minutes=10),
        )

        add_attrs([
            ("auth.result", "success"),
            ("auth.publisher_id", publisher_id),
            ("auth.client_id", client_id),
        ])
        logger.info("OAuth authorize: success publisher=%s client=%s", publisher_id, client_id)
        return redirect(f"{redirect_uri}?{urlencode({'code': code, 'state': state})}")
    except Exception as exc:
        notice_err(exc, [("error.layer", "auth")])
        logger.error("OAuth authorize: unhandled error client=%s", client_id, exc_info=True)
        raise


# ── Token endpoint ────────────────────────────────────────────────────────────

@csrf_exempt
@require_http_methods(["POST"])
@newrelic.agent.function_trace(name="oauth_token", group="Auth")
def oauth_token(request: HttpRequest) -> JsonResponse:
    """Issue or refresh a bearer token; supports authorization_code and refresh_token grants."""
    set_txn_name("Auth/pkce_token", group="Auth")

    data, parse_err = parse_oauth_token_body(request)
    if parse_err:
        add_attrs([("auth.result", "failure"), ("auth.failure_reason", "missing_params")])
        logger.warning("OAuth token: invalid request body content-type=%s", request.content_type)
        return parse_err

    origin_err: Optional[JsonResponse] = check_origin(request)
    if origin_err:
        return origin_err

    grant_type: str = data.get("grant_type", "")
    if grant_type == "implicit":
        add_attrs([("auth.result", "failure"), ("auth.failure_reason", "unsupported_grant")])
        record_metric("Custom/Auth/auth_failure_count", 1)
        logger.warning("OAuth token: rejected implicit grant")
        return JsonResponse(
            {"error": "unsupported_grant_type", "error_description": "Implicit grant is not supported"},
            status=400,
        )
    add_attrs([
        ("auth.flow", "oauth_pkce"),
        ("auth.client_id", data.get("client_id")),
        ("auth.grant_type", grant_type),
    ])

    try:
        # ── Refresh token grant ───────────────────────────────────────────────
        if grant_type == "refresh_token":
            refresh_val: str = data.get("refresh_token", "")
            new_refresh: str = secrets.token_urlsafe(32)

            with transaction.atomic():
                try:
                    existing = (
                        OAuthToken.objects.select_for_update()
                        .get(refresh_token=refresh_val)
                    )
                except OAuthToken.DoesNotExist:
                    add_attrs([("auth.result", "failure"), ("auth.failure_reason", "expired_token")])
                    record_metric("Custom/Auth/auth_failure_count", 1)
                    logger.warning("OAuth token: unknown refresh_token")
                    return JsonResponse(
                        {"error": "invalid_grant", "error_description": "Unknown refresh token"},
                        status=400,
                    )

                # Atomic rotation: old refresh token is replaced before the response is sent.
                access_token = existing.token
                client_id = existing.client_id
                publisher_id = existing.publisher_id
                existing.refresh_token = new_refresh
                existing.save(update_fields=["refresh_token"])

            add_attrs([
                ("auth.result", "success"),
                ("auth.token_reused", True),
                ("auth.publisher_id", publisher_id),
                ("auth.client_id", client_id),
            ])
            record_metric("Custom/Auth/token_refresh_count", 1)
            logger.info("OAuth token refreshed: publisher=%s client=%s", publisher_id, client_id)
            return JsonResponse({
                "access_token": access_token,
                "token_type": "bearer",
                "refresh_token": new_refresh,
            })

        # ── Authorization code grant ──────────────────────────────────────────
        if grant_type != "authorization_code":
            add_attrs([("auth.result", "failure"), ("auth.failure_reason", "missing_params")])
            record_metric("Custom/Auth/auth_failure_count", 1)
            logger.warning("OAuth token: unsupported grant_type=%s client=%s", grant_type, data.get("client_id"))
            return JsonResponse({"error": "unsupported_grant_type"}, status=400)

        code: str = data.get("code", "")
        code_verifier: str = data.get("code_verifier", "")

        try:
            auth_code = OAuthCode.objects.get(code=code)
        except OAuthCode.DoesNotExist:
            add_attrs([("auth.result", "failure"), ("auth.failure_reason", "missing_params")])
            record_metric("Custom/Auth/auth_failure_count", 1)
            logger.warning("OAuth token: unknown code client=%s", data.get("client_id"))
            return JsonResponse({"error": "invalid_grant", "error_description": "Unknown code"}, status=400)

        if auth_code.expires_at < timezone.now():
            auth_code.delete()
            add_attrs([("auth.result", "failure"), ("auth.failure_reason", "expired_token")])
            record_metric("Custom/Auth/auth_failure_count", 1)
            logger.warning("OAuth token: expired code client=%s", data.get("client_id"))
            return JsonResponse({"error": "invalid_grant", "error_description": "Code expired"}, status=400)

        expected: str = base64.urlsafe_b64encode(
            hashlib.sha256(code_verifier.encode()).digest()
        ).rstrip(b"=").decode()
        if expected != auth_code.code_challenge:
            add_attrs([("auth.result", "failure"), ("auth.failure_reason", "invalid_pkce")])
            record_metric("Custom/Auth/auth_failure_count", 1)
            logger.warning("OAuth token: PKCE verification failed client=%s", data.get("client_id"))
            return JsonResponse({"error": "invalid_grant", "error_description": "PKCE verification failed"}, status=400)

        token_redirect_uri: str = data.get("redirect_uri", "")
        if auth_code.redirect_uri and token_redirect_uri != auth_code.redirect_uri:
            add_attrs([("auth.result", "failure"), ("auth.failure_reason", "invalid_redirect_uri")])
            record_metric("Custom/Auth/auth_failure_count", 1)
            logger.warning("OAuth token: redirect_uri mismatch client=%s", data.get("client_id"))
            return JsonResponse(
                {"error": "invalid_grant", "error_description": "redirect_uri mismatch"},
                status=400,
            )

        credentials: dict    = auth_code.credentials
        oauth_client_id: str = data.get("client_id") or auth_code.client_id
        publisher_id         = credentials.get("publisherId", "")
        auth_code.delete()

        # Upsert: return the existing valid token for this client+publisher so
        # the AI client keeps the same stable token identity across re-authorisations.
        existing = OAuthToken.objects.filter(
            client_id=oauth_client_id,
            publisher_id=publisher_id,
        ).first()

        if existing:
            # Backfill refresh_token for tokens created before the refresh_token migration.
            if not existing.refresh_token:
                existing.refresh_token = secrets.token_urlsafe(32)
                existing.save(update_fields=["refresh_token"])
            add_attrs([
                ("auth.result", "success"),
                ("auth.token_reused", True),
                ("auth.publisher_id", publisher_id),
                ("auth.client_id", oauth_client_id),
            ])
            logger.info(
                "OAuth token reused (stable): publisher=%s client=%s",
                publisher_id, oauth_client_id,
            )
            return JsonResponse({
                "access_token": existing.token,
                "token_type": "bearer",
                "refresh_token": existing.refresh_token,
            })

        token: str = secrets.token_urlsafe(32)
        new_refresh: str = secrets.token_urlsafe(32)
        token_credentials = {k: v for k, v in credentials.items() if k != "publisherId"}
        OAuthToken.objects.create(
            token=token,
            client_id=oauth_client_id,
            publisher_id=publisher_id,
            refresh_token=new_refresh,
            credentials=token_credentials,
        )

        add_attrs([
            ("auth.result", "success"),
            ("auth.token_reused", False),
            ("auth.publisher_id", publisher_id),
            ("auth.client_id", oauth_client_id),
        ])
        record_metric("Custom/Auth/token_issued_count", 1)
        logger.info("OAuth token issued (new): publisher=%s client=%s", publisher_id, oauth_client_id)
        return JsonResponse({
            "access_token": token,
            "token_type": "bearer",
            "refresh_token": new_refresh,
        })
    except Exception as exc:
        notice_err(exc, [("error.layer", "auth")])
        logger.error("OAuth token: unhandled error", exc_info=True)
        raise


# ── Session-based auth (browser users) ───────────────────────────────────────

def connect(request: HttpRequest) -> HttpResponse:
    """Render the browser-based session login page."""
    return render(request, "connect.html")


def auth_success(request: HttpRequest) -> HttpResponse:
    """Render the post-login success page; redirect to /connect if session is missing."""
    if not get_session_credentials(request.session):
        return redirect("/connect")
    return render(request, "success.html")


@csrf_exempt
@require_http_methods(["POST"])
@newrelic.agent.function_trace(name="auth_login", group="Auth")
def auth_login(request: HttpRequest) -> JsonResponse:
    """Validate Publive credentials via CDS, create a session, and set the user-chosen TTL."""
    set_txn_name("Auth/session_login", group="Auth")
    add_attrs([("auth.flow", "session")])

    try:
        try:
            body = json.loads(request.body)
        except json.JSONDecodeError:
            add_attrs([("auth.result", "failure"), ("auth.failure_reason", "missing_params")])
            logger.warning("auth_login: invalid JSON body")
            return JsonResponse({"error": "Invalid request body."}, status=400)

        publisher_id: str = str(body.get("publisherId", "")).strip()
        api_key: str      = str(body.get("apiKey", "")).strip()
        api_secret: str   = str(body.get("apiSecret", "")).strip()

        if not all([publisher_id, api_key, api_secret]):
            add_attrs([("auth.result", "failure"), ("auth.failure_reason", "missing_params")])
            record_metric("Custom/Auth/auth_failure_count", 1)
            logger.warning("auth_login: missing params publisher=%s", publisher_id)
            return JsonResponse({"error": "All fields are required."}, status=400)

        try:
            ok, status_code = validate_cds_credentials(publisher_id, api_key, api_secret)
        except requests.RequestException as exc:
            add_attrs([("auth.result", "failure"), ("auth.failure_reason", "cds_auth_failed")])
            record_metric("Custom/Auth/auth_failure_count", 1)
            logger.error("auth_login: CDS unreachable publisher=%s", publisher_id, exc_info=True)
            return JsonResponse({"error": f"Could not reach Publive API: {exc}"}, status=500)

        if ok:
            now_ts: int = int(timezone.now().timestamp())   # Unix epoch — avoids fromisoformat py39 bug
            now_iso: str = timezone.now().isoformat()
            set_session_credentials(request.session, {
                "publisherId": publisher_id,
                "apiKey": api_key,
                "apiSecret": api_secret,
            })
            request.session["authenticatedAt"] = now_iso
            request.session["session_created_at"] = now_ts   # authoritative clock for TTL check (int epoch)
            request.session["session_ttl_seconds"] = -1      # never expires — only /auth/logout ends the session

            # Far-future absolute expiry so Django keeps the session alive until
            # the user explicitly disconnects via /auth/logout.
            request.session.set_expiry(10 * 365 * 24 * 3600)

            add_attrs([
                ("auth.result", "success"),
                ("auth.publisher_id", publisher_id),
            ])
            record_metric("Custom/Auth/session_login_count", 1)
            logger.info("auth_login: success publisher=%s", publisher_id)
            return JsonResponse({"success": True, "redirectTo": "/auth/success"})

        add_attrs([("auth.result", "failure"), ("auth.failure_reason", "cds_auth_failed")])
        record_metric("Custom/Auth/auth_failure_count", 1)
        logger.warning("auth_login: invalid credentials publisher=%s status=%d", publisher_id, status_code)
        if status_code in (401, 403):
            return JsonResponse({"error": "Invalid credentials."}, status=401)
        return JsonResponse({"error": f"HTTP {status_code}"}, status=500)
    except Exception as exc:
        notice_err(exc, [("error.layer", "auth")])
        logger.error("auth_login: unhandled error", exc_info=True)
        raise


@newrelic.agent.function_trace(name="auth_status", group="Auth")
def auth_status(request: HttpRequest) -> JsonResponse:
    """Return the current session state including publisher ID, login time, and TTL."""
    set_txn_name("Auth/session_verify", group="Auth")
    newrelic.agent.suppress_apdex_metric()
    newrelic.agent.suppress_transaction_trace()
    add_attrs([("auth.flow", "session")])

    try:
        credentials = get_session_credentials(request.session)
        if credentials:
            # Enforce server-side absolute TTL — catches sessions that Django's
            # cookie TTL would miss (e.g. SESSION_SAVE_EVERY_REQUEST disabled).
            if check_session_ttl(request.session):
                request.session.flush()
                add_attrs([("auth.result", "failure"), ("auth.failure_reason", "SESSION_EXPIRED")])
                return JsonResponse({"authenticated": False, "error": "SESSION_EXPIRED"})

            add_attrs([
                ("auth.result", "success"),
                ("auth.publisher_id", credentials.get("publisherId")),
            ])
            ttl_seconds: int = request.session.get("session_ttl_seconds", -1)
            if ttl_seconds == -1:
                expires_in: Optional[int] = None          # never
            elif ttl_seconds == 0:
                expires_in = None                          # browser-controlled
            else:
                import time as _time
                created_at_ts = request.session.get("session_created_at", 0)
                try:
                    deadline_ts = int(created_at_ts) + int(ttl_seconds)
                    expires_in = max(0, int(deadline_ts - _time.time()))
                except (ValueError, TypeError):
                    expires_in = request.session.get_expiry_age()

            return JsonResponse({
                "authenticated": True,
                "publisherId": credentials.get("publisherId"),
                "authenticatedAt": request.session.get("authenticatedAt"),
                "session_expires_in_seconds": expires_in,
            })

        add_attrs([("auth.result", "failure"), ("auth.failure_reason", "invalid_session")])
        return JsonResponse({"authenticated": False})
    except Exception as exc:
        notice_err(exc, [("error.layer", "auth")])
        logger.error("auth_status: unhandled error", exc_info=True)
        raise


@csrf_exempt
@require_http_methods(["POST"])
@newrelic.agent.function_trace(name="auth_logout", group="Auth")
def auth_logout(request: HttpRequest) -> JsonResponse:
    """Flush the current session and log the publisher out."""
    set_txn_name("Auth/session_logout", group="Auth")
    creds = get_session_credentials(request.session)
    publisher_id: str = (creds or {}).get("publisherId", "unknown")
    request.session.flush()
    add_attrs([
        ("auth.flow", "session"),
        ("auth.result", "success"),
        ("auth.publisher_id", publisher_id),
    ])
    record_metric("Custom/Auth/session_logout_count", 1)
    logger.info("auth_logout: publisher=%s", publisher_id)
    return JsonResponse({"success": True})


# ── Token revocation (RFC 7009) ───────────────────────────────────────────────

@csrf_exempt
@require_http_methods(["POST"])
@newrelic.agent.function_trace(name="oauth_revoke", group="Auth")
def oauth_revoke(request: HttpRequest) -> JsonResponse:
    """Revoke a bearer access token or refresh token (RFC 7009).

    Always returns HTTP 200 — per spec, the server must not reveal whether
    the token existed. Both access tokens and refresh tokens are accepted.
    """
    set_txn_name("Auth/oauth_revoke", group="Auth")
    add_attrs([("auth.flow", "oauth_revoke")])

    data, parse_err = parse_oauth_token_body(request)
    if parse_err:
        # Still return 200 per RFC 7009 § 2.2 — invalid requests are silently ignored
        return JsonResponse({})

    token_value: str = data.get("token", "").strip()
    hint: str = data.get("token_type_hint", "").strip()

    if not token_value:
        return JsonResponse({})

    revoked = False
    try:
        if hint == "refresh_token":
            # Try refresh token first when hint is given
            deleted, _ = OAuthToken.objects.filter(refresh_token=token_value).delete()
            if not deleted:
                deleted, _ = OAuthToken.objects.filter(token=token_value).delete()
            revoked = bool(deleted)
        else:
            # Default: treat as access token; fall back to refresh token
            deleted, _ = OAuthToken.objects.filter(token=token_value).delete()
            if not deleted:
                deleted, _ = OAuthToken.objects.filter(refresh_token=token_value).delete()
            revoked = bool(deleted)
    except Exception as exc:
        notice_err(exc, [("error.layer", "auth")])
        logger.error("oauth_revoke: unexpected error", exc_info=True)
        # Still return 200 per spec
        return JsonResponse({})

    add_attrs([("auth.result", "success"), ("auth.token_revoked", revoked)])
    record_metric("Custom/Auth/token_revoked_count", 1 if revoked else 0)
    if revoked:
        logger.info("oauth_revoke: token revoked hint=%s", hint or "access_token")
    else:
        logger.info("oauth_revoke: token not found (already expired or invalid) hint=%s", hint or "access_token")

    return JsonResponse({})


# ── UserInfo (OIDC-style identity claims) ─────────────────────────────────────

@newrelic.agent.function_trace(name="oauth_userinfo", group="Auth")
def oauth_userinfo(request: HttpRequest) -> JsonResponse:
    """Return identity claims for the caller resolved from their Bearer token or session.

    Mirrors the OpenID Connect UserInfo endpoint so any OAuth-aware MCP client can
    discover "who am I" via standard discovery (advertised as userinfo_endpoint in
    oauth_server_metadata) instead of guessing from tool results. `sub` is the
    stable subject identifier — here, the delegated Publive publisher ID, since
    Publive credentials are issued per-publisher rather than per individual user.
    """
    set_txn_name("Auth/userinfo", group="Auth")
    add_attrs([("auth.flow", "userinfo")])

    credentials, _, error_code = resolve_credentials(request)
    if not credentials:
        add_attrs([("auth.result", "failure"), ("auth.failure_reason", error_code or "unauthenticated")])
        return build_unauthorized_response(request, error_code)

    publisher_id: str = credentials.get("publisherId", "")
    add_attrs([("auth.result", "success"), ("auth.publisher_id", publisher_id)])
    return JsonResponse({
        "sub": publisher_id,
        "publisher_id": publisher_id,
    })

