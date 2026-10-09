"""
Satu-satunya pintu TULIS ke ledger. Setiap fungsi = 1 pemanggilan fungsi
PostgreSQL (atomik, 1 round-trip, row-lock hanya pada 1 collection).

Identitas: association -> member -> user.
  * `*_id` di sini (issuer_id, actor_id, ...) = ASSOCIATION: pemilik item.
  * `*_member_id` = MEMBER yang bertindak atas nama association itu; tercatat di tiap log.
  * User tidak pernah masuk ledger.

Otorisasi: view/API Anda memastikan siapa user yang login & member mana yang ia pakai.
Lapis ini (`_check_member`) memastikan member itu benar anggota association-nya SEBELUM tulis;
database lalu memverifikasi kepemilikan & state-machine sebagai lapis kedua.
"""
import hashlib
import json
import os
import struct
import uuid
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone

from django.apps import apps
from django.conf import settings
from django.core.files.storage import default_storage
from django.db import DatabaseError, connection, transaction
from django.db.models import Q
from django.utils import timezone as dj_timezone
from django.utils.module_loading import import_string
from nacl import signing
from nacl.encoding import RawEncoder
from nacl.exceptions import BadSignatureError

from . import anchors, shamir
from .models import (
    Action,
    Block,
    BlockAnchor,
    BlockSignature,
    Collection,
    Kind,
    Log,
    SealerKey,
    SealerKeyAuditLog,
)


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
    if isinstance(kind, int):
        return kind
    try:
        return Kind.objects.get(code=kind).id
    except Kind.DoesNotExist:
        raise InvalidInput(f"jenis {kind!r} tidak ada di registry") from None


def _action_id(code):
    try:
        return Action.objects.get(code=code).id
    except Action.DoesNotExist:
        raise InvalidInput(f"aksi {code!r} tidak ada di registry") from None


def default_member_check(association_id, member_id):
    """Bawaan: `tenant.Member` punya kolom `association` (FK) dan pk = member_id.

    Jika model Member Anda berbeda, atau butuh syarat tambahan (aktif / masih berlaku),
    tulis fungsi `(association_id, member_id) -> bool` sendiri dan tunjuk lewat
    settings.LEDGER_MEMBER_CHECK = "path.ke.fungsi".
    """
    Member = apps.get_model("tenant", "Member")
    return Member.objects.filter(pk=member_id, association_id=association_id).exists()


def _check_member(association_id, member_id):
    """Member harus anggota association-nya. DB tidak bisa memeriksa ini (tanpa FK ke tenant)."""
    if association_id is None or member_id is None:
        raise InvalidInput("association dan member wajib diisi")
    path = getattr(settings, "LEDGER_MEMBER_CHECK", None)
    check = import_string(path) if path else default_member_check
    if not check(association_id, member_id):
        raise Forbidden("member ini bukan anggota association tersebut")


def create_entry(issuer_id, issuer_member_id, reason, supply, metadata=None, kind=0, max_as_tool=None, max_as_target=None):
    """Catat alasan + total supply. `kind`: kode registry ("pen") atau id; 0/"generic" = benda umum.
    Item baru ada setelah mint_all().
    issuer_id = association penerbit; issuer_member_id = member yang membuat entry."""
    _check_member(issuer_id, issuer_member_id)
    return _call("SELECT ledger_create_entry(%s, %s, %s, %s, %s::jsonb, %s::smallint, %s::int, %s::int)",
                 [issuer_id, issuer_member_id, reason, supply, json.dumps(metadata or {}), _kind_id(kind), max_as_tool, max_as_target])


def update_entry(entry_id, issuer_id, issuer_member_id, reason=None, supply=None, metadata=None, kind=None, max_as_tool=None, max_as_target=None):
    """Ubah entry yang sudah ada. Argumen None = tidak diubah.
        issuer_id = association pemilik entry; issuer_member_id = member yang melakukan perubahan.
        supply tidak boleh di bawah minted_count. metadata menggantikan seluruh isi lama
        (bukan merge); metadata={} berarti dikosongkan, sedangkan None berarti tidak diubah."""
    _check_member(issuer_id, issuer_member_id)
    return _call(
        "SELECT ledger_update_entry(%s, %s, %s, %s::text, %s::int, %s::jsonb, %s::smallint, %s::int, %s::int)",
        [entry_id, issuer_id, issuer_member_id, reason, supply,
         None if metadata is None else json.dumps(metadata),
         None if kind is None else _kind_id(kind), max_as_tool, max_as_target])


def delete_entry(entry_id, issuer_id, issuer_member_id):
    """Hapus entry. Hanya boleh kalau minted_count = 0 (belum ada item ter-mint).
    Mengembalikan id entry yang dihapus."""
    _check_member(issuer_id, issuer_member_id)
    return _call("SELECT ledger_delete_entry(%s, %s, %s)",
                 [entry_id, issuer_id, issuer_member_id])


def mint_batch(entry_id, issuer_id, issuer_member_id, batch=10_000, prefix=""):
    """Lahirkan hingga `batch` item berikutnya. Return array ID yang dibuat."""
    _check_member(issuer_id, issuer_member_id)
    return _call("SELECT ledger_mint_batch(%s, %s, %s, %s, %s)", [entry_id, issuer_id, issuer_member_id, batch, prefix])


def mint_all(entry_id, issuer_id, issuer_member_id, batch=10_000, prefix=""):
    """Mint seluruh supply. Tiap batch = transaksi sendiri (jangan panggil di dalam atomic() besar)."""
    total = []
    while True:
        ids = mint_batch(entry_id, issuer_id, issuer_member_id, batch, prefix)
        if not ids:
            break
        total.extend(ids)
    return total


def create_item(issuer_id, issuer_member_id, reason, quantity, kind_code, metadata=None, prefix="", max_as_tool=None, max_as_target=None):
    """Buat `quantity` item sejenis (kode jenis dari registry: "pen", "letter", "stamp", ...).
    Return (entry_id, [collection_id, ...])."""
    with transaction.atomic():
        entry_id = create_entry(issuer_id, issuer_member_id, reason, quantity, metadata, kind=kind_code, max_as_tool=max_as_tool, max_as_target=max_as_target)
        ids = mint_all(entry_id, issuer_id, issuer_member_id, batch=10_000, prefix=prefix)
    return entry_id, ids


def send(collection_id, actor_id, actor_member_id, payload=None):
    """Pemilik mengirim. Return (seq, transfer_token_uuid).

    Pengirim tidak perlu tahu penerima. Buat QR dari token UUID yang dikembalikan;
    penerima scan QR lalu memanggil claim_transfer(token).
    """
    _check_member(actor_id, actor_member_id)
    return _call("SELECT * FROM ledger_send(%s, %s, %s, %s::jsonb)",
                 [collection_id, actor_id, actor_member_id, _json(payload)], row=True)


def claim_transfer(token, actor_id, actor_member_id):
    """Penerima klaim transfer via token (dari scan QR). Kepemilikan langsung berpindah.

    token: UUID yang didapat dari QR code (dihasilkan oleh send()).
    actor_id / actor_member_id: association penerima + member yang men-scan QR.
    Item menjadi milik ASSOCIATION; member tercatat di log RECEIVE.
    """
    _check_member(actor_id, actor_member_id)
    return _call("SELECT ledger_claim_transfer(%s, %s, %s)", [token, actor_id, actor_member_id])


