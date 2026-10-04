"""
Tambah kolom state_snapshot ke ledger_logs untuk rekonstruksi Collection state.

Kolom ini menyimpan JSON snapshot Collection state setiap kali log dibuat,
memungkinkan rekonstruksi penuh state Collection dari history log.
"""
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ('ledger', '0004_asset'),
    ]

    operations = [
        migrations.AddField(
            model_name='log',
            name='state_snapshot',
            field=models.JSONField(null=True),
        ),
    ]
