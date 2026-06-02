import secrets
from datetime import timedelta
from io import StringIO

from django.core.management import call_command
from django.test import TestCase
from django.utils import timezone

from auth_app.models import OAuthClient, OAuthCode, OAuthToken


def _future(days=30):
    return timezone.now() + timedelta(days=days)


def _past(days=1):
    return timezone.now() - timedelta(days=days)


class CleanupExpiredTokensTests(TestCase):
    def _run(self):
        out = StringIO()
        call_command("cleanup_expired_tokens", stdout=out)
        return out.getvalue()

    def test_deletes_expired_codes(self):
        OAuthCode.objects.create(
            code=secrets.token_urlsafe(32),
            client_id="c1",
            redirect_uri="https://example.com",
            code_challenge="x",
            credentials={},
            expires_at=_past(),
        )
        self._run()
        self.assertEqual(OAuthCode.objects.count(), 0)

    def test_keeps_unexpired_codes(self):
        OAuthCode.objects.create(
            code=secrets.token_urlsafe(32),
            client_id="c1",
            redirect_uri="https://example.com",
            code_challenge="x",
            credentials={},
            expires_at=_future(),
        )
        self._run()
        self.assertEqual(OAuthCode.objects.count(), 1)

    def test_deletes_expired_tokens(self):
        OAuthToken.objects.create(
            token=secrets.token_urlsafe(32),
            client_id="c1",
            credentials={},
            expires_at=_past(),
        )
        self._run()
        self.assertEqual(OAuthToken.objects.count(), 0)

    def test_keeps_unexpired_tokens(self):
        OAuthToken.objects.create(
            token=secrets.token_urlsafe(32),
            client_id="c1",
            credentials={},
            expires_at=_future(),
        )
        self._run()
        self.assertEqual(OAuthToken.objects.count(), 1)

    def test_deletes_expired_clients_with_expires_at(self):
        OAuthClient.objects.create(
            client_id="expired_client",
            redirect_uris=[],
            expires_at=_past(),
        )
        self._run()
        self.assertFalse(OAuthClient.objects.filter(client_id="expired_client").exists())

    def test_keeps_clients_without_expires_at(self):
        OAuthClient.objects.create(
            client_id="no_expiry_client",
            redirect_uris=[],
            expires_at=None,
        )
        self._run()
        self.assertTrue(OAuthClient.objects.filter(client_id="no_expiry_client").exists())

    def test_keeps_unexpired_clients(self):
        OAuthClient.objects.create(
            client_id="valid_client",
            redirect_uris=[],
            expires_at=_future(days=90),
        )
        self._run()
        self.assertTrue(OAuthClient.objects.filter(client_id="valid_client").exists())

    def test_mixed_records_only_deletes_expired(self):
        OAuthToken.objects.create(token=secrets.token_urlsafe(32), client_id="a", credentials={}, expires_at=_past())
        OAuthToken.objects.create(token=secrets.token_urlsafe(32), client_id="b", credentials={}, expires_at=_future())
        OAuthCode.objects.create(code=secrets.token_urlsafe(32), client_id="a", redirect_uri="x", code_challenge="y", credentials={}, expires_at=_past())
        OAuthCode.objects.create(code=secrets.token_urlsafe(32), client_id="b", redirect_uri="x", code_challenge="y", credentials={}, expires_at=_future())

        self._run()

        self.assertEqual(OAuthToken.objects.count(), 1)
        self.assertEqual(OAuthCode.objects.count(), 1)

    def test_output_reports_counts(self):
        OAuthToken.objects.create(token=secrets.token_urlsafe(32), client_id="a", credentials={}, expires_at=_past())
        OAuthCode.objects.create(code=secrets.token_urlsafe(32), client_id="a", redirect_uri="x", code_challenge="y", credentials={}, expires_at=_past())
        output = self._run()
        self.assertIn("1 expired code", output)
        self.assertIn("1 expired token", output)
