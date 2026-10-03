"""
Satu-satunya pintu TULIS ke ledger. Setiap fungsi = 1 pemanggilan fungsi
PostgreSQL (atomik, 1 round-trip, row-lock hanya pada 1 collection).

Autentikasi/otorisasi level aplikasi (siapa `actor`) ditangani view/API Anda;
database tetap memverifikasi kepemilikan & state-machine sebagai lapis kedua.
"""
import hashlib
import json
import struct
from datetime import datetime, timedelta, timezone

from django.db import DatabaseError, connection, transaction

from .models import Action, Block, Collection, Kind, Log


# ---------------------------------------------------------------- errors
class LedgerError(Exception):
    pass


class NotFound(LedgerError):         # LG001
    pass


class Forbidden(LedgerError):        # LG002
    pass


class InvalidState(LedgerError):     # LG003
    pass


class InvalidInput(LedgerError):     # LG004
    pass


_SQLSTATE = {"LG001": NotFound, "LG002": Forbidden, "LG003": InvalidState, "LG004": InvalidInput}


def _call(sql, params, row=False):
    try:
        with connection.cursor() as cur:
            cur.execute(sql, params)
            r = cur.fetchone()
            return r if row else r[0]
    except DatabaseError as exc:
        cause = exc.__cause__
        code = getattr(cause, "sqlstate", None) or getattr(cause, "pgcode", None)
        if code in _SQLSTATE:
            message = getattr(getattr(cause, "diag", None), "message_primary", None) or str(cause)
            raise _SQLSTATE[code](message) from exc
        raise


def _json(value):
    return None if value is None else json.dumps(value)


# ---------------------------------------------------------------- operasi
def _kind_id(kind):
    return kind if isinstance(kind, int) else Kind.objects.get(code=kind).id


def create_entry(issuer_id, issuer_member_id, reason, supply, metadata=None, kind=0):
    """Catat alasan + total supply. `kind`: kode registry ("pen") atau id; 0/"generic" = benda umum.
    Item baru ada setelah mint_all()."""
    return _call("SELECT ledger_create_entry(%s, %s, %s, %s, %s::jsonb, %s::smallint)",
                 [issuer_id, issuer_member_id, reason, supply, json.dumps(metadata or {}), _kind_id(kind)])


def mint_batch(entry_id, issuer_id, issuer_member_id, batch=10_000, prefix=""): 
    """Lahirkan hingga `batch` item berikutnya. Return jumlah dibuat (0 = supply penuh)."""
    return _call("SELECT ledger_mint_batch(%s, %s, %s, %s, %s)", [entry_id, issuer_id, issuer_member_id, batch, prefix])


def mint_all(entry_id, issuer_id, issuer_member_id, batch=10_000, prefix=""): 
    """Mint seluruh supply. Tiap batch = transaksi sendiri (jangan panggil di dalam atomic() besar)."""
    total = 0
    while (n := mint_batch(entry_id, issuer_id, issuer_member_id, batch, prefix)):
        total += n
    return total


def create_item(issuer_id, issuer_member_id, reason, quantity, kind_code, metadata=None, prefix=""): 
    """Buat `quantity` item sejenis (kode jenis dari registry: "pen", "letter", "stamp", ...).
    Return (entry_id, [collection_id, ...])."""
    with transaction.atomic():
        entry_id = create_entry(issuer_id, issuer_member_id, reason, quantity, metadata, kind=kind_code)
        mint_all(entry_id, issuer_id, issuer_member_id, batch=10_000, prefix=prefix)
    return entry_id, list(Collection.objects.filter(entry_id=entry_id).values_list("id", flat=True))


def send(collection_id, actor_id, actor_member_id, payload=None):
    """Pemilik mengirim. Return (seq, transfer_token_uuid).

    Pengirim tidak perlu tahu penerima. Buat QR dari token UUID yang dikembalikan;
    penerima scan QR lalu memanggil claim_transfer(token).
    """
    return _call("SELECT * FROM ledger_send(%s, %s, %s, %s::jsonb)",
                 [collection_id, actor_id, actor_member_id, _json(payload)], row=True)


def claim_transfer(token, actor_id, actor_member_id):
    """Penerima klaim transfer via token (dari scan QR). Kepemilikan langsung berpindah.

    token: UUID yang didapat dari QR code (dihasilkan oleh send()).
    actor_id: user yang men-scan QR dan mengklaim item.
    """
    return _call("SELECT ledger_claim_transfer(%s, %s, %s)", [token, actor_id, actor_member_id])


def cancel_send(collection_id, actor_id, actor_member_id):
    """Pengirim membatalkan pengiriman. Token transfer dihapus.

    Hanya pengirim (pemilik) yang boleh membatalkan. Di alur QR tidak ada
    'decline' oleh penerima — penerima memilih dengan tidak men-scan QR.
    """
    return _call("SELECT ledger_cancel_send(%s, %s, %s)", [collection_id, actor_id, actor_member_id])


