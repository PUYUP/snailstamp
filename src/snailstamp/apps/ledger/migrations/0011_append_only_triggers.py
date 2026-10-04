"""
Trigger append-only di level DB untuk tabel audit / signature / anchor (selama ini hanya dijaga ORM),
plus penjaga ledger_sealer_keys. Sama seperti ledger_logs: UPDATE/DELETE/TRUNCATE lewat SQL mentah
ditolak (ERRCODE LG005). Hanya superuser yang mematikan trigger yang bisa melewatinya -- dan
penulisan ulang seperti itu tertangkap oleh signature + anchor eksternal (detect_forks).
"""
from django.db import migrations

APPEND_ONLY = ["ledger_sealer_audit_logs", "ledger_block_signatures", "ledger_block_anchors"]

FORWARD = [
    *(f"""
CREATE TRIGGER {t}_append_only BEFORE UPDATE OR DELETE ON {t}
  FOR EACH ROW EXECUTE FUNCTION ledger_forbid_mutation();
CREATE TRIGGER {t}_no_truncate BEFORE TRUNCATE ON {t}
  FOR EACH STATEMENT EXECUTE FUNCTION ledger_forbid_mutation();
""" for t in APPEND_ONLY),
    """
CREATE TRIGGER ledger_blocks_no_truncate BEFORE TRUNCATE ON ledger_blocks
  FOR EACH STATEMENT EXECUTE FUNCTION ledger_forbid_mutation();

-- Sealer key: status/valid_until/metadata boleh berubah (rotate, expire, revoke), tapi identitasnya
-- beku, key yang sudah di-revoke tidak bisa diaktifkan lagi, dan baris tidak boleh dihapus
-- (dibutuhkan untuk memverifikasi blok lama).
CREATE FUNCTION ledger_sealer_key_guard() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  IF (OLD.id, OLD.public_key, OLD.valid_from, OLD.region, OLD.previous_key_id)
     IS DISTINCT FROM (NEW.id, NEW.public_key, NEW.valid_from, NEW.region, NEW.previous_key_id) THEN
    RAISE EXCEPTION 'mengubah identitas sealer key % dilarang', OLD.id USING ERRCODE = 'LG005';
  END IF;
  IF OLD.status = 'revoked' AND NEW.status IS DISTINCT FROM 'revoked' THEN
    RAISE EXCEPTION 'mengaktifkan lagi sealer key % yang sudah di-revoke dilarang', OLD.id
      USING ERRCODE = 'LG005';
  END IF;
  RETURN NEW;
END $$;
CREATE TRIGGER ledger_sealer_keys_guard BEFORE UPDATE ON ledger_sealer_keys
  FOR EACH ROW EXECUTE FUNCTION ledger_sealer_key_guard();
CREATE TRIGGER ledger_sealer_keys_no_delete BEFORE DELETE ON ledger_sealer_keys
  FOR EACH ROW EXECUTE FUNCTION ledger_forbid_mutation();
CREATE TRIGGER ledger_sealer_keys_no_truncate BEFORE TRUNCATE ON ledger_sealer_keys
  FOR EACH STATEMENT EXECUTE FUNCTION ledger_forbid_mutation();
""",
]

REVERSE = [
    *(f"DROP TRIGGER {t}_append_only ON {t}; DROP TRIGGER {t}_no_truncate ON {t};" for t in APPEND_ONLY),
    """
DROP TRIGGER ledger_blocks_no_truncate ON ledger_blocks;
DROP TRIGGER ledger_sealer_keys_guard ON ledger_sealer_keys;
DROP TRIGGER ledger_sealer_keys_no_delete ON ledger_sealer_keys;
DROP TRIGGER ledger_sealer_keys_no_truncate ON ledger_sealer_keys;
DROP FUNCTION ledger_sealer_key_guard();
""",
]


class Migration(migrations.Migration):
    dependencies = [
        ('ledger', '0010_block_anchors'),
    ]

    operations = [migrations.RunSQL(sql=FORWARD, reverse_sql=REVERSE)]