def cancel_send(collection_id, actor_id, actor_member_id):
    """Pengirim membatalkan pengiriman. Token transfer dihapus.

    Hanya pengirim (pemilik) yang boleh membatalkan. Di alur QR tidak ada
    'decline' oleh penerima — penerima memilih dengan tidak men-scan QR.
    """
    _check_member(actor_id, actor_member_id)
    return _call("SELECT ledger_cancel_send(%s, %s, %s)", [collection_id, actor_id, actor_member_id])


def assign(collection_id, actor_id, actor_member_id, new_holder_id=None):
    """Tetapkan / ganti member pemegang item di dalam asosiasi yang sama.

    Tidak memindahkan kepemilikan (owner tetap = Association).
    Operasi ringan: langsung UPDATE kolom holder_id, tanpa menyentuh hash chain.

    actor_id        : UUID association pemilik (harus = owner saat ini)
    actor_member_id : UUID member yang menugaskan (admin / pemilik sebelumnya)
    new_holder_id   : UUID member penerima; None/NULL = lepas dari pemegang

    Contoh (analogi motor keluarga):
        # Ayah (member A) meminjamkan motor ke kakak (member B)
        assign(motor_collection_id, keluarga_id, ayah_member_id, kakak_member_id)

        # Motor dikembalikan, belum ada pemegang
        assign(motor_collection_id, keluarga_id, ayah_member_id, None)
    """
    _check_member(actor_id, actor_member_id)
    return _call("SELECT ledger_assign(%s, %s, %s, %s)",
                 [collection_id, actor_id, actor_member_id, new_holder_id])


def use(collection_id, actor_id, actor_member_id, action_code=None, payload=None):
    """
    Aksi TUNGGAL oleh pemilik saat ini (mis. membaca surat, memakai stiker).
    Menghitung sebagai pemakaian alat -> tunduk pada kinds.max_as_tool.
    """
    _check_member(actor_id, actor_member_id)
    action_id = None if action_code is None else _action_id(action_code)
    return _call("SELECT ledger_use(%s, %s, %s, %s::smallint, %s::jsonb)",
                 [collection_id, actor_id, actor_member_id, action_id, _json(payload)])


def act(action_code, tool_id, target_id, actor_id, actor_member_id, content=None, content_hash=None, payload=None):
    """
    Alat melakukan aksi pada sasaran. Dibaca dari registry (ledger_action_rules).

    action_code  : kode aksi dari ledger_actions (mis. "write", "affix", "postmark")
    tool_id      : collection id alat (pena, perangko, cap pos, ...)
    target_id    : collection id sasaran (jurnal, surat, rol film, ...)
    actor_id     : ASSOCIATION pelaku (pemilik alat)
    actor_member_id : member yang melakukannya (tercatat di KEDUA chain)
    content      : isi aksi (teks, foto, isi gelang pos, ...). Hanya sha256 masuk ledger.
    content_hash : alternatif jika sha256 dihitung sendiri.
    Return (seq_di_chain_alat, seq_di_chain_sasaran).

    Aturan diambil dari registry:
    - (aksi, jenis alat, jenis sasaran) harus terdaftar
    - alat: milik pelaku & tidak sedang dikirim
    - sasaran: tergantung target_access (1 milik pelaku | 2 sedang dikirim | 3 siapa pun aktif)
    - kapasitas: max_as_tool (alat) & max_as_target (sasaran) dari kinds
    """
    _check_member(actor_id, actor_member_id)
    if content is not None:
        content_hash = content_sha256(content)
    return tuple(_call("SELECT * FROM ledger_act(%s::smallint, %s, %s, %s, %s, %s::bytea, %s::jsonb)",
                       [_action_id(action_code), tool_id, target_id, actor_id, actor_member_id, content_hash, _json(payload)], row=True))


def content_sha256(content, version=1):
    # 1. Siapkan prefix versi (misal menjadi "hash_version_1:") dalam bentuk bytes
    version_prefix = f"hash_version_{version}:".encode("utf-8")
    
    # 2. Konversi konten ke bytes jika masih string
    content_bytes = content.encode("utf-8") if isinstance(content, str) else content
    
    # 3. Gabungkan prefix versi dengan konten asli
    payload_to_hash = version_prefix + content_bytes
    
    # 4. Lakukan hashing pada gabungan tersebut
    return hashlib.sha256(payload_to_hash).digest()


def verify_content(collection_id, seq, content):
    """Benarkah `content` ini yang dulu dicatat pada log ACTED_ON (collection_id, seq)?"""
    log = Log.objects.get(pk=(collection_id, seq))
    return (log.event_type == Log.Event.ACTED_ON and log.content_hash is not None
            and bytes(log.content_hash) == content_sha256(content))


def item_history(collection_id):
    """SELURUH riwayat item: lahir, semua aksi (tunggal atau antar-item), kirim/terima."""
    return Log.objects.filter(collection_id=collection_id).order_by("seq")


def item_creator(collection_id):
    """Log MINT (seq 1): `.actor_id` = association, `.actor_member_id` = member yang melahirkan item.
    Terlindung hash chain, jadi bukti siapa pembuatnya tidak bisa ditulis ulang."""
    return Log.objects.get(pk=(collection_id, 1))


def item_uses(collection_id):
    """Aksi tunggal atau sisi alat dari aksi antar-item. `action_id` = kata kerja."""
    return item_history(collection_id).filter(event_type=Log.Event.USE).order_by("seq")


def item_acted_on(collection_id):
    """Sisi sasaran dari aksi antar-item. Siapa melakukan apa dgn alat apa."""
    return item_history(collection_id).filter(event_type=Log.Event.ACTED_ON).order_by("seq")


def item_assignments(collection_id):
    """Seluruh riwayat pergantian pemegang (event ASSIGN).

    Setiap log punya:
      .actor        -> association yang mengassign
      .actor_member -> member yang melakukan assignment
      .payload      -> {"prev_holder": "<uuid|null>", "new_holder": "<uuid|null>"}
      .created_at   -> kapan terjadi (tercatat di chain, tidak bisa diubah)

    Karena masuk hash chain, riwayat ini bisa dibuktikan lewat build_proof().
    """
    return item_history(collection_id).filter(event_type=Log.Event.ASSIGN).order_by("seq")


def verify_chain(collection_id):
    """Hitung ulang seluruh chain + replay aturan. Return (valid, broken_at_seq, detail)."""
    with connection.cursor() as cur:
        cur.execute("SELECT valid, broken_at_seq, detail FROM ledger_verify_chain(%s)", [collection_id])
        return cur.fetchone()


def reconstruct_collection(collection_id, seq=None):
    """
    Rekonstruksi state Collection dari log history.

    Args:
        collection_id: ID collection
        seq: Seq log spesifik (None = state terkini)

    Returns:
        Dictionary yang merepresentasikan state Collection pada seq tersebut,
        atau None jika log tidak ditemukan.

    State snapshot menyimpan:
        - id, entry_id, serial_no, owner_id, holder_id, state, kind
        - last_seq, tool_uses, target_acts, last_hash
        - created_at, updated_at
    """
    if seq is None:
        # Ambil log terakhir (state terkini)
        log = Log.objects.filter(collection_id=collection_id).order_by('-seq').first()
    else:
        log = Log.objects.filter(pk=(collection_id, seq)).first()

    if log is None or log.state_snapshot is None:
        return None

    return log.state_snapshot


