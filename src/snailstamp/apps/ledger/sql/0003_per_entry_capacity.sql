-- =====================================================================
--  0002_per_entry_capacity.sql  --  kapasitas per ENTRY (bukan per KIND)
--  Prasyarat: 0001_schema.sql sudah terpasang. PostgreSQL >= 14.
-- =====================================================================
--  MASALAH
--    ledger_kinds.max_as_target mengunci kapasitas untuk SEMUA barang sejenis.
--    Jurnal 80 halaman dan jurnal 200 halaman = satu kind, tetapi beda barang.
--
--  SOLUSI (tiga tingkat)
--    ledger_kinds.max_as_*       DEFAULT saja (dipakai bila entry tidak menyebut apa-apa)
--    ledger_entries.max_as_*     NILAI FINAL per entry, ikut content_hash (beku setelah mint)
--    ledger_collections.cap_*    SALINAN BEKU dari entry; inilah yang dibaca ledger_act/ledger_use
--
--  SEMANTIK PARAMETER KAPASITAS (create_entry / update_entry)
--    NULL  = create: pakai default kind | update: tidak diubah
--            (khusus update: bila kind BERUBAH dan parameter NULL, kapasitas ikut default kind baru)
--    -1    = tak terbatas (eksplisit)
--    > 0   = batas
--    0 / < -1 = ditolak (LG004)
--
--  HASH ENTRY
--    entries.cap_version = 0 : entry lama, hash TANPA kapasitas (string hash identik dengan 0001)
--    entries.cap_version = 1 : hash selalu memuat '|cap:<tool>:<target>' (NULL = tak terbatas)
--    Versi dibutuhkan supaya "tak terbatas" (NULL,NULL) dapat dibedakan dari entry lama.
--
--  DATA LAMA
--    * entry yang SUDAH ter-mint : tetap cap_version 0 (hash tidak boleh berubah). max_as_* diisi dari
--      default kind saat migrasi (informasional, tidak ikut hash), dipakai bila sisa supply di-mint.
--    * entry yang BELUM ter-mint : otomatis dinaikkan ke cap_version 1 (hash dihitung ulang).
--    * collections lama          : cap_* disalin dari entry.
--
--  JALANKAN di jendela perawatan: ALTER TABLE mengambil ACCESS EXCLUSIVE sampai COMMIT, sehingga
--  mint/act yang berjalan akan menunggu. Backfill dikerjakan per partisi.
--  Perubahan di aplikasi: ledger_create_entry & ledger_update_entry mendapat 2 parameter opsional
--  di UJUNG (p_max_as_tool, p_max_as_target). Panggilan lama tetap jalan.
-- =====================================================================

BEGIN;

-- ---------------------------------------------------------------------
-- 1. KOLOM BARU
-- ---------------------------------------------------------------------
ALTER TABLE ledger_entries
  ADD COLUMN max_as_tool   integer CHECK (max_as_tool   > 0),     -- NULL = tak terbatas
  ADD COLUMN max_as_target integer CHECK (max_as_target > 0),     -- NULL = tak terbatas
  ADD COLUMN cap_version   smallint NOT NULL DEFAULT 0 CHECK (cap_version IN (0, 1));

ALTER TABLE ledger_collections
  ADD COLUMN cap_tool   integer,     -- salinan beku entries.max_as_tool
  ADD COLUMN cap_target integer;     -- salinan beku entries.max_as_target

COMMENT ON COLUMN ledger_kinds.max_as_tool   IS 'DEFAULT batas pemakaian sebagai alat; nilai final ada di ledger_entries.max_as_tool';
COMMENT ON COLUMN ledger_kinds.max_as_target IS 'DEFAULT batas aksi sebagai sasaran; nilai final ada di ledger_entries.max_as_target';
COMMENT ON COLUMN ledger_entries.max_as_tool   IS 'batas pemakaian sebagai ALAT untuk tiap item entry ini (NULL = tak terbatas)';
COMMENT ON COLUMN ledger_entries.max_as_target IS 'batas aksi sebagai SASARAN untuk tiap item entry ini (NULL = tak terbatas), mis. jumlah halaman jurnal';
COMMENT ON COLUMN ledger_entries.cap_version   IS '0 = hash entry tanpa kapasitas (lama); 1 = hash memuat kapasitas';

-- ---------------------------------------------------------------------
-- 2. HASH ENTRY + PENYELESAI KAPASITAS
--    Fungsi lama di-DROP (bukan di-overload): overload dengan DEFAULT membuat panggilan
--    8 argumen menjadi "function is not unique".
-- ---------------------------------------------------------------------
DROP FUNCTION ledger_entry_hash(bigint, uuid, uuid, text, int, smallint, jsonb, timestamptz);

CREATE FUNCTION ledger_entry_hash(p_id bigint, p_issuer uuid, p_issuer_member uuid, p_reason text,
                                  p_supply int, p_kind smallint, p_metadata jsonb, p_ts timestamptz,
                                  p_cap_version smallint DEFAULT 0,
                                  p_max_tool int DEFAULT NULL, p_max_target int DEFAULT NULL)
