import base64
import hashlib
import json
import logging
import secrets
import time
from datetime import timedelta
from urllib.parse import urlencode

import newrelic.agent
import requests
from django.conf import settings
from django.http import JsonResponse
from django.shortcuts import render, redirect
from django.utils import timezone
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_http_methods

from mcp_app.nr_utils import add_attrs, notice_err, record_metric, set_txn_name

from .models import OAuthClient, OAuthCode, OAuthToken

logger = logging.getLogger(__name__)


# ── OAuth security helpers ────────────────────────────────────────────────────

def _check_origin(request):
    """Return None if the Origin is acceptable, or a 403 JsonResponse.

    Desktop MCP clients (Claude Desktop, Cursor) do not send an Origin header
    because they are not browsers. We allow those through unconditionally.
    When an Origin IS present (web-based Claude clients), it must be in the
    configured allowlist from settings.OAUTH_ALLOWED_ORIGINS.
    """
    origin = request.META.get("HTTP_ORIGIN", "").rstrip("/")
    if not origin:
        return None
    allowed = set(getattr(settings, "OAUTH_ALLOWED_ORIGINS", [
        "https://claude.ai",
        "https://api.claude.ai",
    ]))
    allowed.add(settings.BASE_URL.rstrip("/"))   # always allow same-origin
    if origin in allowed:
        return None
    logger.warning("OAuth: blocked request from disallowed origin=%r", origin)
    return JsonResponse(
        {"error": "invalid_origin", "error_description": "Origin not allowed"},
        status=403,
    )


def _validate_redirect_uris(uris):
    """Return True only when every redirect URI matches the configured prefix allowlist."""
    prefixes = getattr(settings, "OAUTH_ALLOWED_REDIRECT_PREFIXES", [
        "https://claude.ai/",
        "http://localhost:",
        "http://127.0.0.1:",
    ])
    for uri in uris:
        if not any(uri.startswith(p) for p in prefixes):
            return False
    return True


# CRITICAL FIX: _validate_cds now has a function trace and records latency + status
# so auth-path CDS calls are fully visible in APM traces.
@newrelic.agent.function_trace(name="validate_cds_auth", group="Auth")
def _validate_cds(publisher_id, api_key, api_secret):
    token = base64.b64encode(f"{api_key}:{api_secret}".encode()).decode()
    t0 = time.perf_counter()
    resp = requests.get(
        f"https://cds-beta.thepublive.com/publisher/{publisher_id}/publisher-data/",
        headers={"Authorization": f"Basic {token}"},
        timeout=10,
    )
    latency_ms = round((time.perf_counter() - t0) * 1000, 2)
    add_attrs([
        ("auth.cds_validation_status", resp.status_code),
        ("auth.cds_validation_ms", latency_ms),
    ])
    logger.info(
        "CDS validation: publisher=%s status=%d latency_ms=%.2f",
        publisher_id, resp.status_code, latency_ms,
    )
    return resp.status_code not in (401, 403), resp.status_code


# ── OAuth discovery ───────────────────────────────────────────────────────────

def oauth_protected_resource(request, resource_path=""):
    base_url = settings.BASE_URL.rstrip("/")
    return JsonResponse({
        "resource": f"{base_url}/mcp",
        "authorization_servers": [base_url],
    })


def oauth_server_metadata(request):
    base_url = settings.BASE_URL.rstrip("/")
    return JsonResponse({
        "issuer": base_url,
        "authorization_endpoint": f"{base_url}/oauth/authorize",
        "token_endpoint": f"{base_url}/oauth/token",
        "registration_endpoint": f"{base_url}/register",
        "response_types_supported": ["code"],
        "grant_types_supported": ["authorization_code", "refresh_token"],
        "code_challenge_methods_supported": ["S256"],
        "token_endpoint_auth_methods_supported": ["none"],
        "scopes_supported": ["read", "write"],
    })


# ── Dynamic client registration ───────────────────────────────────────────────

