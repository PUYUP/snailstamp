-- HANYA UNTUK DEVELOPMENT. Menghapus SELURUH objek ledger_* (tabel, fungsi, sequence), TERMASUK tabel
-- ledger_media_mediaobject (barisnya menunjuk collection yang ikut hilang), lalu menghapus catatan migrasi
-- app `ledger` dan `ledger_media` supaya `migrate` membangun keduanya lagi dari nol.
-- Objek di S3 TIDAK ikut terhapus: kosongkan bucket development sendiri.
-- Ledger production tidak boleh di-reset: migrasi skema sengaja tidak bisa di-reverse.
--
--   psql -d <db> -f snailstamp/apps/ledger/sql/dev_reset.sql
--   python manage.py migrate
DO $$
DECLARE r record;
BEGIN
  FOR r IN SELECT c.oid::regclass AS t FROM pg_class c
            WHERE c.relnamespace = current_schema()::regnamespace
              AND c.relname LIKE 'ledger\_%' AND c.relkind IN ('r', 'p') AND NOT c.relispartition
  LOOP EXECUTE 'DROP TABLE IF EXISTS ' || r.t || ' CASCADE'; END LOOP;
  FOR r IN SELECT p.oid::regprocedure AS f FROM pg_proc p
            WHERE p.pronamespace = current_schema()::regnamespace AND p.proname LIKE 'ledger\_%'
  LOOP EXECUTE 'DROP FUNCTION IF EXISTS ' || r.f || ' CASCADE'; END LOOP;
  DROP SEQUENCE IF EXISTS ledger_entry_id_seq, ledger_collection_id_seq;
  IF to_regclass('django_migrations') IS NOT NULL THEN
    DELETE FROM django_migrations WHERE app IN ('ledger', 'ledger_media');
  END IF;
END $$;
