import base64
from datetime import timedelta
import hashlib
import json
import secrets
from unittest.mock import patch
from urllib.parse import parse_qs, urlparse

from django.test import Client, TestCase, override_settings
from django.utils import timezone

from auth_app.models import OAuthClient, OAuthCode, OAuthToken

ALLOWED_REDIRECT = "https://claude.ai/api/mcp/auth_callback"


def _pkce_pair():
    """Return (verifier, challenge) for S256 PKCE."""
    verifier = secrets.token_urlsafe(32)
    challenge = (
        base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest())
        .rstrip(b"=")
        .decode()
    )
    return verifier, challenge


def _make_client(redirect_uri=ALLOWED_REDIRECT):
    return OAuthClient.objects.create(
        client_id=secrets.token_urlsafe(16),
        redirect_uri=redirect_uri,
    )


# ── Registration ──────────────────────────────────────────────────────────────

@override_settings(OAUTH_ALLOWED_REDIRECT_URIS=[ALLOWED_REDIRECT], OAUTH_ALLOWED_ORIGINS=[])
class OAuthRegisterTests(TestCase):
    def setUp(self):
        self.c = Client()

    def test_register_creates_client(self):
        resp = self.c.post(
            "/register",
            json.dumps({"redirect_uris": [ALLOWED_REDIRECT]}),
            content_type="application/json",
        )
        self.assertEqual(resp.status_code, 201)
        data = resp.json()
        self.assertIn("client_id", data)
        self.assertEqual(data["redirect_uris"], [ALLOWED_REDIRECT])
        self.assertTrue(OAuthClient.objects.filter(client_id=data["client_id"]).exists())

    def test_register_no_uris_succeeds(self):
        resp = self.c.post(
            "/register",
            json.dumps({"redirect_uris": []}),
            content_type="application/json",
        )
        self.assertEqual(resp.status_code, 201)

    def test_register_disallowed_uri_rejected(self):
        resp = self.c.post(
            "/register",
            json.dumps({"redirect_uris": ["https://evil.com/callback"]}),
            content_type="application/json",
        )
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(resp.json()["error"], "invalid_redirect_uri")

    def test_register_permanent_no_expiry(self):
        """Client registrations are now permanent — no expires_at field."""
        resp = self.c.post(
            "/register",
            json.dumps({"redirect_uris": [ALLOWED_REDIRECT]}),
            content_type="application/json",
        )
        self.assertEqual(resp.status_code, 201)
        client = OAuthClient.objects.get(client_id=resp.json()["client_id"])
        self.assertFalse(hasattr(client, "expires_at"))

    def test_register_only_post_allowed(self):
        resp = self.c.get("/register")
        self.assertEqual(resp.status_code, 405)


# ── Authorize GET ─────────────────────────────────────────────────────────────

@override_settings(OAUTH_ALLOWED_REDIRECT_URIS=[ALLOWED_REDIRECT], OAUTH_ALLOWED_ORIGINS=[])
class OAuthAuthorizeGetTests(TestCase):
    def setUp(self):
        self.c = Client()
        self.oauth_client = _make_client()

    def test_get_valid_client_renders_form(self):
        resp = self.c.get("/authorize", {
            "response_type": "code",
            "client_id": self.oauth_client.client_id,
            "redirect_uri": ALLOWED_REDIRECT,
        })
        self.assertEqual(resp.status_code, 200)
        self.assertTemplateUsed(resp, "authorize.html")

    def test_get_unknown_client_returns_error(self):
        resp = self.c.get("/authorize", {
            "response_type": "code",
            "client_id": "nonexistent_client",
            "redirect_uri": ALLOWED_REDIRECT,
        })
        self.assertEqual(resp.status_code, 400)

    def test_get_unknown_client_returns_400(self):
        """No expiry concept any more — unknown client_id is simply rejected."""
        resp = self.c.get("/authorize", {
            "response_type": "code",
            "client_id": "totally_unknown_xyz",
            "redirect_uri": ALLOWED_REDIRECT,
        })
        self.assertEqual(resp.status_code, 400)

    def test_get_implicit_grant_rejected(self):
        resp = self.c.get("/authorize", {
            "response_type": "token",
            "client_id": self.oauth_client.client_id,
            "redirect_uri": ALLOWED_REDIRECT,
        })
        self.assertEqual(resp.status_code, 400)

    def test_get_missing_redirect_uri_returns_error(self):
        resp = self.c.get("/authorize", {
            "response_type": "code",
            "client_id": self.oauth_client.client_id,
        })
        self.assertEqual(resp.status_code, 400)