# HIGH FIX: oauth_register now has a function trace, transaction name, attrs and
# error handling so client self-registration is visible in APM.
@csrf_exempt
@require_http_methods(["POST"])
@newrelic.agent.function_trace(name="oauth_register", group="Auth")
def oauth_register(request):
    set_txn_name("Auth/oauth_register", group="Auth")

    origin_err = _check_origin(request)
    if origin_err:
        return origin_err

    try:
        body = json.loads(request.body)
    except Exception:
        body = {}

    redirect_uris = body.get("redirect_uris", [])
    if not isinstance(redirect_uris, list):
        redirect_uris = []

    # Reject registrations that include disallowed redirect URIs.
    if redirect_uris and not _validate_redirect_uris(redirect_uris):
        add_attrs([("auth.flow", "oauth_register"), ("auth.result", "failure")])
        logger.warning("OAuth register: disallowed redirect_uris=%s", redirect_uris)
        return JsonResponse(
            {"error": "invalid_redirect_uri", "error_description": "One or more redirect URIs are not allowed"},
            status=400,
        )

    client_id = secrets.token_urlsafe(24)
    client_lifetime = timedelta(days=90)

    try:
        # Opportunistic cleanup: delete expired clients so the table doesn't grow unbounded.
        deleted, _ = OAuthClient.objects.filter(expires_at__lt=timezone.now()).delete()
        if deleted:
            logger.info("OAuth register: pruned %d expired client(s)", deleted)

        OAuthClient.objects.create(
            client_id=client_id,
            redirect_uris=redirect_uris,
            expires_at=timezone.now() + client_lifetime,
        )
        add_attrs([
            ("auth.flow", "oauth_register"),
            ("auth.client_id", client_id),
            ("auth.redirect_uri_count", len(redirect_uris)),
            ("auth.result", "success"),
        ])
        record_metric("Custom/Auth/client_registered_count", 1)
        logger.info("OAuth client registered: client_id=%s redirect_uris=%d", client_id, len(redirect_uris))
        return JsonResponse({
            "client_id": client_id,
            "client_id_issued_at": int(timezone.now().timestamp()),
            "client_secret_expires_at": int((timezone.now() + client_lifetime).timestamp()),
            "redirect_uris": redirect_uris,
        }, status=201)
    except Exception as exc:
        add_attrs([
            ("auth.flow", "oauth_register"),
            ("auth.result", "failure"),
        ])
        notice_err(exc, [("error.layer", "auth")])
        logger.error("OAuth client registration failed", exc_info=True)
        raise


# ── Authorization endpoint ────────────────────────────────────────────────────

@require_http_methods(["GET", "POST"])
@newrelic.agent.function_trace(name="oauth_authorize", group="Auth")
def oauth_authorize(request):
    if request.method == "GET":
        return render(request, "authorize.html", {
            "client_id": request.GET.get("client_id", ""),
            "redirect_uri": request.GET.get("redirect_uri", ""),
            "state": request.GET.get("state", ""),
            "code_challenge": request.GET.get("code_challenge", ""),
            "code_challenge_method": request.GET.get("code_challenge_method", "S256"),
        })

    set_txn_name("Auth/pkce_authorize", group="Auth")
    client_id = request.POST.get("client_id") or request.GET.get("client_id")
    add_attrs([
        ("auth.flow", "oauth_pkce"),
        ("auth.client_id", client_id),
    ])

    publisher_id          = request.POST.get("publisherId", "").strip()
    api_key               = request.POST.get("apiKey", "").strip()
    api_secret            = request.POST.get("apiSecret", "").strip()
    redirect_uri          = request.POST.get("redirect_uri", "")
    state                 = request.POST.get("state", "")
    code_challenge        = request.POST.get("code_challenge", "")
    code_challenge_method = request.POST.get("code_challenge_method", "S256")

    ctx = {
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "state": state,
        "code_challenge": code_challenge,
        "code_challenge_method": code_challenge_method,
        "publisherId": publisher_id,
        "apiKey": api_key,
    }

    try:
        # Validate the client is registered and has not expired.
        try:
            oauth_client = OAuthClient.objects.get(client_id=client_id)
        except OAuthClient.DoesNotExist:
            add_attrs([("auth.result", "failure"), ("auth.failure_reason", "unknown_client")])
            ctx["error"] = "Unknown client. Please reconnect your AI client."
            return render(request, "authorize.html", ctx)
        if oauth_client.expires_at and oauth_client.expires_at < timezone.now():
            add_attrs([("auth.result", "failure"), ("auth.failure_reason", "client_expired")])
            ctx["error"] = "Client registration has expired. Please reconnect your AI client."
            return render(request, "authorize.html", ctx)

        if not all([publisher_id, api_key, api_secret]):
            add_attrs([
                ("auth.result", "failure"),
                ("auth.failure_reason", "missing_params"),
            ])
            record_metric("Custom/Auth/auth_failure_count", 1)
            logger.warning("OAuth authorize: missing params client=%s", client_id)
            ctx["error"] = "All fields are required."
            return render(request, "authorize.html", ctx)

        try:
            ok, status_code = _validate_cds(publisher_id, api_key, api_secret)
        except requests.RequestException as exc:
            add_attrs([
                ("auth.result", "failure"),
                ("auth.failure_reason", "cds_auth_failed"),
            ])
            record_metric("Custom/Auth/auth_failure_count", 1)
            logger.error(
                "OAuth authorize: CDS unreachable publisher=%s client=%s",
                publisher_id, client_id, exc_info=True,
            )
            ctx["error"] = f"Could not reach Publive API: {exc}"
            return render(request, "authorize.html", ctx)

        if not ok:
            add_attrs([
                ("auth.result", "failure"),
                ("auth.failure_reason", "cds_auth_failed"),
            ])
            record_metric("Custom/Auth/auth_failure_count", 1)
            logger.warning(
                "OAuth authorize: invalid CDS credentials publisher=%s client=%s status=%d",
                publisher_id, client_id, status_code,
            )
            ctx["error"] = f"Invalid credentials (HTTP {status_code}). Check your Publisher ID, API Key, and API Secret."
            return render(request, "authorize.html", ctx)

        code = secrets.token_urlsafe(32)
        OAuthCode.objects.create(
            code=code,
            client_id=client_id,
            redirect_uri=redirect_uri,
            code_challenge=code_challenge,
            credentials={"publisherId": publisher_id, "apiKey": api_key, "apiSecret": api_secret},
            expires_at=timezone.now() + timedelta(minutes=10),
        )

        add_attrs([
            ("auth.result", "success"),
            ("auth.publisher_id", publisher_id),
            ("auth.client_id", client_id),
        ])
        logger.info(
            "OAuth authorize: success publisher=%s client=%s", publisher_id, client_id
        )
        return redirect(f"{redirect_uri}?{urlencode({'code': code, 'state': state})}")
    except Exception as exc:
        notice_err(exc, [("error.layer", "auth")])
        logger.error("OAuth authorize: unhandled error client=%s", client_id, exc_info=True)
        raise