def use(collection_id, actor_id, actor_member_id, action_code=None, payload=None):
    """
    Aksi TUNGGAL oleh pemilik saat ini (mis. membaca surat, memakai stiker).
    Menghitung sebagai pemakaian alat -> tunduk pada kinds.max_as_tool.
    """
    action_id = None if action_code is None else Action.objects.get(code=action_code).id
    return _call("SELECT ledger_use(%s, %s, %s, %s::smallint, %s::jsonb)",
                 [collection_id, actor_id, actor_member_id, action_id, _json(payload)])


def act(action_code, tool_id, target_id, actor_id, actor_member_id, content=None, content_hash=None, payload=None):
    """
    Alat melakukan aksi pada sasaran. Dibaca dari registry (ledger_action_rules).

    action_code  : kode aksi dari ledger_actions (mis. "write", "affix", "postmark")
    tool_id      : collection id alat (pena, perangko, cap pos, ...)
    target_id    : collection id sasaran (jurnal, surat, rol film, ...)
    actor_id     : siapa yang melakukan aksi
    content      : isi aksi (teks, foto, isi gelang pos, ...). Hanya sha256 masuk ledger.
    content_hash : alternatif jika sha256 dihitung sendiri.
    Return (seq_di_chain_alat, seq_di_chain_sasaran).

    Aturan diambil dari registry:
    - (aksi, jenis alat, jenis sasaran) harus terdaftar
    - alat: milik pelaku & tidak sedang dikirim
    - sasaran: tergantung target_access (1 milik pelaku | 2 sedang dikirim | 3 siapa pun aktif)
    - kapasitas: max_as_tool (alat) & max_as_target (sasaran) dari kinds
    """
    if content is not None:
        content_hash = content_sha256(content)
    ac = Action.objects.get(code=action_code)
    return tuple(_call("SELECT * FROM ledger_act(%s::smallint, %s, %s, %s, %s, %s::bytea, %s::jsonb)",
                       [ac.id, tool_id, target_id, actor_id, actor_member_id, content_hash, _json(payload)], row=True))


def content_sha256(content):
    return hashlib.sha256(content.encode("utf-8") if isinstance(content, str) else content).digest()


def verify_content(collection_id, seq, content):
    """Benarkah `content` ini yang dulu dicatat pada log ACTED_ON (collection_id, seq)?"""
    log = Log.objects.get(pk=(collection_id, seq))
    return (log.event_type == Log.Event.ACTED_ON and log.content_hash is not None
            and bytes(log.content_hash) == content_sha256(content))


def item_history(collection_id):
    """SELURUH riwayat item: lahir, semua aksi (tunggal atau antar-item), kirim/terima."""
    return Log.objects.filter(collection_id=collection_id).order_by("seq")


def item_uses(collection_id):
    """Aksi tunggal atau sisi alat dari aksi antar-item. `action_id` = kata kerja."""
    return item_history(collection_id).filter(event_type=Log.Event.USE).order_by("seq")


def item_acted_on(collection_id):
    """Sisi sasaran dari aksi antar-item. Siapa melakukan apa dgn alat apa."""
    return item_history(collection_id).filter(event_type=Log.Event.ACTED_ON).order_by("seq")

def verify_chain(collection_id):
    """Hitung ulang seluruh chain + replay aturan. Return (valid, broken_at_seq, detail)."""
    with connection.cursor() as cur:
        cur.execute("SELECT valid, broken_at_seq, detail FROM ledger_verify_chain(%s)", [collection_id])
        return cur.fetchone()


# ---------------------------------------------------------------- blocks
# Setiap ~10 detik: Merkle root semua log di jendela waktu itu, ditautkan ke blok
# sebelumnya. Tidak ada lock di jalur tulis; sealer membaca saja.
#
# Jendela [start, end) hanya disegel setelah TIDAK ADA transaksi aktif yang
# mulai sebelum `end` (semua log ber-created_at < end pasti sudah commit).
# created_at memakai clock_timestamp() pada saat fungsi tulis dipanggil.
# Wajib: idle_in_transaction_session_timeout agar transaksi nyangkut tak menahan
# sealer. Role sealer perlu `pg_read_all_stats` untuk melihat pg_stat_activity.
BLOCK_LOCK_KEY = 0x6C65646765  # "ledge"
ZERO32 = b"\x00" * 32
_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)


def _micros(dt):
    return (dt - _EPOCH) // timedelta(microseconds=1)