def reconstruct_collection_full(collection_id):
    """
    Rekonstruksi seluruh history Collection dari semua log.

    Returns:
        List of (seq, event_type, state_snapshot) tuples,
        ordered by seq ascending.
    """
    logs = Log.objects.filter(collection_id=collection_id).order_by('seq')
    return [
        (log.seq, log.event_type, log.state_snapshot)
        for log in logs
        if log.state_snapshot is not None
    ]


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


def _sealer_region(sealer):
    return sealer.region if sealer else ""


def _actor_fields(actor_info):
    actor_info = actor_info or {}
    return {
        "actor_user_id": actor_info.get("user_id"),
        "actor_email": actor_info.get("email"),
        "actor_ip": actor_info.get("ip"),
        "actor_user_agent": actor_info.get("user_agent"),
    }


def _audit(event_type, sealer=None, actor_info=None, **fields):
    """Tulis satu baris SealerKeyAuditLog (append-only). Jangan pernah masukkan private key / share."""
    if sealer is not None:
        fields.setdefault("sealer_id", sealer.id)
        fields.setdefault("sealer_name", sealer.name)
        fields.setdefault("region", sealer.region)
    return SealerKeyAuditLog.objects.create(event_type=event_type, **_actor_fields(actor_info), **fields)


def _latest_block_no():
    return Block.objects.order_by("-block_no").values_list("block_no", flat=True).first()


def _signing_key(private_key):
    if isinstance(private_key, str):
        try:
            private_key = bytes.fromhex(private_key)
        except ValueError as exc:
            raise InvalidInput("private_key harus hex") from exc
    if len(private_key) != 32:
        raise InvalidInput("private_key harus 32 bytes (Ed25519)")
    return signing.SigningKey(private_key)


def _public_hex(signing_key):
    return signing_key.verify_key.encode(encoder=RawEncoder).hex()


def _signatures_required():
    """settings.LEDGER_REQUIRE_BLOCK_SIGNATURES (default True): blok wajib ditandatangani sealer terdaftar."""
    return getattr(settings, "LEDGER_REQUIRE_BLOCK_SIGNATURES", True)


def _active_sealer_for(sealer_id, public_key):
    """SealerKey aktif untuk key ini. sealer_id=None -> cari berdasarkan public key."""
    lookup = {"id": sealer_id} if sealer_id else {"public_key": public_key, "status": SealerKey.KeyStatus.ACTIVE}
    sealer = SealerKey.objects.filter(**lookup).order_by("-valid_from").first()
    if sealer is None:
        raise InvalidInput(f"Sealer {sealer_id or public_key} tidak terdaftar")
    if not sealer.is_active:
        raise InvalidInput(f"Sealer {sealer_id} tidak aktif atau sudah expired")
    if sealer.public_key != public_key:
        raise InvalidInput(f"Public key tidak cocok dengan sealer {sealer_id}")
    return sealer


def seal_next_block(window=timedelta(seconds=10), safety_lag=timedelta(seconds=2), sealer_private_keys=None):
    """Segel satu blok bila jendela berikutnya sudah aman. Return Block atau None.

    Args:
        window: Durasi jendela waktu (default 10 detik)
        safety_lag: Jeda waktu untuk memastikan semua transaksi selesai (default 2 detik)
        sealer_private_keys: Dict atau list dari private keys untuk multi-signature.
                           Format: [{"sealer_id": "uuid", "private_key": "hex"}, ...]
                           atau legacy single key: "hex_string" atau bytes

    Returns:
        Block object atau None jika jendela belum aman

    Setiap pemakaian key dicatat sebagai KEY_USED di SealerKeyAuditLog. Sealer di region lain
    bisa menambah signature belakangan lewat cosign_block().

    Bila settings.LEDGER_REQUIRE_BLOCK_SIGNATURES (default True), menolak menyegel tanpa key, dan
    setiap key harus milik SealerKey aktif yang terdaftar (InvalidInput).
    """
    if isinstance(sealer_private_keys, (str, bytes)):          # legacy: satu key tanpa sealer_id
        sealer_private_keys = [{"sealer_id": None, "private_key": sealer_private_keys}]
    required = _signatures_required()
    if required and not sealer_private_keys:
        raise InvalidInput("Blok wajib ditandatangani: berikan sealer_private_keys "
                           "(LEDGER_REQUIRE_BLOCK_SIGNATURES)")
    signers = []                                               # (signing_key, public_key, sealer|None)
    for sealer_info in sealer_private_keys or []:
        signing_key = _signing_key(sealer_info["private_key"])
        public_key = _public_hex(signing_key)
        sealer_id = sealer_info.get("sealer_id")
        sealer = _active_sealer_for(sealer_id, public_key) if (sealer_id or required) else None
        signers.append((signing_key, public_key, sealer))

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

        signatures = [{
            "sealer_id": str(sealer.id) if sealer else None,
            "public_key": public_key,
            "region": _sealer_region(sealer),
            "signature": signing_key.sign(block_hash, encoder=RawEncoder).signature.hex(),
        } for signing_key, public_key, sealer in signers]
        used = [sealer for _, _, sealer in signers if sealer]

        cur.execute(
            "INSERT INTO ledger_blocks (block_no, window_start, window_end, log_count, "
            "merkle_root, prev_block_hash, block_hash, signatures) "
            "VALUES (%s,%s,%s,%s,%s,%s,%s,%s::jsonb)",
            [block_no, start, end, count, root, prev, block_hash, json.dumps(signatures)])
        for sealer in used:
            _audit(SealerKeyAuditLog.EventType.KEY_USED, sealer,
                   new_key_id=sealer.id, block_at_rotation=block_no,
                   metadata={"mode": "seal", "block_no": block_no})
    return Block.objects.get(pk=block_no)


def verify_blocks(first=1, last=None, verify_signature=True, threshold=None, min_regions=None):
    """Hitung ulang Merkle root dari log + cek tautan antar-blok. Return (ok, block_no_rusak).

    Args:
        first: Block number pertama untuk verifikasi
        last: Block number terakhir (None = sampai terakhir)
        verify_signature: Jika True, verifikasi signature sealer (embedded + co-signature). Blok tanpa
                          signature hanya lolos bila LEDGER_REQUIRE_BLOCK_SIGNATURES=False.
        threshold: Minimum signature valid yang dibutuhkan (None = settings.BLOCK_SIGNATURE_THRESHOLD / 1)
        min_regions: Minimum region berbeda di antara signature valid
                     (None = settings.LEDGER_SEALER_MIN_REGIONS / 1)

    Returns:
        (ok, block_no_rusak) - ok=True jika semua valid, block_no_rusak jika ada error
    """
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

        if verify_signature and (_signatures_required() or b.signatures or b.cosignatures.exists()):
            ok, _ = verify_block_signatures(b, threshold=threshold, min_regions=min_regions)
            if not ok:
                return False, b.block_no

        prev = bytes(b.block_hash)
    return True, None


def generate_sealer_keypair():
    """Generate key pair untuk block sealer (Ed25519).

    Returns:
        (private_key_hex, public_key_hex) - Private key (hex string) dan public key (hex string)
    """
    signing_key = signing.SigningKey.generate()
    private_key = signing_key.encode(encoder=RawEncoder)
    public_key = signing_key.verify_key.encode(encoder=RawEncoder)
    return private_key.hex(), public_key.hex()


