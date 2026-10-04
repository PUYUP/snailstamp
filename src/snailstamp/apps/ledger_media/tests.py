"""
Tes ledger_media. Butuh PostgreSQL (migrasi ledger) dan, untuk tes alur S3, paket `moto[server]`:

    pip install boto3 "moto[server]"
    python manage.py test snailstamp.apps.ledger_media

S3 diganti server moto sungguhan (HTTP lokal), jadi presigned URL benar-benar dipakai untuk PUT/GET.
Tanpa moto, tes alur S3 dilewati; tes unit (sniff, nama file, konfigurasi) tetap jalan.

Keterbatasan yang diketahui: moto gagal memproses PUT `upload_part` lewat presigned URL
(TypeError di kodenya, part_number=None), padahal `upload_part` langsung via API berjalan. Karena itu
tes multipart mengunggah part lewat API dan hanya MEMERIKSA BENTUK presigned URL part-nya.
Tes tidak memakai model tenant: FK memakai db_constraint=False, cukup UUID acak.
"""
import hashlib
import io
import urllib.request
import uuid
from datetime import timedelta
from unittest import skipUnless

from botocore.exceptions import ClientError
from django.core.exceptions import ImproperlyConfigured
from django.core.management import call_command
from django.core.management.base import CommandError
from django.db import IntegrityError, transaction
from django.test import SimpleTestCase, TestCase, override_settings
from django.utils import timezone

try:
    import boto3
    from moto.server import ThreadedMotoServer
    HAVE_MOTO = True
except ImportError:                                          # pragma: no cover
    HAVE_MOTO = False

from ..ledger import services as ledger
from ..ledger.models import Collection, Log
from . import conf, services
from .models import MediaObject
from .sniff import matches, sniff
from .storage import S3Storage

MiB = 1024 * 1024
BUCKET = "test-bucket"
JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 60 + b"isi-foto"
_MEMBERS = {}


def fake_member_check(association_id, member_id):
    return member_id in _MEMBERS.get(association_id, ())


def _put(url, data, headers=None):
    req = urllib.request.Request(url, data=data, method="PUT", headers=headers or {})
    with urllib.request.urlopen(req) as r:
        return r.status