RETURNS bytea LANGUAGE sql IMMUTABLE PARALLEL SAFE AS
$$ SELECT sha256(convert_to(
       p_id::text || '|' || p_issuer::text || '|' || p_issuer_member::text || '|' || p_reason || '|' ||
       p_supply::text || '|' || p_kind::text || '|' || p_metadata::text || '|' || ledger_micros(p_ts)::text ||
       CASE WHEN p_cap_version >= 1
            THEN '|cap:' || coalesce(p_max_tool::text, '') || ':' || coalesce(p_max_target::text, '')
            ELSE '' END,
       'UTF8')) $$;

-- NULL -> default; -1 -> tak terbatas (NULL); > 0 -> nilai itu; selain itu galat.
CREATE FUNCTION ledger_resolve_cap(p_input int, p_default int) RETURNS int
LANGUAGE plpgsql IMMUTABLE PARALLEL SAFE AS $$
BEGIN
  IF p_input IS NULL THEN RETURN p_default; END IF;
  IF p_input = -1 THEN RETURN NULL; END IF;
  IF p_input < 1 THEN
    RAISE EXCEPTION 'kapasitas harus > 0, atau -1 untuk tak terbatas (diterima: %)', p_input USING ERRCODE = 'LG004';
  END IF;
  RETURN p_input;
END $$;

-- ---------------------------------------------------------------------
-- 3. BACKFILL ENTRIES (guard edit dimatikan sebentar: entry ter-mint memang tidak boleh diedit,
--    tetapi di sini hanya mengisi kolom baru yang belum ada sebelumnya)
-- ---------------------------------------------------------------------
DROP TRIGGER ledger_entries_edit_guard ON ledger_entries;

UPDATE ledger_entries e
   SET max_as_tool   = k.max_as_tool,
       max_as_target = k.max_as_target,
       cap_version   = CASE WHEN e.minted_count = 0 THEN 1 ELSE 0 END,
       content_hash  = CASE WHEN e.minted_count = 0
                            THEN ledger_entry_hash(e.id, e.issuer_id, e.issuer_member_id, e.reason, e.supply,
                                                   e.kind, e.metadata, e.created_at,
                                                   1::smallint, k.max_as_tool, k.max_as_target)
                            ELSE e.content_hash END
  FROM ledger_kinds k
 WHERE k.id = e.kind;

-- guard baru: kolom kapasitas & versi ikut dijaga (hanya lewat ledger_update_entry, hanya bila minted_count = 0)
CREATE TRIGGER ledger_entries_edit_guard BEFORE UPDATE ON ledger_entries
  FOR EACH ROW
  WHEN ((OLD.reason, OLD.supply, OLD.kind, OLD.metadata, OLD.content_hash,
         OLD.max_as_tool, OLD.max_as_target, OLD.cap_version)
        IS DISTINCT FROM
        (NEW.reason, NEW.supply, NEW.kind, NEW.metadata, NEW.content_hash,
         NEW.max_as_tool, NEW.max_as_target, NEW.cap_version))
  EXECUTE FUNCTION ledger_entries_guard_edit();

