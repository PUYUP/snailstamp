"""Registry contoh (jenis item, aksi, aturan) dari sql/0002_seed_registry.sql. Idempotent.

Hapus / ganti migrasi ini bila registry Anda sendiri. id kind/aksi masuk ke hash: tetap.
"""
from pathlib import Path

from django.db import migrations

SQL = (Path(__file__).resolve().parent.parent / "sql" / "0002_seed_registry.sql").read_text(encoding="utf-8")


class Migration(migrations.Migration):
    dependencies = [("ledger", "0001_schema")]
    operations = [migrations.RunSQL(sql=[(SQL, None)])]
