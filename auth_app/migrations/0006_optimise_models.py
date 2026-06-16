"""
Optimise OAuthClient:
  - Replace redirect_uris (JSONField list) with redirect_uri (single CharField)
  - Remove expires_at (client registrations are now permanent)
  - Add db_index to client_id (was unique but no explicit index)

Add OAuthToken.created_at for audit trail.
Add OAuth_Pkce_Code.client_id db_index for faster exchange lookups.
"""
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("auth_app", "0005_aiclient"),
    ]

    operations = [
        # OAuthClient: replace redirect_uris list + expires_at with single redirect_uri
        migrations.AddField(
            model_name="oauthclient",
            name="redirect_uri",
            field=models.CharField(max_length=512, blank=True, default=""),
        ),
        migrations.RemoveField(
            model_name="oauthclient",
            name="redirect_uris",
        ),
        migrations.RemoveField(
            model_name="oauthclient",
            name="expires_at",
        ),
        # OAuthToken: add created_at for audit
        migrations.AddField(
            model_name="oauthtoken",
            name="created_at",
            field=models.DateTimeField(auto_now_add=True, null=True),
        ),
        # OAuth_Pkce_Code: add db_index on client_id for faster exchange lookups
        migrations.AlterField(
            model_name="OAuth_Pkce_Code",
            name="client_id",
            field=models.CharField(max_length=64, db_index=True),
        ),
    ]
