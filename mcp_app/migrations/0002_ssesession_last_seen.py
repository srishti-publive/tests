from django.db import migrations, models
import django.utils.timezone


class Migration(migrations.Migration):

    dependencies = [
        ("mcp_app", "0001_initial"),
    ]

    operations = [
        migrations.AddField(
            model_name="ssesession",
            name="last_seen",
            field=models.DateTimeField(
                auto_now_add=True,
                db_index=True,
                default=django.utils.timezone.now,
            ),
            preserve_default=False,
        ),
    ]
