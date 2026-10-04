"""
Update untuk multi-signature dan sealer key management:

1. Hapus kolom sealer_public_key dan sealer_signature dari ledger_blocks
2. Tambah kolom signatures (JSON) untuk multi-signature
3. Buat tabel ledger_sealer_keys untuk tracking sealer keys dengan expiration
"""
import uuid

from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ('ledger', '0006_add_sealer_signature'),
    ]

    operations = [
        # Hapus kolom lama
        migrations.RunSQL(
            """
            ALTER TABLE ledger_blocks
            DROP COLUMN IF EXISTS sealer_signature,
            DROP COLUMN IF EXISTS sealer_public_key;
            """,
            reverse_sql="""
            ALTER TABLE ledger_blocks
            ADD COLUMN sealer_public_key bytea,
            ADD COLUMN sealer_signature bytea;
            """
        ),
        # Tambah kolom signatures untuk multi-sig
        migrations.AddField(
            model_name='block',
            name='signatures',
            field=models.JSONField(default=list, null=True),
        ),
        # Buat tabel ledger_sealer_keys
        migrations.CreateModel(
            name='SealerKey',
            fields=[
                ('id', models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ('name', models.CharField(max_length=100)),
                ('public_key', models.CharField(max_length=64)),
                ('private_key_encrypted', models.TextField(null=True)),
                ('status', models.CharField(choices=[('active', 'Active'), ('expired', 'Expired'), ('revoked', 'Revoked')], default='active', max_length=20)),
                ('valid_from', models.DateTimeField(auto_now_add=True)),
                ('valid_until', models.DateTimeField(null=True)),
                ('threshold', models.IntegerField(default=1)),
                ('metadata', models.JSONField(blank=True, default=dict)),
            ],
            options={
                'db_table': 'ledger_sealer_keys',
                'ordering': ['-valid_from'],
                'indexes': [
                    models.Index(fields=['status'], name='ledger_seale_status_idx'),
                    models.Index(fields=['valid_until'], name='ledger_seale_valid_idx'),
                ],
            },
        ),
    ]
