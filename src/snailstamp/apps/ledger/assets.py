"""
Model Asset untuk index file storage.

Bukan bagian ledger (managed=True) - ini hanya untuk lookup cepat dan
tracking file yang diupload. Integritas file diverifikasi lewat ledger
(content_hash di Log ACTED_ON).

Strategi (One-to-One):
- 1 Collection = 1 Asset (file foto/video)
- Asset.collection_id = UNIQUE constraint (one-to-one)
- Tidak ada versioning (asset di-replace, bukan versioned)
- Deduplication otomatis: file dengan hash sama tidak duplikat di storage
- State snapshot embed asset info untuk fast read
"""
import uuid

from django.db import models
from django.core.files.storage import default_storage


class Asset(models.Model):
    """Index file storage. One-to-one dengan Collection."""

    class AssetStatus(models.TextChoices):
        ACTIVE = 'active', 'Active'
        DELETED = 'deleted', 'Deleted'

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    collection = models.OneToOneField(
        'ledger.Collection',
        on_delete=models.CASCADE,
        related_name='asset',  # One-to-one: singular
        unique=True
    )

    # File info
    storage_path = models.CharField(max_length=500)  # Path di storage (S3/local)
    content_hash = models.CharField(max_length=64)  # SHA256 hex
    original_filename = models.CharField(max_length=255)
    file_size = models.BigIntegerField()
    mime_type = models.CharField(max_length=100)

    # Status (no versioning)
    status = models.CharField(
        max_length=20,
        choices=AssetStatus.choices,
        default=AssetStatus.ACTIVE
    )

    # Metadata
    metadata = models.JSONField(default=dict, blank=True)

    # Timestamps
    uploaded_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    uploaded_by_member = models.ForeignKey(
        'tenant.Member',
        on_delete=models.SET_NULL,
        null=True,
        related_name='uploaded_assets'
    )

    class Meta:
        db_table = 'ledger_assets'
        ordering = ['-uploaded_at']
        indexes = [
            models.Index(fields=['content_hash'], name='ledger_asse_conten_2b2c3e_idx'),
            models.Index(fields=['status'], name='ledger_asse_status_3c2c3e_idx'),
        ]

    def __str__(self):
        return f"{self.original_filename}"

    @property
    def file_url(self):
        """Get URL to file from storage backend."""
        return default_storage.url(self.storage_path)

    def get_file(self):
        """Get file object from storage."""
        return default_storage.open(self.storage_path, 'rb')

    def delete_file(self):
        """Delete file from storage."""
        if default_storage.exists(self.storage_path):
            default_storage.delete(self.storage_path)
