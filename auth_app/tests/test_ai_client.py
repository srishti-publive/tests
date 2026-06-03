"""Tests for the AI client direct-registration auth flow.

Covers:
• Self-registration happy path
• Rate limiting (6th registration from same IP is rejected)
• Invalid request (missing client_name)
• MCP auth with valid client_id → proceeds
• MCP auth with unknown client_id (UUID format) → INVALID_CLIENT_ID
• MCP auth with admin-blocked client_id → CLIENT_BLOCKED
• Admin list / block / unblock / delete — with and without admin credential
"""
import json
import uuid
from datetime import timedelta
from unittest.mock import patch

from django.core.cache import cache
from django.test import Client, TestCase, override_settings
from django.utils import timezone

from auth_app.models import AIClient


def _make_client(status=AIClient.STATUS_ACTIVE, credentials=None):
    return AIClient.objects.create(
        client_name="Test Bot",
        contact="bot@example.com",
        status=status,
        credentials=credentials or {"publisherId": "3567", "apiKey": "k", "apiSecret": "s"},
        registration_ip="127.0.0.1",
    )


# ── Registration ──────────────────────────────────────────────────────────────

class AIClientRegistrationTests(TestCase):
    def setUp(self):
        self.c = Client()
        # Clear rate-limit counters between tests.
        cache.clear()

    def _register(self, body=None, ip="1.2.3.4"):
        payload = body if body is not None else {"client_name": "My Bot"}
        return self.c.post(
            "/ai/register",
            json.dumps(payload),
            content_type="application/json",
            REMOTE_ADDR=ip,
        )

    def test_registration_happy_path(self):
        resp = self._register({"client_name": "My Bot", "contact": "me@example.com"})
        self.assertEqual(resp.status_code, 201)
        data = resp.json()
        self.assertIn("client_id", data)
        self.assertIn("issued_at", data)
        self.assertEqual(data["client_name"], "My Bot")
        # Must be valid UUID v4
        parsed = uuid.UUID(data["client_id"])
        self.assertEqual(parsed.version, 4)
        # Must be persisted
        self.assertTrue(AIClient.objects.filter(client_id=data["client_id"]).exists())

    def test_registration_stores_registration_ip(self):
        resp = self._register(ip="10.0.0.1")
        client_id = resp.json()["client_id"]
        ai_client = AIClient.objects.get(client_id=client_id)
        self.assertEqual(ai_client.registration_ip, "10.0.0.1")

    def test_registration_missing_client_name_returns_400(self):
        resp = self._register({"contact": "x@x.com"})
        self.assertEqual(resp.status_code, 400)
        self.assertIn("error", resp.json())

    def test_registration_only_post_allowed(self):
        resp = self.c.get("/ai/register")
        self.assertEqual(resp.status_code, 405)

    @patch("auth_app.views.validate_cds_credentials", return_value=(True, 200))
    def test_registration_with_valid_credentials(self, _mock):
        resp = self._register({
            "client_name": "My Bot",
            "publisher_id": "3567",
            "api_key": "k",
            "api_secret": "s",
        })
        self.assertEqual(resp.status_code, 201)
        client_id = resp.json()["client_id"]
        ai_client = AIClient.objects.get(client_id=client_id)
        self.assertIsNotNone(ai_client.credentials)
        self.assertEqual(ai_client.credentials["publisherId"], "3567")

    @patch("auth_app.views.validate_cds_credentials", return_value=(False, 401))
    def test_registration_with_invalid_credentials_rejected(self, _mock):
        resp = self._register({
            "client_name": "My Bot",
            "publisher_id": "3567",
            "api_key": "bad",
            "api_secret": "bad",
        })
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(resp.json()["error"], "invalid_credentials")

    def test_rate_limit_allows_5_registrations(self):
        for i in range(5):
            resp = self._register({"client_name": f"Bot {i}"}, ip="9.9.9.9")
            self.assertEqual(resp.status_code, 201, f"Registration {i+1} should succeed")

    def test_rate_limit_blocks_6th_registration(self):
        for _ in range(5):
            self._register({"client_name": "Bot"}, ip="8.8.8.8")
        resp = self._register({"client_name": "Bot 6"}, ip="8.8.8.8")
        self.assertEqual(resp.status_code, 429)
        self.assertEqual(resp.json()["error"], "rate_limited")

    def test_rate_limit_is_per_ip(self):
        """Rate limit on 8.8.8.8 must not affect 7.7.7.7."""
        for _ in range(5):
            self._register({"client_name": "Bot"}, ip="8.8.8.8")
        resp = self._register({"client_name": "Other IP Bot"}, ip="7.7.7.7")
        self.assertEqual(resp.status_code, 201)