# ── Token endpoint ────────────────────────────────────────────────────────────

@csrf_exempt
@require_http_methods(["POST"])
@newrelic.agent.function_trace(name="oauth_token", group="Auth")
def oauth_token(request):
    set_txn_name("Auth/pkce_token", group="Auth")

    if "application/json" in (request.content_type or ""):
        try:
            data = json.loads(request.body)
        except json.JSONDecodeError:
            add_attrs([
                ("auth.result", "failure"),
                ("auth.failure_reason", "missing_params"),
            ])
            logger.warning("OAuth token: invalid JSON body")
            return JsonResponse({"error": "invalid_request"}, status=400)
    else:
        data = request.POST

    origin_err = _check_origin(request)
    if origin_err:
        return origin_err

    grant_type = data.get("grant_type", "")
    add_attrs([
        ("auth.flow", "oauth_pkce"),
        ("auth.client_id", data.get("client_id")),
        ("auth.grant_type", grant_type),
    ])

    try:
        # ── Refresh token grant ───────────────────────────────────────────────
        if grant_type == "refresh_token":
            refresh_val = data.get("refresh_token", "")
            try:
                existing = OAuthToken.objects.get(refresh_token=refresh_val)
            except OAuthToken.DoesNotExist:
                add_attrs([("auth.result", "failure"), ("auth.failure_reason", "expired_token")])
                record_metric("Custom/Auth/auth_failure_count", 1)
                logger.warning("OAuth token: unknown refresh_token")
                return JsonResponse({"error": "invalid_grant", "error_description": "Unknown refresh token"}, status=400)

            if existing.expires_at < timezone.now():
                existing.delete()
                add_attrs([("auth.result", "failure"), ("auth.failure_reason", "expired_token")])
                record_metric("Custom/Auth/auth_failure_count", 1)
                logger.warning("OAuth token: expired refresh_token client=%s", existing.client_id)
                return JsonResponse({"error": "invalid_grant", "error_description": "Refresh token expired"}, status=400)

            # Rotate refresh_token (security); keep the same access_token value (stable identity).
            new_refresh = secrets.token_urlsafe(32)
            existing.refresh_token = new_refresh
            existing.expires_at = timezone.now() + timedelta(days=30)
            existing.save(update_fields=["refresh_token", "expires_at"])

            publisher_id = existing.credentials.get("publisherId", "")
            add_attrs([
                ("auth.result", "success"),
                ("auth.token_reused", True),
                ("auth.publisher_id", publisher_id),
                ("auth.client_id", existing.client_id),
            ])
            record_metric("Custom/Auth/token_refresh_count", 1)
            logger.info(
                "OAuth token refreshed: publisher=%s client=%s",
                publisher_id, existing.client_id,
            )
            return JsonResponse({
                "access_token": existing.token,
                "token_type": "bearer",
                "expires_in": 30 * 24 * 3600,
                "refresh_token": new_refresh,
            })

        # ── Authorization code grant ──────────────────────────────────────────
        if grant_type != "authorization_code":
            add_attrs([("auth.result", "failure"), ("auth.failure_reason", "missing_params")])
            record_metric("Custom/Auth/auth_failure_count", 1)
            logger.warning(
                "OAuth token: unsupported grant_type=%s client=%s",
                grant_type, data.get("client_id"),
            )
            return JsonResponse({"error": "unsupported_grant_type"}, status=400)

        code          = data.get("code", "")
        code_verifier = data.get("code_verifier", "")

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

        expected = base64.urlsafe_b64encode(
            hashlib.sha256(code_verifier.encode()).digest()
        ).rstrip(b"=").decode()
        if expected != auth_code.code_challenge:
            add_attrs([("auth.result", "failure"), ("auth.failure_reason", "invalid_pkce")])
            record_metric("Custom/Auth/auth_failure_count", 1)
            logger.warning("OAuth token: PKCE verification failed client=%s", data.get("client_id"))
            return JsonResponse({"error": "invalid_grant", "error_description": "PKCE verification failed"}, status=400)

        credentials     = auth_code.credentials
        oauth_client_id = data.get("client_id") or auth_code.client_id
        publisher_id    = credentials.get("publisherId", "")
        auth_code.delete()

        # Upsert: return the existing valid token for this client+publisher so
        # the AI client keeps the same stable token identity across re-authorisations.
        existing = OAuthToken.objects.filter(
            client_id=oauth_client_id,
            expires_at__gt=timezone.now(),
        ).filter(credentials__publisherId=publisher_id).first()

        if existing:
            remaining = int((existing.expires_at - timezone.now()).total_seconds())
            # Ensure old tokens (pre-refresh_token migration) get a refresh_token on re-auth.
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
                "OAuth token reused (stable): publisher=%s client=%s expires_in=%ds",
                publisher_id, oauth_client_id, remaining,
            )
            return JsonResponse({
                "access_token": existing.token,
                "token_type": "bearer",
                "expires_in": remaining,
                "refresh_token": existing.refresh_token,
            })

        token       = secrets.token_urlsafe(32)
        new_refresh = secrets.token_urlsafe(32)
        OAuthToken.objects.create(
            token=token,
            client_id=oauth_client_id,
            refresh_token=new_refresh,
            credentials=credentials,
            expires_at=timezone.now() + timedelta(days=30),
        )

        add_attrs([
            ("auth.result", "success"),
            ("auth.token_reused", False),
            ("auth.publisher_id", publisher_id),
            ("auth.client_id", oauth_client_id),
        ])
        record_metric("Custom/Auth/token_issued_count", 1)
        logger.info(
            "OAuth token issued (new): publisher=%s client=%s",
            publisher_id, oauth_client_id,
        )
        return JsonResponse({
            "access_token": token,
            "token_type": "bearer",
            "expires_in": 30 * 24 * 3600,
            "refresh_token": new_refresh,
        })
    except Exception as exc:
        notice_err(exc, [("error.layer", "auth")])
        logger.error("OAuth token: unhandled error", exc_info=True)
        raise


