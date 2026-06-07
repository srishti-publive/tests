import json
from unittest.mock import patch

from django.test import Client, TestCase

from auth_app.services import get_session_credentials


class SessionAuthTests(TestCase):
    def setUp(self):
        self.c = Client()

    # ── Login basic ───────────────────────────────────────────────────────────

    @patch("auth_app.views.validate_cds_credentials", return_value=(True, 200))
    def test_login_success_sets_session(self, _mock):
        resp = self.c.post(
            "/auth/login",
            json.dumps({"publisherId": "3567", "apiKey": "k", "apiSecret": "s"}),
            content_type="application/json",
        )
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(resp.json()["success"])
        session = self.c.session
        self.assertIn("credentials", session)
        creds = get_session_credentials(session)
        self.assertIsNotNone(creds)
        self.assertEqual(creds["publisherId"], "3567")

    @patch("auth_app.views.validate_cds_credentials", return_value=(False, 401))
    def test_login_invalid_credentials_returns_401(self, _mock):
        resp = self.c.post(
            "/auth/login",
            json.dumps({"publisherId": "3567", "apiKey": "bad", "apiSecret": "bad"}),
            content_type="application/json",
        )
        self.assertEqual(resp.status_code, 401)
        self.assertIn("error", resp.json())

    def test_login_missing_fields_returns_400(self):
        resp = self.c.post(
            "/auth/login",
            json.dumps({"publisherId": "3567"}),
            content_type="application/json",
        )
        self.assertEqual(resp.status_code, 400)
        self.assertIn("error", resp.json())

    def test_login_invalid_json_returns_400(self):
        resp = self.c.post(
            "/auth/login",
            "not json",
            content_type="application/json",
        )
        self.assertEqual(resp.status_code, 400)

    # ── Session lifetime ──────────────────────────────────────────────────────

    @patch("auth_app.views.validate_cds_credentials", return_value=(True, 200))
    def test_login_creates_never_expiring_session(self, _mock):
        """Sessions never expire on their own — only /auth/logout ends them."""
        self.c.post(
            "/auth/login",
            json.dumps({"publisherId": "3567", "apiKey": "k", "apiSecret": "s"}),
            content_type="application/json",
        )
        session = self.c.session
        self.assertEqual(session["session_ttl_seconds"], -1)
        self.assertNotIn("remember_for_days", session)

    @patch("auth_app.views.validate_cds_credentials", return_value=(True, 200))
    def test_login_ignores_remember_for_days_in_body(self, _mock):
        """The picker is gone — any remember_for_days sent by old clients is ignored."""
        self.c.post(
            "/auth/login",
            json.dumps({"publisherId": "3567", "apiKey": "k", "apiSecret": "s", "remember_for_days": 7}),
            content_type="application/json",
        )
        session = self.c.session
        self.assertEqual(session["session_ttl_seconds"], -1)
        self.assertNotIn("remember_for_days", session)

    @patch("auth_app.views.validate_cds_credentials", return_value=(True, 200))
    def test_session_created_at_stored(self, _mock):
        """session_created_at must be set at login time as a Unix epoch integer."""
        self.c.post(
            "/auth/login",
            json.dumps({"publisherId": "3567", "apiKey": "k", "apiSecret": "s"}),
            content_type="application/json",
        )
        self.assertIn("session_created_at", self.c.session)
        self.assertIsInstance(self.c.session["session_created_at"], int)

    # ── Status endpoint ───────────────────────────────────────────────────────

    @patch("auth_app.views.validate_cds_credentials", return_value=(True, 200))
    def test_status_authenticated(self, _mock):
        self.c.post(
            "/auth/login",
            json.dumps({"publisherId": "3567", "apiKey": "k", "apiSecret": "s"}),
            content_type="application/json",
        )
        resp = self.c.get("/auth/status")
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertTrue(data["authenticated"])
        self.assertEqual(data["publisherId"], "3567")

    def test_status_unauthenticated(self):
        resp = self.c.get("/auth/status")
        self.assertEqual(resp.status_code, 200)
        self.assertFalse(resp.json()["authenticated"])

    # ── Logout ────────────────────────────────────────────────────────────────

    @patch("auth_app.views.validate_cds_credentials", return_value=(True, 200))
    def test_logout_clears_session(self, _mock):
        self.c.post(
            "/auth/login",
            json.dumps({"publisherId": "3567", "apiKey": "k", "apiSecret": "s"}),
            content_type="application/json",
        )
        self.c.post("/auth/logout")
        resp = self.c.get("/auth/status")
        self.assertFalse(resp.json()["authenticated"])

    # ── Server-side TTL enforcement ───────────────────────────────────────────

    @patch("auth_app.views.validate_cds_credentials", return_value=(True, 200))
    def test_always_session_never_expires(self, _mock):
        """ttl_seconds=-1 ('Always', the only option now) must never trigger SESSION_EXPIRED."""
        self.c.post(
            "/auth/login",
            json.dumps({"publisherId": "3567", "apiKey": "k", "apiSecret": "s"}),
            content_type="application/json",
        )
        # Backdate far into the past (int epoch) — should still be authenticated.
        import time as _time
        session = self.c.session
        session["session_created_at"] = int(_time.time()) - (3650 * 24 * 3600)
        session.save()

        resp = self.c.get("/auth/status")
        self.assertTrue(resp.json()["authenticated"])

    # ── Legacy session compat ─────────────────────────────────────────────────
    # Sessions created before this change may still carry a finite
    # session_ttl_seconds. check_session_ttl() must keep enforcing those until
    # the user logs in again and gets a fresh "Always" session.

    @patch("auth_app.views.validate_cds_credentials", return_value=(True, 200))
    def test_legacy_finite_ttl_session_still_expires(self, _mock):
        """A pre-existing session with a stored finite TTL must still expire."""
        self.c.post(
            "/auth/login",
            json.dumps({"publisherId": "3567", "apiKey": "k", "apiSecret": "s"}),
            content_type="application/json",
        )
        import time as _time
        session = self.c.session
        session["session_ttl_seconds"] = 1 * 24 * 3600          # simulate a legacy 1-day session
        session["session_created_at"] = int(_time.time()) - (2 * 24 * 3600)
        session.save()

        resp = self.c.get("/auth/status")
        data = resp.json()
        self.assertFalse(data["authenticated"])
        self.assertEqual(data.get("error"), "SESSION_EXPIRED")

    @patch("auth_app.views.validate_cds_credentials", return_value=(True, 200))
    def test_legacy_finite_ttl_session_expired_at_mcp(self, _mock):
        """MCP endpoint must also reject a legacy session past its stored finite TTL."""
        self.c.post(
            "/auth/login",
            json.dumps({"publisherId": "3567", "apiKey": "k", "apiSecret": "s"}),
            content_type="application/json",
        )
        import time as _time
        session = self.c.session
        session["session_ttl_seconds"] = 1 * 24 * 3600
        session["session_created_at"] = int(_time.time()) - (2 * 24 * 3600)
        session.save()

        resp = self.c.post("/mcp", "{}", content_type="application/json")
        self.assertEqual(resp.status_code, 401)
        data = resp.json()
        self.assertEqual(data.get("error"), "SESSION_EXPIRED")
