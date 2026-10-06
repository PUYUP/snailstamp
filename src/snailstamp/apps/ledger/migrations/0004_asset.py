"""
Buat tabel ledger_assets untuk index file storage.

Bukan bagian ledger (managed=True) - ini hanya untuk lookup cepat dan
tracking file yang diupload. Integritas file diverifikasi lewat ledger
(content_hash di Log ACTED_ON).

Strategi (One-to-One):
- 1 Collection = 1 Asset (file foto/video)
- Asset.collection_id = UNIQUE constraint (one-to-one)
- Tidak ada versioning (asset di-replace, bukan versioned)
- Deduplication otomatis: file dengan hash sama tidak duplikat di storage
- State snapshot embed asset info untuk fast read
"""
import uuid

from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ('ledger', '0003_state'),
        ('tenant', '0001_initial'),
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
                ('status', models.CharField(choices=[('active', 'Active'), ('deleted', 'Deleted')], default='active', max_length=20)),
                ('metadata', models.JSONField(blank=True, default=dict)),
                ('uploaded_at', models.DateTimeField(auto_now_add=True)),
                ('updated_at', models.DateTimeField(auto_now=True)),
                ('collection', models.OneToOneField(on_delete=models.CASCADE, related_name='asset', to='ledger.collection', unique=True)),
                ('uploaded_by_member', models.ForeignKey(null=True, on_delete=models.SET_NULL, related_name='uploaded_assets', to='tenant.member')),
            ],
            options={
                'db_table': 'ledger_assets',
                'ordering': ['-uploaded_at'],
                'indexes': [
                    models.Index(fields=['content_hash'], name='ledger_asse_conten_2b2c3e_idx'),
                    models.Index(fields=['status'], name='ledger_asse_status_3c2c3e_idx'),
                ],
            },
        ),
    ]