# ── Authorize POST ────────────────────────────────────────────────────────────

@override_settings(OAUTH_ALLOWED_REDIRECT_URIS=[ALLOWED_REDIRECT], OAUTH_ALLOWED_ORIGINS=[])
class OAuthAuthorizePostTests(TestCase):
    def setUp(self):
        self.c = Client()
        self.oauth_client = _make_client()
        self.verifier, self.challenge = _pkce_pair()

    def _post(self, extra=None):
        data = {
            "client_id": self.oauth_client.client_id,
            "redirect_uri": ALLOWED_REDIRECT,
            "state": "test_state",
            "code_challenge": self.challenge,
            "code_challenge_method": "S256",
            "publisherId": "3567",
            "apiKey": "test_key",
            "apiSecret": "test_secret",
        }
        if extra:
            data.update(extra)
        return self.c.post("/authorize", data)

    @patch("auth_app.views.validate_cds_credentials", return_value=(True, 200))
    def test_valid_credentials_redirect_with_code(self, _mock):
        resp = self._post()
        self.assertEqual(resp.status_code, 302)
        location = resp["Location"]
        self.assertIn("code=", location)
        self.assertIn("state=test_state", location)
        qs = parse_qs(urlparse(location).query)
        self.assertTrue(OAuthCode.objects.filter(code=qs["code"][0]).exists())

    @patch("auth_app.views.validate_cds_credentials", return_value=(False, 401))
    def test_invalid_credentials_show_error_in_form(self, _mock):
        resp = self._post()
        self.assertEqual(resp.status_code, 200)
        self.assertTemplateUsed(resp, "authorize.html")
        self.assertIn("error", resp.context)

    @patch("auth_app.views.validate_cds_credentials", return_value=(True, 200))
    def test_missing_publisher_id_shows_error(self, _mock):
        resp = self._post(extra={"publisherId": ""})
        self.assertEqual(resp.status_code, 200)
        self.assertIn("error", resp.context)

    @patch("auth_app.views.validate_cds_credentials", return_value=(True, 200))
    def test_mismatched_redirect_uri_shows_error(self, _mock):
        resp = self._post(extra={"redirect_uri": "https://evil.com/callback"})
        self.assertEqual(resp.status_code, 200)
        self.assertIn("error", resp.context)


# ── Token exchange ────────────────────────────────────────────────────────────

