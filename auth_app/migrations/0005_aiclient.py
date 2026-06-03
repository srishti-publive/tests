import uuid

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("auth_app", "0004_alter_model_meta_options"),
    ]

    operations = [
        migrations.CreateModel(
            name="AIClient",
            fields=[
                (
                    "client_id",
                    models.UUIDField(
                        default=uuid.uuid4,
                        editable=False,
                        primary_key=True,
                        serialize=False,
                    ),
                ),
                ("client_name", models.CharField(max_length=255)),
                ("contact", models.CharField(blank=True, default="", max_length=255)),
                (
                    "status",
                    models.CharField(
                        choices=[("active", "Active"), ("blocked", "Blocked")],
                        db_index=True,
                        default="active",
                        max_length=16,
                    ),
                ),
                ("credentials", models.JSONField(blank=True, null=True)),
                ("registered_at", models.DateTimeField(auto_now_add=True)),
                ("registration_ip", models.GenericIPAddressField(blank=True, null=True)),
                ("last_seen_at", models.DateTimeField(blank=True, null=True)),
            ],
            options={
                "verbose_name": "AI Client",
                "verbose_name_plural": "AI Clients",
                "db_table": "ai_client",
                "ordering": ["-registered_at"],
            },
        ),
    ]
