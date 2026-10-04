"""
Tambah kolom sealer_public_key dan sealer_signature ke ledger_blocks
untuk cryptographic signing block sealer (Ed25519).
"""
from django.db import migrations


class Migration(migrations.Migration):
    dependencies = [
        ('ledger', '0005_add_state_snapshot'),
    ]

    operations = [
        migrations.RunSQL(
            """
            ALTER TABLE ledger_blocks
            ADD COLUMN sealer_public_key bytea,
            ADD COLUMN sealer_signature bytea;
            """,
            reverse_sql="""
            ALTER TABLE ledger_blocks
            DROP COLUMN sealer_signature,
            DROP COLUMN sealer_public_key;
            """
        ),
    ]