# ── MCP auth with AIClient bearer tokens ─────────────────────────────────────

class AIClientMCPAuthTests(TestCase):
    def setUp(self):
        self.c = Client()

    def _mcp_post(self, client_id):
        return self.c.post(
            "/mcp",
            json.dumps({
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2024-11-05",
                    "capabilities": {},
                    "clientInfo": {"name": "test", "version": "1.0"},
                },
            }),
            content_type="application/json",
            HTTP_AUTHORIZATION=f"Bearer {client_id}",
        )

    def test_valid_active_client_id_passes_auth(self):
        ai_client = _make_client()
        resp = self._mcp_post(str(ai_client.client_id))
        # Auth passes — should NOT be 401
        self.assertNotEqual(resp.status_code, 401)

    def test_valid_active_client_updates_last_seen_at(self):
        ai_client = _make_client()
        self.assertIsNone(ai_client.last_seen_at)
        self._mcp_post(str(ai_client.client_id))
        ai_client.refresh_from_db()
        self.assertIsNotNone(ai_client.last_seen_at)

    def test_blocked_client_returns_401_with_client_blocked(self):
        ai_client = _make_client(status=AIClient.STATUS_BLOCKED)
        resp = self._mcp_post(str(ai_client.client_id))
        self.assertEqual(resp.status_code, 401)
        data = resp.json()
        self.assertEqual(data["error"], "CLIENT_BLOCKED")

    def test_unknown_uuid_returns_401_with_invalid_client_id(self):
        unknown_uuid = str(uuid.uuid4())
        resp = self._mcp_post(unknown_uuid)
        self.assertEqual(resp.status_code, 401)
        data = resp.json()
        self.assertEqual(data["error"], "INVALID_CLIENT_ID")

    def test_blocked_response_has_www_authenticate_header(self):
        ai_client = _make_client(status=AIClient.STATUS_BLOCKED)
        resp = self._mcp_post(str(ai_client.client_id))
        self.assertIn("WWW-Authenticate", resp)
        self.assertIn("Bearer", resp["WWW-Authenticate"])

    def test_invalid_client_response_has_www_authenticate_header(self):
        resp = self._mcp_post(str(uuid.uuid4()))
        self.assertIn("WWW-Authenticate", resp)


# ── Admin endpoints ───────────────────────────────────────────────────────────