def merkle_root(leaf_hashes):
    """Merkle root streaming (memori O(log n)). Leaf & node diberi prefix (RFC 6962)."""
    stack, count = [], 0                      # stack berisi (level, hash)
    for h in leaf_hashes:
        count += 1
        node, level = hashlib.sha256(b"\x00" + bytes(h)).digest(), 0
        while stack and stack[-1][0] == level:
            _, left = stack.pop()
            node, level = hashlib.sha256(b"\x01" + left + node).digest(), level + 1
        stack.append((level, node))
    if not stack:
        return hashlib.sha256(b"").digest(), 0
    level, node = stack.pop()
    while stack:                              # ganjil: duplikasi node sisa ke level yang sama
        lvl_left, left = stack.pop()
        while level < lvl_left:
            node, level = hashlib.sha256(b"\x01" + node + node).digest(), level + 1
        node, level = hashlib.sha256(b"\x01" + left + node).digest(), level + 1
    return node, count


def _block_hash_us(prev, root, start_us, end_us, count):
    return hashlib.sha256(prev + root + struct.pack(">qqq", start_us, end_us, count)).digest()


def _block_hash(prev, root, start, end, count):
    return _block_hash_us(prev, root, _micros(start), _micros(end), count)


_LEAVES_SQL = ("SELECT hash FROM ledger_logs WHERE created_at >= %s AND created_at < %s "
               "ORDER BY created_at, collection_id, seq")


def seal_next_block(window=timedelta(seconds=10), safety_lag=timedelta(seconds=2)):
    """Segel satu blok bila jendela berikutnya sudah aman. Return Block atau None."""
    with transaction.atomic(), connection.cursor() as cur:
        cur.execute("SELECT pg_try_advisory_xact_lock(%s)", [BLOCK_LOCK_KEY])
        if not cur.fetchone()[0]:
            return None                       # sealer lain sedang jalan

        cur.execute("SELECT block_no, window_end, block_hash FROM ledger_blocks "
                    "ORDER BY block_no DESC LIMIT 1")
        last = cur.fetchone()
        if last:
            block_no, start, prev = last[0] + 1, last[1], bytes(last[2])
        else:
            cur.execute("SELECT date_trunc('second', min(created_at)) FROM ledger_entries")
            start = cur.fetchone()[0]
            if start is None:
                return None
            block_no, prev = 1, ZERO32
        end = start + window

        cur.execute("SELECT clock_timestamp()")
        if end + safety_lag > cur.fetchone()[0]:
            return None                       # jendela belum cukup tua
        cur.execute("SELECT min(xact_start) FROM pg_stat_activity "
                    "WHERE xact_start IS NOT NULL AND pid <> pg_backend_pid()")
        oldest = cur.fetchone()[0]
        if oldest is not None and oldest < end:
            return None                       # masih ada transaksi lama; coba lagi nanti

        with connection.chunked_cursor() as leaves:   # server-side cursor
            leaves.execute(_LEAVES_SQL, [start, end])
            root, count = merkle_root(row[0] for row in leaves)

        block_hash = _block_hash(prev, root, start, end, count)
        cur.execute(
            "INSERT INTO ledger_blocks (block_no, window_start, window_end, log_count, "
            "merkle_root, prev_block_hash, block_hash) VALUES (%s,%s,%s,%s,%s,%s,%s)",
            [block_no, start, end, count, root, prev, block_hash])
    return Block.objects.get(pk=block_no)


def verify_blocks(first=1, last=None):
    """Hitung ulang Merkle root dari log + cek tautan antar-blok. Return (ok, block_no_rusak)."""
    qs = Block.objects.filter(block_no__gte=first).order_by("block_no")
    if last:
        qs = qs.filter(block_no__lte=last)
    prev = None
    if first > 1:
        prev = bytes(Block.objects.get(block_no=first - 1).block_hash)
    for b in qs.iterator():
        if prev is None and b.block_no == 1:
            prev = ZERO32
        elif prev is None:
            prev = bytes(b.prev_block_hash)
        with connection.chunked_cursor() as leaves:
            leaves.execute(_LEAVES_SQL, [b.window_start, b.window_end])
            root, count = merkle_root(row[0] for row in leaves)
        if (bytes(b.prev_block_hash) != prev or bytes(b.merkle_root) != root or b.log_count != count
                or bytes(b.block_hash) != _block_hash(prev, root, b.window_start, b.window_end, count)):
            return False, b.block_no
        prev = bytes(b.block_hash)
    return True, None


# ---------------------------------------------------------------- bukti "tercatat di blok"
# Pemakaian baru masuk blok setelah jendela waktunya disegel (default ~10-12 detik).
# Sebelum itu build_proof() mengembalikan None.
def block_of(collection_id, seq):
    """Blok yang memuat log ini, atau None jika belum disegel."""
    log = Log.objects.get(pk=(collection_id, seq))
    return Block.objects.filter(window_start__lte=log.created_at, window_end__gt=log.created_at).first()