def verify_block_signature(block, public_key_hex):
    """Verifikasi signature sebuah block dengan public key tertentu (deprecated, gunakan verify_block_signatures).

    Returns:
        True jika ada signature valid dari public key ini (embedded atau co-signature)
    """
    candidates = [s.get("signature") for s in (block.signatures or []) if s.get("public_key") == public_key_hex]
    candidates += list(block.cosignatures.filter(public_key=public_key_hex).values_list("signature", flat=True))
    return any(_signature_ok(block, public_key_hex, sig) for sig in candidates)


def _validate_public_key(public_key_hex):
    try:
        if len(public_key_hex) != 64:
            raise ValueError
        signing.VerifyKey(bytes.fromhex(public_key_hex), encoder=RawEncoder)
    except (TypeError, ValueError) as exc:
        raise InvalidInput("public_key_hex harus 64 chars (32 bytes hex)") from exc


def register_sealer(name, public_key_hex, valid_until=None, threshold=1, metadata=None, actor_info=None,
                    region=None):
    """Register sealer baru dengan key pair.

    Args:
        name: Nama sealer (mis: "sealer-1", "sealer-2")
        public_key_hex: Public key sealer (hex string, 64 chars)
        valid_until: Expiration date (None = tidak pernah expired)
        threshold: Threshold untuk multi-sig (berapa banyak signature dibutuhkan)
        metadata: Dictionary metadata tambahan
        actor_info: Dictionary actor info {"user_id": "...", "email": "...", "ip": "...", "user_agent": "..."}
        region: Region sealer (mis: "ap-southeast-1"); None = settings.LEDGER_SEALER_REGION / ""

    Returns:
        SealerKey object
    """
    _validate_public_key(public_key_hex)
    if region is None:
        region = getattr(settings, "LEDGER_SEALER_REGION", "") or ""

    with transaction.atomic():
        sealer = SealerKey.objects.create(
            name=name,
            public_key=public_key_hex,
            valid_until=valid_until,
            threshold=threshold,
            region=region,
            metadata=metadata or {}
        )
        _audit(SealerKeyAuditLog.EventType.KEY_REGISTERED, sealer, actor_info,
               new_key_id=sealer.id, new_public_key=public_key_hex, new_valid_until=valid_until,
               metadata=metadata or {})
    return sealer


def get_active_sealers(region=None):
    """Ambil semua sealer yang aktif dan belum expired (opsional: hanya satu region).

    Returns:
        QuerySet of SealerKey objects
    """
    qs = SealerKey.objects.filter(
        status=SealerKey.KeyStatus.ACTIVE
    ).filter(
        Q(valid_until__isnull=True) | Q(valid_until__gt=dj_timezone.now())
    )
    if region is not None:
        qs = qs.filter(region=region)
    return qs


def rotate_sealer_key(sealer_id, new_public_key_hex, valid_until=None, reason="scheduled", actor_info=None):
    """Rotate key untuk sealer yang ada.

    Key lama ditandai EXPIRED (deactivated_at = sekarang) tapi signature lamanya tetap sah untuk
    blok yang disegel sebelum rotasi. Rotasi dicatat sebagai KEY_ROTATED di SealerKeyAuditLog.

    Args:
        sealer_id: ID SealerKey yang akan di-rotate
        new_public_key_hex: Public key baru (hex string, 64 chars)
        valid_until: Expiration date baru (None = tidak pernah expired)
        reason: Alasan rotasi (mis: "scheduled", "compromised", "expiring", "manual")
        actor_info: Lihat register_sealer()

    Returns:
        SealerKey object baru
    """
    _validate_public_key(new_public_key_hex)
    with transaction.atomic():
        try:
            old_key = SealerKey.objects.select_for_update().get(id=sealer_id)
        except SealerKey.DoesNotExist:
            raise NotFound(f"Sealer {sealer_id} tidak ditemukan")
        if old_key.status == SealerKey.KeyStatus.REVOKED:
            raise InvalidState(f"Sealer {sealer_id} sudah di-revoke; register key baru")
        if old_key.successors.exists():
            raise InvalidState(f"Sealer {sealer_id} sudah di-rotate sebelumnya")
        if old_key.public_key == new_public_key_hex:
            raise InvalidInput("Public key baru sama dengan key lama")

        now = dj_timezone.now()
        previous_valid_until = old_key.valid_until
        old_key.status = SealerKey.KeyStatus.EXPIRED
        old_key.deactivated_at = old_key.deactivated_at or now
        old_key.save(update_fields=["status", "deactivated_at"])

        new_key = SealerKey.objects.create(
            name=old_key.name,
            public_key=new_public_key_hex,
            valid_until=valid_until,
            threshold=old_key.threshold,
            region=old_key.region,
            metadata=old_key.metadata,
            previous_key=old_key,
        )
        _audit(SealerKeyAuditLog.EventType.KEY_ROTATED, old_key, actor_info,
               old_key_id=old_key.id, new_key_id=new_key.id,
               old_public_key=old_key.public_key, new_public_key=new_public_key_hex,
               rotation_reason=reason, previous_valid_until=previous_valid_until,
               new_valid_until=valid_until, block_at_rotation=_latest_block_no())
    return new_key


def revoke_sealer_key(sealer_id, reason="compromised", actor_info=None):
    """Revoke key (mis. bocor). Signature dari key REVOKED tidak dihitung lagi, termasuk blok lama.

    Returns:
        SealerKey yang di-revoke
    """
    with transaction.atomic():
        try:
            key = SealerKey.objects.select_for_update().get(id=sealer_id)
        except SealerKey.DoesNotExist:
            raise NotFound(f"Sealer {sealer_id} tidak ditemukan")
        if key.status == SealerKey.KeyStatus.REVOKED:
            raise InvalidState(f"Sealer {sealer_id} sudah di-revoke")
        key.status = SealerKey.KeyStatus.REVOKED
        key.deactivated_at = key.deactivated_at or dj_timezone.now()
        key.save(update_fields=["status", "deactivated_at"])
        _audit(SealerKeyAuditLog.EventType.KEY_REVOKED, key, actor_info,
               old_key_id=key.id, old_public_key=key.public_key, rotation_reason=reason,
               previous_valid_until=key.valid_until, block_at_rotation=_latest_block_no())
    return key


def expire_old_keys(actor_info=None):
    """Mark key yang sudah expired sebagai EXPIRED status (dicatat sebagai KEY_EXPIRED).

    Returns:
        Number of keys yang diupdate
    """
    count = 0
    with transaction.atomic():
        keys = SealerKey.objects.select_for_update().filter(
            status=SealerKey.KeyStatus.ACTIVE,
            valid_until__isnull=False,
            valid_until__lt=dj_timezone.now()
        )
        block_no = _latest_block_no()
        for key in keys:
            key.status = SealerKey.KeyStatus.EXPIRED
            key.deactivated_at = key.valid_until
            key.save(update_fields=["status", "deactivated_at"])
            _audit(SealerKeyAuditLog.EventType.KEY_EXPIRED, key, actor_info,
                   old_key_id=key.id, old_public_key=key.public_key,
                   previous_valid_until=key.valid_until, block_at_rotation=block_no)
            count += 1
    return count


def sealer_audit_trail(sealer_name=None, sealer_id=None, event_type=None, region=None, since=None):
    """Query audit log sealer (terbaru dulu). Semua filter opsional."""
    qs = SealerKeyAuditLog.objects.all()
    if sealer_name is not None:
        qs = qs.filter(sealer_name=sealer_name)
    if sealer_id is not None:
        qs = qs.filter(Q(sealer_id=sealer_id) | Q(old_key_id=sealer_id) | Q(new_key_id=sealer_id))
    if event_type is not None:
        qs = qs.filter(event_type=event_type)
    if region is not None:
        qs = qs.filter(region=region)
    if since is not None:
        qs = qs.filter(timestamp__gte=since)
    return qs


