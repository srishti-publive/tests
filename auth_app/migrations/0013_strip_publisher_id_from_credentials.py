"""
Data migration: remove redundant publisherId from OAuthToken.credentials JSON.
publisherId is now sourced exclusively from the flat publisher_id column.
"""
from django.db import migrations


def strip_publisher_id(apps, schema_editor):
    OAuthToken = apps.get_model("auth_app", "OAuthToken")
    tokens = OAuthToken.objects.exclude(credentials={}).filter(
        credentials__has_key="publisherId"
    )
    for token in tokens.iterator():
        token.credentials.pop("publisherId", None)
        token.save(update_fields=["credentials"])


class Migration(migrations.Migration):

    dependencies = [
        ("auth_app", "0012_remove_credentials_encryption"),
    ]

    operations = [
        migrations.RunPython(strip_publisher_id, migrations.RunPython.noop),
    ]
