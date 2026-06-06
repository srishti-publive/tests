from datetime import timedelta
from io import StringIO
import secrets

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

    def test_tokens_are_never_deleted(self):
        """OAuthTokens are permanent — cleanup never touches them."""
        OAuthToken.objects.create(token=secrets.token_urlsafe(32), client_id="c1", credentials={})
        self._run()
        self.assertEqual(OAuthToken.objects.count(), 1)

    def test_clients_are_never_deleted(self):
        """OAuthClient registrations are permanent — cleanup never touches them."""
        OAuthClient.objects.create(client_id="permanent_client")
        self._run()
        self.assertTrue(OAuthClient.objects.filter(client_id="permanent_client").exists())

    def test_mixed_records_only_deletes_expired_codes(self):
        OAuthToken.objects.create(token=secrets.token_urlsafe(32), client_id="a", credentials={})
        OAuthToken.objects.create(token=secrets.token_urlsafe(32), client_id="b", credentials={})
        OAuthCode.objects.create(code=secrets.token_urlsafe(32), client_id="a", redirect_uri="x", code_challenge="y", credentials={}, expires_at=_past())
        OAuthCode.objects.create(code=secrets.token_urlsafe(32), client_id="b", redirect_uri="x", code_challenge="y", credentials={}, expires_at=_future())

        self._run()

        self.assertEqual(OAuthToken.objects.count(), 2)
        self.assertEqual(OAuthCode.objects.count(), 1)

    def test_output_reports_counts(self):
        OAuthCode.objects.create(code=secrets.token_urlsafe(32), client_id="a", redirect_uri="x", code_challenge="y", credentials={}, expires_at=_past())
        output = self._run()
        self.assertIn("1 expired code", output)
