-- Perbaikan untuk database yang sudah menjalankan 0001_schema.sql versi lama:
-- * ledger_mint_batch gagal (CROSS JOIN ... ON, record new_cols bukan ledger_collections) dan
--   menyimpan last_hash = genesis, bukan hash log #1 (chain putus di seq 2).
-- * ledger_verify_chain belum mengenal event ASSIGN (6).

CREATE OR REPLACE FUNCTION ledger_collection_snapshot(c ledger_collections) RETURNS jsonb
LANGUAGE plpgsql STABLE AS $$
BEGIN
RETURN jsonb_build_object(
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
);
END $$;

CREATE OR REPLACE FUNCTION ledger_mint_batch(p_entry_id bigint, p_issuer uuid, p_issuer_member uuid, p_batch int, p_prefix varchar DEFAULT '')
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
    SELECT g.id, g.serial_no,
           ledger_log_hash(g.genesis, g.id, 1, 1::smallint, p_issuer, p_issuer_member, NULL, v_now, NULL) AS h1
    FROM (SELECT x.id, x.serial_no, ledger_genesis_hash(e.content_hash, x.id, x.serial_no) AS genesis
          FROM (SELECT nextval('ledger_collection_id_seq') AS id,
                       p_prefix || ((((s::bigint * 38742041) + (p_entry_id * 1234567)) % 90000000) + 10000000)::text AS serial_no
                FROM generate_series(v_from, v_to) s) x) g
  ), new_cols AS (
    -- last_hash = hash log #1 (bukan genesis) supaya log berikutnya tersambung ke log #1
    INSERT INTO ledger_collections (id, entry_id, serial_no, owner_id, holder_id, state, kind,
                                    last_seq, last_hash, created_at, updated_at)
    SELECT b.id, e.id, b.serial_no, p_issuer, p_issuer_member, 1, e.kind, 1, b.h1, v_now, v_now
    FROM base b
    RETURNING *
  ), new_logs AS (
    INSERT INTO ledger_logs (collection_id, actor_id, actor_member_id, counterparty_id, created_at,
                             seq, event_type, hash, payload, state_snapshot)
    SELECT b.id, p_issuer, p_issuer_member, NULL, v_now, 1, 1::smallint, b.h1, NULL,
           ledger_collection_snapshot(ROW(c.*)::ledger_collections)
    FROM base b
    JOIN new_cols c ON c.id = b.id
    RETURNING collection_id, hash
  )
  INSERT INTO ledger_holdings (owner_id, collection_id, entry_id, acquired_at)
  SELECT p_issuer, id, e.id, v_now FROM new_cols;

  UPDATE ledger_entries SET minted_count = v_to WHERE id = e.id;
  RETURN v_to - v_from + 1;
END $$;

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
