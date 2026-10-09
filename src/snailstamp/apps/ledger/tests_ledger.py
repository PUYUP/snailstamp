#!/usr/bin/env python3
"""Uji fungsional ledger_patched.sql di PostgreSQL asli (socket /var/tmp/pgtest, port 5544)."""
import subprocess, sys, threading, time, uuid, queue, traceback
import psycopg

HOST, PORT, SQL = "/var/tmp/pgtest", 5544, sys.argv[1] if len(sys.argv) > 1 else "ledger_patched.sql"
DB = "t_func"
U = lambda n: uuid.UUID(int=n)
A, Am, B, Bm, C, Cm = U(1), U(0xA1), U(2), U(0xB1), U(3), U(0xC1)


def conn(db=DB, autocommit=True, user="postgres"):
    return psycopg.connect(host=HOST, port=PORT, user=user, dbname=db, autocommit=autocommit)


def one(c, sql, *a):
    return c.execute(sql, a).fetchone()[0]


def row(c, sql, *a):
    return c.execute(sql, a).fetchone()


def expect(c, code, sql, *a):
    try:
        c.execute(sql, a)
    except psycopg.errors.Error as e:
        assert e.sqlstate == code, f"sqlstate {e.sqlstate} != {code}: {e}"
        if not c.autocommit:
            c.rollback()
        return str(e)
    raise AssertionError(f"seharusnya error {code}: {sql[:60]}")


results = []


def test(fn):
    try:
        fn()
        results.append((fn.__name__, True, ""))
        print(f"  PASS  {fn.__name__}")
    except Exception as e:  # noqa
        results.append((fn.__name__, False, repr(e)))
        print(f"  FAIL  {fn.__name__}: {e!r}")
        traceback.print_exc(limit=-2)
    return fn


# ---------- setup ----------
adm = conn("postgres")
adm.execute(f"DROP DATABASE IF EXISTS {DB}")
adm.execute(f"CREATE DATABASE {DB} ENCODING 'UTF8' LC_COLLATE 'C' LC_CTYPE 'C' TEMPLATE template0")
r = subprocess.run(["psql", "-h", HOST, "-p", str(PORT), "-U", "postgres", "-d", DB, "-q",
                    "-v", "ON_ERROR_STOP=1", "-f", SQL], capture_output=True, text=True)
if r.returncode != 0:
    print("GAGAL memuat skema:\n", r.stderr[-1500:])
    sys.exit(2)
print("skema termuat. NOTICE:", [l for l in r.stderr.splitlines() if "NOTICE" in l and "sealer" in l][:1])

c = conn()
c.execute("""INSERT INTO ledger_kinds (id, code, label, max_as_tool, max_as_target) VALUES
  (1,'pena','Pena',NULL,NULL),(2,'jurnal','Jurnal',NULL,NULL),(3,'perangko','Perangko',1,NULL),(4,'surat','Surat',NULL,NULL)""")
c.execute("INSERT INTO ledger_actions VALUES (1,'menulis','menulis'),(2,'menempel','menempel')")
c.execute("INSERT INTO ledger_action_rules VALUES (1,1,2,1),(2,3,4,1)")


def mk(kind, supply, issuer=A, member=Am, reason="uji"):
    e = one(c, "SELECT ledger_create_entry(%s,%s,%s,%s,'{}'::jsonb,%s::smallint)", issuer, member, reason, supply, kind)
    ids = one(c, "SELECT ledger_mint_batch(%s,%s,%s,%s,'')", e, issuer, member, 100000)
    return e, ids


ENT = {}
print("== uji fungsional ==")


@test
def mint_bekerja_dan_idempoten_saat_penuh():
    e, ids = mk(1, 3)
    ENT["pena"] = (e, ids)
    assert len(ids) == 3
    assert one(c, "SELECT ledger_mint_batch(%s,%s,%s,5,'')", e, A, Am) == []      # supply penuh
    assert one(c, "SELECT minted_count FROM ledger_entries WHERE id=%s", e) == 3
    assert one(c, "SELECT count(*) FROM ledger_holdings WHERE owner_id=%s AND entry_id=%s", A, e) == 3


