import json
import secrets
import uuid
from datetime import timedelta
from unittest.mock import patch

from django.test import TestCase, Client, RequestFactory
from django.utils import timezone

from auth_app.models import AIClient, OAuthToken
from mcp_app.protocol.auth import (
    CLIENT_BLOCKED,
    INVALID_CLIENT_ID,
    SESSION_EXPIRED,
    resolve_credentials,
)


def _make_oauth_token(expired=False):
    raw = secrets.token_urlsafe(32)
    expires_at = timezone.now() + (timedelta(days=-1) if expired else timedelta(days=30))
    OAuthToken.objects.create(
        token=raw,
        client_id="test_client",
        credentials={"publisherId": "3567", "apiKey": "k", "apiSecret": "s"},
        expires_at=expires_at,
    )
    return raw


def _make_ai_client(status=AIClient.STATUS_ACTIVE):
    return AIClient.objects.create(
        client_name="Test AI",
        credentials={"publisherId": "3567", "apiKey": "k", "apiSecret": "s"},
        registration_ip="127.0.0.1",
        status=status,
    )


# ── resolve_credentials unit tests ───────────────────────────────────────────

class GetCredentialsTests(TestCase):
    def _req(self, token=None, session_creds=None, session_extras=None):
        factory = RequestFactory()
        req = factory.get("/mcp")
        if token:
            req.META["HTTP_AUTHORIZATION"] = f"Bearer {token}"
        req.session = {}
        if session_creds:
            req.session["credentials"] = session_creds
        if session_extras:
            req.session.update(session_extras)
        return req

    # OAuthToken path

    def test_valid_bearer_token_returns_credentials(self):
        raw = _make_oauth_token()
        creds, expires_at, err = resolve_credentials(self._req(token=raw))
        self.assertIsNotNone(creds)
        self.assertEqual(creds["publisherId"], "3567")
        self.assertIsNotNone(expires_at)
        self.assertIsNone(err)

    def test_expired_bearer_token_falls_through_to_none(self):
        raw = _make_oauth_token(expired=True)
        creds, _, err = resolve_credentials(self._req(token=raw))
        self.assertIsNone(creds)
        self.assertIsNone(err)

    def test_unknown_bearer_token_falls_through_to_none(self):
        creds, _, err = resolve_credentials(self._req(token="nonexistent_token_abc"))
        self.assertIsNone(creds)
        self.assertIsNone(err)

    def test_bearer_prefix_case_sensitive(self):
        raw = _make_oauth_token()
        req = self._req()
        req.META["HTTP_AUTHORIZATION"] = f"bearer {raw}"   # lowercase → not a bearer header
        creds, _, err = resolve_credentials(req)
        self.assertIsNone(creds)
        self.assertIsNone(err)

    def test_token_with_leading_whitespace_still_resolves(self):
        raw = _make_oauth_token()
        req = self._req()
        req.META["HTTP_AUTHORIZATION"] = f"Bearer  {raw}"  # extra space stripped by .strip()
        creds, _, err = resolve_credentials(req)
        self.assertIsNotNone(creds)

    # AIClient path (UUID v4 bearer)

    def test_active_ai_client_returns_credentials(self):
        ai_client = _make_ai_client()
        creds, expires_at, err = resolve_credentials(self._req(token=str(ai_client.client_id)))
        self.assertIsNotNone(creds)
        self.assertEqual(creds["publisherId"], "3567")
        self.assertIsNone(expires_at)
        self.assertIsNone(err)

    def test_blocked_ai_client_returns_client_blocked(self):
        ai_client = _make_ai_client(status=AIClient.STATUS_BLOCKED)
        creds, _, err = resolve_credentials(self._req(token=str(ai_client.client_id)))
        self.assertIsNone(creds)
        self.assertEqual(err, CLIENT_BLOCKED)

    def test_unknown_uuid_returns_invalid_client_id(self):
        unknown = str(uuid.uuid4())
        creds, _, err = resolve_credentials(self._req(token=unknown))
        self.assertIsNone(creds)
        self.assertEqual(err, INVALID_CLIENT_ID)

    def test_ai_client_lookup_updates_last_seen_at(self):
        ai_client = _make_ai_client()
        self.assertIsNone(ai_client.last_seen_at)
        resolve_credentials(self._req(token=str(ai_client.client_id)))
        ai_client.refresh_from_db()
        self.assertIsNotNone(ai_client.last_seen_at)

    # Session path

    def test_no_auth_header_uses_session_credentials(self):
        session_creds = {"publisherId": "999", "apiKey": "a", "apiSecret": "b"}
        creds, expires_at, err = resolve_credentials(self._req(session_creds=session_creds))
        self.assertEqual(creds, session_creds)
        self.assertIsNone(expires_at)
        self.assertIsNone(err)

    def test_no_auth_and_no_session_returns_none(self):
        creds, _, err = resolve_credentials(self._req())
        self.assertIsNone(creds)
        self.assertIsNone(err)

    def test_expired_session_returns_session_expired(self):
        import time as _time
        session_creds = {"publisherId": "123", "apiKey": "a", "apiSecret": "b"}
        extras = {
            "session_ttl_seconds": 86400,
            "session_created_at": int(_time.time()) - (2 * 24 * 3600),   # 2 days ago
        }
        creds, _, err = resolve_credentials(self._req(session_creds=session_creds, session_extras=extras))
        self.assertIsNone(creds)
        self.assertEqual(err, SESSION_EXPIRED)

    def test_always_session_does_not_expire(self):
        import time as _time
        session_creds = {"publisherId": "123", "apiKey": "a", "apiSecret": "b"}
        extras = {
            "session_ttl_seconds": -1,
            "session_created_at": int(_time.time()) - (3650 * 24 * 3600),
        }
        creds, _, err = resolve_credentials(self._req(session_creds=session_creds, session_extras=extras))
        self.assertIsNotNone(creds)
        self.assertIsNone(err)

    def test_browser_session_not_checked_server_side(self):
        """ttl_seconds=0 (browser session) is not enforced here — browser handles it."""
        import time as _time
        session_creds = {"publisherId": "123", "apiKey": "a", "apiSecret": "b"}
        extras = {
            "session_ttl_seconds": 0,
            "session_created_at": int(_time.time()) - (365 * 24 * 3600),
        }
        creds, _, err = resolve_credentials(self._req(session_creds=session_creds, session_extras=extras))
        self.assertIsNotNone(creds)
        self.assertIsNone(err)


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
        raw = _make_oauth_token()
        resp = self.c.post(
            "/mcp",
            '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2024-11-05","capabilities":{},"clientInfo":{"name":"test","version":"1.0"}}}',
            content_type="application/json",
            HTTP_AUTHORIZATION=f"Bearer {raw}",
        )
        self.assertNotEqual(resp.status_code, 401)

    def test_initialize_returns_protocol_version(self):
        raw = _make_oauth_token()
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
        raw = _make_oauth_token(expired=True)
        resp = self.c.get("/mcp", HTTP_AUTHORIZATION=f"Bearer {raw}")
        self.assertEqual(resp.status_code, 401)

    def test_blocked_ai_client_returns_401_with_typed_code(self):
        ai_client = _make_ai_client(status=AIClient.STATUS_BLOCKED)
        resp = self.c.get("/mcp", HTTP_AUTHORIZATION=f"Bearer {ai_client.client_id}")
        self.assertEqual(resp.status_code, 401)
        self.assertEqual(resp.json()["error"], CLIENT_BLOCKED)

    def test_unknown_uuid_returns_401_with_typed_code(self):
        resp = self.c.get("/mcp", HTTP_AUTHORIZATION=f"Bearer {uuid.uuid4()}")
        self.assertEqual(resp.status_code, 401)
        self.assertEqual(resp.json()["error"], INVALID_CLIENT_ID)

    def test_session_expired_returns_401_with_typed_code(self):
        import time as _time
        from django.test import Client as DjangoClient
        sc = DjangoClient()
        with patch("auth_app.views.validate_cds_credentials", return_value=(True, 200)):
            sc.post(
                "/auth/login",
                json.dumps({"publisherId": "3567", "apiKey": "k", "apiSecret": "s", "remember_for_days": 1}),
                content_type="application/json",
            )
        session = sc.session
        session["session_created_at"] = int(_time.time()) - (2 * 24 * 3600)   # 2 days ago
        session.save()

        resp = sc.post("/mcp", "{}", content_type="application/json")
        self.assertEqual(resp.status_code, 401)
        self.assertEqual(resp.json()["error"], SESSION_EXPIRED)
