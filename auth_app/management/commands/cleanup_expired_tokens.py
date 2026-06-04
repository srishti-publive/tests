from django.core.management.base import BaseCommand
from django.utils import timezone

from auth_app.models import OAuthClient, OAuthCode, OAuthToken


class Command(BaseCommand):
    help = "Delete expired OAuth codes, tokens, and clients from the database."

    def handle(self, *args, **options):
        now = timezone.now()

        codes, _  = OAuthCode.objects.filter(expires_at__lt=now).delete()
        tokens, _ = OAuthToken.objects.filter(expires_at__lt=now).delete()
        # OAuthClients are now permanent — no expires_at field, nothing to clean up.

        self.stdout.write(
            self.style.SUCCESS(
                f"Cleaned up: {codes} expired code(s), {tokens} expired token(s)"
            )
        )