@override_settings(ADMIN_SECRET_KEY="test-admin-secret-xyz")
class AdminClientTests(TestCase):
    def setUp(self):
        self.c = Client()
        self.ai_client = _make_client()
        self.admin_headers = {"HTTP_AUTHORIZATION": "Bearer test-admin-secret-xyz"}

    # List

    def test_list_clients_with_valid_admin_key(self):
        resp = self.c.get("/admin/clients", **self.admin_headers)
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertIn("clients", data)
        self.assertEqual(data["count"], 1)
        client_data = data["clients"][0]
        self.assertEqual(client_data["client_id"], str(self.ai_client.client_id))
        self.assertEqual(client_data["status"], "active")
        self.assertIn("registered_at", client_data)
        self.assertIn("registration_ip", client_data)

    def test_list_clients_without_admin_key_returns_401(self):
        resp = self.c.get("/admin/clients")
        self.assertEqual(resp.status_code, 401)

    def test_list_clients_with_wrong_admin_key_returns_401(self):
        resp = self.c.get("/admin/clients", HTTP_AUTHORIZATION="Bearer wrong-key")
        self.assertEqual(resp.status_code, 401)
        self.assertEqual(resp.json()["error"], "unauthorized")

    def test_list_only_get_allowed(self):
        resp = self.c.post("/admin/clients", **self.admin_headers)
        self.assertEqual(resp.status_code, 405)

    # Block

    def test_block_client_with_valid_admin_key(self):
        resp = self.c.post(
            f"/admin/clients/{self.ai_client.client_id}/block",
            **self.admin_headers,
        )
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["status"], "blocked")
        self.ai_client.refresh_from_db()
        self.assertEqual(self.ai_client.status, AIClient.STATUS_BLOCKED)

    def test_block_client_without_admin_key_returns_401(self):
        resp = self.c.post(f"/admin/clients/{self.ai_client.client_id}/block")
        self.assertEqual(resp.status_code, 401)

    def test_block_nonexistent_client_returns_404(self):
        resp = self.c.post(
            f"/admin/clients/{uuid.uuid4()}/block",
            **self.admin_headers,
        )
        self.assertEqual(resp.status_code, 404)

    # Unblock

    def test_unblock_client(self):
        self.ai_client.status = AIClient.STATUS_BLOCKED
        self.ai_client.save()
        resp = self.c.post(
            f"/admin/clients/{self.ai_client.client_id}/unblock",
            **self.admin_headers,
        )
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["status"], "active")
        self.ai_client.refresh_from_db()
        self.assertEqual(self.ai_client.status, AIClient.STATUS_ACTIVE)

    def test_unblock_client_without_admin_key_returns_401(self):
        resp = self.c.post(f"/admin/clients/{self.ai_client.client_id}/unblock")
        self.assertEqual(resp.status_code, 401)

    # Delete

    def test_delete_client_with_valid_admin_key(self):
        client_id = self.ai_client.client_id
        resp = self.c.delete(
            f"/admin/clients/{client_id}",
            **self.admin_headers,
        )
        self.assertEqual(resp.status_code, 200)
        self.assertFalse(AIClient.objects.filter(client_id=client_id).exists())

    def test_delete_client_without_admin_key_returns_401(self):
        resp = self.c.delete(f"/admin/clients/{self.ai_client.client_id}")
        self.assertEqual(resp.status_code, 401)

    def test_delete_nonexistent_client_returns_404(self):
        resp = self.c.delete(
            f"/admin/clients/{uuid.uuid4()}",
            **self.admin_headers,
        )
        self.assertEqual(resp.status_code, 404)

    # Block takes effect immediately on next request

    def test_blocked_client_rejected_immediately(self):
        # Block via admin endpoint
        self.c.post(
            f"/admin/clients/{self.ai_client.client_id}/block",
            **self.admin_headers,
        )
        # Next MCP request must fail with CLIENT_BLOCKED
        resp = self.c.post(
            "/mcp",
            json.dumps({
                "jsonrpc": "2.0", "id": 1, "method": "initialize",
                "params": {"protocolVersion": "2024-11-05", "capabilities": {}, "clientInfo": {"name": "t", "version": "1"}},
            }),
            content_type="application/json",
            HTTP_AUTHORIZATION=f"Bearer {self.ai_client.client_id}",
        )
        self.assertEqual(resp.status_code, 401)
        self.assertEqual(resp.json()["error"], "CLIENT_BLOCKED")


@override_settings(ADMIN_SECRET_KEY="")
class AdminNotConfiguredTests(TestCase):
    def test_admin_returns_503_when_key_not_set(self):
        resp = Client().get("/admin/clients")
        self.assertEqual(resp.status_code, 503)
        self.assertEqual(resp.json()["error"], "admin_not_configured")
