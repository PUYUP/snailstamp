import uuid

from django.db import models
from snailstamp.core.models import TimeMixin


class Entry(TimeMixin):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    association = models.ForeignKey("tenant.Association", on_delete=models.PROTECT, related_name="entries")
    assigner = models.ForeignKey("tenant.User", on_delete=models.SET_NULL, null=True, blank=True, related_name="entries")

    title = models.CharField(max_length=255)
    description = models.TextField(blank=True)

    # Optional context
    occurred_at = models.DateTimeField(null=True, blank=True)
    location = models.CharField(max_length=255, blank=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["association", "-created_at"]),
            models.Index(fields=["occurred_at"]),
        ]


class CollectionType(TimeMixin):
    """
    Tabel ini menyimpan JENIS barang. 
    Admin bisa menambah jenis baru kapan saja via Django Admin tanpa coding!
    Contoh isi: "Mail", "Pen", "Postcard", "Package"
    """
    name = models.CharField(max_length=255, unique=True)
    slug = models.SlugField(max_length=255, unique=True, blank=True)
    description = models.TextField(blank=True)

    def __str__(self):
        return self.name


class Entry(TimeMixin):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    association = models.ForeignKey(
        "tenant.Association",
        on_delete=models.PROTECT,
        related_name="entries"
    )
    member = models.ForeignKey(
        "tenant.Member",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="entries",
    )

    name = models.CharField(max_length=255)
    description = models.TextField(blank=True)

    # Optional context
    occurred_at = models.DateTimeField(null=True, blank=True)
    location = models.CharField(max_length=255, blank=True)

    class Meta:
        indexes = [
            models.Index(fields=["association", "-created_at"]),
            models.Index(fields=["occurred_at"]),
        ]

    def __str__(self):
        return f"{self.name} ({self.association.name})"


class Collection(TimeMixin):
    """
    Tabel ini menyimpan SEMUA barang fisik.
    """
    # Relasi ke jenis barang
    collection_type = models.ForeignKey(CollectionType, on_delete=models.PROTECT)

    # PEMILIK (Workspace/Organization)
    # Pena ini secara hukum/sistem milik siapa? (Misal: Lamy Germany)
    # Tidak boleh dihapus
    association = models.ForeignKey(
        'tenant.Association',
        on_delete=models.PROTECT,
        related_name='collections'
    )

    # Pembuat/Pemilik awal
    assigner = models.ForeignKey(
        "tenant.User", 
        on_delete=models.SET_NULL, 
        null=True, 
        blank=True,
        related_name='assigned_collections',
        help_text="Manusia (User) yang membuat atau mendaftarkan aset ini"
    )

    # Nama identifikasi umum
    name = models.CharField(max_length=255) # Cth: "Surat ke Budi", "Pena Lamy Safari"
    slug = models.SlugField(max_length=255, unique=True, blank=True)

    # KUNCI FLEKSIBILITAS: Simpan atribut spesifik di sini!
    properties = models.JSONField(default=dict, blank=True)
    
    # Status pelacakan global
    status = models.CharField(max_length=50, default='CREATED')

    # Timestamp saat aset pertama kali di-signature (Immutable)
    signed_at = models.DateTimeField(null=True, blank=True)

    def __str__(self):
        return f"{self.name} ({self.collection_type.name})"


class CollectionSignature(TimeMixin):
    collection = models.ForeignKey(Collection, on_delete=models.CASCADE, related_name="signatures")
    certificate = models.ForeignKey("tenant.Certificate", on_delete=models.PROTECT, related_name="signatures")
    algorithm = models.CharField(max_length=30, default="ed25519")
    payload_version = models.PositiveIntegerField(default=1)
    payload_hash = models.CharField(max_length=64)   # sha256 hex
    signature = models.TextField()                    # base64
    signed_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        indexes = [models.Index(fields=["collection", "-signed_at"])]

    def __str__(self):
        return f"Signature for {self.collection.name} on {self.signed_at}"