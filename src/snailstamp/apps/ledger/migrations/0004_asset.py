"""
Buat tabel ledger_assets untuk index file storage.

Bukan bagian ledger (managed=True) - ini hanya untuk lookup cepat dan
tracking file yang diupload. Integritas file diverifikasi lewat ledger
(content_hash di Log ACTED_ON).
"""
import uuid

from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ('ledger', '0003_state'),
    ]

    operations = [
        migrations.CreateModel(
            name='Asset',
            fields=[
                ('id', models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ('storage_path', models.CharField(max_length=500)),
                ('content_hash', models.CharField(max_length=64)),
                ('original_filename', models.CharField(max_length=255)),
                ('file_size', models.BigIntegerField()),
                ('mime_type', models.CharField(max_length=100)),
                ('version', models.IntegerField(default=1)),
                ('status', models.CharField(choices=[('active', 'Active'), ('replaced', 'Replaced'), ('deleted', 'Deleted')], default='active', max_length=20)),
                ('metadata', models.JSONField(blank=True, default=dict)),
                ('uploaded_at', models.DateTimeField(auto_now_add=True)),
                ('collection', models.ForeignKey(on_delete=models.CASCADE, related_name='assets', to='ledger.collection')),
                ('replaces', models.ForeignKey(blank=True, null=True, on_delete=models.SET_NULL, related_name='replaced_by', to='ledger.asset')),
                ('uploaded_by_member', models.ForeignKey(null=True, on_delete=models.SET_NULL, related_name='uploaded_assets', to='tenant.member')),
            ],
            options={
                'db_table': 'ledger_assets',
                'ordering': ['-uploaded_at'],
                'indexes': [
                    models.Index(fields=['collection', 'version'], name='ledger_asse_collec_0d2c3e_idx'),
                    models.Index(fields=['content_hash'], name='ledger_asse_conten_2b2c3e_idx'),
                    models.Index(fields=['status'], name='ledger_asse_status_3c2c3e_idx'),
                ],
            },
        ),
    ]
