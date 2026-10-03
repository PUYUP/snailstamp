-- HANYA UNTUK DEVELOPMENT. Menghapus SELURUH objek ledger_* (tabel, fungsi, sequence).
-- Ledger production tidak boleh di-reset: migrasi skema sengaja tidak bisa di-reverse.
--   psql -d <db> -f ledger/sql/dev_reset.sql
--   psql -d <db> -c "DELETE FROM django_migrations WHERE app = 'ledger'"
--   python manage.py migrate ledger
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
END $$;
