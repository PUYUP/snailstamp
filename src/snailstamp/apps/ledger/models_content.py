"""
Model Content untuk menampung keterangan dari user.

Bukan bagian ledger (managed=True) - ini hanya untuk lookup cepat dan
tracking content dari user. Integritas content diverifikasi lewat ledger
(content_hash di Log ACTED_ON).

Strategi (One-to-One):
- 1 Collection = 1 Content (mis: deskripsi dari user)
- Content.collection_id = UNIQUE constraint (one-to-one)
- Tidak ada versioning (content di-replace, bukan versioned)
- Deduplication otomatis: content dengan hash sama tidak duplikat di storage
- State snapshot embed content info untuk fast read
"""
import uuid
from django.db import models


class Content(models.Model):
    """Index content dari user. One-to-one dengan Collection."""

    class ContentStatus(models.TextChoices):
        ACTIVE = 'active', 'Active'
        DELETED = 'deleted', 'Deleted'

    class TextFormat(models.TextChoices):
        PLAIN = 'plain', 'Plain Text'
        MARKDOWN = 'markdown', 'Markdown'
        HTML = 'html', 'HTML'
        JSON = 'json', 'JSON'

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    collection = models.OneToOneField(
        'ledger.Collection',
        on_delete=models.CASCADE,
        related_name='content',
    )

    # --- Content Info ---
    title = models.CharField(max_length=255, blank=True, help_text="Judul atau label singkat dari konten")
    format = models.CharField(max_length=20, choices=TextFormat.choices, default=TextFormat.PLAIN)
    body = models.TextField(help_text="Isi konten teks utama")
    excerpt = models.CharField(max_length=255, blank=True, help_text="Preview singkat untuk list view")
    
    # --- Integrity & Size ---
    content_hash = models.CharField(max_length=64, help_text="SHA256 hex dari kolom body")
    char_count = models.PositiveIntegerField(default=0, help_text="Jumlah karakter untuk fast query/lookup")

    # --- Status ---
    status = models.CharField(
        max_length=20,
        choices=ContentStatus.choices,
        default=ContentStatus.ACTIVE
    )

    # --- Metadata & Timestamps ---
    metadata = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    created_by_member = models.ForeignKey(
        'tenant.Member',
        on_delete=models.SET_NULL,
        null=True,
        related_name='created_contents'
    )

    class Meta:
        db_table = 'ledger_contents'
        ordering = ['-created_at']
        indexes = [
            models.Index(fields=['content_hash'], name='ledger_cont_conten_2b2c3e_idx'),
            models.Index(fields=['status'], name='ledger_cont_status_3c2c3e_idx'),
        ]

    def __str__(self):
        # Fallback jika title kosong, gunakan awalan ID dan hash
        if self.title:
            return self.title
        return f"Content-{str(self.id)[:8]} ({self.content_hash[:8]})"

    def save(self, *args, **kwargs):
        # Auto-calculate char_count sebelum save jika dibutuhkan
        if self.body:
            self.char_count = len(self.body)
            # Auto-generate excerpt jika kosong
            if not self.excerpt:
                self.excerpt = self.body[:250] + '...' if len(self.body) > 250 else self.body
        super().save(*args, **kwargs)