@override_settings(OAUTH_ALLOWED_REDIRECT_URIS=[ALLOWED_REDIRECT], OAUTH_ALLOWED_ORIGINS=[])
class OAuthTokenExchangeTests(TestCase):
    def setUp(self):
        self.c = Client()
        self.oauth_client = _make_client()
        self.verifier, self.challenge = _pkce_pair()
        self.code_obj = OAuthCode.objects.create(
            code=secrets.token_urlsafe(32),
            client_id=self.oauth_client.client_id,
            redirect_uri=ALLOWED_REDIRECT,
            code_challenge=self.challenge,
            credentials={"publisherId": "3567", "apiKey": "k", "apiSecret": "s"},
            expires_at=timezone.now() + timedelta(minutes=10),
        )

    def _exchange(self, extra=None):
        body = {
            "grant_type": "authorization_code",
            "code": self.code_obj.code,
            "redirect_uri": ALLOWED_REDIRECT,
            "code_verifier": self.verifier,
            "client_id": self.oauth_client.client_id,
        }
        if extra:
            body.update(extra)
        return self.c.post("/token", json.dumps(body), content_type="application/json")

    def test_valid_exchange_issues_token(self):
        resp = self._exchange()
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertIn("access_token", data)
        self.assertIn("refresh_token", data)
        self.assertEqual(data["token_type"], "bearer")
        self.assertTrue(OAuthToken.objects.filter(token=data["access_token"]).exists())

    def test_code_is_consumed_after_exchange(self):
        self._exchange()
        self.assertFalse(OAuthCode.objects.filter(code=self.code_obj.code).exists())

    def test_expired_code_rejected(self):
        self.code_obj.expires_at = timezone.now() - timedelta(minutes=1)
        self.code_obj.save()
        resp = self._exchange()
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(resp.json()["error"], "invalid_grant")

    def test_pkce_failure_rejected(self):
        resp = self._exchange(extra={"code_verifier": "wrong_verifier"})
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(resp.json()["error"], "invalid_grant")

    def test_unknown_code_rejected(self):
        resp = self._exchange(extra={"code": "nonexistent_code_xyz"})
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(resp.json()["error"], "invalid_grant")

    def test_redirect_uri_mismatch_rejected(self):
        resp = self._exchange(extra={"redirect_uri": "https://evil.com/callback"})
        self.assertEqual(resp.status_code, 400)

    def test_same_client_publisher_reuses_existing_token(self):
        # First exchange
        resp1 = self._exchange()
        token1 = resp1.json()["access_token"]

        # Create a fresh code for the same client+publisher
        verifier2, challenge2 = _pkce_pair()
        code2 = OAuthCode.objects.create(
            code=secrets.token_urlsafe(32),
            client_id=self.oauth_client.client_id,
            redirect_uri=ALLOWED_REDIRECT,
            code_challenge=challenge2,
            credentials={"publisherId": "3567", "apiKey": "k", "apiSecret": "s"},
            expires_at=timezone.now() + timedelta(minutes=10),
        )
        resp2 = self.c.post("/token", json.dumps({
            "grant_type": "authorization_code",
            "code": code2.code,
            "redirect_uri": ALLOWED_REDIRECT,
            "code_verifier": verifier2,
            "client_id": self.oauth_client.client_id,
        }), content_type="application/json")
        # Same token should be returned (stable upsert)
        self.assertEqual(resp2.json()["access_token"], token1)

    def test_unsupported_grant_type_rejected(self):
        resp = self._exchange(extra={"grant_type": "client_credentials"})
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(resp.json()["error"], "unsupported_grant_type")


# ── Token refresh ─────────────────────────────────────────────────────────────

@override_settings(OAUTH_ALLOWED_REDIRECT_URIS=[ALLOWED_REDIRECT], OAUTH_ALLOWED_ORIGINS=[])
class OAuthTokenRefreshTests(TestCase):
    def setUp(self):
        self.c = Client()
        self.oauth_client = _make_client()
        self.token_obj = OAuthToken.objects.create(
            token=secrets.token_urlsafe(32),
            client_id=self.oauth_client.client_id,
            refresh_token=secrets.token_urlsafe(32),
            credentials={"publisherId": "3567", "apiKey": "k", "apiSecret": "s"},
        )

    def _refresh(self, refresh_token=None):
        return self.c.post("/token", json.dumps({
            "grant_type": "refresh_token",
            "refresh_token": refresh_token or self.token_obj.refresh_token,
        }), content_type="application/json")

    def test_valid_refresh_returns_same_access_token_and_new_refresh(self):
        resp = self._refresh()
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertEqual(data["access_token"], self.token_obj.token)
        # Refresh token should be rotated
        self.assertNotEqual(data["refresh_token"], self.token_obj.refresh_token)

    def test_unknown_refresh_token_rejected(self):
        resp = self._refresh(refresh_token="bogus_refresh_token")
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(resp.json()["error"], "invalid_grant")