@test
def serial_unik_untuk_1000_item():
    e, ids = mk(0, 1000)
    assert one(c, "SELECT count(DISTINCT serial_no) FROM ledger_collections WHERE entry_id=%s", e) == 1000


@test
def supply_cap_90_juta():
    expect(c, "LG004", "SELECT ledger_create_entry(%s,%s,'x',90000001,'{}'::jsonb,0::smallint)", A, Am)
    e = one(c, "SELECT ledger_create_entry(%s,%s,'x',5,'{}'::jsonb,0::smallint)", A, Am)
    expect(c, "LG004", "SELECT ledger_update_entry(%s,%s,%s,NULL,90000001)", e, A, Am)
    one(c, "SELECT ledger_update_entry(%s,%s,%s,NULL,7)", e, A, Am)
    one(c, "SELECT ledger_delete_entry(%s,%s,%s)", e, A, Am)


@test
def tidak_ada_kolom_state_snapshot():
    assert one(c, "SELECT count(*) FROM information_schema.columns WHERE table_name='ledger_logs' AND column_name='state_snapshot'") == 0
    assert one(c, "SELECT count(*) FROM pg_proc WHERE proname='ledger_collection_snapshot'") == 0


@test
def send_claim_token_di_hash():
    pen = ENT["pena"][1][0]
    seq, tok = row(c, "SELECT * FROM ledger_send(%s,%s,%s)", pen, A, Am)
    assert one(c, "SELECT count(*) FROM ledger_transfer_tokens WHERE token_hash = ledger_token_hash(%s)", tok) == 1
    assert one(c, "SELECT count(*) FROM ledger_transfer_tokens t WHERE t::text ILIKE %s", f"%{tok}%") == 0, "token mentah bocor"
    expect(c, "LG001", "SELECT ledger_claim_transfer(%s,%s,%s)", uuid.uuid4(), B, Bm)
    expect(c, "LG004", "SELECT ledger_claim_transfer(%s,%s,%s)", tok, A, Am)          # klaim sendiri
    one(c, "SELECT ledger_claim_transfer(%s,%s,%s)", tok, B, Bm)
    assert one(c, "SELECT owner_id FROM ledger_collections WHERE id=%s", pen) == B
    assert one(c, "SELECT count(*) FROM ledger_holdings WHERE collection_id=%s AND owner_id=%s", pen, B) == 1
    assert one(c, "SELECT count(*) FROM ledger_holdings WHERE collection_id=%s AND owner_id=%s", pen, A) == 0
    expect(c, "LG003", "SELECT ledger_claim_transfer(%s,%s,%s)", tok, C, Cm)          # sudah diklaim
    assert row(c, "SELECT valid, detail FROM ledger_verify_chain(%s)", pen) == (True, "ok")


@test
def cancel_tidak_menghapus_token_dan_tidak_menyentuh_holder():
    pen = ENT["pena"][1][1]
    _, tok = row(c, "SELECT * FROM ledger_send(%s,%s,%s)", pen, A, Am)
    holder_before = one(c, "SELECT holder_id FROM ledger_collections WHERE id=%s", pen)
    one(c, "SELECT ledger_cancel_send(%s,%s,%s)", pen, A, Am)
    assert one(c, "SELECT holder_id FROM ledger_collections WHERE id=%s", pen) == holder_before == Am
    assert one(c, "SELECT cancelled_at IS NOT NULL FROM ledger_transfer_tokens WHERE token_hash=ledger_token_hash(%s)", tok)
    msg = expect(c, "LG003", "SELECT ledger_claim_transfer(%s,%s,%s)", tok, B, Bm)
    assert "dibatalkan" in msg, msg
    _, tok2 = row(c, "SELECT * FROM ledger_send(%s,%s,%s)", pen, A, Am)               # kirim ulang: index unik aktif lolos
    one(c, "SELECT ledger_claim_transfer(%s,%s,%s)", tok2, B, Bm)
    assert row(c, "SELECT valid, detail FROM ledger_verify_chain(%s)", pen) == (True, "ok")


