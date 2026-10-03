import uuid
from django.db import models
from django.contrib.contenttypes.fields import GenericForeignKey, GenericRelation
from django.contrib.contenttypes.models import ContentType
from snailstamp.core.models import TimeMixin


# ==========================================
# 1. MODEL CERTIFICATE (Pusat Kunci Generic)
# ==========================================
class Certificate(TimeMixin):
    """
    Menyimpan pasangan kunci (Keypair) untuk entitas fisik apapun.
    Bisa terhubung ke Pen, Collection (Surat), Album, atau entitas masa depan.
    """
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)

    # ========================================================
    # 1. PIHAK PEMILIK (Bisa Individu/Creator, bisa Lembaga/Institution)
    # ========================================================
    issuer_content_type = models.ForeignKey(
        ContentType, on_delete=models.CASCADE, related_name='issuer_certificates'
    )
    issuer_object_id = models.CharField(max_length=50, db_index=True)
    issuer = GenericForeignKey('issuer_content_type', 'issuer_object_id')

    # ========================================================
    # 2. BARANG FISIK (Bisa Pen, Collection/Surat, Album, dll)
    # ========================================================
    collection_content_type = models.ForeignKey(
        ContentType, on_delete=models.CASCADE, related_name='collection_certificates'
    )
    collection_object_id = models.CharField(max_length=50, db_index=True)
    collection = GenericForeignKey('collection_content_type', 'collection_object_id')

    # ========================================================
    # 3. DATA KRIPTOGRAFI
    # ========================================================
    public_key = models.TextField()
    encrypted_private_key = models.TextField()
    encryption_iv = models.TextField()
    key_version = models.IntegerField(default=1)

    is_active = models.BooleanField(default=True)

    class Meta:
        unique_together = ('collection_content_type', 'collection_object_id')
        indexes = [
            models.Index(fields=['issuer_content_type', 'issuer_object_id']),
            models.Index(fields=['collection_content_type', 'collection_object_id']),
        ]

    def __str__(self):
        return f"Certificate for {self.collection_content_type.name} [{self.collection_object_id}]"


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


class Collection(TimeMixin):
    """
    Tabel ini menyimpan SEMUA barang fisik.
    """
    # Relasi ke jenis barang
    collection_type = models.ForeignKey(CollectionType, on_delete=models.PROTECT)
    
    # Pembuat/Pemilik awal
    issuer = models.ForeignKey("tenant.User", on_delete=models.CASCADE)
    
    # Nama identifikasi umum
    name = models.CharField(max_length=255) # Cth: "Surat ke Budi", "Pena Lamy Safari"
    slug = models.SlugField(max_length=255, unique=True, blank=True)
    
    # KUNCI FLEKSIBILITAS: Simpan atribut spesifik di sini!
    properties = models.JSONField(default=dict, blank=True)
    
    # Status pelacakan global
    status = models.CharField(max_length=50, default='CREATED')

    def __str__(self):
        return f"{self.name} ({self.collection_type.name})"