@skipUnless(HAVE_MOTO, "butuh `pip install boto3 'moto[server]'`")
class MediaFlowTests(TestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.server = ThreadedMotoServer(port=0, verbose=False)
        cls.server.start()
        host, port = cls.server.get_host_and_port()
        cls.endpoint = f"http://{host}:{port}"
        cls.s3 = boto3.client("s3", endpoint_url=cls.endpoint, region_name="us-east-1",
                              aws_access_key_id="k", aws_secret_access_key="s")
        cls.s3.create_bucket(Bucket=BUCKET)
        cls._ov = override_settings(LEDGER_MEMBER_CHECK=f"{__name__}.fake_member_check",
                                    LEDGER_MEDIA=cls.conf())
        cls._ov.enable()

    @classmethod
    def tearDownClass(cls):
        cls._ov.disable()
        cls.server.stop()
        super().tearDownClass()

    @classmethod
    def conf(cls, **extra):
        return {"BUCKET": BUCKET, "REGION": "us-east-1", "ENDPOINT_URL": cls.endpoint, "ADDRESSING_STYLE": "path",
                "ACCESS_KEY_ID": "k", "SECRET_ACCESS_KEY": "s", **extra}

    def setUp(self):
        _MEMBERS.clear()
        self.fam_a, self.fam_b = uuid.uuid4(), uuid.uuid4()
        self.ayah, self.kakak, self.mem_b = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
        _MEMBERS[self.fam_a] = {self.ayah, self.kakak}
        _MEMBERS[self.fam_b] = {self.mem_b}
        _, (self.coll,) = ledger.create_item(self.fam_a, self.ayah, "album keluarga", 1, "journal")

    # ---- helpers
    def exists(self, key):
        try:
            self.s3.head_object(Bucket=BUCKET, Key=key)
            return True
        except ClientError:
            return False

    def body(self, key):
        return self.s3.get_object(Bucket=BUCKET, Key=key)["Body"].read()

    def log_count(self):
        return Log.objects.filter(collection_id=self.coll).count()

    def upload(self, data=JPEG, ctype="image/jpeg", name="foto ibu.jpg"):
        r = services.request_upload(self.fam_a, self.ayah, self.coll, name, ctype, len(data))
        self.assertEqual(_put(r["url"], data, r["headers"]), 200)
        return r["media_id"]

    def ready(self, **kw):
        return services.finalize_upload(self.upload(**kw), self.fam_a, self.ayah)

    # ---- alur utama
    def test_single_upload_is_hashed_and_recorded_in_ledger(self):
        media = self.ready()
        sha = hashlib.sha256(JPEG).digest()
        self.assertEqual((media.status, bytes(media.sha256)), (MediaObject.Status.READY, sha))

        entry = Log.objects.get(pk=(self.coll, media.log_seq))
        self.assertEqual((entry.event_type, bytes(entry.content_hash)), (Log.Event.USE, sha))
        self.assertEqual((entry.actor_id, entry.actor_member_id), (self.fam_a, self.ayah))
        self.assertEqual(entry.payload, {"media": str(media.id), "type": "image/jpeg", "bytes": len(JPEG)})
        self.assertNotIn("foto", str(entry.payload))                      # nama file tidak masuk ledger
        self.assertEqual(ledger.verify_chain(self.coll), (True, None, "ok"))
        self.assertTrue(ledger.verify_content(self.coll, media.log_seq, content=JPEG))
        self.assertEqual(services.verify_media(media, rehash=True), (True, "ok"))

        self.assertFalse(self.exists(media.upload_key))                   # lokasi tulis klien dikosongkan
        self.assertEqual(self.body(media.media_key), JPEG)
        self.assertEqual(self.s3.head_object(Bucket=BUCKET, Key=media.media_key)["ContentType"], "image/jpeg")

    def test_presigned_url_cannot_alter_a_finalized_file(self):
        """URL unggah klien masih berlaku sampai TTL habis. Menulis lewat URL itu setelah finalisasi
        tidak boleh mengubah file yang sudah di-hash (hash dihitung di media_key milik server)."""
        r = services.request_upload(self.fam_a, self.ayah, self.coll, "a.jpg", "image/jpeg", len(JPEG))
        _put(r["url"], JPEG, r["headers"])
        media = services.finalize_upload(r["media_id"], self.fam_a, self.ayah)
        self.assertEqual(_put(r["url"], b"\xff\xd8\xffISI-PALSU", r["headers"]), 200)     # URL lama masih "berlaku"
        self.assertEqual(self.body(media.media_key), JPEG)
        self.assertEqual(services.verify_media(media, rehash=True), (True, "ok"))

    def test_finalize_is_idempotent(self):
        media = self.ready()
        before = self.log_count()
        again = services.finalize_upload(media.id, self.fam_a, self.ayah)
        self.assertEqual((again.log_seq, self.log_count()), (media.log_seq, before))

    def test_content_not_matching_declared_type_is_rejected(self):
        mid = self.upload(b"<html><script>alert(1)</script></html>", "image/jpeg", "x.jpg")
        before = self.log_count()
        with self.assertRaises(services.InvalidInput):
            services.finalize_upload(mid, self.fam_a, self.ayah)
        media = MediaObject.objects.get(pk=mid)
        self.assertEqual(media.status, MediaObject.Status.FAILED)
        self.assertEqual(self.log_count(), before)                        # tidak ada yang masuk ledger
        self.assertFalse(self.exists(media.media_key) or self.exists(media.upload_key))

    def test_size_different_from_declared_is_rejected(self):
        r = services.request_upload(self.fam_a, self.ayah, self.coll, "a.jpg", "image/jpeg", len(JPEG) + 10)
        _put(r["url"], JPEG, r["headers"])
        with self.assertRaises(services.InvalidInput):
            services.finalize_upload(r["media_id"], self.fam_a, self.ayah)
        self.assertEqual(MediaObject.objects.get(pk=r["media_id"]).status, MediaObject.Status.FAILED)

    def test_finalize_before_upload_can_be_retried(self):
        r = services.request_upload(self.fam_a, self.ayah, self.coll, "a.jpg", "image/jpeg", len(JPEG))
        with self.assertRaises(services.NotUploaded):
            services.finalize_upload(r["media_id"], self.fam_a, self.ayah)
        self.assertEqual(MediaObject.objects.get(pk=r["media_id"]).status, MediaObject.Status.PENDING)
        _put(r["url"], JPEG, r["headers"])
        self.assertEqual(services.finalize_upload(r["media_id"], self.fam_a, self.ayah).status,
                         MediaObject.Status.READY)

    def test_request_validation(self):
        bad = [("image/svg+xml", 10), ("text/html", 10), ("application/pdf", 10), ("image/jpeg", 0),
               ("image/jpeg", -1), ("image/jpeg", True), ("image/jpeg", 26 * MiB), ("video/mp4", 3 * 1024 * MiB)]
        for ctype, size in bad:
            with self.assertRaises(services.InvalidInput, msg=(ctype, size)):
                services.request_upload(self.fam_a, self.ayah, self.coll, "a", ctype, size)
        self.assertEqual(MediaObject.objects.count(), 0)

    # ---- otorisasi
    def test_authorization(self):
        with self.assertRaises(services.Forbidden):                       # member bukan anggota association
            services.request_upload(self.fam_a, self.mem_b, self.coll, "a.jpg", "image/jpeg", 10)
        with self.assertRaises(services.Forbidden):                       # anggota sah, tapi bukan pemilik collection
            services.request_upload(self.fam_b, self.mem_b, self.coll, "a.jpg", "image/jpeg", 10)
        r = services.request_upload(self.fam_a, self.ayah, self.coll, "a.jpg", "image/jpeg", len(JPEG))
        _put(r["url"], JPEG, r["headers"])
        with self.assertRaises(services.Forbidden):                       # hanya peminta yang boleh menyelesaikan
            services.finalize_upload(r["media_id"], self.fam_a, self.kakak)
        with self.assertRaises(services.NotFound):
            services.finalize_upload(uuid.uuid4(), self.fam_a, self.ayah)
        with self.assertRaises(services.InvalidInput):
            services.finalize_upload("bukan-uuid", self.fam_a, self.ayah)

    def test_access_follows_the_current_owner_after_inheritance(self):
        media = self.ready(name="kenangan.jpg")
        dl = services.get_download_url(media.id, self.fam_a, self.kakak)   # kakak: anggota association yang sama
        with urllib.request.urlopen(dl["url"]) as r:
            self.assertEqual(r.read(), JPEG)
            self.assertEqual(r.headers["Content-Type"], "image/jpeg")
            self.assertIn('inline; filename="kenangan.jpg"', r.headers["Content-Disposition"])
        self.assertEqual(dl["sha256"], hashlib.sha256(JPEG).hexdigest())

        _, token = ledger.send(self.coll, self.fam_a, self.ayah)           # diwariskan ke association lain
        ledger.claim_transfer(token, self.fam_b, self.mem_b)
        with urllib.request.urlopen(services.get_download_url(media.id, self.fam_b, self.mem_b)["url"]) as r:
            self.assertEqual(r.read(), JPEG)                               # penerima baru melihat media lama
        self.assertEqual([m.id for m in services.list_media(self.coll, self.fam_b, self.mem_b)], [media.id])
        with self.assertRaises(services.Forbidden):                        # pemilik lama tak lagi punya akses
            services.get_download_url(media.id, self.fam_a, self.ayah)
        with self.assertRaises(services.Forbidden):
            services.request_upload(self.fam_a, self.ayah, self.coll, "a.jpg", "image/jpeg", 10)

    def test_gallery_order_follows_ledger_order(self):
        ids = [self.ready(data=JPEG + bytes([i])).id for i in range(3)]
        listed = list(services.list_media(self.coll, self.fam_a, self.ayah))
        self.assertEqual([m.id for m in listed], ids)
        self.assertEqual([m.log_seq for m in listed], sorted(m.log_seq for m in listed))

    # ---- hapus
    def test_erase_removes_bytes_but_ledger_proof_remains(self):
        media = self.ready()
        with self.assertRaises(services.Forbidden):
            services.erase_media(media.id, self.fam_b, self.mem_b)
        erased = services.erase_media(media.id, self.fam_a, self.kakak)
        self.assertEqual((erased.status, erased.erased_by_id), (MediaObject.Status.ERASED, self.kakak))
        self.assertFalse(self.exists(media.media_key))
        self.assertEqual(ledger.verify_chain(self.coll), (True, None, "ok"))
        self.assertTrue(ledger.verify_content(self.coll, media.log_seq, content=JPEG))     # hash tetap terbukti
        self.assertEqual(services.verify_media(erased), (True, "dihapus; bukti di ledger utuh"))
        with self.assertRaises(services.InvalidState):
            services.get_download_url(media.id, self.fam_a, self.ayah)
        self.assertEqual(services.erase_media(media.id, self.fam_a, self.ayah).status, MediaObject.Status.ERASED)

    # ---- audit: pemalsuan terdeteksi
    def test_verify_media_detects_storage_and_table_tampering(self):
        media = self.ready()
        self.assertEqual(services.verify_media(media, rehash=True), (True, "ok"))

        self.s3.put_object(Bucket=BUCKET, Key=media.media_key, Body=JPEG[:-1] + b"X")   # file diganti di S3
        self.assertEqual(services.verify_media(media), (True, "ok"))                     # tanpa rehash tak terlihat
        ok, why = services.verify_media(media, rehash=True)
        self.assertFalse(ok)
        self.assertIn("isi file", why)

        MediaObject.objects.filter(pk=media.pk).update(sha256=hashlib.sha256(b"lain").digest())
        media.refresh_from_db()
        self.assertIn("berbeda dari ledger", services.verify_media(media)[1])

        MediaObject.objects.filter(pk=media.pk).update(sha256=hashlib.sha256(JPEG).digest(), uploaded_by_id=self.kakak)
        media.refresh_from_db()
        self.assertIn("association/member", services.verify_media(media)[1])

    def test_audit_command(self):
        media = self.ready()
        out = io.StringIO()
        call_command("audit_media", "--rehash", stdout=out)
        self.assertIn("diperiksa=1 bermasalah=0", out.getvalue())
        self.assertIsNotNone(MediaObject.objects.get(pk=media.pk).last_verified_at)

        self.s3.put_object(Bucket=BUCKET, Key=media.media_key, Body=b"diganti")
        with self.assertRaises(CommandError):
            call_command("audit_media", "--rehash", stdout=io.StringIO(), stderr=io.StringIO())

    # ---- kegagalan & perlombaan
    def test_collection_in_transit_at_finalize_fails_cleanly(self):
        mid = self.upload()
        ledger.send(self.coll, self.fam_a, self.ayah)
        before = self.log_count()
        with self.assertRaises(services.InvalidState):
            services.finalize_upload(mid, self.fam_a, self.ayah)
        media = MediaObject.objects.get(pk=mid)
        self.assertEqual(media.status, MediaObject.Status.FAILED)
        self.assertEqual(self.log_count(), before)
        self.assertFalse(self.exists(media.media_key) or self.exists(media.upload_key))

    def test_busy_and_stale_processing(self):
        mid = self.upload()
        MediaObject.objects.filter(pk=mid).update(status=MediaObject.Status.PROCESSING, processing_since=timezone.now())
        with self.assertRaises(services.InvalidState):                     # worker lain masih bekerja
            services.finalize_upload(mid, self.fam_a, self.ayah)
        MediaObject.objects.filter(pk=mid).update(processing_since=timezone.now() - timedelta(hours=1))
        self.assertEqual(services.finalize_upload(mid, self.fam_a, self.ayah).status, MediaObject.Status.READY)

    def test_fail_never_overwrites_a_media_finished_by_another_worker(self):
        """Worker lambat yang kalah balapan tidak boleh menandai FAILED / menghapus file media yang sudah READY."""
        media = self.ready()
        stale_copy = MediaObject.objects.get(pk=media.pk)
        self.assertFalse(services._fail(stale_copy, S3Storage(), "kalah balapan"))
        self.assertEqual(MediaObject.objects.get(pk=media.pk).status, MediaObject.Status.READY)
        self.assertEqual(self.body(media.media_key), JPEG)

    def test_expire_pending_and_cleanup_command(self):
        fresh = services.request_upload(self.fam_a, self.ayah, self.coll, "a.jpg", "image/jpeg", len(JPEG))["media_id"]
        old = services.request_upload(self.fam_a, self.ayah, self.coll, "b.jpg", "image/jpeg", len(JPEG))
        _put(old["url"], JPEG, old["headers"])
        MediaObject.objects.filter(pk=old["media_id"]).update(created_at=timezone.now() - timedelta(days=2))
        out = io.StringIO()
        call_command("cleanup_media", stdout=out)
        self.assertIn("dibersihkan=1", out.getvalue())
        self.assertEqual(MediaObject.objects.get(pk=old["media_id"]).status, MediaObject.Status.FAILED)
        self.assertFalse(self.exists(f"uploads/{old['media_id']}"))
        self.assertEqual(MediaObject.objects.get(pk=fresh).status, MediaObject.Status.PENDING)

    # ---- multipart (video besar)
    def test_multipart_upload(self):
        size = 5 * MiB + 1000
        data = JPEG + b"\x00" * (size - len(JPEG))
        with override_settings(LEDGER_MEDIA=self.conf(SINGLE_PUT_MAX=5 * MiB, PART_SIZE=5 * MiB)):
            r = services.request_upload(self.fam_a, self.ayah, self.coll, "film.jpg", "image/jpeg", size)
            self.assertEqual((r["mode"], r["part_count"]), ("multipart", 2))
            for p in r["parts"]:                                           # bentuk presigned URL (lihat catatan moto)
                self.assertIn(f"partNumber={p['part_number']}", p["url"])
                self.assertIn("uploadId=", p["url"])
                self.assertIn("X-Amz-Signature=", p["url"])
            media = MediaObject.objects.get(pk=r["media_id"])
            with self.assertRaises(services.NotUploaded):                  # finalize tanpa daftar part
                services.finalize_upload(media.id, self.fam_a, self.ayah)
            with self.assertRaises(services.InvalidParts):                 # belum ada part / ETag keliru
                services.finalize_upload(media.id, self.fam_a, self.ayah, parts=[{"PartNumber": 1, "ETag": '"x"'}])
            self.assertEqual(MediaObject.objects.get(pk=media.pk).status, MediaObject.Status.PENDING)  # bisa diulang
            parts = []
            for n, chunk in ((1, data[:5 * MiB]), (2, data[5 * MiB:])):
                etag = self.s3.upload_part(Bucket=BUCKET, Key=media.upload_key, UploadId=media.multipart_upload_id,
                                           PartNumber=n, Body=chunk)["ETag"]
                parts.append({"PartNumber": n, "ETag": etag})
            with self.assertRaises(services.InvalidParts):                 # ETag salah SESUDAH part terunggah
                services.finalize_upload(media.id, self.fam_a, self.ayah,
                                         parts=[{"PartNumber": 1, "ETag": '"salah"'}, parts[1]])
            self.assertEqual(MediaObject.objects.get(pk=media.pk).status, MediaObject.Status.PENDING)
            self.assertEqual(len(self.s3.list_parts(Bucket=BUCKET, Key=media.upload_key,
                                                    UploadId=media.multipart_upload_id)["Parts"]), 2)  # part utuh
            for bad in ([], "x", [{"PartNumber": "1", "ETag": "e"}], [{"ETag": "e"}]):
                with self.assertRaises(services.InvalidInput):
                    services.finalize_upload(media.id, self.fam_a, self.ayah, parts=bad)
            done = services.finalize_upload(media.id, self.fam_a, self.ayah, parts=parts)
            self.assertEqual(bytes(done.sha256), hashlib.sha256(data).digest())
            self.assertEqual(self.body(done.media_key), data)
            self.assertEqual(ledger.verify_chain(self.coll), (True, None, "ok"))

            r2 = services.request_upload(self.fam_a, self.ayah, self.coll, "film2.jpg", "image/jpeg", size)
            refreshed = services.presign_parts(r2["media_id"], self.fam_a, self.ayah, [2])
            self.assertEqual([p["part_number"] for p in refreshed], [2])
            with self.assertRaises(services.InvalidInput):
                services.presign_parts(r2["media_id"], self.fam_a, self.ayah, [3])
            with self.assertRaises(services.InvalidState):                 # bukan multipart
                services.presign_parts(self.upload(), self.fam_a, self.ayah, [1])

    # ---- constraint database
    def test_database_constraints_protect_the_proof_link(self):
        media = self.ready()
        with self.assertRaises(IntegrityError), transaction.atomic():      # 'ready' tanpa hash/bukti ditolak
            MediaObject.objects.filter(pk=media.pk).update(sha256=None)
        other = MediaObject.objects.create(
            collection_id=self.coll, association_id=self.fam_a, uploaded_by_id=self.ayah, content_type="image/jpeg",
            size=1, upload_key="uploads/zz", media_key="media/zz")
        with self.assertRaises(IntegrityError), transaction.atomic():      # satu log = satu media
            MediaObject.objects.filter(pk=other.pk).update(log_seq=media.log_seq)


class MediaUnitTests(SimpleTestCase):
    def test_sniff_known_formats(self):
        cases = {
            b"\xff\xd8\xff\xe0abc": "image/jpeg",
            b"\x89PNG\r\n\x1a\n....": "image/png",
            b"GIF89a....": "image/gif",
            b"RIFF\x00\x00\x00\x00WEBPVP8 ": "image/webp",
            b"\x1a\x45\xdf\xa3....": "video/webm",
            b"\x00\x00\x00\x18ftypisom": "video/mp4",
            b"\x00\x00\x00\x14ftypqt  ": "video/quicktime",
            b"\x00\x00\x00\x18ftypheic": "image/heic",
        }
        for head, expected in cases.items():
            self.assertEqual(sniff(head), expected, head)
        for head in (b"<html><body>", b"", b"\x00\x00\x00\x18ftypavif", b"%PDF-1.7", b"<svg xmlns="):
            self.assertIsNone(sniff(head), head)

    def test_declared_type_must_match_content(self):
        self.assertTrue(matches("image/jpeg", "image/jpeg"))
        self.assertTrue(matches("image/heif", "image/heic"))
        self.assertFalse(matches("image/jpeg", "image/png"))
        self.assertFalse(matches("image/jpeg", None))

    def test_clean_filename(self):
        f = services.clean_filename
        self.assertEqual(f("../../etc/passwd"), "passwd")
        self.assertEqual(f('a"b<c>.jpg'), "abc.jpg")
        self.assertEqual(f("foto\r\nX-Evil: 1.jpg"), "fotoX-Evil 1.jpg")
        self.assertEqual(f("C:\\Users\\ibu\\foto.jpg"), "foto.jpg")
        self.assertEqual(f(""), "file")
        self.assertEqual(f(None), "file")
        self.assertEqual(f("..."), "file")
        self.assertEqual(len(f("x" * 500)), 150)

    @override_settings(LEDGER_MEDIA={})
    def test_bucket_is_required(self):
        with self.assertRaises(ImproperlyConfigured):
            conf.get("BUCKET")
