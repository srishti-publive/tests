"""Drop orphan per-field encrypted columns left by an earlier, rewritten migration.

An older iteration of the schema stored credentials as separate encrypted columns
(encrypted_api_secret, encrypted_api_key, encrypted_publisher_id) before they were
consolidated into the single `credentials` EncryptedJSONField. The migration that
created those columns was later rewritten/removed, so no current migration manages
them — yet databases that ran the old migration (the committed SQLite db and the
Railway Postgres) still carry them. Because they are NOT NULL and the model never
populates them, every INSERT into oauth_code / oauth_token fails:

    NOT NULL constraint failed: oauth_code.encrypted_api_secret

This migration removes them if present. It introspects each table first and only
issues a plain ALTER TABLE ... DROP COLUMN for columns that actually exist —
SQLite does not support DROP COLUMN IF EXISTS, so we cannot rely on that clause.
Plain DROP COLUMN is supported by both PostgreSQL and SQLite >= 3.35 (Django 4.2's
minimum). On a clean database this is a no-op.
"""
from django.db import migrations

_ORPHAN_COLUMNS = ("encrypted_api_secret", "encrypted_api_key", "encrypted_publisher_id")
_TABLES = ("oauth_code", "oauth_token")


def _drop_orphans(apps, schema_editor):
    connection = schema_editor.connection
    quote = connection.ops.quote_name
    with connection.cursor() as cursor:
        existing_tables = set(connection.introspection.table_names(cursor))
        for table in _TABLES:
            if table not in existing_tables:
                continue
            present = {
                col.name
                for col in connection.introspection.get_table_description(cursor, table)
            }
            for column in _ORPHAN_COLUMNS:
                if column in present:
                    cursor.execute(
                        f"ALTER TABLE {quote(table)} DROP COLUMN {quote(column)};"
                    )


class Migration(migrations.Migration):

    dependencies = [
        ("auth_app", "0010_delete_aiclient_alter_oauthtoken_options_and_more"),
    ]

    operations = [
        # State is already correct (these columns are not in any model); this only
        # reconciles the database. reverse is a no-op — we never want them back.
        migrations.RunPython(_drop_orphans, migrations.RunPython.noop),
    ]
