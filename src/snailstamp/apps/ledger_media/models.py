import uuid

from django.db import models
from django.db.models import Q


def _association_fk(**kw):
    # Sama seperti ledger: tanpa FK fisik ke tenant (db_constraint=False). PROTECT mencegah
    # Member/Association yang punya media terhapus lewat ORM (UUID-nya juga ada di ledger).
    return models.ForeignKey("tenant.Association", on_delete=models.PROTECT, db_constraint=False,
                             related_name="+", **kw)


def _member_fk(**kw):
    return models.ForeignKey("tenant.Member", on_delete=models.PROTECT, db_constraint=False,
                             related_name="+", **kw)


class MediaObject(models.Model):
    """Metadata satu file (foto/video) yang dilampirkan ke sebuah collection.

    BUKAN sumber kebenaran: baris ini boleh berubah (status, dihapus). Buktinya ada di ledger:
    log USE (collection_id, log_seq) memuat sha256 file + siapa (association, member) dan kapan.
    Byte file ada di S3; ledger hanya menyimpan sidik jarinya.
    """

    class Status(models.TextChoices):
        PENDING = "pending", "menunggu unggahan"
        PROCESSING = "processing", "sedang diverifikasi"
        READY = "ready", "siap (tercatat di ledger)"
        FAILED = "failed", "gagal / kedaluwarsa"
        ERASED = "erased", "dihapus (hash di ledger tetap)"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    collection_id = models.BigIntegerField()                    # ledger_collections.id (tanpa FK fisik)
    log_seq = models.IntegerField(null=True)                    # seq log USE yang mencatatnya; NULL sampai READY
    association = _association_fk()                             # association pemilik saat diunggah
    uploaded_by = _member_fk()                                  # member yang mengunggah (= actor di log)

    filename = models.CharField(max_length=255, blank=True)     # sudah disanitasi; TIDAK masuk ledger
    content_type = models.CharField(max_length=100)             # tervalidasi terhadap isi file
    size = models.BigIntegerField()                             # byte; diverifikasi == ukuran aktual
    sha256 = models.BinaryField(max_length=32, null=True)       # = ledger_logs.content_hash

    upload_key = models.CharField(max_length=255, unique=True)  # tempat klien menulis (presigned)
    media_key = models.CharField(max_length=255, unique=True)   # tempat final; hanya server yang menulis
    multipart_upload_id = models.CharField(max_length=255, blank=True)

    status = models.CharField(max_length=12, choices=Status.choices, default=Status.PENDING)
    failure_reason = models.CharField(max_length=200, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    processing_since = models.DateTimeField(null=True)
    finalized_at = models.DateTimeField(null=True)
    last_verified_at = models.DateTimeField(null=True)
    erased_at = models.DateTimeField(null=True)
    erased_by = _member_fk(null=True)

    class Meta:
        indexes = [
            models.Index(fields=["collection_id", "status"], name="media_coll_status_idx"),
            models.Index(fields=["status", "created_at"], name="media_status_created_idx"),
        ]
        constraints = [
            # satu log ledger mencatat paling banyak satu media
            models.UniqueConstraint(fields=["collection_id", "log_seq"], name="media_one_per_log",
                                    condition=Q(log_seq__isnull=False)),
            # media yang 'ready' WAJIB punya hash dan bukti di ledger
            models.CheckConstraint(name="media_ready_has_proof",
                                   condition=~Q(status="ready") | (Q(sha256__isnull=False) & Q(log_seq__isnull=False))),
        ]

    def __str__(self):
        return f"{self.id} [{self.status}] {self.content_type} {self.size}B"
