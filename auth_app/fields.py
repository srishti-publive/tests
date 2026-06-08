"""Custom Django model field — stores JSON dicts as Fernet-encrypted TEXT at rest.

Usage is transparent: assign/read a plain dict, encryption happens automatically.
Because the underlying column is TEXT (not JSONB), ORM JSON-path lookups
(credentials__publisherId=...) are not supported — use a separate indexed column instead.
"""
import json
import logging

from django.db import models

from .crypto import InvalidToken, decrypt_json, encrypt_json

logger = logging.getLogger(__name__)


class EncryptedJSONField(models.TextField):
    """Stores a dict as Fernet-encrypted text. Python callers always see a plain dict."""

    def from_db_value(self, value, expression, connection):
        if value is None:
            return None
        try:
            return decrypt_json(value)
        except (InvalidToken, json.JSONDecodeError):
            # Wrong/rotated encryption key, or a corrupted row — treat it the
            # same way auth_app.services.get_session_credentials treats a bad
            # session: as absent rather than a hard 500, so callers fall through
            # to the normal "unauthenticated" path and the user is asked to re-auth.
            logger.warning("EncryptedJSONField: failed to decrypt stored value — treating as absent")
            return None

    def to_python(self, value):
        if value is None or isinstance(value, dict):
            return value
        try:
            return decrypt_json(value)
        except (InvalidToken, json.JSONDecodeError):
            # Migration-window fallback: treat as legacy plaintext JSON
            return json.loads(value)

    def get_prep_value(self, value):
        if value is None:
            return None
        if isinstance(value, dict):
            return encrypt_json(value)
        return super().get_prep_value(value)