def _signature_ok(block, public_key_hex, signature_hex):
    try:
        verify_key = signing.VerifyKey(bytes.fromhex(public_key_hex), encoder=RawEncoder)
        verify_key.verify(bytes(block.block_hash), bytes.fromhex(signature_hex), encoder=RawEncoder)
        return True
    except (BadSignatureError, TypeError, ValueError):
        return False


def _block_signature_candidates(block):
    """(sealer_id|None, public_key, signature, signed_at) dari Block.signatures + BlockSignature."""
    for sig in block.signatures or []:
        yield sig.get("sealer_id"), sig.get("public_key"), sig.get("signature"), block.sealed_at
    for cs in block.cosignatures.all():
        yield cs.sealer_id, cs.public_key, cs.signature, cs.signed_at


def valid_block_signers(block):
    """Daftar SealerKey yang signature-nya valid untuk blok ini (unik per public key).

    Signature hanya dihitung bila: public key cocok dengan SealerKey terdaftar, key tersebut sah
    pada saat menandatangani (lihat SealerKey.was_valid_at), dan signature Ed25519-nya valid.
    """
    seen, signers = set(), []
    for sealer_id, public_key, signature, signed_at in _block_signature_candidates(block):
        if not public_key or not signature or public_key in seen:
            continue
        lookup = {"id": sealer_id} if sealer_id else {"public_key": public_key}
        sealer = SealerKey.objects.filter(**lookup).order_by("valid_from").first()
        if sealer is None or sealer.public_key != public_key or not sealer.was_valid_at(signed_at):
            continue
        if _signature_ok(block, public_key, signature):
            seen.add(public_key)
            signers.append(sealer)
    return signers


def verify_block_signatures(block, threshold=None, min_regions=None):
    """Verifikasi multi-signature sebuah block dengan threshold (dan jumlah region minimum).

    Args:
        block: Block object
        threshold: Minimum signature valid (None = settings.BLOCK_SIGNATURE_THRESHOLD / 1)
        min_regions: Minimum region berbeda (None = settings.LEDGER_SEALER_MIN_REGIONS / 1)

    Returns:
        (valid, count) - valid=True jika cukup signature valid, count=jumlah signature valid
    """
    signers = valid_block_signers(block)
    required = threshold or getattr(settings, "BLOCK_SIGNATURE_THRESHOLD", 1) or 1
    regions_required = min_regions or getattr(settings, "LEDGER_SEALER_MIN_REGIONS", 1) or 1
    regions = {s.region for s in signers}
    return len(signers) >= required and len(regions) >= regions_required, len(signers)


# ---------------------------------------------------------------- multi-region co-sign
def cosign_block(block_no, sealer_id, private_key, actor_info=None):
    """Tambah signature sealer (biasanya dari region lain) ke blok yang sudah disegel.

    Co-signer memverifikasi ulang Merkle root & tautan blok dari log di database SEBELUM
    menandatangani, jadi signature-nya adalah kesaksian independen atas isi blok.

    Returns:
        BlockSignature object
    """
    signing_key = _signing_key(private_key)
    public_key = _public_hex(signing_key)
    sealer = _active_sealer_for(sealer_id, public_key)
    try:
        block = Block.objects.get(pk=block_no)
    except Block.DoesNotExist:
        raise NotFound(f"Blok #{block_no} tidak ditemukan")
    if any(s.get("public_key") == public_key for s in block.signatures or []):
        raise InvalidState(f"Blok #{block_no} sudah ditandatangani sealer ini")
    ok, _ = verify_blocks(first=block_no, last=block_no, verify_signature=False)
    if not ok:
        raise InvalidState(f"Blok #{block_no} tidak cocok dengan log; menolak co-sign")
    with transaction.atomic():
        if BlockSignature.objects.filter(block_id=block_no, sealer=sealer).exists():
            raise InvalidState(f"Blok #{block_no} sudah ditandatangani sealer ini")
        cosig = BlockSignature.objects.create(
            block_id=block_no, sealer=sealer, region=sealer.region, public_key=public_key,
            signature=signing_key.sign(bytes(block.block_hash), encoder=RawEncoder).signature.hex(),
        )
        _audit(SealerKeyAuditLog.EventType.KEY_USED, sealer, actor_info,
               new_key_id=sealer.id, block_at_rotation=block_no,
               metadata={"mode": "cosign", "block_no": block_no})
    return cosig


def pending_cosign_blocks(sealer_id, limit=100):
    """Blok yang belum ditandatangani sealer ini (embedded maupun co-signature), urut naik.

    Sealer region baru juga akan menandatangani ulang riwayat lama (backfill), setelah
    memverifikasi ulang tiap blok terhadap log.
    """
    sealer = SealerKey.objects.get(id=sealer_id)
    signed = BlockSignature.objects.filter(sealer=sealer).values("block_id")
    return (Block.objects.exclude(block_no__in=signed)
            .exclude(signatures__contains=[{"public_key": sealer.public_key}])
            .order_by("block_no")[:limit])


# ---------------------------------------------------------------- anchoring & fork detection
# Checkpoint blok diterbitkan ke luar DB (anchors.py). detect_forks() membandingkan checkpoint
# eksternal dengan isi DB: rollback (blok hilang), rewrite (hash beda), equivocation (key sealer
# yang sama menandatangani dua riwayat berbeda), dan chain yang tidak lagi konsisten.
@dataclass(frozen=True)
class ForkFinding:
    kind: str            # missing_block | hash_mismatch | equivocation | invalid_checkpoint |
                         # anchor_conflict | chain_invalid | backend_error | stale_chain |
                         # cosign_lag | witness_refused
    block_no: int | None
    backend: str
    detail: str
    expected_hash: str = ""
    actual_hash: str = ""

    def to_dict(self):
        return asdict(self)


def _valid_signatures(block_hash, signatures):
    """Public key dari signature Ed25519 yang valid atas block_hash (bytes)."""
    keys = set()
    for public_key, signature in signatures:
        try:
            signing.VerifyKey(bytes.fromhex(public_key), encoder=RawEncoder).verify(
                block_hash, bytes.fromhex(signature), encoder=RawEncoder)
            keys.add(public_key)
        except (BadSignatureError, TypeError, ValueError):
            continue
    return keys


def checkpoint_for_block(block):
    """Checkpoint (anchors.Checkpoint) untuk blok: hash, tautan, dan semua signature valid saat ini."""
    sigs, seen = [], set()
    for sealer_id, public_key, signature, _ in _block_signature_candidates(block):
        if public_key and signature and public_key not in seen and _signature_ok(block, public_key, signature):
            seen.add(public_key)
            region = next((c.region for c in block.cosignatures.all() if c.public_key == public_key), None)
            if region is None:
                region = next((s.get("region", "") for s in block.signatures or []
                               if s.get("public_key") == public_key), "")
            sigs.append({"public_key": public_key, "signature": signature, "region": region})
    return anchors.Checkpoint(block_no=block.block_no, block_hash=bytes(block.block_hash).hex(),
                              prev_block_hash=bytes(block.prev_block_hash).hex(),
                              window_end_us=_micros(block.window_end), signatures=sigs)


