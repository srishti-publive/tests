from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("auth_app", "0002_oauthtoken_client_id"),
    ]

    operations = [
        migrations.AddField(
            model_name="oauthclient",
            name="expires_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="oauthtoken",
            name="refresh_token",
            field=models.CharField(blank=True, max_length=128, null=True, unique=True),
        ),
    ]
