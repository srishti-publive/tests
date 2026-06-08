import os

from django.apps import AppConfig
from django.conf import settings
from django.core.checks import Error, register


class AuthAppConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "auth_app"

    def ready(self):
        register(check_credentials_encryption_key)


def check_credentials_encryption_key(app_configs, **kwargs):
    """Refuse to boot in production without a real encryption key.

    auth_app.crypto.get_fernet() silently falls back to a random ephemeral key
    when CREDENTIALS_ENCRYPTION_KEY is unset — the server starts fine, but every
    stored OAuthToken/OAuthCode/session credential becomes permanently
    unreadable on the next restart (silent mass logout, no error at the source).
    Catching it here, once, at startup costs nothing at request time.
    """
    if settings.DEBUG:
        return []

    if os.environ.get("CREDENTIALS_ENCRYPTION_KEY"):
        return []

    return [
        Error(
            "CREDENTIALS_ENCRYPTION_KEY is not set.",
            hint=(
                "Without it, encrypted credentials become unreadable after every "
                "restart. Generate one with: python -c \"from cryptography.fernet "
                "import Fernet; print(Fernet.generate_key().decode())\" and set it "
                "in the environment before starting the server."
            ),
            id="auth_app.E001",
        )
    ]