def _signed_enough(block, threshold=None, min_regions=None):
    """Sama dengan aturan verify_blocks(): blok memenuhi threshold signature / region."""
    if not (_signatures_required() or block.signatures or block.cosignatures.exists()):
        return True
    return verify_block_signatures(block, threshold=threshold, min_regions=min_regions)[0]


def latest_fully_signed_block(after=0, threshold=None, min_regions=None):
    """Blok tertinggi N > after sehingga SEMUA blok after+1..N sudah memenuhi threshold signature
    (BLOCK_SIGNATURE_THRESHOLD / LEDGER_SEALER_MIN_REGIONS). None bila blok after+1 belum lengkap."""
    good = None
    for block in Block.objects.filter(block_no__gt=after).order_by("block_no").iterator(chunk_size=500):
        if not _signed_enough(block, threshold, min_regions):
            break
        good = block
    return good


def _cosign_lag():
    return timedelta(seconds=getattr(settings, "LEDGER_MAX_COSIGN_LAG", 300))


def anchor_blocks(force=False, backends=None, interval=None):
    """Terbitkan checkpoint blok terakhir yang signature-nya SUDAH LENGKAP ke tiap backend anchor.

    Dengan threshold multi-region, blok terbaru biasanya belum di-co-sign region lain; yang di-anchor
    adalah blok tertinggi yang dia dan semua pendahulunya sudah memenuhi threshold. Bila sebuah blok
    tertahan lebih lama dari LEDGER_MAX_COSIGN_LAG (default 300 dtk) -> InvalidState.

    Per backend: hanya bila sudah >= interval (default settings.LEDGER_ANCHOR_INTERVAL = 100) blok
    sejak anchor terakhir backend itu (atau force=True). Sebelum menerbitkan, chain sejak anchor
    terakhir diverifikasi ulang -- ledger yang rusak tidak pernah di-anchor.

    Returns:
        list BlockAnchor yang baru dibuat
    """
    backends = anchors.get_backends() if backends is None else backends
    if not backends:
        return []
    interval = max(1, interval or getattr(settings, "LEDGER_ANCHOR_INTERVAL", 100) or 1)
    last_nos = {}
    for backend in backends:
        last = BlockAnchor.objects.filter(backend=backend.name).order_by("-block_no").first()
        if last:
            block = Block.objects.filter(pk=last.block_no).first()
            if block is None or bytes(block.block_hash).hex() != last.block_hash:
                raise InvalidState(f"Blok #{last.block_no} berbeda dari anchor {backend.name}; menolak anchor")
        last_nos[backend.name] = last.block_no if last else 0

    start = min(last_nos.values()) + 1
    target = latest_fully_signed_block(after=start - 1)
    stuck = Block.objects.filter(block_no=(target.block_no if target else start - 1) + 1).first()
    if stuck and stuck.sealed_at < datetime.now(timezone.utc) - _cosign_lag():
        raise InvalidState(f"Blok #{stuck.block_no} belum memenuhi threshold signature lebih dari "
                           f"{_cosign_lag()}; menolak anchor")
    if target is None:
        return []
    ok, bad = verify_blocks(first=start, last=target.block_no)
    if not ok:
        raise InvalidState(f"Blok #{bad} gagal verifikasi; menolak anchor")

    checkpoint, created = checkpoint_for_block(target), []
    for backend in backends:
        last_no = last_nos[backend.name]
        if target.block_no <= last_no or (not force and target.block_no - last_no < interval):
            continue
        receipt = backend.publish(checkpoint)
        created.append(BlockAnchor.objects.create(block_no=target.block_no, block_hash=checkpoint.block_hash,
                                                  backend=backend.name, receipt=receipt))
    return created


def ledger_health(max_block_age=None, max_cosign_lag=None):
    """Kesehatan sealer untuk watchtower.

    stale_chain: jendela blok terakhir sudah lebih tua dari max_block_age (default
                 LEDGER_MAX_BLOCK_AGE = 60 dtk) -> semua sealer mati / tertinggal.
    cosign_lag:  blok yang disegel lebih dari max_cosign_lag lalu (default LEDGER_MAX_COSIGN_LAG
                 = 300 dtk) belum memenuhi threshold signature -> co-signer region lain mati.
    """
    now = datetime.now(timezone.utc)
    if max_block_age is None:
        max_block_age = timedelta(seconds=getattr(settings, "LEDGER_MAX_BLOCK_AGE", 60))
    max_cosign_lag = _cosign_lag() if max_cosign_lag is None else max_cosign_lag
    findings = []
    latest = Block.objects.order_by("-block_no").first()
    if latest and now - latest.window_end > max_block_age:
        findings.append(ForkFinding("stale_chain", latest.block_no, "",
                                    f"jendela blok terakhir berakhir {latest.window_end.isoformat()}; "
                                    f"sealer mati / tertinggal lebih dari {max_block_age}"))
    old = Block.objects.filter(sealed_at__lte=now - max_cosign_lag).order_by("-block_no").first()
    if old and not _signed_enough(old):
        findings.append(ForkFinding("cosign_lag", old.block_no, "",
                                    f"belum memenuhi threshold signature lebih dari {max_cosign_lag}"))
    return findings


def detect_forks(backends=None, verify_chain=True):
    """Bandingkan checkpoint di backend anchor eksternal dengan blok di DB.

    Args:
        backends: list AnchorBackend (None = dari settings)
        verify_chain: juga jalankan verify_blocks() atas seluruh chain

    Returns:
        list ForkFinding -- kosong berarti DB konsisten dengan semua checkpoint yang diterbitkan
    """
    backends = anchors.get_backends() if backends is None else backends
    findings, seen = [], {}                                     # block_no -> (hash, backend)
    for backend in backends:
        try:
            checkpoints = backend.checkpoints()
        except anchors.AnchorError as exc:
            findings.append(ForkFinding("backend_error", None, backend.name, str(exc)))
            continue
        for cp in checkpoints:
            findings += _check_checkpoint(cp, backend.name, seen)
    if verify_chain:
        ok, bad = verify_blocks()
        if not ok:
            findings.append(ForkFinding("chain_invalid", bad, "", "verify_blocks gagal (Merkle/tautan/signature)"))
    return findings


def _check_checkpoint(cp, backend_name, seen):
    try:
        expected = bytes.fromhex(cp.block_hash)
    except ValueError:
        return [ForkFinding("invalid_checkpoint", cp.block_no, backend_name, "block_hash bukan hex")]
    cp_keys = _valid_signatures(expected, [(s.get("public_key"), s.get("signature")) for s in cp.signatures])
    registered = set(SealerKey.objects.filter(public_key__in=cp_keys).values_list("public_key", flat=True))
    if _signatures_required() and not registered:
        return [ForkFinding("invalid_checkpoint", cp.block_no, backend_name,
                            "checkpoint tanpa signature valid dari sealer terdaftar", cp.block_hash)]
    findings = []
    other = seen.setdefault(cp.block_no, (cp.block_hash, backend_name))
    if other[0] != cp.block_hash:
        findings.append(ForkFinding("anchor_conflict", cp.block_no, backend_name,
                                    f"checkpoint berbeda dengan backend {other[1]}", other[0], cp.block_hash))
    block = Block.objects.filter(pk=cp.block_no).first()
    if block is None:
        findings.append(ForkFinding("missing_block", cp.block_no, backend_name,
                                    "blok yang sudah di-anchor tidak ada di DB (rollback/truncate)", cp.block_hash))
        return findings
    actual = bytes(block.block_hash)
    if actual == expected:
        return findings
    db_keys = _valid_signatures(actual, [(pk, sig) for _, pk, sig, _ in _block_signature_candidates(block)])
    both = sorted(registered & db_keys)
    if both:
        findings.append(ForkFinding("equivocation", cp.block_no, backend_name,
                                    f"key sealer menandatangani dua blok berbeda: {', '.join(both)}",
                                    cp.block_hash, actual.hex()))
    else:
        findings.append(ForkFinding("hash_mismatch", cp.block_no, backend_name,
                                    "block_hash di DB berbeda dari checkpoint (riwayat ditulis ulang)",
                                    cp.block_hash, actual.hex()))
    return findings


