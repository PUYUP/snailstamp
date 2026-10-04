"""Perbaiki ledger_mint_batch, ledger_collection_snapshot, ledger_verify_chain di database yang sudah ada.

Lihat sql/0010_fix_ledger_functions.sql. Database baru sudah mendapat versi benar dari 0001.
"""
from pathlib import Path

from django.db import migrations

SQL = (Path(__file__).resolve().parent.parent / "sql" / "0010_fix_ledger_functions.sql").read_text(encoding="utf-8")


class Migration(migrations.Migration):
    dependencies = [
        ("ledger", "0009_multi_region_sealer_and_shamir"),
    ]

    operations = [migrations.RunSQL(sql=[(SQL, None)], reverse_sql=migrations.RunSQL.noop)]
