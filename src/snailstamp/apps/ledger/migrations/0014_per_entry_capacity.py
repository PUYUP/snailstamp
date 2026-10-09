"""Kapasitas pemakaian per ENTRY (bukan per kind): entries.max_as_*, collections.cap_*.
 
Menambah kolom, memperbarui hash entry (cap_version), mem-backfill data lama, memasang ulang
trigger beku, dan mengganti fungsi tulis/verifikasi dari sql/0014_per_entry_capacity.sql.
 
Sengaja TIDAK reversible, sama seperti 0001: setelah entry baru terbit dengan cap_version 1,
hash-nya memuat kapasitas dan tidak bisa dikembalikan ke rumus lama. Skrip dikirim utuh sebagai
satu pernyataan (params=None) supaya badan fungsi `$$ ... $$` tidak dipecah dan tanda % di
format() tidak ditafsirkan. Berjalan dalam satu transaksi (atomic) milik Django.
"""
from pathlib import Path

from django.db import migrations

SQL = (Path(__file__).resolve().parent.parent / "sql" / "0003_per_entry_capacity.sql").read_text(encoding="utf-8")


class Migration(migrations.Migration):
    initial = True
    dependencies = [("ledger", "0013_add_content")]
    operations = [migrations.RunSQL(sql=[(SQL, None)])]