-- ---------------------------------------------------------------------
-- 4. BACKFILL COLLECTIONS (per partisi) + bekukan kolom baru
-- ---------------------------------------------------------------------
DO $$
DECLARE p regclass;
BEGIN
  FOR p IN SELECT inhrelid::regclass FROM pg_inherits WHERE inhparent = 'ledger_collections'::regclass
  LOOP
    EXECUTE format('UPDATE %s c SET cap_tool = e.max_as_tool, cap_target = e.max_as_target
                      FROM ledger_entries e WHERE e.id = c.entry_id', p);
  END LOOP;
END $$;

DROP TRIGGER ledger_collections_frozen ON ledger_collections;
CREATE TRIGGER ledger_collections_frozen BEFORE UPDATE ON ledger_collections
  FOR EACH ROW
  WHEN ((OLD.id, OLD.entry_id, OLD.serial_no, OLD.kind, OLD.created_at, OLD.cap_tool, OLD.cap_target)
        IS DISTINCT FROM (NEW.id, NEW.entry_id, NEW.serial_no, NEW.kind, NEW.created_at, NEW.cap_tool, NEW.cap_target))
  EXECUTE FUNCTION ledger_forbid_mutation();

-- ---------------------------------------------------------------------
-- 5. ENTRY: create & update (signature berubah -> DROP lalu CREATE)
-- ---------------------------------------------------------------------
DROP FUNCTION ledger_create_entry(uuid, uuid, text, int, jsonb, smallint);
DROP FUNCTION ledger_update_entry(bigint, uuid, uuid, text, int, jsonb, smallint);

CREATE FUNCTION ledger_create_entry(p_issuer uuid, p_issuer_member uuid, p_reason text, p_supply int,
                                    p_metadata jsonb DEFAULT '{}', p_kind smallint DEFAULT 0,
                                    p_max_as_tool int DEFAULT NULL, p_max_as_target int DEFAULT NULL)
RETURNS bigint LANGUAGE plpgsql SECURITY DEFINER SET search_path = public, pg_temp AS $$
DECLARE
  v_id bigint := nextval('ledger_entry_id_seq'); v_now timestamptz := clock_timestamp();
  k ledger_kinds; v_tool int; v_target int;
BEGIN
  PERFORM ledger_require_actor(p_issuer, p_issuer_member);
  IF p_supply IS NULL OR p_supply < 1 OR p_supply > 90000000 THEN
    RAISE EXCEPTION 'supply harus 1..90000000' USING ERRCODE = 'LG004';
  END IF;
  IF p_reason IS NULL OR length(btrim(p_reason)) = 0 THEN
    RAISE EXCEPTION 'reason wajib diisi' USING ERRCODE = 'LG004';
  END IF;
  IF p_kind IS NULL THEN
    RAISE EXCEPTION 'kind wajib diisi' USING ERRCODE = 'LG004';
  END IF;
  SELECT * INTO k FROM ledger_kinds WHERE id = p_kind;
  IF NOT FOUND THEN
    RAISE EXCEPTION 'kind % tidak ada di registry', p_kind USING ERRCODE = 'LG004';
  END IF;
  IF k.restricted AND NOT EXISTS (SELECT 1 FROM ledger_kind_issuers WHERE kind_id = p_kind AND association_id = p_issuer) THEN
    RAISE EXCEPTION 'jenis "%" hanya boleh dibuat oleh penerbit resmi', k.code USING ERRCODE = 'LG002';
  END IF;

  v_tool   := ledger_resolve_cap(p_max_as_tool,   k.max_as_tool);     -- NULL = default kind, -1 = tak terbatas
  v_target := ledger_resolve_cap(p_max_as_target, k.max_as_target);
  p_metadata := coalesce(p_metadata, '{}'::jsonb);

  INSERT INTO ledger_entries (id, issuer_id, issuer_member_id, reason, supply, kind, metadata,
                              max_as_tool, max_as_target, cap_version, content_hash, created_at)
  VALUES (v_id, p_issuer, p_issuer_member, p_reason, p_supply, p_kind, p_metadata,
          v_tool, v_target, 1,
          ledger_entry_hash(v_id, p_issuer, p_issuer_member, p_reason, p_supply, p_kind, p_metadata, v_now,
                            1::smallint, v_tool, v_target),
          v_now);
  RETURN v_id;
END $$;

-- UPDATE ENTRY: NULL = tidak diubah. Hanya selama minted_count = 0.
-- Kapasitas: -1 = tak terbatas, > 0 = batas. Bila kind berubah dan parameter kapasitas NULL,
-- kapasitas ikut default kind BARU (supaya tidak tertinggal nilai kind lama).
CREATE FUNCTION ledger_update_entry(p_entry_id bigint, p_issuer uuid, p_issuer_member uuid,
                                    p_reason text DEFAULT NULL, p_supply int DEFAULT NULL,
                                    p_metadata jsonb DEFAULT NULL, p_kind smallint DEFAULT NULL,
                                    p_max_as_tool int DEFAULT NULL, p_max_as_target int DEFAULT NULL)
RETURNS bigint LANGUAGE plpgsql SECURITY DEFINER SET search_path = public, pg_temp AS $$
DECLARE e ledger_entries; k ledger_kinds; v_kind_changed boolean := false;
BEGIN
  PERFORM ledger_require_actor(p_issuer, p_issuer_member);
  IF p_reason IS NULL AND p_supply IS NULL AND p_metadata IS NULL AND p_kind IS NULL
     AND p_max_as_tool IS NULL AND p_max_as_target IS NULL THEN
    RAISE EXCEPTION 'tidak ada yang diubah' USING ERRCODE = 'LG004';
  END IF;

  SELECT * INTO e FROM ledger_entries WHERE id = p_entry_id FOR UPDATE;  -- serial dengan ledger_mint_batch
  IF NOT FOUND THEN RAISE EXCEPTION 'entry % tidak ada', p_entry_id USING ERRCODE = 'LG001'; END IF;
  IF e.issuer_id <> p_issuer THEN
    RAISE EXCEPTION 'hanya issuer yang boleh mengubah entry' USING ERRCODE = 'LG002';
  END IF;
  IF e.minted_count > 0 THEN
    RAISE EXCEPTION 'entry % tidak boleh diubah: sudah ada % item ter-mint', e.id, e.minted_count
      USING ERRCODE = 'LG003';
  END IF;

  IF p_reason IS NOT NULL THEN
    IF length(btrim(p_reason)) = 0 THEN
      RAISE EXCEPTION 'reason wajib diisi' USING ERRCODE = 'LG004';
    END IF;
    e.reason := p_reason;
  END IF;
  IF p_supply IS NOT NULL THEN
    IF p_supply < 1 OR p_supply > 90000000 THEN
      RAISE EXCEPTION 'supply harus 1..90000000' USING ERRCODE = 'LG004';
    END IF;
    e.supply := p_supply;
  END IF;
  IF p_metadata IS NOT NULL THEN
    e.metadata := p_metadata;
  END IF;
  IF p_kind IS NOT NULL THEN
    SELECT * INTO k FROM ledger_kinds WHERE id = p_kind;
    IF NOT FOUND THEN
      RAISE EXCEPTION 'kind % tidak ada di registry', p_kind USING ERRCODE = 'LG004';
    END IF;
    IF k.restricted AND NOT EXISTS (SELECT 1 FROM ledger_kind_issuers WHERE kind_id = p_kind AND association_id = p_issuer) THEN
      RAISE EXCEPTION 'jenis "%" hanya boleh dibuat oleh penerbit resmi', k.code USING ERRCODE = 'LG002';
    END IF;
    v_kind_changed := (p_kind <> e.kind);
    e.kind := p_kind;
  END IF;

  -- kapasitas: nilai eksplisit menang; bila kind berganti, ambil default kind baru
  IF p_max_as_tool IS NOT NULL THEN
    e.max_as_tool := ledger_resolve_cap(p_max_as_tool, NULL);
  ELSIF v_kind_changed THEN
    e.max_as_tool := k.max_as_tool;
  END IF;
  IF p_max_as_target IS NOT NULL THEN
    e.max_as_target := ledger_resolve_cap(p_max_as_target, NULL);
  ELSIF v_kind_changed THEN
    e.max_as_target := k.max_as_target;
  END IF;
  e.cap_version := 1;

  e.content_hash := ledger_entry_hash(e.id, e.issuer_id, e.issuer_member_id, e.reason, e.supply, e.kind,
                                      e.metadata, e.created_at, e.cap_version, e.max_as_tool, e.max_as_target);

  PERFORM set_config('ledger.entry_edit', 'on', true);   -- true = hanya berlaku di transaksi ini
  UPDATE ledger_entries
     SET reason = e.reason, supply = e.supply, kind = e.kind, metadata = e.metadata,
         max_as_tool = e.max_as_tool, max_as_target = e.max_as_target, cap_version = e.cap_version,
         content_hash = e.content_hash
   WHERE id = e.id;
  PERFORM set_config('ledger.entry_edit', 'off', true);
  RETURN e.id;
END $$;

-- ---------------------------------------------------------------------
-- 6. MINT: salin kapasitas entry ke tiap collection (signature sama -> CREATE OR REPLACE)
-- ---------------------------------------------------------------------
CREATE OR REPLACE FUNCTION ledger_mint_batch(p_entry_id bigint, p_issuer uuid, p_issuer_member uuid, p_batch int, p_prefix varchar DEFAULT '')
RETURNS bigint[] LANGUAGE plpgsql SECURITY DEFINER SET search_path = public, pg_temp AS $$
DECLARE e ledger_entries; v_from int; v_to int; v_now timestamptz := clock_timestamp(); v_created_ids bigint[];
BEGIN
  PERFORM ledger_require_actor(p_issuer, p_issuer_member);
  IF p_batch IS NULL OR p_batch < 1 OR p_batch > 100000 THEN
    RAISE EXCEPTION 'batch harus 1..100000' USING ERRCODE = 'LG004';
  END IF;
  SELECT * INTO e FROM ledger_entries WHERE id = p_entry_id FOR UPDATE;
  IF NOT FOUND THEN RAISE EXCEPTION 'entry % tidak ada', p_entry_id USING ERRCODE = 'LG001'; END IF;
  IF e.issuer_id <> p_issuer THEN
    RAISE EXCEPTION 'hanya issuer yang boleh mint' USING ERRCODE = 'LG002';
  END IF;

  v_from := e.minted_count + 1;
  v_to   := least(e.supply, e.minted_count + p_batch);
  IF v_from > v_to THEN RETURN ARRAY[]::bigint[]; END IF;     -- supply sudah penuh

  WITH base AS MATERIALIZED (
    SELECT g.id, g.serial_no,
           ledger_log_hash(g.genesis, g.id, 1, 1::smallint, p_issuer, p_issuer_member, NULL, v_now, NULL) AS h1
    FROM (SELECT x.id, x.serial_no, ledger_genesis_hash(e.content_hash, x.id, x.serial_no) AS genesis
          FROM (SELECT nextval('ledger_collection_id_seq') AS id,
                       p_prefix || ((((s::bigint * 38742041) + (p_entry_id * 1234567)) % 90000000) + 10000000)::text AS serial_no
                FROM generate_series(v_from, v_to) s) x) g
  ), new_cols AS (
    -- last_hash = hash log #1 (bukan genesis) supaya log berikutnya tersambung ke log #1
    -- cap_tool/cap_target = salinan beku kapasitas entry
    INSERT INTO ledger_collections (id, entry_id, serial_no, owner_id, holder_id, state, kind,
                                    cap_tool, cap_target, last_seq, last_hash, created_at, updated_at)
    SELECT b.id, e.id, b.serial_no, p_issuer, p_issuer_member, 1, e.kind,
           e.max_as_tool, e.max_as_target, 1, b.h1, v_now, v_now
    FROM base b
    RETURNING *
  ), new_logs AS (
    INSERT INTO ledger_logs (collection_id, actor_id, actor_member_id, counterparty_id, created_at,
                             seq, event_type, hash, payload)
    SELECT b.id, p_issuer, p_issuer_member, NULL, v_now, 1, 1::smallint, b.h1, NULL
    FROM base b
    RETURNING collection_id, hash
  ), inserted_holdings AS (
    INSERT INTO ledger_holdings (owner_id, collection_id, entry_id, acquired_at)
    SELECT p_issuer, id, e.id, v_now FROM new_cols
    RETURNING collection_id
  )
  SELECT array_agg(collection_id) INTO v_created_ids FROM inserted_holdings;

  UPDATE ledger_entries SET minted_count = v_to WHERE id = e.id;
  RETURN coalesce(v_created_ids, ARRAY[]::bigint[]);
END $$;

-- ---------------------------------------------------------------------
-- 7. USE & ACT: baca kapasitas dari COLLECTION (bukan dari kind) -> satu lookup registry lebih sedikit
-- ---------------------------------------------------------------------
CREATE OR REPLACE FUNCTION ledger_use(p_collection_id bigint, p_actor uuid, p_actor_member uuid,
                           p_action smallint DEFAULT NULL, p_payload jsonb DEFAULT NULL)
RETURNS int LANGUAGE plpgsql SECURITY DEFINER SET search_path = public, pg_temp AS $$
DECLARE c ledger_collections; w record; v_now timestamptz := clock_timestamp();
BEGIN
  PERFORM ledger_require_actor(p_actor, p_actor_member);
  IF p_action IS NOT NULL AND NOT EXISTS (SELECT 1 FROM ledger_actions WHERE id = p_action) THEN
    RAISE EXCEPTION 'aksi % tidak ada di registry', p_action USING ERRCODE = 'LG004';
  END IF;
  SELECT * INTO c FROM ledger_collections WHERE id = p_collection_id FOR UPDATE;
  IF NOT FOUND THEN RAISE EXCEPTION 'collection % tidak ada', p_collection_id USING ERRCODE = 'LG001'; END IF;
  IF c.owner_id <> p_actor THEN RAISE EXCEPTION 'anda bukan pemilik' USING ERRCODE = 'LG002'; END IF;
  IF c.state <> 1 THEN RAISE EXCEPTION 'collection sedang dalam pengiriman' USING ERRCODE = 'LG003'; END IF;
  IF c.cap_tool IS NOT NULL AND c.tool_uses >= c.cap_tool THEN
    RAISE EXCEPTION 'item sudah habis terpakai (batas % kali)', c.cap_tool USING ERRCODE = 'LG003';
  END IF;

  SELECT * INTO w FROM ledger_write_log(c, 4::smallint, p_actor, p_actor_member, NULL, p_payload, v_now,
                                        NULL, NULL, p_action, NULL);
  UPDATE ledger_collections
     SET last_seq = w.new_seq, last_hash = w.new_hash, tool_uses = tool_uses + 1, updated_at = v_now
   WHERE id = c.id;                       -- HOT update: tidak ada kolom ber-index yang berubah
  RETURN w.new_seq;
END $$;

CREATE OR REPLACE FUNCTION ledger_act(p_action smallint, p_tool bigint, p_target bigint, p_actor uuid, p_actor_member uuid,
                           p_content_hash bytea DEFAULT NULL, p_payload jsonb DEFAULT NULL)
RETURNS TABLE (tool_log_seq int, target_log_seq int)
LANGUAGE plpgsql SECURITY DEFINER SET search_path = public, pg_temp AS $$
DECLARE
  tool ledger_collections; tgt ledger_collections; rl ledger_action_rules;
  wt record; wg record;
  v_now timestamptz := clock_timestamp();
BEGIN
  PERFORM ledger_require_actor(p_actor, p_actor_member);
  IF p_action IS NULL THEN RAISE EXCEPTION 'aksi wajib diisi' USING ERRCODE = 'LG004'; END IF;
  IF p_content_hash IS NOT NULL AND octet_length(p_content_hash) <> 32 THEN
    RAISE EXCEPTION 'content_hash harus sha256 (32 byte)' USING ERRCODE = 'LG004';
  END IF;
  IF p_tool = p_target THEN
    RAISE EXCEPTION 'alat dan sasaran harus berbeda' USING ERRCODE = 'LG004';
  END IF;

  IF p_tool < p_target THEN
    SELECT * INTO tool FROM ledger_collections WHERE id = p_tool   FOR UPDATE;
    SELECT * INTO tgt  FROM ledger_collections WHERE id = p_target FOR UPDATE;
  ELSE
    SELECT * INTO tgt  FROM ledger_collections WHERE id = p_target FOR UPDATE;
    SELECT * INTO tool FROM ledger_collections WHERE id = p_tool   FOR UPDATE;
  END IF;
  IF tool.id IS NULL THEN RAISE EXCEPTION 'alat % tidak ada', p_tool USING ERRCODE = 'LG001'; END IF;
  IF tgt.id  IS NULL THEN RAISE EXCEPTION 'sasaran % tidak ada', p_target USING ERRCODE = 'LG001'; END IF;

  SELECT * INTO rl FROM ledger_action_rules
   WHERE action_id = p_action AND tool_kind = tool.kind AND target_kind = tgt.kind;
  IF NOT FOUND THEN
    RAISE EXCEPTION 'aksi "%" tidak berlaku untuk "%" terhadap "%"',
      (SELECT code FROM ledger_actions WHERE id = p_action),
      (SELECT code FROM ledger_kinds WHERE id = tool.kind),
      (SELECT code FROM ledger_kinds WHERE id = tgt.kind)
      USING ERRCODE = 'LG004';
  END IF;

  IF tool.owner_id <> p_actor THEN RAISE EXCEPTION 'anda bukan pemilik alat' USING ERRCODE = 'LG002'; END IF;
  IF tool.state <> 1 THEN RAISE EXCEPTION 'alat sedang dalam pengiriman' USING ERRCODE = 'LG003'; END IF;
  IF rl.target_access = 1 THEN
    IF tgt.owner_id <> p_actor THEN RAISE EXCEPTION 'anda bukan pemilik sasaran' USING ERRCODE = 'LG002'; END IF;
    IF tgt.state <> 1 THEN RAISE EXCEPTION 'sasaran sedang dalam pengiriman' USING ERRCODE = 'LG003'; END IF;
  ELSIF rl.target_access = 2 THEN
    IF tgt.state <> 2 THEN RAISE EXCEPTION 'aksi ini hanya untuk sasaran yang sedang dikirim' USING ERRCODE = 'LG003'; END IF;
  ELSE
    IF tgt.state <> 1 THEN RAISE EXCEPTION 'sasaran sedang dalam pengiriman' USING ERRCODE = 'LG003'; END IF;
  END IF;

  -- kapasitas per ITEM (salinan beku dari entry), bukan per kind
  IF tool.cap_tool IS NOT NULL AND tool.tool_uses >= tool.cap_tool THEN
    RAISE EXCEPTION 'alat sudah habis terpakai (batas % kali)', tool.cap_tool USING ERRCODE = 'LG003';
  END IF;
  IF tgt.cap_target IS NOT NULL AND tgt.target_acts >= tgt.cap_target THEN
    RAISE EXCEPTION 'sasaran sudah penuh (batas % aksi)', tgt.cap_target USING ERRCODE = 'LG003';
  END IF;

  -- seq kedua sisi sudah pasti (baris terkunci) -> bisa saling di-hash
  SELECT * INTO wt FROM ledger_write_log(tool, 4::smallint, p_actor, p_actor_member, NULL, NULL, v_now,
                                         tgt.id, tgt.last_seq + 1, p_action, NULL);
  SELECT * INTO wg FROM ledger_write_log(tgt, 7::smallint, p_actor, p_actor_member, NULL, p_payload, v_now,
                                         tool.id, tool.last_seq + 1, p_action, p_content_hash);
  UPDATE ledger_collections
     SET last_seq = wt.new_seq, last_hash = wt.new_hash, tool_uses = tool_uses + 1, updated_at = v_now
   WHERE id = tool.id;
  UPDATE ledger_collections
     SET last_seq = wg.new_seq, last_hash = wg.new_hash, target_acts = target_acts + 1, updated_at = v_now
   WHERE id = tgt.id;
  RETURN QUERY SELECT wt.new_seq, wg.new_seq;
END $$;

-- ---------------------------------------------------------------------
-- 8. VERIFIKASI: hash entry versi baru + kapasitas collection harus sama dengan entry
--    + jumlah pemakaian hasil replay tidak boleh melebihi kapasitas
-- ---------------------------------------------------------------------
CREATE OR REPLACE FUNCTION ledger_verify_chain(p_collection_id bigint)
RETURNS TABLE (valid boolean, broken_at_seq int, detail text)
LANGUAGE plpgsql STABLE AS $$
DECLARE
  c ledger_collections; e ledger_entries; r ledger_logs; o ledger_logs;
  v_prev bytea; v_seq int := 0; v_err text;
  v_owner uuid; v_state smallint := 1; v_tool int := 0; v_target int := 0;
BEGIN
  SELECT * INTO c FROM ledger_collections WHERE id = p_collection_id;
  IF NOT FOUND THEN RETURN QUERY SELECT false, NULL::int, 'collection tidak ada'; RETURN; END IF;
  SELECT * INTO e FROM ledger_entries WHERE id = c.entry_id;
  IF e.content_hash <> ledger_entry_hash(e.id, e.issuer_id, e.issuer_member_id, e.reason, e.supply, e.kind,
                                         e.metadata, e.created_at, e.cap_version, e.max_as_tool, e.max_as_target) THEN
    RETURN QUERY SELECT false, 0, 'hash entry tidak cocok'; RETURN;
  END IF;
  IF c.kind <> e.kind THEN
    RETURN QUERY SELECT false, 0, 'kind collection berbeda dari entry'; RETURN;
  END IF;
  IF c.cap_tool IS DISTINCT FROM e.max_as_tool OR c.cap_target IS DISTINCT FROM e.max_as_target THEN
    RETURN QUERY SELECT false, 0, 'kapasitas collection berbeda dari entry'; RETURN;
  END IF;

  v_prev := ledger_genesis_hash(e.content_hash, c.id, c.serial_no);
  FOR r IN SELECT * FROM ledger_logs WHERE collection_id = p_collection_id ORDER BY seq LOOP
    v_seq := v_seq + 1;
    IF r.seq <> v_seq THEN
      RETURN QUERY SELECT false, v_seq, 'urutan seq bolong'; RETURN;
    END IF;
    IF r.hash <> ledger_log_hash(v_prev, r.collection_id, r.seq, r.event_type, r.actor_id, r.actor_member_id,
                                 r.counterparty_id, r.created_at, r.payload,
                                 r.target_id, r.target_seq, r.action_id, r.content_hash) THEN
      RETURN QUERY SELECT false, r.seq, 'hash log tidak cocok'; RETURN;
    END IF;
    v_prev := r.hash;

    v_err := NULL;                       -- replay aturan bisnis
    IF (r.seq = 1) <> (r.event_type = 1) THEN v_err := 'MINT harus tepat di seq 1';
    ELSIF r.event_type = 1 THEN
      IF r.actor_id <> e.issuer_id THEN v_err := 'MINT bukan oleh association penerbit entry'; END IF;
      v_owner := r.actor_id;
    ELSIF r.event_type = 2 THEN
      IF v_state <> 1 OR v_owner <> r.actor_id THEN v_err := 'SEND tidak valid'; END IF;
      v_state := 2;
    ELSIF r.event_type = 3 THEN
      IF v_state <> 2 OR r.counterparty_id <> v_owner THEN v_err := 'RECEIVE tidak valid'; END IF;
      v_owner := r.actor_id; v_state := 1;
    ELSIF r.event_type = 4 THEN
      IF v_state <> 1 OR v_owner <> r.actor_id THEN v_err := 'USE tidak valid'; END IF;
      v_tool := v_tool + 1;
    ELSIF r.event_type = 5 THEN
      IF v_state <> 2 OR v_owner <> r.actor_id THEN v_err := 'CANCEL tidak valid'; END IF;
      v_state := 1;
    ELSIF r.event_type = 6 THEN
      -- ASSIGN: ganti pemegang (member) dalam association pemilik; owner & state tetap
      IF v_state <> 1 OR v_owner <> r.actor_id THEN v_err := 'ASSIGN tidak valid'; END IF;
    ELSIF r.event_type = 7 THEN
      -- pelaku boleh bukan pemilik (rule target_access 2/3), jadi owner TIDAK dicek di sini;
      -- pasangannya (log USE di chain alat) yang mewajibkan pelaku = pemilik alat.
      IF r.target_id IS NULL OR r.action_id IS NULL THEN v_err := 'ACTED_ON tidak valid'; END IF;
      v_target := v_target + 1;
    ELSE v_err := 'event_type tidak dikenal';
    END IF;

    -- tautan silang alat <-> sasaran: log pasangannya harus ada dan menunjuk balik
    IF v_err IS NULL AND r.target_id IS NOT NULL THEN
      IF r.event_type NOT IN (4, 7) THEN
        v_err := 'target hanya untuk USE/WRITE';
      ELSE
        SELECT * INTO o FROM ledger_logs WHERE collection_id = r.target_id AND seq = r.target_seq;
        IF NOT FOUND
           OR (r.event_type = 4 AND o.event_type <> 7) OR (r.event_type = 7 AND o.event_type <> 4)
           OR o.target_id IS DISTINCT FROM r.collection_id OR o.target_seq IS DISTINCT FROM r.seq
           OR o.actor_id <> r.actor_id OR o.actor_member_id <> r.actor_member_id
           OR o.created_at <> r.created_at
           OR o.action_id IS DISTINCT FROM r.action_id THEN
          v_err := 'tautan pena<->jurnal tidak cocok (log pasangan hilang/berubah)';
        END IF;
      END IF;
    END IF;
    IF v_err IS NOT NULL THEN RETURN QUERY SELECT false, r.seq, v_err; RETURN; END IF;

    -- kapasitas tidak boleh terlampaui di titik mana pun dalam riwayat
    IF r.event_type = 4 AND c.cap_tool IS NOT NULL AND v_tool > c.cap_tool THEN
      RETURN QUERY SELECT false, r.seq, 'pemakaian sebagai alat melebihi kapasitas'; RETURN;
    END IF;
    IF r.event_type = 7 AND c.cap_target IS NOT NULL AND v_target > c.cap_target THEN
      RETURN QUERY SELECT false, r.seq, 'aksi sebagai sasaran melebihi kapasitas'; RETURN;
    END IF;
  END LOOP;

  IF v_seq <> c.last_seq OR v_prev IS DISTINCT FROM c.last_hash THEN
    RETURN QUERY SELECT false, v_seq, 'ujung chain tidak cocok dengan collections.last_hash'; RETURN;
  END IF;
  IF v_tool <> c.tool_uses OR v_target <> c.target_acts THEN
    RETURN QUERY SELECT false, v_seq, 'penghitung tool_uses/target_acts tidak cocok dengan log'; RETURN;
  END IF;
  IF v_owner <> c.owner_id OR v_state <> c.state THEN
    RETURN QUERY SELECT false, v_seq, 'state hasil replay tidak cocok dengan baris collections'; RETURN;
  END IF;
  RETURN QUERY SELECT true, NULL::int, 'ok';
END $$;

-- ---------------------------------------------------------------------
-- 9. HAK AKSES untuk signature baru (yang lama ikut terhapus bersama DROP)
-- ---------------------------------------------------------------------
REVOKE EXECUTE ON FUNCTION
  ledger_create_entry(uuid, uuid, text, int, jsonb, smallint, int, int),
  ledger_update_entry(bigint, uuid, uuid, text, int, jsonb, smallint, int, int)
FROM PUBLIC;

DO $$
BEGIN
  IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'ledger_app') THEN
    GRANT EXECUTE ON FUNCTION
      ledger_create_entry(uuid, uuid, text, int, jsonb, smallint, int, int),
      ledger_update_entry(bigint, uuid, uuid, text, int, jsonb, smallint, int, int)
    TO ledger_app;
  END IF;
