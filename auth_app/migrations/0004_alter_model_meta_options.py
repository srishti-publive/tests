from django.db import migrations


class Migration(migrations.Migration):

    dependencies = [
        ("auth_app", "0003_oauthclient_expires_at_oauthtoken_refresh_token"),
    ]

    operations = [
        migrations.AlterModelOptions(
            name="oauthclient",
            options={
                "db_table": "oauth_client",
                "ordering": ["-created_at"],
                "verbose_name": "OAuth Client",
                "verbose_name_plural": "OAuth Clients",
            },
        ),
        migrations.AlterModelOptions(
            name="oauthcode",
            options={
                "db_table": "oauth_code",
                "ordering": ["-expires_at"],
                "verbose_name": "OAuth Code",
                "verbose_name_plural": "OAuth Codes",
            },
        ),
        migrations.AlterModelOptions(
            name="oauthtoken",
            options={
                "db_table": "oauth_token",
                "ordering": ["-expires_at"],
                "verbose_name": "OAuth Token",
                "verbose_name_plural": "OAuth Tokens",
            },
        ),
    ]