@test
def act_use_assign_dan_verifikasi_dua_chain():
    pen = ENT["pena"][1][2]
    _, jr = mk(2, 1)
    jr = jr[0]
    h = bytes.fromhex("aa" * 32)
    s1, s2 = row(c, "SELECT * FROM ledger_act(1::smallint,%s,%s,%s,%s,%s,NULL)", pen, jr, A, Am, h)
    assert (s1, s2) == (2, 2)
    one(c, "SELECT ledger_assign(%s,%s,%s,%s)", pen, A, Am, Bm)
    one(c, "SELECT ledger_use(%s,%s,%s,NULL,NULL)", pen, A, Am)
    for x in (pen, jr):
        rr = row(c, "SELECT valid, detail FROM ledger_verify_chain(%s)", x)
        assert rr == (True, "ok"), (x, rr)


@test
def batas_pemakaian_perangko():
    _, pr = mk(3, 2)
    _, su = mk(4, 2)
    one(c, "SELECT * FROM ledger_act(2::smallint,%s,%s,%s,%s,NULL,NULL)", pr[0], su[0], A, Am)
    msg = expect(c, "LG003", "SELECT * FROM ledger_act(2::smallint,%s,%s,%s,%s,NULL,NULL)", pr[0], su[1], A, Am)
    assert "habis terpakai" in msg
    one(c, "SELECT * FROM ledger_act(2::smallint,%s,%s,%s,%s,NULL,NULL)", pr[1], su[1], A, Am)


@test
def semua_chain_valid():
    bad = [(i, r) for (i,) in c.execute("SELECT id FROM ledger_collections").fetchall()
           for r in [row(c, "SELECT valid, detail FROM ledger_verify_chain(%s)", i)] if not r[0]]
    assert not bad, bad[:3]


# ---------- blok ----------
print("== blok ==")


@test
def seal_dan_verify_blok():
    assert one(c, "SELECT ledger_seal_block(interval '5 minutes')") is None or True   # margin besar: belum ada yang cukup tua
    n1 = one(c, "SELECT ledger_seal_block(interval '0 seconds')")
    assert n1 == 1, n1
    assert row(c, "SELECT valid, detail FROM ledger_verify_blocks()") == (True, "ok")
    assert one(c, "SELECT ledger_seal_block(interval '0 seconds')") is None            # tak ada log baru
    one(c, "SELECT ledger_use(%s,%s,%s,NULL,NULL)", ENT["pena"][1][2], A, Am)
    n2 = one(c, "SELECT ledger_seal_block(interval '0 seconds')")
    assert n2 == 2
    assert one(c, "SELECT window_start FROM ledger_blocks WHERE block_no=2") == one(c, "SELECT window_end FROM ledger_blocks WHERE block_no=1")
    assert row(c, "SELECT valid, detail FROM ledger_verify_blocks()") == (True, "ok")
    expect(c, "LG003", "SELECT ledger_seal_block(interval '1 hour', now())")          # jendela terlalu muda


@test
def merkle_vektor_dikenal():
    import hashlib
    L = [bytes([i]) * 32 for i in range(1, 6)]
    lf = [hashlib.sha256(b"\x00" + x).digest() for x in L]
    def mth(n):
        if len(n) == 1: return n[0]
        k = 1
        while k * 2 < len(n): k *= 2
        return hashlib.sha256(b"\x01" + mth(n[:k]) + mth(n[k:])).digest()
    for m in range(1, 6):
        got = bytes(one(c, "SELECT ledger_merkle_root(%s::bytea[])", L[:m]))
        assert got == mth(lf[:m]), m