# ---------------------------------------------------------------- Shamir's Secret Sharing
def split_sealer_private_key(sealer_id, private_key, shares, threshold, actor_info=None):
    """Pecah private key sealer menjadi `shares` share (butuh `threshold` untuk rekonstruksi).

    Private key harus cocok dengan public key sealer. Yang dicatat di audit log hanya
    parameter + sidik jari share; share & private key tidak pernah disimpan.

    Returns:
        list[str] - share untuk dibagikan ke custodian (masing-masing simpan terpisah)
    """
    signing_key = _signing_key(private_key)
    try:
        sealer = SealerKey.objects.get(id=sealer_id)
    except SealerKey.DoesNotExist:
        raise NotFound(f"Sealer {sealer_id} tidak ditemukan")
    if sealer.public_key != _public_hex(signing_key):
        raise InvalidInput(f"Private key tidak cocok dengan sealer {sealer_id}")
    try:
        result = shamir.split_secret(signing_key.encode(encoder=RawEncoder), shares, threshold)
    except shamir.ShamirError as exc:
        raise InvalidInput(str(exc)) from exc
    _audit(SealerKeyAuditLog.EventType.KEY_SPLIT, sealer, actor_info,
           metadata={"shares": shares, "threshold": threshold,
                     "share_set": shamir.parse_share(result[0])[0],
                     "share_fingerprints": [shamir.share_fingerprint(s) for s in result]})
    return result


def recover_sealer_private_key(sealer_id, shares, actor_info=None):
    """Rekonstruksi private key sealer dari share Shamir. Return private key hex.

    Hasil diverifikasi terhadap public key sealer, jadi share yang salah / tercampur ditolak.
    """
    try:
        sealer = SealerKey.objects.get(id=sealer_id)
    except SealerKey.DoesNotExist:
        raise NotFound(f"Sealer {sealer_id} tidak ditemukan")
    try:
        secret = shamir.combine_shares(shares)
    except shamir.ShamirError as exc:
        raise InvalidInput(str(exc)) from exc
    signing_key = _signing_key(secret)
    if _public_hex(signing_key) != sealer.public_key:
        raise InvalidInput(f"Share tidak menghasilkan private key sealer {sealer_id}")
    _audit(SealerKeyAuditLog.EventType.KEY_RECOVERED, sealer, actor_info,
           metadata={"shares_used": len(shares),
                     "share_fingerprints": [shamir.share_fingerprint(s) for s in shares]})
    return secret.hex()


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
    # UUID -> str agar bukti JSON-serializable; _canon_log memakai str() jadi hash tetap sama
    log = {k: str(v) if isinstance(v, uuid.UUID) else v for k, v in zip(keys, row[:12])}
    # row = 12 kolom log (0..11) + l.hash (12) + hash sebelumnya (13)
    leaf, prev = bytes(row[12]), bytes(row[13])
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
                  "block_hash": bytes(blk.block_hash).hex(),
                  "signatures": checkpoint_for_block(blk).signatures},
    }


def trusted_sealer_keys():
    """{public_key_hex: region} semua key sealer yang belum di-revoke, untuk dipin oleh light client.

    Key yang di-rotate / expired tetap dipercaya untuk blok lama; key REVOKED tidak.
    """
    return dict(SealerKey.objects.exclude(status=SealerKey.KeyStatus.REVOKED)
                .values_list("public_key", "region"))


def _proof_signers(block_hash, signatures, trusted_keys):
    """{public_key: region} dari signature valid atas block_hash (hanya key tepercaya bila diberikan)."""
    if trusted_keys is not None and not isinstance(trusted_keys, dict):
        trusted_keys = dict.fromkeys(trusted_keys, "")
    signers = {}
    for sig in signatures or []:
        public_key, signature = sig.get("public_key"), sig.get("signature")
        if not isinstance(public_key, str) or not isinstance(signature, str) or public_key in signers:
            continue
        if trusted_keys is not None and public_key not in trusted_keys:
            continue
        if _valid_signatures(block_hash, [(public_key, signature)]):
            # region dari daftar tepercaya; region di dalam bukti tidak diautentikasi
            signers[public_key] = trusted_keys[public_key] if trusted_keys is not None else ""
    return signers


def verify_proof(proof, trusted_keys=None, threshold=1, min_regions=1):
    """Murni Python, tanpa DB. Cek: isi log -> hash log -> jalur Merkle -> root -> hash blok -> signature.

    Args:
        proof: hasil build_proof()
        trusted_keys: public key sealer yang dipercaya client -- dict {public_key: region} dari
                      trusted_sealer_keys(), atau iterable public key. None = signature valid dari
                      key mana pun dihitung (hanya membuktikan blok ditandatangani, BUKAN oleh siapa).
        threshold: minimum signature valid atas block_hash
        min_regions: minimum region berbeda (butuh trusted_keys berupa dict)
    """
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
        if expect.hex() != b["block_hash"] or not b["start_us"] <= proof["log"]["created_us"] < b["end_us"]:
            return False
        signers = _proof_signers(expect, b.get("signatures"), trusted_keys)
        return len(signers) >= max(1, threshold) and len(set(signers.values())) >= max(1, min_regions)
    except (KeyError, TypeError, ValueError):
        return False


# ---------------------------------------------------------------- asset management
# Fungsi untuk upload dan verifikasi file yang terkait dengan collection.
# File disimpan dengan content-addressable storage (nama = hash) untuk deduplication.
# Integritas diverifikasi lewat ledger (content_hash di log ACTED_ON).

def _file_sha256(file_obj, version=1):
    """Hitung SHA256 file dengan menyertakan versi. file_obj bisa berupa File object atau path string."""
    version_prefix = f"hash_version_{version}:".encode("utf-8")
    hasher = hashlib.sha256()

    # 1. Masukkan prefix versi ke dalam perhitungan hash di awal
    hasher.update(version_prefix)

    # 2. Proses pembacaan file (secara streaming agar hemat memori)
    if isinstance(file_obj, str):
        with open(file_obj, 'rb') as f:
            for chunk in iter(lambda: f.read(8192), b''):
                hasher.update(chunk)
    else:
        # File object (Django UploadedFile / InMemoryUploadedFile / TemporaryUploadedFile)
        if hasattr(file_obj, 'seek'):
            file_obj.seek(0)
        for chunk in iter(lambda: f.read(8192), b''):
            hasher.update(chunk)
        if hasattr(file_obj, 'seek'):
            file_obj.seek(0)
            
    # 3. Kembalikan dalam bentuk bytes (atau gunakan .hexdigest() jika ingin string hex)
    return hasher.digest()