END $$;

-- ---------------------------------------------------------------------
-- 10. PEMERIKSAAN AKHIR: bila salah satu gagal, seluruh migrasi dibatalkan (ROLLBACK otomatis)
-- ---------------------------------------------------------------------
DO $$
DECLARE v_bad bigint;
BEGIN
  -- (a) hash tiap entry (lama maupun baru) masih cocok dengan rumusnya
  SELECT count(*) INTO v_bad FROM ledger_entries e
   WHERE e.content_hash <> ledger_entry_hash(e.id, e.issuer_id, e.issuer_member_id, e.reason, e.supply, e.kind,
                                             e.metadata, e.created_at, e.cap_version, e.max_as_tool, e.max_as_target);
  IF v_bad > 0 THEN
    RAISE EXCEPTION 'migrasi 0002 gagal: % entry hash-nya tidak cocok', v_bad;
  END IF;

  -- (b) kapasitas semua collection sama dengan entry-nya
  SELECT count(*) INTO v_bad FROM ledger_collections c JOIN ledger_entries e ON e.id = c.entry_id
   WHERE c.cap_tool IS DISTINCT FROM e.max_as_tool OR c.cap_target IS DISTINCT FROM e.max_as_target;
  IF v_bad > 0 THEN
    RAISE EXCEPTION 'migrasi 0002 gagal: % collection kapasitasnya berbeda dari entry', v_bad;
  END IF;

  -- (c) tidak ada collection yang sudah melewati kapasitas barunya
  SELECT count(*) INTO v_bad FROM ledger_collections
   WHERE (cap_tool IS NOT NULL AND tool_uses > cap_tool) OR (cap_target IS NOT NULL AND target_acts > cap_target);
  IF v_bad > 0 THEN
    RAISE NOTICE 'peringatan: % collection sudah melewati kapasitas default kind (data lama); periksa manual', v_bad;
  END IF;