# ── Legacy session-based auth (kept for direct browser use) ──────────────────

def connect(request):
    return render(request, "connect.html")


def auth_success(request):
    if not request.session.get("credentials"):
        return redirect("/connect")
    return render(request, "success.html")


@csrf_exempt
@require_http_methods(["POST"])
@newrelic.agent.function_trace(name="auth_login", group="Auth")
def auth_login(request):
    set_txn_name("Auth/session_login", group="Auth")
    add_attrs([("auth.flow", "session")])

    try:
        try:
            body = json.loads(request.body)
        except json.JSONDecodeError:
            add_attrs([
                ("auth.result", "failure"),
                ("auth.failure_reason", "missing_params"),
            ])
            logger.warning("auth_login: invalid JSON body")
            return JsonResponse({"error": "Invalid request body."}, status=400)

        publisher_id = str(body.get("publisherId", "")).strip()
        api_key      = str(body.get("apiKey", "")).strip()
        api_secret   = str(body.get("apiSecret", "")).strip()

        if not all([publisher_id, api_key, api_secret]):
            add_attrs([
                ("auth.result", "failure"),
                ("auth.failure_reason", "missing_params"),
            ])
            record_metric("Custom/Auth/auth_failure_count", 1)
            logger.warning("auth_login: missing params publisher=%s", publisher_id)
            return JsonResponse({"error": "All fields are required."}, status=400)

        try:
            ok, status_code = _validate_cds(publisher_id, api_key, api_secret)
        except requests.RequestException as exc:
            add_attrs([
                ("auth.result", "failure"),
                ("auth.failure_reason", "cds_auth_failed"),
            ])
            record_metric("Custom/Auth/auth_failure_count", 1)
            logger.error(
                "auth_login: CDS unreachable publisher=%s", publisher_id, exc_info=True
            )
            return JsonResponse({"error": f"Could not reach Publive API: {exc}"}, status=500)

        if ok:
            from datetime import datetime

            # User-chosen session duration. 0 = expire when browser closes.
            _VALID_DURATIONS = {0, 1, 7, 30, 90}
            try:
                remember_for_days = int(body.get("remember_for_days", 30))
            except (TypeError, ValueError):
                remember_for_days = 30
            if remember_for_days not in _VALID_DURATIONS:
                remember_for_days = 30

            request.session["credentials"] = {
                "publisherId": publisher_id,
                "apiKey": api_key,
                "apiSecret": api_secret,
            }
            request.session["authenticatedAt"] = datetime.now().isoformat()
            request.session["remember_for_days"] = remember_for_days

            if remember_for_days == 0:
                request.session.set_expiry(0)       # browser-session cookie
            else:
                request.session.set_expiry(remember_for_days * 24 * 3600)

            add_attrs([
                ("auth.result", "success"),
                ("auth.publisher_id", publisher_id),
                ("auth.remember_for_days", remember_for_days),
            ])
            record_metric("Custom/Auth/session_login_count", 1)
            logger.info(
                "auth_login: success publisher=%s remember_for_days=%d",
                publisher_id, remember_for_days,
            )
            return JsonResponse({"success": True, "redirectTo": "/auth/success"})

        add_attrs([
            ("auth.result", "failure"),
            ("auth.failure_reason", "cds_auth_failed"),
        ])
        record_metric("Custom/Auth/auth_failure_count", 1)
        logger.warning(
            "auth_login: invalid credentials publisher=%s status=%d", publisher_id, status_code
        )
        if status_code in (401, 403):
            return JsonResponse({"error": "Invalid credentials."}, status=401)

        return JsonResponse({"error": f"HTTP {status_code}"}, status=500)
    except Exception as exc:
        notice_err(exc, [("error.layer", "auth")])
        logger.error("auth_login: unhandled error", exc_info=True)
        raise


