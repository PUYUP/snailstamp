"""
Tambah tabel ledger_sealer_audit_logs untuk audit trail key operations.

Audit log mencatat semua events yang terkait dengan sealer key lifecycle:
- Key registration
- Key rotation
- Key expiration
- Key revocation
- Key usage (sealing blocks)
"""
import uuid

from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ('ledger', '0007_multi_signature_and_sealer_keys'),
    ]

    operations = [
        migrations.CreateModel(
            name='SealerKeyAuditLog',
            fields=[
                ('id', models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ('event_type', models.CharField(choices=[('key_registered', 'Key Registered'), ('key_rotated', 'Key Rotated'), ('key_expired', 'Key Expired'), ('key_revoked', 'Key Revoked'), ('key_used', 'Key Used')], max_length=50)),
                ('timestamp', models.DateTimeField(auto_now_add=True)),
                ('actor_user_id', models.CharField(blank=True, max_length=100, null=True)),
                ('actor_email', models.CharField(blank=True, max_length=255, null=True)),
                ('actor_ip', models.GenericIPAddressField(blank=True, null=True)),
                ('actor_user_agent', models.TextField(blank=True, null=True)),
                ('sealer_id', models.UUIDField(blank=True, null=True)),
                ('sealer_name', models.CharField(blank=True, max_length=100, null=True)),
                ('old_key_id', models.UUIDField(blank=True, null=True)),
                ('new_key_id', models.UUIDField(blank=True, null=True)),
                ('old_public_key', models.CharField(blank=True, max_length=64, null=True)),
                ('new_public_key', models.CharField(blank=True, max_length=64, null=True)),
                ('rotation_reason', models.CharField(blank=True, max_length=100, null=True)),
                ('previous_valid_until', models.DateTimeField(blank=True, null=True)),
                ('new_valid_until', models.DateTimeField(blank=True, null=True)),
                ('block_at_rotation', models.BigIntegerField(blank=True, null=True)),
                ('metadata', models.JSONField(blank=True, default=dict)),
            ],
            options={
                'db_table': 'ledger_sealer_audit_logs',
                'ordering': ['-timestamp'],
                'indexes': [
                    models.Index(fields=['event_type'], name='ledger_audit_event_idx'),
                    models.Index(fields=['sealer_id'], name='ledger_audit_sealer_idx'),
                    models.Index(fields=['timestamp'], name='ledger_audit_time_idx'),
                ],
            },
        ),
    ]
