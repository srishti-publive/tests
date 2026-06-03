import json
from datetime import timedelta
from unittest.mock import patch

from django.test import TestCase, Client
from django.utils import timezone


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
        self.assertEqual(session["credentials"]["publisherId"], "3567")

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

    # ── Session duration picker ───────────────────────────────────────────────

    @patch("auth_app.views.validate_cds_credentials", return_value=(True, 200))
    def test_login_default_is_90_days(self, _mock):
        """When remember_for_days is omitted the default must be 90 days."""
        self.c.post(
            "/auth/login",
            json.dumps({"publisherId": "3567", "apiKey": "k", "apiSecret": "s"}),
            content_type="application/json",
        )
        session = self.c.session
        self.assertEqual(session["remember_for_days"], 90)
        self.assertEqual(session["session_ttl_seconds"], 90 * 24 * 3600)

    @patch("auth_app.views.validate_cds_credentials", return_value=(True, 200))
    def test_login_always_option(self, _mock):
        """remember_for_days=-1 means 'Always' — ttl_seconds must be -1 (never expires)."""
        self.c.post(
            "/auth/login",
            json.dumps({"publisherId": "3567", "apiKey": "k", "apiSecret": "s", "remember_for_days": -1}),
            content_type="application/json",
        )
        session = self.c.session
        self.assertEqual(session["remember_for_days"], -1)
        self.assertEqual(session["session_ttl_seconds"], -1)

    @patch("auth_app.views.validate_cds_credentials", return_value=(True, 200))
    def test_login_session_only_option(self, _mock):
        """remember_for_days=0 means 'This session only' — ttl_seconds must be 0."""
        self.c.post(
            "/auth/login",
            json.dumps({"publisherId": "3567", "apiKey": "k", "apiSecret": "s", "remember_for_days": 0}),
            content_type="application/json",
        )
        session = self.c.session
        self.assertEqual(session["remember_for_days"], 0)
        self.assertEqual(session["session_ttl_seconds"], 0)

    @patch("auth_app.views.validate_cds_credentials", return_value=(True, 200))
    def test_login_custom_days(self, _mock):
        """Any positive integer is a valid custom duration."""
        self.c.post(
            "/auth/login",
            json.dumps({"publisherId": "3567", "apiKey": "k", "apiSecret": "s", "remember_for_days": 14}),
            content_type="application/json",
        )
        session = self.c.session
        self.assertEqual(session["remember_for_days"], 14)
        self.assertEqual(session["session_ttl_seconds"], 14 * 24 * 3600)

    @patch("auth_app.views.validate_cds_credentials", return_value=(True, 200))
    def test_login_invalid_remember_for_days_defaults_to_90(self, _mock):
        """Invalid/negative durations (other than -1) fall back to 90 days."""
        self.c.post(
            "/auth/login",
            json.dumps({"publisherId": "3567", "apiKey": "k", "apiSecret": "s", "remember_for_days": -99}),
            content_type="application/json",
        )
        self.assertEqual(self.c.session["remember_for_days"], 90)

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
    def test_expired_session_returns_session_expired_at_status(self, _mock):
        """auth_status must flush an expired session and return SESSION_EXPIRED."""
        self.c.post(
            "/auth/login",
            json.dumps({"publisherId": "3567", "apiKey": "k", "apiSecret": "s", "remember_for_days": 1}),
            content_type="application/json",
        )
        # Backdate session_created_at (int epoch) so the 1-day TTL is exceeded.
        import time as _time
        session = self.c.session
        session["session_created_at"] = int(_time.time()) - (2 * 24 * 3600)
        session.save()

        resp = self.c.get("/auth/status")
        data = resp.json()
        self.assertFalse(data["authenticated"])
        self.assertEqual(data.get("error"), "SESSION_EXPIRED")

    @patch("auth_app.views.validate_cds_credentials", return_value=(True, 200))
    def test_always_session_never_expires(self, _mock):
        """ttl_seconds=-1 ('Always') must never trigger SESSION_EXPIRED."""
        self.c.post(
            "/auth/login",
            json.dumps({"publisherId": "3567", "apiKey": "k", "apiSecret": "s", "remember_for_days": -1}),
            content_type="application/json",
        )
        # Backdate far into the past (int epoch) — should still be authenticated.
        import time as _time
        session = self.c.session
        session["session_created_at"] = int(_time.time()) - (3650 * 24 * 3600)
        session.save()

        resp = self.c.get("/auth/status")
        self.assertTrue(resp.json()["authenticated"])

    @patch("auth_app.views.validate_cds_credentials", return_value=(True, 200))
    def test_expired_session_returns_session_expired_at_mcp(self, _mock):
        """MCP endpoint must return 401 with SESSION_EXPIRED reason for expired sessions."""
        self.c.post(
            "/auth/login",
            json.dumps({"publisherId": "3567", "apiKey": "k", "apiSecret": "s", "remember_for_days": 1}),
            content_type="application/json",
        )
        import time as _time
        session = self.c.session
        session["session_created_at"] = int(_time.time()) - (2 * 24 * 3600)
        session.save()

        resp = self.c.post("/mcp", "{}", content_type="application/json")
        self.assertEqual(resp.status_code, 401)
        data = resp.json()
        self.assertEqual(data.get("error"), "SESSION_EXPIRED")

    # ── Legacy field compat ───────────────────────────────────────────────────

    @patch("auth_app.views.validate_cds_credentials", return_value=(True, 200))
    def test_remember_for_days_stored_in_session(self, _mock):
        """Existing callers that read remember_for_days must still find it."""
        self.c.post(
            "/auth/login",
            json.dumps({"publisherId": "3567", "apiKey": "k", "apiSecret": "s", "remember_for_days": 7}),
            content_type="application/json",
        )
        self.assertEqual(self.c.session["remember_for_days"], 7)
