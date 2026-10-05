from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("ledger", "0011_append_only_triggers")]

    operations = [
        migrations.CreateModel(
            name="QueuedTransaction",
            fields=[
                ("id", models.BigAutoField(primary_key=True, serialize=False)),
                ("association_id", models.UUIDField(db_index=True)),
                ("member_id", models.UUIDField()),
                ("operation", models.CharField(max_length=32)),
                ("payload", models.JSONField()),
                ("priority", models.PositiveSmallIntegerField(default=0)),
                ("status", models.CharField(choices=[("pending", "Pending"), ("succeeded", "Succeeded"), ("failed", "Failed")], default="pending", max_length=16)),
                ("result", models.JSONField(blank=True, null=True)),
                ("error", models.TextField(blank=True, default="")),
                ("created_at", models.DateTimeField(auto_now_add=True, db_index=True)),
                ("finished_at", models.DateTimeField(blank=True, null=True)),
            ],
            options={
                "db_table": "ledger_queued_transactions",
                "indexes": [
                    models.Index(fields=["status", "-priority", "created_at"], name="ledger_queue_order_idx"),
                    models.Index(fields=["association_id", "created_at"], name="ledger_queue_rate_idx"),
                ],
            },
        ),
    ]
