import json
from unittest.mock import patch

from django.test import TestCase, Client


class SessionAuthTests(TestCase):
    def setUp(self):
        self.c = Client()

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

    @patch("auth_app.views.validate_cds_credentials", return_value=(True, 200))
    def test_remember_for_days_stored_in_session(self, _mock):
        self.c.post(
            "/auth/login",
            json.dumps({"publisherId": "3567", "apiKey": "k", "apiSecret": "s", "remember_for_days": 7}),
            content_type="application/json",
        )
        self.assertEqual(self.c.session["remember_for_days"], 7)

    @patch("auth_app.views.validate_cds_credentials", return_value=(True, 200))
    def test_invalid_remember_for_days_defaults_to_30(self, _mock):
        self.c.post(
            "/auth/login",
            json.dumps({"publisherId": "3567", "apiKey": "k", "apiSecret": "s", "remember_for_days": 999}),
            content_type="application/json",
        )
        self.assertEqual(self.c.session["remember_for_days"], 30)
