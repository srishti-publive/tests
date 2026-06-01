from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("auth_app", "0001_initial"),
    ]

    operations = [
        migrations.AddField(
            model_name="oauthtoken",
            name="client_id",
            field=models.CharField(blank=True, db_index=True, default="", max_length=64),
        ),
    ]
