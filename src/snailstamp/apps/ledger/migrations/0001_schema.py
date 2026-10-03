"""Membuat seluruh skema ledger (tabel, partisi, trigger, fungsi) dari sql/0001_schema.sql.

Sengaja TIDAK reversible: ledger bersifat append-only. Untuk reset development lihat
sql/dev_reset.sql. Skrip dikirim utuh sebagai satu pernyataan (params=None) supaya badan
fungsi `$$ ... $$` tidak dipecah oleh pemisah statement dan tanda % tidak ditafsirkan.
"""
from pathlib import Path

from django.db import migrations

SQL = (Path(__file__).resolve().parent.parent / "sql" / "0001_schema.sql").read_text(encoding="utf-8")


class Migration(migrations.Migration):
    initial = True
    dependencies = []
    operations = [migrations.RunSQL(sql=[(SQL, None)])]