def _get_storage_path(content_hash, filename):
    """Generate storage path berdasarkan hash (content-addressable)."""
    hash_hex = content_hash.hex()
    # Struktur: assets/ab/cdef1234567890... (2 karakter pertama sebagai folder)
    return f"assets/{hash_hex[:2]}/{hash_hex}"


def upload_asset(collection_id, actor_id, actor_member_id, file_obj,
                 original_filename=None, mime_type=None, metadata=None,
                 action_code="upload", replace_existing=False):
    """
    Upload file ke storage dan catat di ledger.

    Args:
        collection_id: ID collection yang terkait dengan file
        actor_id: ID association pemilik collection
        actor_member_id: ID member yang melakukan upload
        file_obj: File object (Django UploadedFile) atau path file
        original_filename: Nama asli file (opsional, akan diambil dari file_obj jika None)
        mime_type: MIME type file (opsional, akan dideteksi jika None)
        metadata: Dictionary metadata tambahan (opsional)
        action_code: Kode aksi untuk log ledger (default: "upload")
        replace_existing: Jika True, ganti asset lama yang active (jika ada)

    Returns:
        (asset_id, seq) - ID Asset yang dibuat dan seq log di ledger

    Proses:
        1. Hitung SHA256 file
        2. Cek deduplication: jika file dengan hash sama sudah ada, skip upload
        3. Upload ke storage dengan nama = hash (content-addressable)
        4. Buat/update record Asset (one-to-one dengan Collection)
        5. Log ACTED_ON ke ledger dengan content_hash
    """
    _check_member(actor_id, actor_member_id)

    # Hitung hash file
    content_hash = _file_sha256(file_obj)
    hash_hex = content_hash.hex()

    # Ambil info file
    if isinstance(file_obj, str):
        if original_filename is None:
            original_filename = file_obj.split('/')[-1]
        file_size = os.path.getsize(file_obj)
    else:
        if original_filename is None:
            original_filename = getattr(file_obj, 'name', 'unknown')
        file_size = file_obj.size

    # Upload ke storage (deduplication by hash)
    storage_path = _get_storage_path(content_hash, original_filename)

    # Cek apakah file sudah ada di storage
    if not default_storage.exists(storage_path):
        # Upload file baru
        if isinstance(file_obj, str):
            with open(file_obj, 'rb') as f:
                default_storage.save(storage_path, f)
        else:
            if hasattr(file_obj, 'seek'):
                file_obj.seek(0)
            default_storage.save(storage_path, file_obj)
            if hasattr(file_obj, 'seek'):
                file_obj.seek(0)

    # Import Asset model (delayed import untuk circular dependency)
    from .models_assets import Asset

    # Cek asset lama jika replace_existing
    old_asset = None
    if replace_existing:
        old_asset = Asset.objects.filter(
            collection_id=collection_id,
            status=Asset.AssetStatus.ACTIVE
        ).first()

    # Hapus asset lama jika replace (one-to-one: hanya 1 asset per collection)
    if old_asset:
        old_asset.delete()

    # Buat record Asset (one-to-one)
    asset = Asset.objects.create(
        collection_id=collection_id,
        storage_path=storage_path,
        content_hash=hash_hex,
        original_filename=original_filename,
        file_size=file_size,
        mime_type=mime_type or 'application/octet-stream',
        metadata=metadata or {},
        uploaded_by_member_id=actor_member_id
    )

    # Log ke ledger (ACTED_ON event)
    payload = {
        "asset_id": str(asset.id),
        "original_filename": original_filename,
        "file_size": file_size,
        "mime_type": mime_type or 'application/octet-stream',
    }
    if metadata:
        payload["metadata"] = metadata

    seq = act(action_code, collection_id, collection_id, actor_id, actor_member_id,
              content_hash=content_hash, payload=payload)[0]

    return str(asset.id), seq


def verify_asset(collection_id, seq, file_obj):
    """
    Verifikasi file sama dengan yang dicatat di ledger log ACTED_ON.

    Args:
        collection_id: ID collection
        seq: Seq log ACTED_ON yang ingin diverifikasi
        file_obj: File object atau path file yang ingin diverifikasi

    Returns:
        (valid, asset_id) - valid=True jika hash cocok, asset_id dari log payload
    """
    try:
        log = Log.objects.get(pk=(collection_id, seq))
    except Log.DoesNotExist:
        return False, None

    if log.event_type != Log.Event.ACTED_ON or log.content_hash is None:
        return False, None

    # Hitung hash file yang ingin diverifikasi
    file_hash = _file_sha256(file_obj)

    # Bandingkan dengan hash di ledger
    if bytes(log.content_hash) != file_hash:
        return False, None

    # Ambil asset_id dari payload
    asset_id = None
    if log.payload:
        asset_id = log.payload.get('asset_id')

    return True, asset_id


def get_asset(collection_id, status=None):
    """
    Ambil Asset untuk collection tertentu (one-to-one).

    Args:
        collection_id: ID collection
        status: Filter status (None = semua)

    Returns:
        Asset object atau None
    """
    from .models_assets import Asset

    qs = Asset.objects.filter(collection_id=collection_id)

    if status is not None:
        qs = qs.filter(status=status)

    return qs.first()


# ---------------------------------------------------------------- content management
# Fungsi untuk mencatat dan menyimpan content text (judul, isi tulisan) ke collection.

def add_or_update_content(collection_id, actor_id, actor_member_id, body,
                          title="", format="plain", metadata=None,
                          action_code="write"):
    """
    Tulis/update content dan catat di ledger.
    
    Args:
        collection_id: ID collection yang terkait dengan content
        actor_id: ID association pemilik collection
        actor_member_id: ID member yang melakukan aksi
        body: Isi utama teks
        title: Judul atau label singkat (opsional)
        format: plain / markdown / html / json
        metadata: Dictionary metadata tambahan (opsional)
        action_code: Kode aksi untuk log ledger (default: "write")
        
    Returns:
        (content_id, seq) - ID Content yang dibuat/diupdate dan seq log di ledger
    """
    _check_member(actor_id, actor_member_id)

    # Hitung hash
    content_hash = content_sha256(body)
    hash_hex = content_hash.hex()
    
    # Delayed import untuk menghindari circular import
    from .models_content import Content
    
    # Ambil content lama jika ada
    content = Content.objects.filter(
        collection_id=collection_id,
        status=Content.ContentStatus.ACTIVE
    ).first()
    
    if content:
        content.title = title
        content.body = body
        content.format = format
        content.content_hash = hash_hex
        content.metadata = metadata or {}
        content.save()
    else:
        content = Content.objects.create(
            collection_id=collection_id,
            title=title,
            body=body,
            format=format,
            content_hash=hash_hex,
            metadata=metadata or {},
            created_by_member_id=actor_member_id
        )

    # Log ke ledger (ACTED_ON event)
    payload = {
        "content_id": str(content.id),
        "title": title,
        "format": format,
        "char_count": content.char_count,
    }
    if metadata:
        payload["metadata"] = metadata

    seq = act(action_code, collection_id, collection_id, actor_id, actor_member_id,
              content_hash=content_hash, payload=payload)[0]

    return str(content.id), seq


def get_content(collection_id, status=None):
    """
    Ambil Content untuk collection tertentu.
    
    Args:
        collection_id: ID collection
        status: Filter status (None = semua)
        
    Returns:
        Content object atau None
    """
    from .models_content import Content
    qs = Content.objects.filter(collection_id=collection_id)
    if status is not None:
        qs = qs.filter(status=status)
    return qs.first()