END $$;

COMMIT;

-- =====================================================================
--  CONTOH PEMAKAIAN (jangan dijalankan sebagai bagian migrasi)
-- =====================================================================
--  -- penjual A: jurnal 120 halaman; penjual B: jurnal 200 halaman; kind sama
--  SELECT ledger_create_entry(:sellerA, :memA, 'Jurnal A5 polos',  500, '{"pages":120}', :kind_jurnal, NULL, 120);
--  SELECT ledger_create_entry(:sellerB, :memB, 'Jurnal dotted',    300, '{"pages":200}', :kind_jurnal, NULL, 200);
--  -- jurnal tanpa batas halaman (buku tamu digital):
--  SELECT ledger_create_entry(:sellerC, :memC, 'Buku tamu',         50, '{}',            :kind_jurnal, NULL, -1);
--  -- varian 80 halaman = entry terpisah (satu entry = satu kapasitas)
--  -- koreksi sebelum mint:
--  SELECT ledger_update_entry(:entry_id, :sellerA, :memA, NULL, NULL, NULL, NULL, NULL, 96);
--
--  -- periksa kapasitas terpasang:
--  SELECT e.id, e.reason, e.max_as_tool, e.max_as_target, e.cap_version FROM ledger_entries e ORDER BY e.id DESC LIMIT 10;