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
from datetime import datetime, timedelta, timezone

from django.apps import apps
from django.conf import settings
from django.core.files.storage import default_storage
from django.db import DatabaseError, connection, transaction
from django.db.models import Q
from django.utils.module_loading import import_string
from nacl import signing
from nacl.encoding import RawEncoder

from .models import Action, Block, Collection, Kind, Log, SealerKey, SealerKeyAuditLog


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


def create_entry(issuer_id, issuer_member_id, reason, supply, metadata=None, kind=0):
    """Catat alasan + total supply. `kind`: kode registry ("pen") atau id; 0/"generic" = benda umum.
    Item baru ada setelah mint_all().
    issuer_id = association penerbit; issuer_member_id = member yang membuat entry."""
    _check_member(issuer_id, issuer_member_id)
    return _call("SELECT ledger_create_entry(%s, %s, %s, %s, %s::jsonb, %s::smallint)",
                 [issuer_id, issuer_member_id, reason, supply, json.dumps(metadata or {}), _kind_id(kind)])


def mint_batch(entry_id, issuer_id, issuer_member_id, batch=10_000, prefix=""):
    """Lahirkan hingga `batch` item berikutnya. Return jumlah dibuat (0 = supply penuh).
    Member yang melahirkan dicatat di log seq 1 tiap item (boleh beda dari pembuat entry)."""
    _check_member(issuer_id, issuer_member_id)
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
    """
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

        # Sign block dengan multi-signature
        signatures = []
        if sealer_private_keys is not None:
            # Handle legacy single key
            if isinstance(sealer_private_keys, (str, bytes)):
                sealer_private_keys = [{"sealer_id": None, "private_key": sealer_private_keys}]

            # Handle multiple keys
            for sealer_info in sealer_private_keys:
                private_key = sealer_info["private_key"]
                sealer_id = sealer_info.get("sealer_id")

                if isinstance(private_key, str):
                    private_key = bytes.fromhex(private_key)
                if len(private_key) != 32:
                    raise InvalidInput("private_key harus 32 bytes (Ed25519)")

                # Generate signing key dari private key
                signing_key = signing.SigningKey(private_key)
                public_key = signing_key.verify_key.encode(encoder=RawEncoder).hex()
                signature = signing_key.sign(block_hash, encoder=RawEncoder).signature.hex()

                # Cek sealer_id valid dan aktif
                if sealer_id:
                    try:
                        sealer = SealerKey.objects.get(id=sealer_id)
                        if not sealer.is_active:
                            raise InvalidInput(f"Sealer {sealer_id} tidak aktif atau sudah expired")
                        if sealer.public_key != public_key:
                            raise InvalidInput(f"Public key tidak cocok dengan sealer {sealer_id}")
                    except SealerKey.DoesNotExist:
                        raise InvalidInput(f"Sealer {sealer_id} tidak ditemukan")

                signatures.append({
                    "sealer_id": str(sealer_id) if sealer_id else None,
                    "public_key": public_key,
                    "signature": signature
                })

        cur.execute(
            "INSERT INTO ledger_blocks (block_no, window_start, window_end, log_count, "
            "merkle_root, prev_block_hash, block_hash, signatures) "
            "VALUES (%s,%s,%s,%s,%s,%s,%s,%s::jsonb)",
            [block_no, start, end, count, root, prev, block_hash, json.dumps(signatures)])
    return Block.objects.get(pk=block_no)


def verify_blocks(first=1, last=None, verify_signature=True, threshold=None):
    """Hitung ulang Merkle root dari log + cek tautan antar-blok. Return (ok, block_no_rusak).

    Args:
        first: Block number pertama untuk verifikasi
        last: Block number terakhir (None = sampai terakhir)
        verify_signature: Jika True, verifikasi signature sealer jika ada
        threshold: Minimum signature valid yang dibutuhkan (None = default 1)

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

        # Verifikasi signature jika ada dan verify_signature=True
        if verify_signature:
            valid_count = 0

            # New: cek signatures field (multi-sig)
            if b.signatures:
                for sig in b.signatures:
                    try:
                        public_key = sig.get('public_key')
                        signature = sig.get('signature')
                        sealer_id = sig.get('sealer_id')

                        # Cek apakah sealer masih aktif
                        if sealer_id:
                            try:
                                sealer = SealerKey.objects.get(id=sealer_id)
                                if not sealer.is_active:
                                    continue  # Skip expired/revoked sealer
                            except SealerKey.DoesNotExist:
                                continue  # Skip unknown sealer

                        verify_key = signing.VerifyKey(bytes.fromhex(public_key), encoder=RawEncoder)
                        verify_key.verify(bytes(b.block_hash), bytes.fromhex(signature), encoder=RawEncoder)
                        valid_count += 1
                    except Exception:
                        continue  # Skip invalid signature

                # Cek threshold
                block_threshold = threshold or 1
                if valid_count < block_threshold:
                    return False, b.block_no

            # Legacy: cek field lama untuk backward compatibility
            elif hasattr(b, 'sealer_public_key') and hasattr(b, 'sealer_signature'):
                if b.sealer_public_key is not None and b.sealer_signature is not None:
                    try:
                        verify_key = signing.VerifyKey(bytes(b.sealer_public_key), encoder=RawEncoder)
                        verify_key.verify(bytes(b.block_hash), bytes(b.sealer_signature), encoder=RawEncoder)
                    except Exception:
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

    Args:
        block: Block object
        public_key_hex: Public key sealer (hex string)

    Returns:
        True jika signature valid, False jika tidak valid atau tidak ada signature
    """
    # Legacy: cek field lama untuk backward compatibility
    if hasattr(block, 'sealer_public_key') and hasattr(block, 'sealer_signature'):
        if block.sealer_public_key is None or block.sealer_signature is None:
            return False
        try:
            verify_key = signing.VerifyKey(bytes.fromhex(public_key_hex), encoder=RawEncoder)
            verify_key.verify(bytes(block.block_hash), bytes(block.sealer_signature), encoder=RawEncoder)
            return True
        except Exception:
            return False

    # New: cek signatures field (multi-sig)
    if block.signatures:
        for sig in block.signatures:
            if sig.get('public_key') == public_key_hex:
                try:
                    verify_key = signing.VerifyKey(bytes.fromhex(public_key_hex), encoder=RawEncoder)
                    verify_key.verify(bytes(block.block_hash), bytes.fromhex(sig['signature']), encoder=RawEncoder)
                    return True
                except Exception:
                    return False
    return False


def register_sealer(name, public_key_hex, valid_until=None, threshold=1, metadata=None, actor_info=None):
    """Register sealer baru dengan key pair.

    Args:
        name: Nama sealer (mis: "sealer-1", "sealer-2")
        public_key_hex: Public key sealer (hex string, 64 chars)
        valid_until: Expiration date (None = tidak pernah expired)
        threshold: Threshold untuk multi-sig (berapa banyak signature dibutuhkan)
        metadata: Dictionary metadata tambahan
        actor_info: Dictionary actor info {"user_id": "...", "email": "...", "ip": "...", "user_agent": "..."}

    Returns:
        SealerKey object
    """
    if len(public_key_hex) != 64:
        raise InvalidInput("public_key_hex harus 64 chars (32 bytes hex)")

    sealer = SealerKey.objects.create(
        name=name,
        public_key=public_key_hex,
        valid_until=valid_until,
        threshold=threshold,
        metadata=metadata or {}
    )

    # Log audit event
    SealerKeyAuditLog.objects.create(
        event_type=SealerKeyAuditLog.EventType.KEY_REGISTERED,
        sealer_id=sealer.id,
        sealer_name=name,
        new_key_id=sealer.id,
        new_public_key=public_key_hex,
        new_valid_until=valid_until,
        actor_user_id=actor_info.get("user_id") if actor_info else None,
        actor_email=actor_info.get("email") if actor_info else None,
        actor_ip=actor_info.get("ip") if actor_info else None,
        actor_user_agent=actor_info.get("user_agent") if actor_info else None,
        metadata=metadata or {}
    )

    return sealer


def get_active_sealers():
    """Ambil semua sealer yang aktif dan belum expired.

    Returns:
        QuerySet of SealerKey objects
    """
    now = timezone.now()
    return SealerKey.objects.filter(
        status=SealerKey.KeyStatus.ACTIVE
    ).filter(
        Q(valid_until__isnull=True) | Q(valid_until__gt=now)
    )


def rotate_sealer_key(sealer_id, new_public_key_hex, valid_until=None):
    """Rotate key untuk sealer yang ada.

    Args:
        sealer_id: ID SealerKey yang akan di-rotate
        new_public_key_hex: Public key baru (hex string, 64 chars)
        valid_until: Expiration date baru (None = tidak pernah expired)

    Returns:
        SealerKey object baru
    """
    old_key = SealerKey.objects.get(id=sealer_id)

    # Mark old key as expired
    old_key.status = SealerKey.KeyStatus.EXPIRED
    old_key.save()

    # Create new key dengan nama yang sama
    return SealerKey.objects.create(
        name=old_key.name,
        public_key=new_public_key_hex,
        valid_until=valid_until,
        threshold=old_key.threshold,
        metadata=old_key.metadata
    )


def expire_old_keys():
    """Mark key yang sudah expired sebagai EXPIRED status.

    Returns:
        Number of keys yang diupdate
    """
    now = timezone.now()
    count = SealerKey.objects.filter(
        status=SealerKey.KeyStatus.ACTIVE,
        valid_until__isnull=False,
        valid_until__lt=now
    ).update(status=SealerKey.KeyStatus.EXPIRED)
    return count


def verify_block_signatures(block, threshold=None):
    """Verifikasi multi-signature sebuah block dengan threshold.

    Args:
        block: Block object
        threshold: Minimum signature valid yang dibutuhkan (None = 1)

    Returns:
        (valid, count) - valid=True jika cukup signature valid, count=jumlah signature valid
    """
    if not block.signatures:
        return False, 0

    valid_count = 0
    for sig in block.signatures:
        try:
            public_key = sig.get('public_key')
            signature = sig.get('signature')
            sealer_id = sig.get('sealer_id')

            # Cek apakah sealer masih aktif
            if sealer_id:
                try:
                    sealer = SealerKey.objects.get(id=sealer_id)
                    if not sealer.is_active:
                        continue  # Skip expired/revoked sealer
                except SealerKey.DoesNotExist:
                    continue  # Skip unknown sealer

            verify_key = signing.VerifyKey(bytes.fromhex(public_key), encoder=RawEncoder)
            verify_key.verify(bytes(block.block_hash), bytes.fromhex(signature), encoder=RawEncoder)
            valid_count += 1
        except Exception:
            continue  # Skip invalid signature

    required = threshold or 1
    return valid_count >= required, valid_count


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


# ---------------------------------------------------------------- asset management
# Fungsi untuk upload dan verifikasi file yang terkait dengan collection.
# File disimpan dengan content-addressable storage (nama = hash) untuk deduplication.
# Integritas diverifikasi lewat ledger (content_hash di log ACTED_ON).

def _file_sha256(file_obj):
    """Hitung SHA256 file. file_obj bisa berupa File object atau path string."""
    hasher = hashlib.sha256()
    if isinstance(file_obj, str):
        with open(file_obj, 'rb') as f:
            for chunk in iter(lambda: f.read(8192), b''):
                hasher.update(chunk)
    else:
        # File object (Django UploadedFile)
        if hasattr(file_obj, 'seek'):
            file_obj.seek(0)
        for chunk in iter(lambda: file_obj.read(8192), b''):
            hasher.update(chunk)
        if hasattr(file_obj, 'seek'):
            file_obj.seek(0)
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
    from .assets import Asset

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
    from .assets import Asset

    qs = Asset.objects.filter(collection_id=collection_id)

    if status is not None:
        qs = qs.filter(status=status)

    return qs.first()