def _canon_log(f):
    """Teks kanonik yang di-hash. HARUS identik dengan ledger_log_hash() di SQL."""
    n = lambda v: "" if v is None else str(v)
    return "|".join([str(f["collection_id"]), str(f["seq"]), str(f["event_type"]), str(f["actor_id"]), str(f["actor_member_id"]),
                     n(f["counterparty_id"]), str(f["created_us"]), n(f["payload_text"]),
                     n(f["target_id"]), n(f["target_seq"]), n(f["action_id"]), n(f["content_hash_hex"])]).encode("utf-8")


def _merkle_path(leaves, index):
    level = [hashlib.sha256(b"\x00" + bytes(x)).digest() for x in leaves]
    path = []
    while len(level) > 1:
        if len(level) % 2:
            level.append(level[-1])
        sib = index ^ 1
        path.append(("L" if sib < index else "R", level[sib].hex()))
        level = [hashlib.sha256(b"\x01" + level[i] + level[i + 1]).digest() for i in range(0, len(level), 2)]
        index //= 2
    return path


def build_proof(collection_id, seq):
    """
    Bukti mandiri bahwa log (collection_id, seq) tercatat di sebuah blok.
    Berisi isi log, hash sebelumnya, jalur Merkle, dan data blok. Bisa diverifikasi
    siapa pun TANPA akses database (verify_proof). None jika belum disegel.
    Catatan: membaca seluruh leaf blok itu (jendela 10 dtk = ribuan s/d jutaan baris);
    untuk pemakaian massal, cache hasilnya.
    """
    blk = block_of(collection_id, seq)
    if blk is None:
        return None
    with connection.cursor() as cur:
        cur.execute(
            "SELECT l.collection_id, l.seq, l.event_type, l.actor_id, l.actor_member_id, l.counterparty_id, "
            "ledger_micros(l.created_at), l.payload::text, l.target_id, l.target_seq, "
            "l.action_id, encode(l.content_hash, 'hex'), l.hash, "
            "CASE WHEN l.seq = 1 THEN ledger_genesis_hash(e.content_hash, c.id, c.serial_no) "
            "     ELSE (SELECT p.hash FROM ledger_logs p WHERE p.collection_id = l.collection_id "
            "           AND p.seq = l.seq - 1) END "
            "FROM ledger_logs l JOIN ledger_collections c ON c.id = l.collection_id "
            "JOIN ledger_entries e ON e.id = c.entry_id "
            "WHERE l.collection_id = %s AND l.seq = %s", [collection_id, seq])
        row = cur.fetchone()
    keys = ["collection_id", "seq", "event_type", "actor_id", "actor_member_id", "counterparty_id", "created_us",
            "payload_text", "target_id", "target_seq", "action_id", "content_hash_hex"]
    log = dict(zip(keys, row[:12]))
    leaf, prev = bytes(row[11]), bytes(row[12])
    with connection.chunked_cursor() as cur:
        cur.execute(_LEAVES_SQL, [blk.window_start, blk.window_end])
        leaves = [bytes(r[0]) for r in cur]
    return {
        "log": log, "prev_hash": prev.hex(), "log_hash": leaf.hex(),
        "merkle_path": _merkle_path(leaves, leaves.index(leaf)),
        "block": {"block_no": blk.block_no, "start_us": _micros(blk.window_start),
                  "end_us": _micros(blk.window_end), "log_count": blk.log_count,
                  "merkle_root": bytes(blk.merkle_root).hex(),
                  "prev_block_hash": bytes(blk.prev_block_hash).hex(),
                  "block_hash": bytes(blk.block_hash).hex()},
    }


def verify_proof(proof):
    """Murni Python, tanpa DB. Cek: isi log -> hash log -> jalur Merkle -> root -> hash blok."""
    try:
        h = hashlib.sha256(bytes.fromhex(proof["prev_hash"]) + _canon_log(proof["log"])).digest()
        if h.hex() != proof["log_hash"]:
            return False
        node = hashlib.sha256(b"\x00" + h).digest()
        for side, sib in proof["merkle_path"]:
            sib = bytes.fromhex(sib)
            node = hashlib.sha256(b"\x01" + (sib + node if side == "L" else node + sib)).digest()
        b = proof["block"]
        if node.hex() != b["merkle_root"]:
            return False
        expect = _block_hash_us(bytes.fromhex(b["prev_block_hash"]), node, b["start_us"], b["end_us"], b["log_count"])
        return expect.hex() == b["block_hash"] and b["start_us"] <= proof["log"]["created_us"] < b["end_us"]
    except (KeyError, ValueError):
        return False
