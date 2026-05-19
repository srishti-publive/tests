import base64
import hashlib
import json
import secrets
from datetime import timedelta
from urllib.parse import urlencode

import requests
from django.conf import settings
from django.http import JsonResponse
from django.shortcuts import render, redirect
from django.utils import timezone
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_http_methods

from .models import OAuthClient, OAuthCode, OAuthToken


def _validate_cds(publisher_id, api_key, api_secret):
    token = base64.b64encode(f"{api_key}:{api_secret}".encode()).decode()
    resp = requests.get(
        f"https://cds.thepublive.com/publisher/{publisher_id}/publisher-data/",
        headers={"Authorization": f"Basic {token}"},
        timeout=10,
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
        "grant_types_supported": ["authorization_code"],
        "code_challenge_methods_supported": ["S256"],
        "token_endpoint_auth_methods_supported": ["none"],
    })


# ── Dynamic client registration ───────────────────────────────────────────────

@csrf_exempt
@require_http_methods(["POST"])
def oauth_register(request):
    try:
        body = json.loads(request.body)
    except Exception:
        body = {}

    client_id = secrets.token_urlsafe(24)
    OAuthClient.objects.create(
        client_id=client_id,
        redirect_uris=body.get("redirect_uris", []),
    )
    return JsonResponse({
        "client_id": client_id,
        "client_id_issued_at": int(timezone.now().timestamp()),
        "redirect_uris": body.get("redirect_uris", []),
    }, status=201)


# ── Authorization endpoint ────────────────────────────────────────────────────

@require_http_methods(["GET", "POST"])
def oauth_authorize(request):
    if request.method == "GET":
        return render(request, "authorize.html", {
            "client_id": request.GET.get("client_id", ""),
            "redirect_uri": request.GET.get("redirect_uri", ""),
            "state": request.GET.get("state", ""),
            "code_challenge": request.GET.get("code_challenge", ""),
            "code_challenge_method": request.GET.get("code_challenge_method", "S256"),
        })

    publisher_id          = request.POST.get("publisherId", "").strip()
    api_key               = request.POST.get("apiKey", "").strip()
    api_secret            = request.POST.get("apiSecret", "").strip()
    client_id             = request.POST.get("client_id", "")
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

    if not all([publisher_id, api_key, api_secret]):
        ctx["error"] = "All fields are required."
        return render(request, "authorize.html", ctx)

    try:
        ok, status_code = _validate_cds(publisher_id, api_key, api_secret)
    except requests.RequestException as exc:
        ctx["error"] = f"Could not reach Publive API: {exc}"
        return render(request, "authorize.html", ctx)

    if not ok:
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

    return redirect(f"{redirect_uri}?{urlencode({'code': code, 'state': state})}")


# ── Token endpoint ────────────────────────────────────────────────────────────

@csrf_exempt
@require_http_methods(["POST"])
def oauth_token(request):
    if "application/json" in (request.content_type or ""):
        try:
            data = json.loads(request.body)
        except json.JSONDecodeError:
            return JsonResponse({"error": "invalid_request"}, status=400)
    else:
        data = request.POST

    if data.get("grant_type", "") != "authorization_code":
        return JsonResponse({"error": "unsupported_grant_type"}, status=400)

    code          = data.get("code", "")
    code_verifier = data.get("code_verifier", "")

    try:
        auth_code = OAuthCode.objects.get(code=code)
    except OAuthCode.DoesNotExist:
        return JsonResponse({"error": "invalid_grant", "error_description": "Unknown code"}, status=400)

    if auth_code.expires_at < timezone.now():
        auth_code.delete()
        return JsonResponse({"error": "invalid_grant", "error_description": "Code expired"}, status=400)

    expected = base64.urlsafe_b64encode(
        hashlib.sha256(code_verifier.encode()).digest()
    ).rstrip(b"=").decode()
    if expected != auth_code.code_challenge:
        return JsonResponse({"error": "invalid_grant", "error_description": "PKCE verification failed"}, status=400)

    token = secrets.token_urlsafe(32)
    OAuthToken.objects.create(
        token=token,
        credentials=auth_code.credentials,
        expires_at=timezone.now() + timedelta(days=30),
    )
    auth_code.delete()

    return JsonResponse({
        "access_token": token,
        "token_type": "bearer",
        "expires_in": 30 * 24 * 3600,
    })


# ── Legacy session-based auth (kept for direct browser use) ──────────────────

def connect(request):
    return render(request, "connect.html")


def auth_success(request):
    if not request.session.get("credentials"):
        return redirect("/connect")
    return render(request, "success.html")


@csrf_exempt
@require_http_methods(["POST"])
def auth_login(request):
    try:
        body = json.loads(request.body)
    except json.JSONDecodeError:
        return JsonResponse({"error": "Invalid request body."}, status=400)

    publisher_id = str(body.get("publisherId", "")).strip()
    api_key      = str(body.get("apiKey", "")).strip()
    api_secret   = str(body.get("apiSecret", "")).strip()

    if not all([publisher_id, api_key, api_secret]):
        return JsonResponse({"error": "All fields are required."}, status=400)

    try:
        ok, status_code = _validate_cds(publisher_id, api_key, api_secret)
    except requests.RequestException as exc:
        return JsonResponse({"error": f"Could not reach Publive API: {exc}"}, status=500)

    if ok:
        from datetime import datetime
        request.session["credentials"] = {"publisherId": publisher_id, "apiKey": api_key, "apiSecret": api_secret}
        request.session["authenticatedAt"] = datetime.now().isoformat()
        return JsonResponse({"success": True, "redirectTo": "/auth/success"})

    if status_code in (401, 403):
        return JsonResponse({"error": "Invalid credentials."}, status=401)

    return JsonResponse({"error": f"HTTP {status_code}"}, status=500)


def auth_status(request):
    credentials = request.session.get("credentials")
    if credentials:
        return JsonResponse({
            "authenticated": True,
            "publisherId": credentials.get("publisherId"),
            "authenticatedAt": request.session.get("authenticatedAt"),
        })
    return JsonResponse({"authenticated": False})


@csrf_exempt
@require_http_methods(["POST"])
def auth_logout(request):
    request.session.flush()
    return JsonResponse({"success": True})
