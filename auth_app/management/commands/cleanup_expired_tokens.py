from django.core.management.base import BaseCommand
from django.utils import timezone

from auth_app.models import OAuthClient, OAuthCode, OAuthToken


class Command(BaseCommand):
    help = "Delete expired OAuth codes, tokens, and clients from the database."

    def handle(self, *args, **options):
        now = timezone.now()

        codes, _ = OAuthCode.objects.filter(expires_at__lt=now).delete()
        tokens, _ = OAuthToken.objects.filter(expires_at__lt=now).delete()
        # expires_at is nullable on OAuthClient; only delete rows with an explicit expiry
        clients, _ = OAuthClient.objects.filter(
            expires_at__isnull=False, expires_at__lt=now
        ).delete()

        self.stdout.write(
            self.style.SUCCESS(
                f"Cleaned up: {codes} expired code(s), "
                f"{tokens} expired token(s), "
                f"{clients} expired client(s)"
            )
        )
