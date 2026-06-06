"""Encrypt credentials at rest and add publisher_id index to OAuthToken.

Order of operations:
  1. Add publisher_id column to oauth_token (indexed, blank-safe default).
  2. Alter credentials columns from JSONField (JSONB/TEXT) to EncryptedJSONField (TEXT).
  3. Data migration: encrypt all existing plaintext JSON credentials and populate publisher_id.

Production prerequisite:
  CREDENTIALS_ENCRYPTION_KEY must be set before running this migration.
  Generate a key:
      python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
"""
import json
import logging
import os

from django.db import migrations, models

import auth_app.fields

logger = logging.getLogger(__name__)


def encrypt_existing_credentials(apps, schema_editor):
    """Read plaintext JSON credentials from DB, encrypt them in-place, populate publisher_id."""
    from cryptography.fernet import Fernet

    key = os.environ.get("CREDENTIALS_ENCRYPTION_KEY", "")
    if not key:
        logger.warning(
            "CREDENTIALS_ENCRYPTION_KEY is not set during migration. "
            "Using an ephemeral key — credentials will be unreadable after restart. "
            "Set CREDENTIALS_ENCRYPTION_KEY in production before running this migration."
        )
        key = Fernet.generate_key().decode()

    fernet = Fernet(key.encode() if isinstance(key, str) else key)

    def encrypt_raw(raw):
        if raw is None:
            return None
        if isinstance(raw, dict):
            raw = json.dumps(raw, separators=(",", ":"))
        return fernet.encrypt(raw.encode()).decode()

    with schema_editor.connection.cursor() as cursor:
        # OAuthToken — encrypt credentials and populate publisher_id
        cursor.execute("SELECT id, credentials FROM oauth_token")
        rows = cursor.fetchall()
        for row_id, raw in rows:
            if raw is None:
                continue
            try:
                creds_dict = raw if isinstance(raw, dict) else json.loads(raw)
                publisher_id = creds_dict.get("publisherId", "")
            except Exception:
                publisher_id = ""
            encrypted = encrypt_raw(raw)
            cursor.execute(
                "UPDATE oauth_token SET credentials = %s, publisher_id = %s WHERE id = %s",
                [encrypted, publisher_id, row_id],
            )

        # OAuthCode — encrypt credentials only
        cursor.execute("SELECT id, credentials FROM oauth_code")
        rows = cursor.fetchall()
        for row_id, raw in rows:
            if raw is None:
                continue
            encrypted = encrypt_raw(raw)
            cursor.execute(
                "UPDATE oauth_code SET credentials = %s WHERE id = %s",
                [encrypted, row_id],
            )


class Migration(migrations.Migration):

    dependencies = [
        ("auth_app", "0007_remove_aiclient"),
    ]

    operations = [
        # 1. Add publisher_id column to OAuthToken for direct indexed lookups
        #    (replaces credentials__publisherId JSON-path ORM queries which are
        #    incompatible with encrypted TextField storage)
        migrations.AddField(
            model_name="oauthtoken",
            name="publisher_id",
            field=models.CharField(blank=True, db_index=True, default="", max_length=64),
        ),
        # 2. Change credentials column types from JSONField to EncryptedJSONField (TEXT)
        migrations.AlterField(
            model_name="oauthcode",
            name="credentials",
            field=auth_app.fields.EncryptedJSONField(),
        ),
        migrations.AlterField(
            model_name="oauthtoken",
            name="credentials",
            field=auth_app.fields.EncryptedJSONField(),
        ),
        # 3. Encrypt all existing plaintext JSON and populate publisher_id
        migrations.RunPython(encrypt_existing_credentials, migrations.RunPython.noop),
    ]
