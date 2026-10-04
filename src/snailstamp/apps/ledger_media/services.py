"""
Lampiran media (foto/video) untuk collection. Byte file di S3; ledger hanya menyimpan sha256-nya.

Alur (lihat README):
    request_upload -> klien PUT langsung ke S3 (presigned) -> finalize_upload -> get_download_url

Identitas mengikuti ledger: association = pemilik, member = pelaku. Setiap fungsi publik memeriksa
keanggotaan lewat ledger.services.check_member (aturan yang sama dengan penulisan ledger).
Izin BACA/HAPUS media mengikuti pemilik collection SAAT INI: bila collection diwariskan ke
association lain, media ikut berpindah akses tanpa menyalin file apa pun.
"""
import hashlib
import logging
import uuid
from datetime import timedelta
from urllib.parse import quote

from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from ..ledger import services as ledger
from ..ledger.models import Collection, Log
from ..ledger.services import Forbidden, InvalidInput, InvalidState, LedgerError, NotFound  # noqa: F401  (re-export)
from . import conf
from .models import MediaObject
from .sniff import matches, sniff
from .storage import S3Storage

log = logging.getLogger(__name__)

__all__ = ["request_upload", "presign_parts", "finalize_upload", "get_download_url", "list_media",
           "erase_media", "verify_media", "expire_pending", "clean_filename", "NotUploaded", "InvalidParts",
           "Forbidden", "InvalidInput", "InvalidState", "NotFound", "LedgerError"]


class NotUploaded(InvalidState):
    """finalize dipanggil sebelum file selesai diunggah. Status tetap PENDING: unggah lalu coba lagi."""


class InvalidParts(InvalidInput):
    """Daftar part multipart ditolak S3 (ETag keliru / belum lengkap). BISA DIULANG: media tetap PENDING dan
    part yang sudah terunggah tidak dibuang; kirim daftar yang benar."""


class _Reject(InvalidInput):
    """File ditolak setelah diperiksa (ukuran/tipe salah). Terminal: media menjadi FAILED."""


# ------------------------------------------------------------------ util
def _uuid(value, what="id"):
    if isinstance(value, uuid.UUID):
        return value
    try:
        return uuid.UUID(str(value))
    except ValueError:
        raise InvalidInput(f"{what} bukan UUID yang valid") from None


def clean_filename(name):
    """Nama file aman untuk header & tampilan: tanpa path, tanda kutip, atau karakter kontrol."""
    name = (name or "").replace("\\", "/").rsplit("/", 1)[-1]
    name = "".join(ch for ch in name if ch.isprintable() and ch not in '"<>|:*?')
    return name.strip(" .")[:150] or "file"


def _disposition(media):
    kind = "inline" if media.content_type.startswith(("image/", "video/")) else "attachment"
    ascii_name = media.filename.encode("ascii", "ignore").decode() or "file"
    return f"{kind}; filename=\"{ascii_name}\"; filename*=UTF-8''{quote(media.filename)}"


def _owned_collection(collection_id, association_id):
    """Collection harus ada dan dimiliki association ini SAAT INI."""
    c = Collection.objects.filter(pk=collection_id).first()
    if c is None:
        raise NotFound(f"collection {collection_id} tidak ada")
    if c.owner_id != association_id:
        raise Forbidden("hanya association pemilik collection saat ini yang boleh")
    return c


def _get_media(media_id):
    media = MediaObject.objects.filter(pk=_uuid(media_id, "media_id")).first()
    if media is None:
        raise NotFound("media tidak ada")
    return media


def _uploader_media(media_id, association_id, member_id):
    """Pemanggil harus member yang meminta unggahan ini (dan masih anggota association-nya)."""
    ledger.check_member(association_id, member_id)
    media = _get_media(media_id)
    if media.association_id != association_id or media.uploaded_by_id != member_id:
        raise Forbidden("unggahan ini bukan milik anda")
    return media


