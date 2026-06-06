from django.db import migrations


class Migration(migrations.Migration):
    dependencies = [
        ("auth_app", "0008_encrypt_credentials"),
    ]

    operations = [
        migrations.RemoveField(
            model_name="oauthtoken",
            name="expires_at",
        ),
    ]