# HIGH FIX: suppress_apdex_metric() + suppress_transaction_trace() so the Railway
# health check (hits /auth/status every few seconds) doesn't pollute Apdex scores
# or flood the slow transaction trace list.
@newrelic.agent.function_trace(name="auth_status", group="Auth")
def auth_status(request):
    set_txn_name("Auth/session_verify", group="Auth")
    newrelic.agent.suppress_apdex_metric()
    newrelic.agent.suppress_transaction_trace()
    add_attrs([("auth.flow", "session")])

    try:
        credentials = request.session.get("credentials")
        if credentials:
            add_attrs([
                ("auth.result", "success"),
                ("auth.publisher_id", credentials.get("publisherId")),
            ])
            expiry_age = request.session.get_expiry_age()   # seconds remaining
            return JsonResponse({
                "authenticated": True,
                "publisherId": credentials.get("publisherId"),
                "authenticatedAt": request.session.get("authenticatedAt"),
                "remember_for_days": request.session.get("remember_for_days", 30),
                "session_expires_in_seconds": expiry_age,
            })

        add_attrs([
            ("auth.result", "failure"),
            ("auth.failure_reason", "invalid_session"),
        ])
        return JsonResponse({"authenticated": False})
    except Exception as exc:
        notice_err(exc, [("error.layer", "auth")])
        logger.error("auth_status: unhandled error", exc_info=True)
        raise


@csrf_exempt
@require_http_methods(["POST"])
@newrelic.agent.function_trace(name="auth_logout", group="Auth")
def auth_logout(request):
    set_txn_name("Auth/session_logout", group="Auth")
    publisher_id = (request.session.get("credentials") or {}).get("publisherId", "unknown")
    request.session.flush()
    add_attrs([
        ("auth.flow", "session"),
        ("auth.result", "success"),
        ("auth.publisher_id", publisher_id),
    ])
    record_metric("Custom/Auth/session_logout_count", 1)
    logger.info("auth_logout: publisher=%s", publisher_id)
    return JsonResponse({"success": True})