def _best_effort(what, fn, *args):
    try:
        fn(*args)
    except Exception:                                   # pembersihan tidak boleh menutupi galat asli
        log.warning("ledger_media: gagal %s", what, exc_info=True)


def _purge_objects(media, storage):
    if media.multipart_upload_id:
        _best_effort("abort multipart", storage.abort_multipart, media.upload_key, media.multipart_upload_id)
    _best_effort("hapus upload_key", storage.delete, media.upload_key)
    _best_effort("hapus media_key", storage.delete, media.media_key)


# ------------------------------------------------------------------ 1. minta izin unggah
def request_upload(association_id, member_id, collection_id, filename, content_type, size, storage=None):
    """Validasi lalu beri klien URL unggah langsung ke S3 (file TIDAK lewat Django).

    Return dict:
      mode "single"    -> {"method": "PUT", "url", "headers": {...}}  (satu PUT; header WAJIB dikirim apa adanya)
      mode "multipart" -> {"part_size", "part_count", "parts": [{"part_number", "url"}]}; PUT tiap part lalu
                          kirim daftar {"PartNumber", "ETag"} ke finalize_upload(parts=...)
    """
    association_id, member_id = _uuid(association_id, "association_id"), _uuid(member_id, "member_id")
    ledger.check_member(association_id, member_id)

    ctype = (content_type or "").strip().lower()
    allowed = conf.get("ALLOWED_TYPES")
    if ctype not in allowed:
        raise InvalidInput(f"tipe {content_type!r} tidak diizinkan")
    if not isinstance(size, int) or isinstance(size, bool) or size <= 0:
        raise InvalidInput("size harus bilangan bulat > 0")
    if size > allowed[ctype]:
        raise InvalidInput(f"ukuran melebihi batas {allowed[ctype]} byte untuk {ctype}")

    c = _owned_collection(collection_id, association_id)
    if c.state != Collection.State.ACTIVE:
        raise InvalidState("collection sedang dalam pengiriman")

    st = storage or S3Storage()
    media_id = uuid.uuid4()
    media = MediaObject(id=media_id, collection_id=collection_id, association_id=association_id,
                        uploaded_by_id=member_id, filename=clean_filename(filename), content_type=ctype, size=size,
                        upload_key=f"{conf.get('UPLOAD_PREFIX')}{media_id}",
                        media_key=f"{conf.get('MEDIA_PREFIX')}{media_id}")
    ttl = conf.get("UPLOAD_URL_TTL")

    if size <= conf.get("SINGLE_PUT_MAX"):
        media.save()
        return {"media_id": media_id, "mode": "single", "method": "PUT", "expires_in": ttl,
                "url": st.presign_put(media.upload_key, ctype, ttl), "headers": {"Content-Type": ctype}}

    part_size = conf.get("PART_SIZE")
    count = -(-size // part_size)
    if count > 10_000:
        raise InvalidInput("terlalu banyak part; perbesar LEDGER_MEDIA['PART_SIZE']")
    media.multipart_upload_id = st.create_multipart(media.upload_key, ctype)
    media.save()
    return {"media_id": media_id, "mode": "multipart", "expires_in": ttl, "part_size": part_size,
            "part_count": count,
            "parts": [{"part_number": n, "url": st.presign_part(media.upload_key, media.multipart_upload_id, n, ttl)}
                      for n in range(1, count + 1)]}


def presign_parts(media_id, association_id, member_id, part_numbers, storage=None):
    """Perbarui URL part yang kedaluwarsa (unggahan besar bisa lebih lama dari TTL)."""
    association_id, member_id = _uuid(association_id, "association_id"), _uuid(member_id, "member_id")
    media = _uploader_media(media_id, association_id, member_id)
    if media.status != MediaObject.Status.PENDING or not media.multipart_upload_id:
        raise InvalidState("bukan unggahan multipart yang sedang berjalan")
    count = -(-media.size // conf.get("PART_SIZE"))
    st, ttl = storage or S3Storage(), conf.get("UPLOAD_URL_TTL")
    out = []
    for n in part_numbers:
        if not isinstance(n, int) or not 1 <= n <= count:
            raise InvalidInput(f"part_number harus 1..{count}")
        out.append({"part_number": n, "url": st.presign_part(media.upload_key, media.multipart_upload_id, n, ttl)})
    return out


# ------------------------------------------------------------------ 2. finalisasi
def _claim(media_id):
    """PENDING -> PROCESSING secara atomik (cegah finalisasi ganda). Finalisasi macet boleh diambil alih."""
    now = timezone.now()
    stale = now - timedelta(seconds=conf.get("PROCESSING_STALE"))
    return MediaObject.objects.filter(pk=media_id).filter(
        Q(status=MediaObject.Status.PENDING) | Q(status=MediaObject.Status.PROCESSING, processing_since__lt=stale)
    ).update(status=MediaObject.Status.PROCESSING, processing_since=now)


def _release(media_id):
    MediaObject.objects.filter(pk=media_id, status=MediaObject.Status.PROCESSING).update(
        status=MediaObject.Status.PENDING, processing_since=None)


def _fail(media, storage, reason, from_statuses=(MediaObject.Status.PROCESSING,)):
    """PROCESSING/PENDING -> FAILED, lalu hapus objeknya. Bersyarat: bila worker lain sudah
    menyelesaikan media ini (READY) kita TIDAK menimpanya dan TIDAK menghapus file-nya."""
    won = MediaObject.objects.filter(pk=media.pk, status__in=from_statuses).update(
        status=MediaObject.Status.FAILED, failure_reason=reason[:200], processing_since=None)
    if won:
        _purge_objects(media, storage)
    return bool(won)


def _move_and_hash(media, storage, parts):
    """Pindahkan file ke key milik server lalu hitung sha256-nya DI SANA.

    Mengapa dipindah dulu: presigned URL klien masih berlaku sampai TTL habis. Bila kita meng-hash
    objek di upload_key, pemegang URL bisa menimpa isinya SESUDAH di-hash. media_key tak punya URL
    presigned untuk ditulis siapa pun, jadi hash yang dihitung di sana pasti = byte yang tersimpan.
    Setiap langkah idempoten, sehingga finalize aman diulang setelah gagal di tengah jalan.
    """
    info = storage.head(media.media_key)
    if info is None:                                             # belum dipindah
        info = storage.head(media.upload_key)
        if info is None and media.multipart_upload_id and parts:
            try:
                storage.complete_multipart(media.upload_key, media.multipart_upload_id, parts)
            except Exception as e:                               # noqa: BLE001 (disaring lewat kode galat)
                code = getattr(e, "response", {}).get("Error", {}).get("Code")
                if code in ("InvalidPart", "InvalidPartOrder", "EntityTooSmall"):
                    raise InvalidParts(f"daftar part ditolak S3 ({code}); periksa ETag tiap part") from None
                if code != "NoSuchUpload":
                    raise
            info = storage.head(media.upload_key)
        if info is None:
            raise NotUploaded("file belum selesai diunggah")
        limit = conf.get("ALLOWED_TYPES").get(media.content_type, 0)
        if info["size"] != media.size or info["size"] > limit:
            raise _Reject(f"ukuran file ({info['size']}) tidak sama dengan yang dideklarasikan ({media.size})")
        storage.copy(media.upload_key, media.media_key, media.content_type)
        storage.delete(media.upload_key)

    head, total, h = b"", 0, hashlib.sha256()
    for chunk in storage.iter_chunks(media.media_key):
        if not head:
            head = chunk[:64]
        total += len(chunk)
        h.update(chunk)
    if total != media.size:
        raise _Reject(f"ukuran file ({total}) tidak sama dengan yang dideklarasikan ({media.size})")
    if not matches(media.content_type, sniff(head)):
        raise _Reject("isi file tidak sesuai tipe yang dideklarasikan")
    return h.digest()


def _record(media_id, member_id, sha):
    """Catat ke ledger DAN tandai READY dalam satu transaksi: keduanya berhasil atau tidak sama sekali."""
    with transaction.atomic():
        m = MediaObject.objects.select_for_update().get(pk=media_id)
        if m.status != MediaObject.Status.PROCESSING:
            raise InvalidState("status media berubah saat diproses")
        seq = ledger.use(m.collection_id, m.association_id, member_id, conf.get("ACTION"),
                         payload={"media": str(m.id), "type": m.content_type, "bytes": m.size},
                         content_hash=sha)
        m.sha256, m.log_seq = sha, seq
        m.status, m.finalized_at, m.processing_since = MediaObject.Status.READY, timezone.now(), None
        m.save(update_fields=["sha256", "log_seq", "status", "finalized_at", "processing_since"])
        return m


def _valid_parts(parts):
    if parts is None:
        return None
    ok = (isinstance(parts, (list, tuple)) and parts
          and all(isinstance(p, dict) and isinstance(p.get("PartNumber"), int) and isinstance(p.get("ETag"), str)
                  for p in parts))
    if not ok:
        raise InvalidInput('parts harus daftar [{"PartNumber": int, "ETag": str}, ...]')
    return parts


def finalize_upload(media_id, association_id, member_id, parts=None, storage=None):
    """Setelah klien selesai mengunggah: verifikasi file, hitung sha256, catat ke ledger.

    parts: untuk multipart, daftar [{"PartNumber": n, "ETag": "..."}] hasil PUT tiap part.
    Idempoten: memanggil ulang untuk media yang sudah READY mengembalikannya tanpa menulis ledger lagi.
    Galat: NotUploaded / InvalidParts (bisa diulang, media tetap PENDING) | InvalidInput (file ditolak:
    ukuran/tipe salah, terminal) | Forbidden/InvalidState
    (mis. collection sudah berpindah atau sedang dikirim) -> media menjadi FAILED dan file dihapus.
    """
    association_id, member_id = _uuid(association_id, "association_id"), _uuid(member_id, "member_id")
    parts = _valid_parts(parts)
    media = _uploader_media(media_id, association_id, member_id)
    if media.status == MediaObject.Status.READY:
        return media
    if media.status in (MediaObject.Status.FAILED, MediaObject.Status.ERASED):
        raise InvalidState(f"media berstatus {media.status}; minta unggahan baru")

    st = storage or S3Storage()
    if not _claim(media.pk):
        media.refresh_from_db()
        if media.status == MediaObject.Status.READY:
            return media
        raise InvalidState("unggahan ini sedang diproses; coba lagi sebentar")
    try:
        sha = _move_and_hash(media, st, parts)
        return _record(media.pk, member_id, sha)
    except (NotUploaded, InvalidParts):                          # bisa diulang: jangan buang unggahan
        _release(media.pk)
        raise
    except LedgerError as e:                                     # ditolak (file/ledger): terminal
        _fail(media, st, str(e))
        raise
    except Exception:                                            # galat infrastruktur: boleh dicoba lagi
        _release(media.pk)
        raise


# ------------------------------------------------------------------ 3. baca / daftar / hapus
def _readable(media_id, association_id, member_id):
    association_id, member_id = _uuid(association_id, "association_id"), _uuid(member_id, "member_id")
    ledger.check_member(association_id, member_id)
    media = _get_media(media_id)
    _owned_collection(media.collection_id, association_id)       # izin = pemilik SAAT INI
    return media, association_id, member_id


def get_download_url(media_id, association_id, member_id, storage=None):
    """URL unduh berumur pendek. Tipe & nama dipaksa dari data tervalidasi, bukan dari objek S3."""
    media, *_ = _readable(media_id, association_id, member_id)
    if media.status != MediaObject.Status.READY:
        raise InvalidState(f"media berstatus {media.status}")
    ttl = conf.get("DOWNLOAD_URL_TTL")
    st = storage or S3Storage()
    return {"url": st.presign_get(media.media_key, ttl, media.content_type, _disposition(media)),
            "expires_in": ttl, "content_type": media.content_type, "filename": media.filename,
            "size": media.size, "sha256": bytes(media.sha256).hex()}


def list_media(collection_id, association_id, member_id):
    """Galeri sebuah collection, urut sesuai urutan log di ledger (tak bisa disisipi diam-diam)."""
    association_id, member_id = _uuid(association_id, "association_id"), _uuid(member_id, "member_id")
    ledger.check_member(association_id, member_id)
    _owned_collection(collection_id, association_id)
    return MediaObject.objects.filter(collection_id=collection_id, status=MediaObject.Status.READY).order_by("log_seq")


def erase_media(media_id, association_id, member_id, storage=None):
    """Hapus BYTE file. Ledger tetap utuh: hash, waktu, dan pelaku unggahan tetap terbukti.
    Hanya association pemilik collection saat ini yang boleh menghapus."""
    media, association_id, member_id = _readable(media_id, association_id, member_id)
    if media.status == MediaObject.Status.ERASED:
        return media
    if media.status == MediaObject.Status.PROCESSING:
        raise InvalidState("media sedang diproses")
    _purge_objects(media, storage or S3Storage())
    MediaObject.objects.filter(pk=media.pk).update(
        status=MediaObject.Status.ERASED, erased_at=timezone.now(), erased_by_id=member_id)
    media.refresh_from_db()
    return media


# ------------------------------------------------------------------ 4. audit & perawatan
def verify_media(media, rehash=False, storage=None):
    """Cocokkan baris media dengan bukti di ledger (dan, bila rehash, dengan byte di S3).
    Return (ok, detail). Tidak mengubah apa pun."""
    if media.status not in (MediaObject.Status.READY, MediaObject.Status.ERASED):
        return False, f"status {media.status}: belum tercatat di ledger"
    entry = Log.objects.filter(pk=(media.collection_id, media.log_seq)).first()
    if entry is None:
        return False, "log ledger tidak ditemukan"
    if entry.event_type != Log.Event.USE or entry.content_hash is None:
        return False, "log ledger bukan lampiran (USE dengan content_hash)"
    if bytes(entry.content_hash) != bytes(media.sha256):
        return False, "sha256 di tabel media berbeda dari ledger"
    if entry.actor_id != media.association_id or entry.actor_member_id != media.uploaded_by_id:
        return False, "association/member di tabel media berbeda dari ledger"
    if (entry.payload or {}).get("media") != str(media.id):
        return False, "payload ledger menunjuk media lain"
    if rehash and media.status == MediaObject.Status.READY:
        st = storage or S3Storage()
        info = st.head(media.media_key)
        if info is None:
            return False, "objek hilang dari S3"
        sha = ledger.sha256_chunks(st.iter_chunks(media.media_key))
        if sha != bytes(media.sha256):
            return False, "isi file di S3 berbeda dari hash di ledger"
    return True, "ok" if media.status == MediaObject.Status.READY else "dihapus; bukti di ledger utuh"


def expire_pending(storage=None, now=None):
    """Tandai FAILED unggahan yang tak pernah difinalisasi (dan finalisasi yang macet), hapus sisa objeknya."""
    now = now or timezone.now()
    cutoff = now - timedelta(seconds=conf.get("PENDING_TTL"))
    stale = now - timedelta(seconds=conf.get("PROCESSING_STALE"))
    st = storage or S3Storage()
    rows = MediaObject.objects.filter(
        Q(status=MediaObject.Status.PENDING, created_at__lt=cutoff)
        | Q(status=MediaObject.Status.PROCESSING, processing_since__lt=stale, created_at__lt=cutoff))
    n = 0
    for media in rows.iterator():
        if _fail(media, st, "kedaluwarsa: unggahan tidak difinalisasi",
                 from_statuses=(MediaObject.Status.PENDING, MediaObject.Status.PROCESSING)):
            n += 1
    return n
