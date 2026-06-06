from django.core.management.base import BaseCommand
from django.utils import timezone

from auth_app.models import OAuthCode


class Command(BaseCommand):
    help = "Delete expired OAuth authorization codes from the database."

    def handle(self, *args, **options):
        now = timezone.now()

        codes, _ = OAuthCode.objects.filter(expires_at__lt=now).delete()
        # OAuthTokens and OAuthClients are permanent — nothing to clean up.

        self.stdout.write(
            self.style.SUCCESS(
                f"Cleaned up: {codes} expired code(s)"
            )
        )