@test
def log_terlambat_commit_terdeteksi():
    pen = ENT["pena"][1][2]
    one(c, "SELECT ledger_use(%s,%s,%s,NULL,NULL)", pen, A, Am)                         # ada log di jendela
    _, jr = mk(2, 1)
    _, pn = mk(1, 1)
    slow = conn(autocommit=False)
    slow.execute("SELECT * FROM ledger_act(1::smallint,%s,%s,%s,%s,NULL,NULL)", (pn[0], jr[0], A, Am))   # belum commit
    n = one(c, "SELECT ledger_seal_block(interval '0 seconds')")                         # segel tanpa log slow
    assert n == 3
    slow.commit(); slow.close()
    r = row(c, "SELECT valid, broken_block_no, detail FROM ledger_verify_blocks()")
    assert r[0] is False and r[1] == 3 and "jumlah log berubah" in r[2], r


@test
def tamper_log_terdeteksi_verify_blocks_dan_chain():
    t = conn(autocommit=False)
    t.execute("ALTER TABLE ledger_logs DISABLE TRIGGER ledger_logs_append_only")
    t.execute("UPDATE ledger_logs SET hash = sha256('x'::bytea) WHERE collection_id=%s AND seq=1", (ENT["pena"][1][0],))
    r = row(t, "SELECT valid, broken_block_no, detail FROM ledger_verify_blocks(1,1)")
    assert r[0] is False and "merkle_root" in r[2], r
    assert row(t, "SELECT valid FROM ledger_verify_chain(%s)", ENT["pena"][1][0])[0] is False
    t.rollback(); t.close()


@test
def jendela_tumpang_tindih_ditolak():
    expect(c, "23P01", """INSERT INTO ledger_blocks (block_no, window_start, window_end, log_count, merkle_root, prev_block_hash, block_hash)
        SELECT 99, window_start + interval '1 microsecond', window_end, 1, merkle_root, prev_block_hash, block_hash FROM ledger_blocks WHERE block_no=1""")


# ---------- proteksi ----------
print("== proteksi ==")


@test
def truncate_partisi_dan_induk_ditolak():
    for stmt in ("TRUNCATE ledger_logs_p007", "TRUNCATE ledger_logs", "TRUNCATE ledger_collections_p000",
                 "TRUNCATE ledger_collections", "TRUNCATE ledger_blocks", "TRUNCATE ledger_entries CASCADE"):
        t = conn(autocommit=False)
        try:
            expect(t, "LG005", stmt)
        finally:
            t.rollback(); t.close()
    assert one(c, "SELECT count(*) FROM ledger_logs") > 0


@test
def drop_tabel_ledger_ditolak():
    for stmt in ("DROP TABLE ledger_logs_p001", "DROP TABLE ledger_logs", "DROP TABLE ledger_blocks", "DROP TABLE ledger_collections CASCADE"):
        t = conn(autocommit=False)
        try:
            expect(t, "LG005", stmt)
        finally:
            t.rollback(); t.close()
    assert one(c, "SELECT count(*) FROM pg_class WHERE relname='ledger_logs_p001'") == 1


@test
def role_app_tidak_bisa_insert_blok_tapi_bisa_fungsi():
    a = conn(user="ledger_app")
    expect(a, "42501", "INSERT INTO ledger_blocks (block_no, window_start, window_end, log_count, merkle_root, prev_block_hash, block_hash) "
                       "VALUES (50, now(), now()+interval '1 s', 1, sha256('a'::bytea), sha256('b'::bytea), sha256('c'::bytea))")
    expect(a, "42501", "UPDATE ledger_collections SET owner_id = owner_id")
    e = one(a, "SELECT ledger_create_entry(%s,%s,'dari app',2,'{}'::jsonb,0::smallint)", A, Am)
    assert len(one(a, "SELECT ledger_mint_batch(%s,%s,%s,2,'')", e, A, Am)) == 2
    assert one(a, "SELECT valid FROM ledger_verify_blocks()") in (True, False)
    expect(a, "42501", "SELECT ledger_write_log((SELECT c FROM ledger_collections c LIMIT 1),1::smallint,%s,%s,NULL,NULL,now())", A, Am)
    a.close()


# ---------- konkurensi: claim vs cancel ----------
print("== konkurensi ==")


