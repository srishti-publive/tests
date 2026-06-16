"""Add publisher_id index to OAuthToken and move `credentials` to a plain TEXT column.

Order of operations:
  1. Add publisher_id column to oauth_token (indexed, blank-safe default).
  2. Alter credentials columns from JSONField (JSONB/TEXT) to TextField (TEXT).
  3. Data migration: populate publisher_id from existing credentials JSON.

(Storage at rest was briefly routed through a Fernet-encrypting field here; that
layer was removed — see migration 0012 — so this just records the TEXT column
shape the live databases already carry.)
"""
import json
import logging

from django.db import migrations, models

logger = logging.getLogger(__name__)


def populate_publisher_id(apps, schema_editor):
    """Read JSON credentials from DB and populate the new publisher_id column."""
    with schema_editor.connection.cursor() as cursor:
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
            cursor.execute(
                "UPDATE oauth_token SET publisher_id = %s WHERE id = %s",
                [publisher_id, row_id],
            )


class Migration(migrations.Migration):

    dependencies = [
        ("auth_app", "0007_remove_aiclient"),
    ]

    operations = [
        # 1. Add publisher_id column to OAuthToken for direct indexed lookups
        #    (replaces credentials__publisherId JSON-path ORM queries which are
        #    incompatible with TextField storage)
        migrations.AddField(
            model_name="oauthtoken",
            name="publisher_id",
            field=models.CharField(blank=True, db_index=True, default="", max_length=64),
        ),
        # 2. Change credentials column types from JSONField to TextField (TEXT)
        migrations.AlterField(
            model_name="oauthcode",
            name="credentials",
            field=models.TextField(),
        ),
        migrations.AlterField(
            model_name="oauthtoken",
            name="credentials",
            field=models.TextField(),
        ),
        # 3. Populate publisher_id from existing credentials JSON
        migrations.RunPython(populate_publisher_id, migrations.RunPython.noop),
    ]
