-- =====================================================================
--  LEDGER (pseudo-blockchain di atas PostgreSQL)  --  PostgreSQL >= 14
-- =====================================================================
--  entries      = alasan / landasan terjadinya sesuatu   ("cetak 1000 stiker")
--  collections  = tiap item hasil entry                   (stiker #1 .. #1000)
--  logs         = riwayat append-only tiap collection     (lahir, kirim, terima, pakai)
--  SEMUA JENIS BENDA & AKSI ADALAH DATA (registry), bukan kode:
--    ledger_kinds        jenis item   : pena, jurnal, surat, perangko, kamera, rol film, ...
--                        max_as_tool  = batas pemakaian sebagai ALAT   (perangko = 1)
--                        max_as_target= batas aksi sebagai SASARAN     (rol film = 36)
--    ledger_actions      kata kerja   : menulis, menempel, mengecap, memotret, memutar, ...
--    ledger_action_rules siapa boleh apa: (aksi, jenis alat, jenis sasaran, target_access)
--  Hobi baru = INSERT beberapa baris. Tidak perlu migrasi skema atau fungsi baru.
--
--  ACT = dua item bertemu (ledger_act). Dicatat DUA SISI dalam satu transaksi,
--  saling menunjuk (target_id + target_seq):
--     chain alat   : USE      (alat ini dipakai pada sasaran S, log ke-n sasaran)
--     chain sasaran: ACTED_ON (sasaran dikenai aksi; content_hash = sha256 isinya)
--  Pelaku selalu pemegang ALAT. Siapa yang boleh jadi pelaku terhadap SASARAN diatur
--  rule.target_access: 1 pemilik sasaran | 2 siapa pun, saat sasaran DIKIRIM (cap pos)
--                      | 3 siapa pun, sasaran aktif (buku tamu).
--  Aksi tunggal (membaca surat, dst.) = ledger_use. Isi (teks, foto) TIDAK PERNAH
--  masuk ledger (tidak bisa dihapus); cukup sha256-nya.
--  holdings     = read-model "siapa pegang apa" (turunan dari logs)
--  transfer_tokens = token rahasia untuk klaim item (via QR)
--  blocks       = checkpoint global (Merkle root per jendela waktu)
--
--  IDENTITAS:  association -> member -> user
--    * KEPEMILIKAN menempel ke ASSOCIATION (entries.issuer_id, collections.owner_id,
--      holdings.owner_id, logs.actor_id/counterparty_id, transfer_tokens.*_association_id),
--      bukan ke user. Association tidak "meninggal": anggotanya boleh berganti, riwayat utuh.
--    * Tiap tulis juga mencatat MEMBER yang bertindak (issuer_member_id / actor_member_id /
--      from_member_id / claimed_by_member_id). Member masuk ke HASH -> tidak bisa ditulis ulang.
--    * Ledger hanya menyimpan UUID. Apakah member itu benar anggota association-nya dicek di
--      lapis aplikasi (services._check_member): tidak ada FK ke tabel tenant (prinsip no.5).
--    * Pewarisan di dalam association = menambah member baru; tak ada yang ditulis ke ledger.
--      Pewarisan ke association lain = send/claim biasa. Jangan HAPUS baris member (UUID-nya
--      ada di hash); nonaktifkan saja.
--
--  Prinsip desain:
--   1. Per-collection hash chain, BUKAN satu chain global.
--      Chain global = antrean tunggal = tidak bisa scale. Per-collection chain
--      berarti tiap operasi hanya menyentuh SATU collection -> paralel penuh.
--   2. Keamanan global didapat dari `ledger_blocks` (Merkle root berkala,
--      saling bertaut). Tidak ada lock global di jalur tulis.
--   3. Semua tulis lewat fungsi SECURITY DEFINER (satu round-trip, atomik).
--      Role aplikasi hanya boleh SELECT + EXECUTE.
--   4. Tabel besar dipartisi HASH; kunci partisi = collection_id (logs,
--      collections) sehingga satu collection = satu partisi = satu shard.
--      Siap dipindah ke Citus / multi-node tanpa ubah model data.
--   5. Tidak ada FK ke tabel besar (logs/holdings) dan ke tabel association:
--      FK = lookup tambahan di setiap INSERT dan menghalangi sharding.
--      Integritas dijaga oleh fungsi tulis + verifikasi chain.
--   6. Baris logs dibuat sesempit mungkin (~100 byte). Di skala triliun,
--      1 byte per baris = 1 TB.
-- =====================================================================

-- Kode error (SQLSTATE) khusus, dipetakan ke exception Python di services.py
--   LG001 tidak ditemukan | LG002 bukan hak anda | LG003 state tidak valid
--   LG004 input tidak valid | LG005 tabel append-only

-- ---------------------------------------------------------------------
-- 0. Sequence ID
--    Multi-node nanti:  INCREMENT BY <jumlah_node> START <nomor_node>
-- ---------------------------------------------------------------------
CREATE SEQUENCE ledger_entry_id_seq      AS bigint;
CREATE SEQUENCE ledger_collection_id_seq AS bigint CACHE 1000;

-- ---------------------------------------------------------------------
-- 1. Fungsi hash (deterministik, bisa direplikasi di Python/verifier luar)
-- ---------------------------------------------------------------------
CREATE FUNCTION ledger_micros(ts timestamptz) RETURNS bigint
LANGUAGE sql IMMUTABLE PARALLEL SAFE AS
$$ SELECT floor(extract(epoch FROM ts) * 1000000)::bigint $$;

-- hash entry = sidik jari "alasan" -> menjadi benih semua chain di bawahnya
CREATE FUNCTION ledger_entry_hash(p_id bigint, p_issuer uuid, p_issuer_member uuid, p_reason text,
                                  p_supply int, p_kind smallint, p_metadata jsonb, p_ts timestamptz)
RETURNS bytea LANGUAGE sql IMMUTABLE PARALLEL SAFE AS
$$ SELECT sha256(convert_to(
       p_id::text || '|' || p_issuer::text || '|' || p_issuer_member::text || '|' || p_reason || '|' ||
       p_supply::text || '|' || p_kind::text || '|' || p_metadata::text || '|' || ledger_micros(p_ts)::text,
       'UTF8')) $$;

-- hash "sebelum log #1" suatu collection
CREATE FUNCTION ledger_genesis_hash(p_entry_hash bytea, p_collection_id bigint, p_serial varchar)
RETURNS bytea LANGUAGE sql IMMUTABLE PARALLEL SAFE AS
$$ SELECT sha256(p_entry_hash ||
       convert_to(p_collection_id::text || '|' || p_serial::text, 'UTF8')) $$;

-- hash log ke-n = sha256(hash log ke-(n-1) || isi log ke-n)
CREATE FUNCTION ledger_log_hash(p_prev bytea, p_collection_id bigint, p_seq int,
                                p_event smallint, p_actor uuid, p_actor_member uuid, p_counterparty uuid,
                                p_ts timestamptz, p_payload jsonb,
                                p_target_id bigint DEFAULT NULL, p_target_seq int DEFAULT NULL,
                                p_action_id smallint DEFAULT NULL, p_content_hash bytea DEFAULT NULL)
RETURNS bytea LANGUAGE sql IMMUTABLE PARALLEL SAFE AS
$$ SELECT sha256(p_prev || convert_to(
       p_collection_id::text || '|' || p_seq::text || '|' || p_event::text || '|' ||
       p_actor::text || '|' || p_actor_member::text || '|' || coalesce(p_counterparty::text, '') || '|' ||
       ledger_micros(p_ts)::text || '|' || coalesce(p_payload::text, '') || '|' ||
       coalesce(p_target_id::text, '') || '|' || coalesce(p_target_seq::text, '') || '|' ||
       coalesce(p_action_id::text, '') || '|' || coalesce(encode(p_content_hash, 'hex'), ''),
       'UTF8')) $$;

-- ---------------------------------------------------------------------
-- 1b. REGISTRY (kecil, jarang berubah). id = bagian dari hash -> TETAP, jangan dipakai ulang.
-- ---------------------------------------------------------------------
CREATE TABLE ledger_kinds (
    id            smallint PRIMARY KEY,
    code          text     NOT NULL UNIQUE,
    label         text     NOT NULL,
    max_as_tool   integer  CHECK (max_as_tool   > 0),   -- NULL = tak terbatas (perangko = 1)
    max_as_target integer  CHECK (max_as_target > 0),   -- NULL = tak terbatas (rol film = 36)
    restricted    boolean  NOT NULL DEFAULT false       -- true: hanya penerbit resmi (ledger_kind_issuers)
);
-- Penerbit resmi untuk jenis `restricted` (mis. hanya kantor pos yang boleh membuat cap pos).
-- Tanpa ini, siapa pun bisa membuat "cap pos" palsu dan mengecap surat orang lain.
CREATE TABLE ledger_kind_issuers (
    kind_id        smallint NOT NULL REFERENCES ledger_kinds (id),
    association_id uuid   NOT NULL,
    PRIMARY KEY (kind_id, association_id)
);
CREATE TABLE ledger_actions (
    id    smallint PRIMARY KEY,
    code  text     NOT NULL UNIQUE,
    label text     NOT NULL
);
CREATE TABLE ledger_action_rules (
    action_id     smallint NOT NULL REFERENCES ledger_actions (id),
    tool_kind     smallint NOT NULL REFERENCES ledger_kinds (id),
    target_kind   smallint NOT NULL REFERENCES ledger_kinds (id),
    target_access smallint NOT NULL DEFAULT 1 CHECK (target_access IN (1, 2, 3)),
    PRIMARY KEY (action_id, tool_kind, target_kind)
);
INSERT INTO ledger_kinds (id, code, label) VALUES (0, 'generic', 'benda umum');

-- ---------------------------------------------------------------------
-- 2. ENTRIES  (tidak dipartisi: jumlahnya jauh lebih kecil dari collections)
-- ---------------------------------------------------------------------
CREATE TABLE ledger_entries (
    id            bigint      PRIMARY KEY DEFAULT nextval('ledger_entry_id_seq'),
    issuer_id        uuid     NOT NULL,                 -- association penerbit
    issuer_member_id uuid     NOT NULL,                 -- member yang membuat entry
    reason        text        NOT NULL,                 -- alasan / landasan
    supply        integer     NOT NULL CHECK (supply > 0),  -- jumlah TOTAL yang boleh ada, selamanya
    kind          smallint    NOT NULL DEFAULT 0 REFERENCES ledger_kinds (id),
    minted_count  integer     NOT NULL DEFAULT 0,
    metadata      jsonb       NOT NULL DEFAULT '{}',
    content_hash  bytea       NOT NULL,
    created_at    timestamptz NOT NULL,
    CHECK (minted_count BETWEEN 0 AND supply)
);
CREATE INDEX ledger_entries_issuer_idx ON ledger_entries (issuer_id, id DESC);

-- ---------------------------------------------------------------------
-- 3. COLLECTIONS  (hash-partition by id)
--    Kolom yang sering berubah (last_seq/last_hash/state/owner) TIDAK diindeks
--    -> UPDATE bisa HOT (tanpa menyentuh index). Pencarian "milik siapa"
--    dilayani ledger_holdings.
--    Catatan: UNIQUE (entry_id, serial_no) tidak bisa global karena PostgreSQL
--    mewajibkan kolom partisi ada di unique index. Keunikan dijamin
--    ledger_mint_batch (serial = minted_count+1.., di bawah row-lock entry).
-- ---------------------------------------------------------------------
CREATE TABLE ledger_collections (
    id            bigint      NOT NULL DEFAULT nextval('ledger_collection_id_seq'),
    entry_id      bigint      NOT NULL REFERENCES ledger_entries (id),
    serial_no     varchar(100) NOT NULL,
    owner_id      uuid        NOT NULL,                 -- association pemilik SAAT INI (berubah tiap transfer)
    holder_id     uuid,                                -- member pemegang SAAT INI (dalam asosiasi yang sama)
                                                      -- NULL = belum ditetapkan ke member tertentu
    state         smallint    NOT NULL DEFAULT 1 CHECK (state IN (1, 2)),  -- 1 aktif, 2 dalam pengiriman
    kind          smallint    NOT NULL,                                    -- salinan entries.kind (beku)
    last_seq      integer     NOT NULL,
    tool_uses     integer     NOT NULL DEFAULT 0,   -- berapa kali dipakai sebagai ALAT
    target_acts   integer     NOT NULL DEFAULT 0,   -- berapa kali dikenai aksi sebagai SASARAN
    last_hash     bytea       NOT NULL,
    created_at    timestamptz NOT NULL,
    updated_at    timestamptz NOT NULL,
    PRIMARY KEY (id)
) PARTITION BY HASH (id);

-- ---------------------------------------------------------------------
-- 4. LOGS  (hash-partition by collection_id) -- tabel terbesar
--    PK (collection_id, seq): riwayat 1 collection = 1 range scan di 1 partisi.
--    prev_hash TIDAK disimpan (= hash baris seq-1), hemat ~33 byte/baris.
-- ---------------------------------------------------------------------
CREATE TABLE ledger_logs (
    collection_id   bigint      NOT NULL,
    actor_id        uuid        NOT NULL,   -- association pelaku
    actor_member_id uuid        NOT NULL,   -- member yang bertindak atas nama association itu
    counterparty_id uuid,                   -- association lawan (asal terima)
    target_id       bigint,                 -- USE/ACTED_ON: collection pasangan (alat<->sasaran)
    created_at      timestamptz NOT NULL,
    seq             integer     NOT NULL,   -- urutan per collection: 1,2,3,...
    target_seq      integer,                -- seq log pasangan di collection target
    event_type      smallint    NOT NULL,   -- 1 MINT, 2 SEND, 3 RECEIVE, 4 USE (alat), 5 CANCEL_SEND, 6 ASSIGN, 7 ACTED_ON (sasaran)
    action_id       smallint,               -- USE/ACTED_ON: kata kerja dari ledger_actions
    hash            bytea       NOT NULL,   -- sha256, 32 byte
    content_hash    bytea,                  -- ACTED_ON: sha256 isi (tulisan/foto/...). Isi asli di luar ledger
    payload         jsonb,                  -- metadata kecil bebas (maks 4 KB). JANGAN isi data pribadi
    state_snapshot  jsonb,                  -- JSON snapshot Collection state saat log dibuat (untuk rekonstruksi)
    PRIMARY KEY (collection_id, seq)
) PARTITION BY HASH (collection_id);

-- ---------------------------------------------------------------------
-- 5. HOLDINGS  (hash-partition by owner_id) -- "item milik association ini"
-- ---------------------------------------------------------------------
CREATE TABLE ledger_holdings (
    owner_id      uuid        NOT NULL,                 -- association pemilik
    collection_id bigint      NOT NULL,
    entry_id      bigint      NOT NULL,
    acquired_at   timestamptz NOT NULL,
    PRIMARY KEY (owner_id, collection_id)
) PARTITION BY HASH (owner_id);

-- ---------------------------------------------------------------------
-- 6. TRANSFER TOKENS (tidak dipartisi) -- token QR; satu aktif per collection
-- ---------------------------------------------------------------------
CREATE TABLE ledger_transfer_tokens (
    token           uuid        PRIMARY KEY DEFAULT gen_random_uuid(),
    collection_id   bigint      NOT NULL,
    from_association_id uuid    NOT NULL,
    from_member_id  uuid        NOT NULL,               -- member pengirim
    created_at      timestamptz NOT NULL,
    expires_at      timestamptz,                  -- opsional: token kedaluwarsa
    claimed_by_id   uuid,                         -- association penerima; NULL sampai diklaim
    claimed_by_member_id uuid,                    -- member penerima;      NULL sampai diklaim
    claimed_at      timestamptz,                  -- NULL sampai diklaim
    CHECK ((claimed_by_id IS NULL) = (claimed_at IS NULL) AND (claimed_by_member_id IS NULL) = (claimed_by_id IS NULL))
);

-- Hanya satu token aktif (belum diklaim) per collection
CREATE UNIQUE INDEX ledger_transfer_tokens_active_idx
    ON ledger_transfer_tokens (collection_id) WHERE claimed_by_id IS NULL;

-- ---------------------------------------------------------------------
-- 7. BLOCKS -- checkpoint global, saling bertaut
-- ---------------------------------------------------------------------
CREATE TABLE ledger_blocks (
    block_no        bigint      PRIMARY KEY,
    window_start    timestamptz NOT NULL UNIQUE,
    window_end      timestamptz NOT NULL,
    log_count       bigint      NOT NULL,
    merkle_root     bytea       NOT NULL,
    prev_block_hash bytea       NOT NULL,
    block_hash      bytea       NOT NULL,
    sealed_at       timestamptz NOT NULL DEFAULT now(),
    CHECK (window_end > window_start)
);

-- ---------------------------------------------------------------------
-- 8. PARTISI.  Jumlah partisi TIDAK bisa diubah mudah -> tentukan di awal.
--    64 untuk satu node; skala triliun => shard ke banyak node (lihat catatan).
-- ---------------------------------------------------------------------
CREATE FUNCTION ledger_make_partitions(p_parent text, p_modulus int, p_opts text DEFAULT '')
RETURNS void LANGUAGE plpgsql AS $$
BEGIN
  FOR i IN 0 .. p_modulus - 1 LOOP
    EXECUTE format(
      'CREATE TABLE %I PARTITION OF %I FOR VALUES WITH (MODULUS %s, REMAINDER %s) %s',
      p_parent || '_p' || lpad(i::text, 3, '0'), p_parent, p_modulus, i, p_opts);
  END LOOP;
END $$;

SELECT ledger_make_partitions('ledger_collections', 64,
  'WITH (fillfactor = 80, autovacuum_vacuum_scale_factor = 0.02, autovacuum_analyze_scale_factor = 0.02)');
-- logs append-only: vacuum dipicu oleh INSERT (freeze + visibility map), freeze langsung
SELECT ledger_make_partitions('ledger_logs', 64,
  'WITH (autovacuum_vacuum_insert_scale_factor = 0.02, autovacuum_freeze_min_age = 0)');
SELECT ledger_make_partitions('ledger_holdings', 64,
  'WITH (autovacuum_vacuum_scale_factor = 0.02)');

DROP FUNCTION ledger_make_partitions(text, int, text);

-- ---------------------------------------------------------------------
-- 9. INDEX (tiap index = biaya tulis + penyimpanan di skala besar)
-- ---------------------------------------------------------------------
-- daftar item milik issuer per entry (fan-out ke semua partisi; jarang dipakai)
CREATE INDEX ledger_collections_entry_idx ON ledger_collections (entry_id, serial_no);

-- feed aktivitas association. INDEX TERMAHAL (~25% storage logs).
-- Buang jika feed association dilayani dari store lain.
CREATE INDEX ledger_logs_actor_idx ON ledger_logs (actor_id, created_at DESC);

-- BRIN super kecil: dipakai sealer blok untuk membaca log per jendela waktu
CREATE INDEX ledger_logs_created_brin ON ledger_logs USING brin (created_at) WITH (pages_per_range = 64);

CREATE INDEX ledger_holdings_entry_idx ON ledger_holdings (owner_id, entry_id);
CREATE INDEX ledger_transfer_tokens_from_idx ON ledger_transfer_tokens (from_association_id, created_at DESC);
CREATE INDEX ledger_transfer_tokens_collection_idx ON ledger_transfer_tokens (collection_id, created_at DESC);



-- ---------------------------------------------------------------------
-- 10. IMMUTABILITY (trigger row-level hanya "menyala" bila ada yang mencoba
--     UPDATE/DELETE -> biaya nol di jalur INSERT normal)
-- ---------------------------------------------------------------------
CREATE FUNCTION ledger_forbid_mutation() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  RAISE EXCEPTION '% pada % dilarang (append-only)', TG_OP, TG_TABLE_NAME USING ERRCODE = 'LG005';
END $$;

-- Immutability: kolom identitas beku
CREATE TRIGGER ledger_transfer_tokens_frozen BEFORE UPDATE ON ledger_transfer_tokens
  FOR EACH ROW
  WHEN ((OLD.token, OLD.collection_id, OLD.from_association_id, OLD.from_member_id, OLD.created_at)
        IS DISTINCT FROM (NEW.token, NEW.collection_id, NEW.from_association_id, NEW.from_member_id, NEW.created_at))
  EXECUTE FUNCTION ledger_forbid_mutation();

CREATE TRIGGER ledger_logs_append_only   BEFORE UPDATE OR DELETE ON ledger_logs
  FOR EACH ROW EXECUTE FUNCTION ledger_forbid_mutation();
CREATE TRIGGER ledger_logs_no_truncate   BEFORE TRUNCATE ON ledger_logs
  FOR EACH STATEMENT EXECUTE FUNCTION ledger_forbid_mutation();
CREATE TRIGGER ledger_blocks_append_only BEFORE UPDATE OR DELETE ON ledger_blocks
  FOR EACH ROW EXECUTE FUNCTION ledger_forbid_mutation();

-- collection & entry: tidak boleh dihapus; kolom identitasnya beku
CREATE TRIGGER ledger_collections_no_delete BEFORE DELETE ON ledger_collections
  FOR EACH ROW EXECUTE FUNCTION ledger_forbid_mutation();
CREATE TRIGGER ledger_collections_no_truncate BEFORE TRUNCATE ON ledger_collections
  FOR EACH STATEMENT EXECUTE FUNCTION ledger_forbid_mutation();
CREATE TRIGGER ledger_collections_frozen BEFORE UPDATE ON ledger_collections
  FOR EACH ROW
  WHEN ((OLD.id, OLD.entry_id, OLD.serial_no, OLD.kind, OLD.created_at)
        IS DISTINCT FROM (NEW.id, NEW.entry_id, NEW.serial_no, NEW.kind, NEW.created_at))
  EXECUTE FUNCTION ledger_forbid_mutation();

CREATE TRIGGER ledger_kinds_frozen BEFORE UPDATE ON ledger_kinds
  FOR EACH ROW WHEN ((OLD.id, OLD.code) IS DISTINCT FROM (NEW.id, NEW.code))
  EXECUTE FUNCTION ledger_forbid_mutation();
CREATE TRIGGER ledger_kinds_no_delete BEFORE DELETE ON ledger_kinds
  FOR EACH ROW EXECUTE FUNCTION ledger_forbid_mutation();
CREATE TRIGGER ledger_actions_frozen BEFORE UPDATE ON ledger_actions
  FOR EACH ROW WHEN ((OLD.id, OLD.code) IS DISTINCT FROM (NEW.id, NEW.code))
  EXECUTE FUNCTION ledger_forbid_mutation();
CREATE TRIGGER ledger_actions_no_delete BEFORE DELETE ON ledger_actions
  FOR EACH ROW EXECUTE FUNCTION ledger_forbid_mutation();

CREATE TRIGGER ledger_entries_no_delete BEFORE DELETE ON ledger_entries
  FOR EACH ROW EXECUTE FUNCTION ledger_forbid_mutation();
CREATE TRIGGER ledger_entries_frozen BEFORE UPDATE ON ledger_entries
  FOR EACH ROW
  WHEN ((OLD.id, OLD.issuer_id, OLD.issuer_member_id, OLD.reason, OLD.supply, OLD.kind, OLD.metadata, OLD.content_hash, OLD.created_at)
        IS DISTINCT FROM (NEW.id, NEW.issuer_id, NEW.issuer_member_id, NEW.reason, NEW.supply, NEW.kind, NEW.metadata, NEW.content_hash, NEW.created_at))
  EXECUTE FUNCTION ledger_forbid_mutation();

-- ---------------------------------------------------------------------
-- 11. FUNGSI TULIS (satu-satunya jalan menulis)
-- ---------------------------------------------------------------------

-- Association DAN member wajib ada. Tanpa ini NULL lolos diam-diam di perbandingan
-- (NULL <> x = NULL) lalu gagal dengan galat NOT NULL yang membingungkan.
CREATE FUNCTION ledger_require_actor(p_association uuid, p_member uuid) RETURNS void
LANGUAGE plpgsql AS $$
BEGIN
  IF p_association IS NULL OR p_member IS NULL THEN
    RAISE EXCEPTION 'association dan member wajib diisi' USING ERRCODE = 'LG004';
  END IF;
END $$;

-- internal: generate JSON snapshot dari Collection state (untuk rekonstruksi)
CREATE FUNCTION ledger_collection_snapshot(c ledger_collections) RETURNS jsonb
LANGUAGE sql STABLE AS $$
SELECT jsonb_build_object(
  'id', c.id,
  'entry_id', c.entry_id,
  'serial_no', c.serial_no,
  'owner_id', c.owner_id,
  'holder_id', c.holder_id,
  'state', c.state,
  'kind', c.kind,
  'last_seq', c.last_seq,
  'tool_uses', c.tool_uses,
  'target_acts', c.target_acts,
  'last_hash', encode(c.last_hash, 'hex'),
  'created_at', c.created_at,
  'updated_at', c.updated_at,
  'asset', (
    SELECT jsonb_build_object(
      'id', a.id,
      'storage_path', a.storage_path,
      'content_hash', a.content_hash,
      'original_filename', a.original_filename,
      'file_size', a.file_size,
      'mime_type', a.mime_type,
      'status', a.status,
      'metadata', a.metadata,
      'uploaded_at', a.uploaded_at,
      'updated_at', a.updated_at
    )
    FROM ledger_assets a
    WHERE a.collection_id = c.id AND a.status = 'active'
    LIMIT 1
  )
) $$;

-- internal: tulis satu log + hitung hash chain. Dipanggil di bawah row-lock collection.
CREATE FUNCTION ledger_write_log(c ledger_collections, p_event smallint, p_actor uuid, p_actor_member uuid,
                                 p_counterparty uuid, p_payload jsonb, p_ts timestamptz,
                                 p_target_id bigint DEFAULT NULL, p_target_seq int DEFAULT NULL,
                                 p_action_id smallint DEFAULT NULL, p_content_hash bytea DEFAULT NULL,
                                 p_state_snapshot jsonb DEFAULT NULL,
                                 OUT new_seq int, OUT new_hash bytea)
LANGUAGE plpgsql AS $$
BEGIN
  IF p_payload IS NOT NULL AND octet_length(p_payload::text) > 4096 THEN
    RAISE EXCEPTION 'payload maksimal 4096 byte' USING ERRCODE = 'LG004';
  END IF;
  new_seq  := c.last_seq + 1;
  new_hash := ledger_log_hash(c.last_hash, c.id, new_seq, p_event, p_actor, p_actor_member,
                              p_counterparty, p_ts, p_payload, p_target_id, p_target_seq,
                              p_action_id, p_content_hash);
  INSERT INTO ledger_logs (collection_id, actor_id, actor_member_id, counterparty_id, target_id, created_at,
                           seq, target_seq, event_type, action_id, hash, content_hash, payload, state_snapshot)
  VALUES (c.id, p_actor, p_actor_member, p_counterparty, p_target_id, p_ts, new_seq, p_target_seq,
          p_event, p_action_id, new_hash, p_content_hash, p_payload, p_state_snapshot);
END $$;

-- ENTRY: catat alasan + total supply. Item belum ada sampai di-mint.
CREATE FUNCTION ledger_create_entry(p_issuer uuid, p_issuer_member uuid, p_reason text, p_supply int,
                                    p_metadata jsonb DEFAULT '{}', p_kind smallint DEFAULT 0)
RETURNS bigint LANGUAGE plpgsql SECURITY DEFINER SET search_path = public, pg_temp AS $$
DECLARE v_id bigint := nextval('ledger_entry_id_seq'); v_now timestamptz := clock_timestamp();
BEGIN
  PERFORM ledger_require_actor(p_issuer, p_issuer_member);
  IF p_supply IS NULL OR p_supply < 1 THEN
    RAISE EXCEPTION 'supply harus >= 1' USING ERRCODE = 'LG004';
  END IF;
  IF p_reason IS NULL OR length(btrim(p_reason)) = 0 THEN
    RAISE EXCEPTION 'reason wajib diisi' USING ERRCODE = 'LG004';
  END IF;
  IF p_kind IS NULL OR NOT EXISTS (SELECT 1 FROM ledger_kinds WHERE id = p_kind) THEN
    RAISE EXCEPTION 'kind % tidak ada di registry', p_kind USING ERRCODE = 'LG004';
  END IF;
  IF EXISTS (SELECT 1 FROM ledger_kinds WHERE id = p_kind AND restricted)
     AND NOT EXISTS (SELECT 1 FROM ledger_kind_issuers WHERE kind_id = p_kind AND association_id = p_issuer) THEN
    RAISE EXCEPTION 'jenis "%" hanya boleh dibuat oleh penerbit resmi',
      (SELECT code FROM ledger_kinds WHERE id = p_kind) USING ERRCODE = 'LG002';
  END IF;
  p_metadata := coalesce(p_metadata, '{}'::jsonb);
  INSERT INTO ledger_entries (id, issuer_id, issuer_member_id, reason, supply, kind, metadata, content_hash, created_at)
  VALUES (v_id, p_issuer, p_issuer_member, p_reason, p_supply, p_kind, p_metadata,
          ledger_entry_hash(v_id, p_issuer, p_issuer_member, p_reason, p_supply, p_kind, p_metadata, v_now), v_now);
  RETURN v_id;
END $$;

-- MINT: lahirkan item berikutnya (serial minted_count+1 ...). Panggil berulang
-- (mis. batch 10.000) sampai minted_count = supply. Mengembalikan jumlah yang dibuat.
-- Item + log #1 + holdings dibuat dalam SATU statement.
CREATE FUNCTION ledger_mint_batch(p_entry_id bigint, p_issuer uuid, p_issuer_member uuid, p_batch int, p_prefix varchar DEFAULT '')
RETURNS int LANGUAGE plpgsql SECURITY DEFINER SET search_path = public, pg_temp AS $$
DECLARE e ledger_entries; v_from int; v_to int; v_now timestamptz := clock_timestamp();
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
  IF v_from > v_to THEN RETURN 0; END IF;     -- supply sudah penuh

  WITH base AS MATERIALIZED (
    SELECT x.id, x.serial_no,
           ledger_genesis_hash(e.content_hash, x.id, x.serial_no) AS genesis
    FROM (SELECT nextval('ledger_collection_id_seq') AS id,
                 p_prefix || ((((s::bigint * 38742041) + (p_entry_id * 1234567)) % 90000000) + 10000000)::text AS serial_no
          FROM generate_series(v_from, v_to) s) x
  ), new_cols AS (
    INSERT INTO ledger_collections (id, entry_id, serial_no, owner_id, holder_id, state, kind,
                                    last_seq, last_hash, created_at, updated_at)
    SELECT b.id, e.id, b.serial_no, p_issuer, p_issuer_member, 1, e.kind, 1, b.genesis, v_now, v_now
    FROM base b
    RETURNING id, entry_id, serial_no, owner_id, holder_id, state, kind, last_seq, last_hash, created_at, updated_at
  ), new_logs AS (
    INSERT INTO ledger_logs (collection_id, actor_id, actor_member_id, counterparty_id, created_at,
                             seq, event_type, hash, payload, state_snapshot)
    SELECT b.id, p_issuer, p_issuer_member, NULL, v_now, 1, 1::smallint,
           ledger_log_hash(b.genesis, b.id, 1, 1::smallint, p_issuer, p_issuer_member, NULL, v_now, NULL), NULL,
           ledger_collection_snapshot(c)
    FROM base b
    CROSS JOIN new_cols c ON c.id = b.id
    RETURNING collection_id, hash
  )
  INSERT INTO ledger_holdings (owner_id, collection_id, entry_id, acquired_at)
  SELECT p_issuer, id, e.id, v_now FROM new_cols;

  UPDATE ledger_entries SET minted_count = v_to WHERE id = e.id;
  RETURN v_to - v_from + 1;
END $$;

-- SEND: pemilik mengirim -> tanpa penerima. Kembalikan (seq, token UUID)
CREATE FUNCTION ledger_send(p_collection_id bigint, p_actor uuid, p_actor_member uuid,
                            p_payload jsonb DEFAULT NULL,
                            OUT new_seq int, OUT transfer_token uuid)
LANGUAGE plpgsql SECURITY DEFINER SET search_path = public, pg_temp AS $$
DECLARE c ledger_collections; w record; v_now timestamptz := clock_timestamp();
BEGIN
  PERFORM ledger_require_actor(p_actor, p_actor_member);
  SELECT * INTO c FROM ledger_collections WHERE id = p_collection_id FOR UPDATE;
  IF NOT FOUND THEN RAISE EXCEPTION 'collection % tidak ada', p_collection_id USING ERRCODE = 'LG001'; END IF;
  IF c.owner_id <> p_actor THEN RAISE EXCEPTION 'anda bukan pemilik' USING ERRCODE = 'LG002'; END IF;
  IF c.state <> 1 THEN RAISE EXCEPTION 'collection sedang dalam pengiriman' USING ERRCODE = 'LG003'; END IF;

  -- Log SEND: counterparty = NULL (penerima belum diketahui)
  SELECT * INTO w FROM ledger_write_log(c, 2::smallint, p_actor, p_actor_member, NULL, p_payload, v_now,
                                        NULL, NULL, NULL, NULL, ledger_collection_snapshot(c));
  transfer_token := gen_random_uuid();

  UPDATE ledger_collections
     SET state = 2, last_seq = w.new_seq, last_hash = w.new_hash, updated_at = v_now
   WHERE id = c.id;

  INSERT INTO ledger_transfer_tokens (token, collection_id, from_association_id, from_member_id, created_at)
  VALUES (transfer_token, c.id, p_actor, p_actor_member, v_now);

  new_seq := w.new_seq;
END $$;

-- CLAIM TRANSFER (Penerima): penerima mendapat item dari scan QR
CREATE FUNCTION ledger_claim_transfer(p_token uuid, p_actor uuid, p_actor_member uuid)
RETURNS int LANGUAGE plpgsql SECURITY DEFINER SET search_path = public, pg_temp AS $$
DECLARE
  t ledger_transfer_tokens; c ledger_collections; w record; v_now timestamptz := clock_timestamp();
BEGIN
  PERFORM ledger_require_actor(p_actor, p_actor_member);
  SELECT * INTO t FROM ledger_transfer_tokens WHERE token = p_token FOR UPDATE;
  IF NOT FOUND THEN RAISE EXCEPTION 'token transfer tidak ditemukan' USING ERRCODE = 'LG001'; END IF;
  IF t.claimed_by_id IS NOT NULL THEN RAISE EXCEPTION 'token sudah diklaim' USING ERRCODE = 'LG003'; END IF;
  IF t.expires_at IS NOT NULL AND t.expires_at < v_now THEN RAISE EXCEPTION 'token sudah kedaluwarsa' USING ERRCODE = 'LG003'; END IF;
  IF t.from_association_id = p_actor THEN RAISE EXCEPTION 'tidak bisa mengklaim transfer sendiri' USING ERRCODE = 'LG004'; END IF;

  SELECT * INTO c FROM ledger_collections WHERE id = t.collection_id FOR UPDATE;
  IF NOT FOUND THEN RAISE EXCEPTION 'collection % tidak ada', t.collection_id USING ERRCODE = 'LG001'; END IF;
  IF c.state <> 2 THEN RAISE EXCEPTION 'collection tidak dalam pengiriman' USING ERRCODE = 'LG003'; END IF;

  -- Log RECEIVE: actor = penerima, counterparty = pengirim (pemilik lama)
  SELECT * INTO w FROM ledger_write_log(c, 3::smallint, p_actor, p_actor_member, c.owner_id, NULL, v_now,
                                        NULL, NULL, NULL, NULL, ledger_collection_snapshot(c));

  UPDATE ledger_collections
     SET owner_id = p_actor, holder_id = p_actor_member, state = 1,
         last_seq = w.new_seq, last_hash = w.new_hash, updated_at = v_now
   WHERE id = c.id;

  DELETE FROM ledger_holdings WHERE owner_id = c.owner_id AND collection_id = c.id;
  INSERT INTO ledger_holdings (owner_id, collection_id, entry_id, acquired_at)
  VALUES (p_actor, c.id, c.entry_id, v_now);

  UPDATE ledger_transfer_tokens
     SET claimed_by_id = p_actor, claimed_by_member_id = p_actor_member, claimed_at = v_now
   WHERE token = p_token;

  RETURN w.new_seq;
END $$;

-- CANCEL (oleh pengirim): batalkan pengiriman tertunda.
CREATE FUNCTION ledger_cancel_send(p_collection_id bigint, p_actor uuid, p_actor_member uuid)
RETURNS int LANGUAGE plpgsql SECURITY DEFINER SET search_path = public, pg_temp AS $$
DECLARE c ledger_collections; w record; v_now timestamptz := clock_timestamp();
BEGIN
  PERFORM ledger_require_actor(p_actor, p_actor_member);
  SELECT * INTO c FROM ledger_collections WHERE id = p_collection_id FOR UPDATE;
  IF NOT FOUND THEN RAISE EXCEPTION 'collection % tidak ada', p_collection_id USING ERRCODE = 'LG001'; END IF;
  IF c.state <> 2 THEN RAISE EXCEPTION 'tidak ada pengiriman yang menunggu' USING ERRCODE = 'LG003'; END IF;
  IF c.owner_id <> p_actor THEN RAISE EXCEPTION 'hanya pengirim yang boleh membatalkan' USING ERRCODE = 'LG002'; END IF;

  -- Log CANCEL_SEND: counterparty = NULL
  SELECT * INTO w FROM ledger_write_log(c, 5::smallint, p_actor, p_actor_member, NULL, NULL, v_now,
                                        NULL, NULL, NULL, NULL, ledger_collection_snapshot(c));
  UPDATE ledger_collections
     SET state = 1, last_seq = w.new_seq, last_hash = w.new_hash, updated_at = v_now
   WHERE id = c.id;

  -- Kembalikan holder ke NULL (pengirim membatalkan; belum ada pemegang baru)
  UPDATE ledger_collections SET holder_id = NULL WHERE id = c.id;
  DELETE FROM ledger_transfer_tokens WHERE collection_id = c.id AND claimed_by_id IS NULL;
  RETURN w.new_seq;
END $$;

-- ASSIGN: tetapkan / ganti member pemegang di dalam asosiasi yang SAMA.
-- Kepemilikan (owner_id) TIDAK berubah. Setiap pergantian pemegang dicatat
-- sebagai event ASSIGN (type 6) di dalam hash chain -> permanen & bisa dibuktikan.
-- Payload: {"prev_holder": "<uuid|null>", "new_holder": "<uuid|null>"}
-- actor_id        = association pemilik (= owner_id)
-- actor_member_id = member yang mengassign (misalnya admin keluarga)
-- p_new_holder    = member penerima; NULL = lepas dari pemegang
CREATE FUNCTION ledger_assign(
  p_collection_id bigint,
  p_actor         uuid,
  p_actor_member  uuid,
  p_new_holder    uuid          -- UUID member tujuan; NULL = lepas dari pemegang
) RETURNS int LANGUAGE plpgsql SECURITY DEFINER SET search_path = public, pg_temp AS $$
DECLARE
  c       ledger_collections;
  w       record;
  v_now   timestamptz := clock_timestamp();
  v_payload jsonb;
BEGIN
  PERFORM ledger_require_actor(p_actor, p_actor_member);
  SELECT * INTO c FROM ledger_collections WHERE id = p_collection_id FOR UPDATE;
  IF NOT FOUND THEN RAISE EXCEPTION 'collection % tidak ada', p_collection_id USING ERRCODE = 'LG001'; END IF;
  IF c.owner_id <> p_actor THEN
    RAISE EXCEPTION 'hanya pemilik association yang boleh menugaskan pemegang' USING ERRCODE = 'LG002';
  END IF;
  IF c.state <> 1 THEN
    RAISE EXCEPTION 'tidak bisa mengubah pemegang saat item sedang dalam pengiriman' USING ERRCODE = 'LG003';
  END IF;

  -- Bangun payload: rekam siapa pemegang sebelumnya dan siapa penggantinya
  v_payload := jsonb_build_object(
    'prev_holder', c.holder_id::text,
    'new_holder',  p_new_holder::text
  );

  -- Tulis ke chain sebagai event ASSIGN (6); counterparty = NULL
  SELECT * INTO w FROM ledger_write_log(c, 6::smallint, p_actor, p_actor_member, NULL, v_payload, v_now,
                                        NULL, NULL, NULL, NULL, ledger_collection_snapshot(c));

  -- Update kolom holder_id + last_seq/last_hash
  UPDATE ledger_collections
     SET holder_id  = p_new_holder,
         last_seq   = w.new_seq,
         last_hash  = w.new_hash,
         updated_at = v_now
   WHERE id = c.id;

  RETURN w.new_seq;
END $$;

-- USE: aksi TUNGGAL oleh pemilik SAAT INI (mis. membaca surat, memakai stiker).
-- p_action opsional (kata kerja dari registry). Menghitung sebagai "pemakaian alat"
-- sehingga tunduk pada kinds.max_as_tool. Aksi antar-dua-item memakai ledger_act().
CREATE FUNCTION ledger_use(p_collection_id bigint, p_actor uuid, p_actor_member uuid,
                           p_action smallint DEFAULT NULL, p_payload jsonb DEFAULT NULL)
RETURNS int LANGUAGE plpgsql SECURITY DEFINER SET search_path = public, pg_temp AS $$
DECLARE c ledger_collections; k ledger_kinds; w record; v_now timestamptz := clock_timestamp();
BEGIN
  PERFORM ledger_require_actor(p_actor, p_actor_member);
  IF p_action IS NOT NULL AND NOT EXISTS (SELECT 1 FROM ledger_actions WHERE id = p_action) THEN
    RAISE EXCEPTION 'aksi % tidak ada di registry', p_action USING ERRCODE = 'LG004';
  END IF;
  SELECT * INTO c FROM ledger_collections WHERE id = p_collection_id FOR UPDATE;
  IF NOT FOUND THEN RAISE EXCEPTION 'collection % tidak ada', p_collection_id USING ERRCODE = 'LG001'; END IF;
  IF c.owner_id <> p_actor THEN RAISE EXCEPTION 'anda bukan pemilik' USING ERRCODE = 'LG002'; END IF;
  IF c.state <> 1 THEN RAISE EXCEPTION 'collection sedang dalam pengiriman' USING ERRCODE = 'LG003'; END IF;
  SELECT * INTO k FROM ledger_kinds WHERE id = c.kind;
  IF k.max_as_tool IS NOT NULL AND c.tool_uses >= k.max_as_tool THEN
    RAISE EXCEPTION 'item sudah habis terpakai (batas % kali)', k.max_as_tool USING ERRCODE = 'LG003';
  END IF;

  SELECT * INTO w FROM ledger_write_log(c, 4::smallint, p_actor, p_actor_member, NULL, p_payload, v_now,
                                        NULL, NULL, p_action, NULL, ledger_collection_snapshot(c));
  UPDATE ledger_collections
     SET last_seq = w.new_seq, last_hash = w.new_hash, tool_uses = tool_uses + 1, updated_at = v_now
   WHERE id = c.id;                       -- HOT update: tidak ada kolom ber-index yang berubah
  RETURN w.new_seq;
END $$;

-- ACT: p_actor (pemegang ALAT p_tool) melakukan p_action terhadap SASARAN p_target.
--   contoh: pena menulis di jurnal | perangko ditempel ke surat | kamera memotret rol film
--           | cap pos mengecap surat yang sedang dikirim | pena menulis di buku tamu
-- Aturan dibaca dari registry, bukan kode:
--   * (aksi, jenis alat, jenis sasaran) harus terdaftar di ledger_action_rules
--   * alat: milik pelaku & tidak sedang dikirim
--   * sasaran menurut rule.target_access: 1 milik pelaku & aktif | 2 sedang dikirim (siapa pun)
--     | 3 aktif (siapa pun)
--   * kapasitas: kinds.max_as_tool (alat) dan kinds.max_as_target (sasaran)
-- Atomik, DUA chain; dua baris dikunci dengan urutan id tetap -> tanpa deadlock.
CREATE FUNCTION ledger_act(p_action smallint, p_tool bigint, p_target bigint, p_actor uuid, p_actor_member uuid,
                           p_content_hash bytea DEFAULT NULL, p_payload jsonb DEFAULT NULL)
RETURNS TABLE (tool_log_seq int, target_log_seq int)
LANGUAGE plpgsql SECURITY DEFINER SET search_path = public, pg_temp AS $$
DECLARE
  tool ledger_collections; tgt ledger_collections; rl ledger_action_rules;
  kt ledger_kinds; kg ledger_kinds; wt record; wg record;
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

  SELECT * INTO kt FROM ledger_kinds WHERE id = tool.kind;
  SELECT * INTO kg FROM ledger_kinds WHERE id = tgt.kind;
  IF kt.max_as_tool IS NOT NULL AND tool.tool_uses >= kt.max_as_tool THEN
    RAISE EXCEPTION 'alat sudah habis terpakai (batas % kali)', kt.max_as_tool USING ERRCODE = 'LG003';
  END IF;
  IF kg.max_as_target IS NOT NULL AND tgt.target_acts >= kg.max_as_target THEN
    RAISE EXCEPTION 'sasaran sudah penuh (batas % aksi)', kg.max_as_target USING ERRCODE = 'LG003';
  END IF;

  -- seq kedua sisi sudah pasti (baris terkunci) -> bisa saling di-hash
  SELECT * INTO wt FROM ledger_write_log(tool, 4::smallint, p_actor, p_actor_member, NULL, NULL, v_now,
                                         tgt.id, tgt.last_seq + 1, p_action, NULL, ledger_collection_snapshot(tool));
  SELECT * INTO wg FROM ledger_write_log(tgt, 7::smallint, p_actor, p_actor_member, NULL, p_payload, v_now,
                                         tool.id, tool.last_seq + 1, p_action, p_content_hash, ledger_collection_snapshot(tgt));
  UPDATE ledger_collections
     SET last_seq = wt.new_seq, last_hash = wt.new_hash, tool_uses = tool_uses + 1, updated_at = v_now
   WHERE id = tool.id;
  UPDATE ledger_collections
     SET last_seq = wg.new_seq, last_hash = wg.new_hash, target_acts = target_acts + 1, updated_at = v_now
   WHERE id = tgt.id;
  RETURN QUERY SELECT wt.new_seq, wg.new_seq;
END $$;

-- ---------------------------------------------------------------------
-- 12. VERIFIKASI: hitung ulang seluruh chain + replay state machine
--     dan bandingkan dengan baris collections.
-- ---------------------------------------------------------------------
CREATE FUNCTION ledger_verify_chain(p_collection_id bigint)
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
  IF e.content_hash <> ledger_entry_hash(e.id, e.issuer_id, e.issuer_member_id, e.reason, e.supply, e.kind, e.metadata, e.created_at) THEN
    RETURN QUERY SELECT false, 0, 'hash entry tidak cocok'; RETURN;
  END IF;
  IF c.kind <> e.kind THEN
    RETURN QUERY SELECT false, 0, 'kind collection berbeda dari entry'; RETURN;
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
    ELSIF r.event_type = 7 THEN
      -- pelaku boleh bukan pemilik (rule target_access 2/3), jadi owner TIDAK dicek di sini;
      -- pasangannya (log USE di chain alat) yang mewajibkan pelaku = pemilik alat.
      IF r.target_id IS NULL OR r.action_id IS NULL THEN v_err := 'ACTED_ON tidak valid'; END IF;
      v_target := v_target + 1;
    ELSE v_err := 'event_type tidak dikenal';
    END IF;

    -- tautan silang pena <-> jurnal: log pasangannya harus ada dan menunjuk balik
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
-- 13. HAK AKSES (opsional, aktif jika role `ledger_app` ada):
--     aplikasi TIDAK boleh INSERT/UPDATE/DELETE langsung; hanya SELECT + EXECUTE.
--     Registry diubah lewat migrasi oleh role pemilik, bukan oleh aplikasi.
--       CREATE ROLE ledger_app LOGIN PASSWORD '...';   -- sebelum migrasi
-- ---------------------------------------------------------------------
REVOKE EXECUTE ON FUNCTION ledger_write_log(ledger_collections, smallint, uuid, uuid, uuid, jsonb,
                                            timestamptz, bigint, int, smallint, bytea, jsonb) FROM PUBLIC;
DO $$
BEGIN
  IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'ledger_app') THEN
    GRANT SELECT ON ledger_entries, ledger_collections, ledger_logs, ledger_holdings,
                    ledger_transfer_tokens, ledger_blocks,
                    ledger_kinds, ledger_actions, ledger_action_rules, ledger_kind_issuers TO ledger_app;
    GRANT INSERT ON ledger_blocks TO ledger_app;    -- sealer. Pisahkan ke role khusus bila perlu.
    GRANT EXECUTE ON FUNCTION ledger_create_entry(uuid, uuid, text, int, jsonb, smallint),
                              ledger_mint_batch(bigint, uuid, uuid, int, varchar),
                              ledger_send(bigint, uuid, uuid, jsonb),
                              ledger_claim_transfer(uuid, uuid, uuid),
                              ledger_cancel_send(bigint, uuid, uuid),
                              ledger_use(bigint, uuid, uuid, smallint, jsonb),
                              ledger_act(smallint, bigint, bigint, uuid, uuid, bytea, jsonb),
                              ledger_verify_chain(bigint) TO ledger_app;
  END IF;
END $$;