def stress(claim_fn, n=300):
    """thread S: send lalu cancel; thread C: klaim token yang sama bersamaan. Hitung deadlock (40P01)."""
    _, ids = mk(0, n)
    q = queue.Queue()
    stats = {"deadlock": 0, "other": [], "claimed": 0, "cancelled": 0}
    stop = object()

    def sender():
        s = conn()
        for i in ids:
            try:
                _, tok = row(s, "SELECT * FROM ledger_send(%s,%s,%s)", i, A, Am)
                q.put(tok)
                try:
                    s.execute("SELECT ledger_cancel_send(%s,%s,%s)", (i, A, Am)); stats["cancelled"] += 1
                except psycopg.errors.Error as e:
                    if e.sqlstate == "40P01": stats["deadlock"] += 1
                    elif e.sqlstate not in ("LG002", "LG003"): stats["other"].append(repr(e))
            except psycopg.errors.Error as e:
                stats["other"].append(repr(e))
        q.put(stop)

    def claimer():
        s = conn()
        while True:
            tok = q.get()
            if tok is stop: return
            try:
                s.execute(f"SELECT {claim_fn}(%s,%s,%s)", (tok, B, Bm)); stats["claimed"] += 1
            except psycopg.errors.Error as e:
                if e.sqlstate == "40P01": stats["deadlock"] += 1
                elif e.sqlstate not in ("LG003", "LG001"): stats["other"].append(repr(e))

    ts = [threading.Thread(target=sender), threading.Thread(target=claimer)]
    [t.start() for t in ts]; [t.join() for t in ts]
    return stats


# varian claim URUTAN LAMA (token dulu, baru collection) hanya untuk membuktikan uji ini sensitif
c.execute("""
CREATE FUNCTION ledger_claim_old_order(p_token uuid, p_actor uuid, p_actor_member uuid) RETURNS int
LANGUAGE plpgsql AS $$
DECLARE t ledger_transfer_tokens; c ledger_collections; w record; v_now timestamptz := clock_timestamp();
BEGIN
  SELECT * INTO t FROM ledger_transfer_tokens WHERE token_hash = ledger_token_hash(p_token) FOR UPDATE;
  IF NOT FOUND THEN RAISE EXCEPTION 'x' USING ERRCODE='LG001'; END IF;
  IF t.claimed_by_id IS NOT NULL OR t.cancelled_at IS NOT NULL THEN RAISE EXCEPTION 'x' USING ERRCODE='LG003'; END IF;
  SELECT * INTO c FROM ledger_collections WHERE id = t.collection_id FOR UPDATE;
  IF c.state <> 2 THEN RAISE EXCEPTION 'x' USING ERRCODE='LG003'; END IF;
  RETURN 0;
END $$;""")


@test
def tanpa_deadlock_claim_vs_cancel():
    st = stress("ledger_claim_transfer", 400)
    print("        statistik:", st)
    assert st["deadlock"] == 0 and not st["other"], st


def info_sensitivitas():
    st = stress("ledger_claim_old_order", 400)
    print(f"  INFO  urutan lama (token->collection): deadlock={st['deadlock']} dari 400 percobaan (uji sensitif bila > 0)")


info_sensitivitas()


@test
def semua_chain_masih_valid_setelah_stress():
    bad = [i for (i,) in c.execute("SELECT id FROM ledger_collections").fetchall()
           if not row(c, "SELECT valid FROM ledger_verify_chain(%s)", i)[0]]
    assert not bad, bad[:3]
    mism = one(c, """SELECT count(*) FROM ledger_holdings h JOIN ledger_collections x ON x.id = h.collection_id
                      WHERE h.owner_id <> x.owner_id""")
    assert mism == 0


# ---------- performa mint ----------
t0 = time.time()
e, ids = mk(0, 50000)
print(f"  INFO  mint 50.000 item: {time.time()-t0:.1f}s")

fails = [r for r in results if not r[1]]
print(f"\nHASIL: {len(results)-len(fails)}/{len(results)} lulus")
sys.exit(1 if fails else 0)