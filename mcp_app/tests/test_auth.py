import secrets
from datetime import timedelta

from django.test import TestCase, Client, RequestFactory
from django.utils import timezone

from auth_app.models import OAuthToken
from mcp_app.views import _get_credentials


def _make_token(expired=False):
    raw = secrets.token_urlsafe(32)
    expires_at = timezone.now() + (timedelta(days=-1) if expired else timedelta(days=30))
    OAuthToken.objects.create(
        token=raw,
        client_id="test_client",
        credentials={"publisherId": "3567", "apiKey": "k", "apiSecret": "s"},
        expires_at=expires_at,
    )
    return raw


# ── _get_credentials unit tests ───────────────────────────────────────────────

class GetCredentialsTests(TestCase):
    def _req(self, token=None, session_creds=None):
        factory = RequestFactory()
        req = factory.get("/mcp")
        if token:
            req.META["HTTP_AUTHORIZATION"] = f"Bearer {token}"
        # Attach a minimal session-like object
        req.session = {}
        if session_creds:
            req.session["credentials"] = session_creds
        return req

    def test_valid_bearer_token_returns_credentials(self):
        raw = _make_token()
        creds, expires_at = _get_credentials(self._req(token=raw))
        self.assertIsNotNone(creds)
        self.assertEqual(creds["publisherId"], "3567")
        self.assertIsNotNone(expires_at)

    def test_expired_bearer_token_falls_through_to_none(self):
        raw = _make_token(expired=True)
        creds, _ = _get_credentials(self._req(token=raw))
        self.assertIsNone(creds)

    def test_unknown_bearer_token_falls_through_to_none(self):
        creds, _ = _get_credentials(self._req(token="nonexistent_token_abc"))
        self.assertIsNone(creds)

    def test_no_auth_header_uses_session_credentials(self):
        session_creds = {"publisherId": "999", "apiKey": "a", "apiSecret": "b"}
        creds, expires_at = _get_credentials(self._req(session_creds=session_creds))
        self.assertEqual(creds, session_creds)
        self.assertIsNone(expires_at)

    def test_no_auth_and_no_session_returns_none(self):
        creds, _ = _get_credentials(self._req())
        self.assertIsNone(creds)

    def test_bearer_prefix_case_sensitive(self):
        # Must start exactly with "Bearer " (capital B)
        raw = _make_token()
        req = self._req()
        req.META["HTTP_AUTHORIZATION"] = f"bearer {raw}"  # lowercase
        creds, _ = _get_credentials(req)
        self.assertIsNone(creds)

    def test_token_with_leading_whitespace_still_resolves(self):
        # .strip() on token_value should handle any accidental whitespace
        raw = _make_token()
        req = self._req()
        req.META["HTTP_AUTHORIZATION"] = f"Bearer  {raw}"  # extra space
        # This should NOT resolve — two spaces means the token has a leading space
        # and won't match; this documents current behavior
        creds, _ = _get_credentials(req)
        # With .strip() the extra space is removed → should find the token
        self.assertIsNotNone(creds)


# ── MCP endpoint authentication ───────────────────────────────────────────────

class MCPEndpointAuthTests(TestCase):
    def setUp(self):
        self.c = Client()

    def test_unauthenticated_get_returns_401(self):
        resp = self.c.get("/mcp")
        self.assertEqual(resp.status_code, 401)

    def test_unauthenticated_post_returns_401(self):
        resp = self.c.post("/mcp", "{}", content_type="application/json")
        self.assertEqual(resp.status_code, 401)

    def test_401_response_contains_www_authenticate_header(self):
        resp = self.c.get("/mcp")
        self.assertIn("WWW-Authenticate", resp)
        self.assertIn("Bearer", resp["WWW-Authenticate"])
        self.assertIn("resource_metadata", resp["WWW-Authenticate"])

    def test_invalid_bearer_token_returns_401(self):
        resp = self.c.get("/mcp", HTTP_AUTHORIZATION="Bearer totally_fake_token")
        self.assertEqual(resp.status_code, 401)

    def test_valid_bearer_token_passes_auth_on_post(self):
        raw = _make_token()
        resp = self.c.post(
            "/mcp",
            '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2024-11-05","capabilities":{},"clientInfo":{"name":"test","version":"1.0"}}}',
            content_type="application/json",
            HTTP_AUTHORIZATION=f"Bearer {raw}",
        )
        # Auth passes — should NOT be 401
        self.assertNotEqual(resp.status_code, 401)

    def test_initialize_returns_protocol_version(self):
        raw = _make_token()
        resp = self.c.post(
            "/mcp",
            '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2024-11-05","capabilities":{},"clientInfo":{"name":"test","version":"1.0"}}}',
            content_type="application/json",
            HTTP_AUTHORIZATION=f"Bearer {raw}",
        )
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertEqual(data["result"]["protocolVersion"], "2024-11-05")
        self.assertIn("tools", data["result"]["capabilities"])

    def test_expired_token_returns_401(self):
        raw = _make_token(expired=True)
        resp = self.c.get("/mcp", HTTP_AUTHORIZATION=f"Bearer {raw}")
        self.assertEqual(resp.status_code, 401)
