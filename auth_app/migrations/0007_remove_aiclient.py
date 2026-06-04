from django.db import migrations


class Migration(migrations.Migration):
    """Drop ai_client table if it exists.

    Uses IF EXISTS so this is safe to run whether the table was previously
    created (production deployments that ran 0005) or never created
    (dev environments where 0005 was skipped).
    """

    dependencies = [
        ("auth_app", "0006_optimise_models"),
    ]

    operations = [
        migrations.RunSQL(
            sql="DROP TABLE IF EXISTS ai_client;",
            reverse_sql=migrations.RunSQL.noop,
            state_operations=[],
        ),
    ]
