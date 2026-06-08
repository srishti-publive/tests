"""Revert `credentials` storage from a TEXT column (formerly Fernet-encrypted via
EncryptedJSONField) back to plain JSONField.

Live rows may hold Fernet-encrypted strings, which are not valid JSON, so they
can't be cast in place. Per project decision, this data is sample/test only —
the migration clears both tables before changing the column type, forcing a
fresh OAuth handshake for any existing clients.
"""
from django.db import migrations, models


def _clear_credentials(apps, schema_editor):
    OAuthCode = apps.get_model("auth_app", "OAuthCode")
    OAuthToken = apps.get_model("auth_app", "OAuthToken")
    OAuthCode.objects.all().delete()
    OAuthToken.objects.all().delete()


class Migration(migrations.Migration):

    dependencies = [
        ("auth_app", "0011_drop_orphan_encrypted_columns"),
    ]

    operations = [
        migrations.RunPython(_clear_credentials, migrations.RunPython.noop),
        migrations.AlterField(
            model_name="oauthcode",
            name="credentials",
            field=models.JSONField(),
        ),
        migrations.AlterField(
            model_name="oauthtoken",
            name="credentials",
            field=models.JSONField(),
        ),
    ]
