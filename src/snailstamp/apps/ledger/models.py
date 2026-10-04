"""
Model READ-ONLY untuk tabel ledger (managed = False).

Skema dibuat oleh sql/0001_schema.sql. Semua TULIS lewat ledger/services.py,
yang memanggil fungsi PostgreSQL (atomik, 1 round-trip). Model di sini hanya
untuk membaca/mengquery; save()/delete()/update() sengaja dilarang.

Butuh Django >= 5.2 (CompositePrimaryKey).

Identitas: association -> member -> user. Ledger hanya mengenal association (pemilik) dan
member (yang bertindak); user tidak pernah masuk ledger. Semua FK ke tenant.* tanpa
constraint fisik (db_constraint=False).
"""
import uuid

from django.db import models
from django.utils import timezone


class LedgerWriteForbidden(RuntimeError):
    pass


class LedgerQuerySet(models.QuerySet):
    def _forbid(self, *args, **kwargs):
        raise LedgerWriteForbidden("Tabel ledger hanya boleh ditulis lewat ledger.services")

    create = bulk_create = bulk_update = update = delete = _forbid
    get_or_create = update_or_create = _forbid


class LedgerModel(models.Model):
    objects = LedgerQuerySet.as_manager()

    class Meta:
        abstract = True
        managed = False

    def save(self, *args, **kwargs):
        raise LedgerWriteForbidden("Gunakan ledger.services")

    def delete(self, *args, **kwargs):
        raise LedgerWriteForbidden("Gunakan ledger.services")


def _association_fk(**kw):
    """Association = pemilik / pihak yang sah secara hukum. Tidak 'meninggal'."""
    # db_constraint=False: tanpa FK fisik (lihat catatan desain no.5 di schema)
    return models.ForeignKey("tenant.Association", on_delete=models.DO_NOTHING,
                             db_constraint=False, related_name="+", **kw)


def _member_fk(**kw):
    """Member = orang yang menekan tombolnya, atas nama association-nya (masuk ke hash)."""
    return models.ForeignKey("tenant.Member", on_delete=models.DO_NOTHING,
                             db_constraint=False, related_name="+", **kw)


class Kind(LedgerModel):
    """Registry jenis item (ledger_kinds): pena, jurnal, surat, perangko, rol film, ..."""
    id = models.SmallIntegerField(primary_key=True)
    code = models.CharField(unique=True)
    label = models.CharField()
    max_as_tool = models.IntegerField(null=True)      # NULL = tak terbatas (perangko = 1)
    max_as_target = models.IntegerField(null=True)    # NULL = tak terbatas (rol film = 36)
    restricted = models.BooleanField()                # True: hanya penerbit resmi boleh membuat entry jenis ini

    class Meta(LedgerModel.Meta):
        db_table = "ledger_kinds"


class KindIssuer(LedgerModel):
    """Penerbit resmi untuk jenis `restricted` (mis. kantor pos untuk cap pos)."""
    pk = models.CompositePrimaryKey("kind_id", "association_id")
    kind = models.ForeignKey(Kind, on_delete=models.DO_NOTHING, db_constraint=False, related_name="+")
    association = _association_fk()

    class Meta(LedgerModel.Meta):
        db_table = "ledger_kind_issuers"


class Action(LedgerModel):
    """Registry kata kerja (ledger_actions): menulis, menempelkan, mengecap pos, ..."""
    id = models.SmallIntegerField(primary_key=True)
    code = models.CharField(unique=True)
    label = models.CharField()

    class Meta(LedgerModel.Meta):
        db_table = "ledger_actions"


class ActionRule(LedgerModel):
    """Registry aturan (ledger_action_rules): aksi apa boleh dilakukan alat apa pada sasaran apa."""

    class Access(models.IntegerChoices):
        ISSUER = 1, "pemilik sasaran"
        IN_TRANSIT = 2, "siapa pun, saat sasaran sedang dikirim (cap pos)"
        ANYONE = 3, "siapa pun, sasaran aktif (buku tamu)"

    pk = models.CompositePrimaryKey("action_id", "tool_kind_id", "target_kind_id")
    action = models.ForeignKey(Action, on_delete=models.DO_NOTHING, db_constraint=False, related_name="+")
    tool_kind = models.ForeignKey(Kind, on_delete=models.DO_NOTHING, db_constraint=False,
                                  db_column="tool_kind", related_name="+")
    target_kind = models.ForeignKey(Kind, on_delete=models.DO_NOTHING, db_constraint=False,
                                    db_column="target_kind", related_name="+")
    target_access = models.SmallIntegerField(choices=Access.choices)

    class Meta(LedgerModel.Meta):
        db_table = "ledger_action_rules"


class Entry(LedgerModel):
    """Alasan / landasan. Contoh: 'saya mencetak 1000 stiker untuk dijual'."""
    id = models.BigAutoField(primary_key=True)
    issuer = _association_fk()
    issuer_member = _member_fk()
    reason = models.TextField()
    supply = models.IntegerField()
    kind = models.ForeignKey(Kind, on_delete=models.DO_NOTHING, db_constraint=False,
                             db_column="kind", related_name="+")
    minted_count = models.IntegerField()
    metadata = models.JSONField()
    content_hash = models.BinaryField()
    created_at = models.DateTimeField()

    class Meta(LedgerModel.Meta):
        db_table = "ledger_entries"


class Collection(LedgerModel):
    """Satu item (stiker #n, pena, surat, ...). Tidak pernah dibuat ulang / dihapus, hanya berpindah.

    Siapa (association + member) yang melahirkannya = log seq 1 (event MINT), terlindung hash:
    lihat services.item_creator(). Entry.issuer / issuer_member = pembuat entry-nya.

    owner   = Association pemilik SAH (punya 'BPKB'). Hanya berubah lewat transfer antar-asosiasi.
    holder  = Member yang SEDANG MEMEGANG / MEMAKAI item ini di dalam asosiasi tersebut.
              Boleh NULL (belum ditetapkan). Gunakan services.assign() untuk mengubahnya.
              Analogi: 1 keluarga punya 4 motor -> tiap motor dipegang 1 anggota keluarga.
    """

    class State(models.IntegerChoices):
        ACTIVE = 1, "aktif"
        IN_TRANSIT = 2, "dalam pengiriman"

    id = models.BigAutoField(primary_key=True)
    entry = models.ForeignKey(Entry, on_delete=models.DO_NOTHING, related_name="collections")
    serial_no = models.CharField(max_length=100)
    owner = _association_fk()              # association pemilik SAAT INI; berubah tiap transfer
    holder = _member_fk(null=True)         # member pemegang SAAT INI; NULL = belum ditugaskan
    state = models.SmallIntegerField(choices=State.choices)
    kind = models.ForeignKey(Kind, on_delete=models.DO_NOTHING, db_constraint=False,
                             db_column="kind", related_name="+")      # salinan beku entries.kind
    last_seq = models.IntegerField()
    tool_uses = models.IntegerField()      # berapa kali dipakai sebagai ALAT (batas: kinds.max_as_tool)
    target_acts = models.IntegerField()    # berapa kali dikenai aksi sebagai SASARAN (batas: max_as_target)
    last_hash = models.BinaryField()
    created_at = models.DateTimeField()
    updated_at = models.DateTimeField()

    class Meta(LedgerModel.Meta):
        db_table = "ledger_collections"


class Log(LedgerModel):
    """Riwayat append-only. PK = (collection_id, seq). Tabel terbesar.

    Setiap log menyimpan state_snapshot = JSON snapshot Collection saat itu,
    memungkinkan rekonstruksi penuh state Collection dari history log.
    """

    class Event(models.IntegerChoices):
        MINT        = 1, "lahir"
        SEND        = 2, "dikirim"
        RECEIVE     = 3, "diterima"
        USE         = 4, "dipakai (aksi tunggal, atau sisi ALAT dari aksi antar-item)"
        CANCEL_SEND = 5, "pengiriman dibatalkan pengirim"
        ASSIGN      = 6, "pemegang diganti (di dalam asosiasi yang sama)"  # holder berubah; payload: prev/new
        ACTED_ON    = 7, "dikenai aksi (sisi SASARAN dari aksi antar-item)"

    pk = models.CompositePrimaryKey("collection_id", "seq")
    collection = models.ForeignKey(Collection, on_delete=models.DO_NOTHING,
                                   db_constraint=False, related_name="logs")
    seq = models.IntegerField()
    event_type = models.SmallIntegerField(choices=Event.choices)
    action = models.ForeignKey(Action, on_delete=models.DO_NOTHING, db_constraint=False,
                               null=True, related_name="+")        # kata kerja (USE / ACTED_ON)
    actor = _association_fk()
    actor_member = _member_fk()
    counterparty = _association_fk(null=True)
    target_id = models.BigIntegerField(null=True)      # USE/ACTED_ON: collection pasangan (alat <-> sasaran)
    target_seq = models.IntegerField(null=True)        # seq log pasangan di collection tersebut
    created_at = models.DateTimeField()
    hash = models.BinaryField()
    content_hash = models.BinaryField(null=True)       # ACTED_ON: sha256 isi (isi asli di luar ledger)
    payload = models.JSONField(null=True)
    state_snapshot = models.JSONField(null=True)        # Snapshot Collection state saat log dibuat

    class Meta(LedgerModel.Meta):
        db_table = "ledger_logs"


class Holding(LedgerModel):
    """Read-model: association mana memegang apa. Dipelihara oleh fungsi ledger_*.
    Member penerima tidak disimpan di sini; ia ada di log RECEIVE."""
    pk = models.CompositePrimaryKey("owner_id", "collection_id")
    owner = _association_fk()
    collection = models.ForeignKey(Collection, on_delete=models.DO_NOTHING,
                                   db_constraint=False, related_name="+")
    entry = models.ForeignKey(Entry, on_delete=models.DO_NOTHING,
                              db_constraint=False, related_name="+")
    acquired_at = models.DateTimeField()

    class Meta(LedgerModel.Meta):
        db_table = "ledger_holdings"


class TransferToken(LedgerModel):
    """Token rahasia untuk QR transfer. Satu token aktif per collection yang dikirim.

    Alur: pengirim memanggil send() → dapat token UUID → buat QR dari token.
    Penerima scan QR → memanggil claim_transfer(token) → kepemilikan pindah.
    Token lama (sudah diklaim) tetap tersimpan sebagai audit trail.
    """
    token = models.UUIDField(primary_key=True)
    collection = models.ForeignKey(Collection, on_delete=models.DO_NOTHING,
                                   db_constraint=False, related_name="+")
    from_association = _association_fk()
    from_member = _member_fk()
    created_at = models.DateTimeField()
    expires_at = models.DateTimeField(null=True)       # opsional: token kedaluwarsa
    claimed_by = _association_fk(null=True)            # NULL sampai diklaim
    claimed_by_member = _member_fk(null=True)          # NULL sampai diklaim
    claimed_at = models.DateTimeField(null=True)       # NULL sampai diklaim

    class Meta(LedgerModel.Meta):
        db_table = "ledger_transfer_tokens"


class Block(LedgerModel):
    """Checkpoint global: Merkle root semua log dalam satu jendela waktu."""
    block_no = models.BigIntegerField(primary_key=True)
    window_start = models.DateTimeField()
    window_end = models.DateTimeField()
    log_count = models.BigIntegerField()
    merkle_root = models.BinaryField()
    prev_block_hash = models.BinaryField()
    block_hash = models.BinaryField()
    sealed_at = models.DateTimeField()
    signatures = models.JSONField(null=True, default=list)  # List of sealer signatures for multi-sig

    class Meta(LedgerModel.Meta):
        db_table = "ledger_blocks"


class SealerKey(models.Model):
    """Tracking sealer keys dengan expiration untuk multi-signature dan rotation.

    Bukan bagian ledger (managed=True) - ini hanya metadata untuk manajemen sealer.
    """
    class KeyStatus(models.TextChoices):
        ACTIVE = 'active', 'Active'
        EXPIRED = 'expired', 'Expired'
        REVOKED = 'revoked', 'Revoked'

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    name = models.CharField(max_length=100)  # Nama sealer (mis: "sealer-1", "sealer-2")
    public_key = models.CharField(max_length=64)  # Public key hex (32 bytes = 64 hex chars)
    private_key_encrypted = models.TextField(null=True)  # Private key encrypted (opsional, disimpan terpisah)
    status = models.CharField(max_length=20, choices=KeyStatus.choices, default=KeyStatus.ACTIVE)
    valid_from = models.DateTimeField(auto_now_add=True)
    valid_until = models.DateTimeField(null=True)  # Expiration date, NULL = tidak pernah expired
    threshold = models.IntegerField(default=1)  # Threshold untuk multi-sig (berapa banyak signature dibutuhkan)
    metadata = models.JSONField(default=dict, blank=True)
    region = models.CharField(max_length=64, blank=True, default="", db_index=True)  # mis: "ap-southeast-1"
    deactivated_at = models.DateTimeField(null=True, blank=True)  # saat di-rotate / revoke / expire
    previous_key = models.ForeignKey('self', on_delete=models.PROTECT, null=True, blank=True,
                                     related_name='successors')  # key lama sebelum rotasi

    class Meta:
        db_table = 'ledger_sealer_keys'
        ordering = ['-valid_from']
        indexes = [
            models.Index(fields=['status'], name='ledger_seale_status_idx'),
            models.Index(fields=['valid_until'], name='ledger_seale_valid_idx'),
        ]

    def __str__(self):
        return f"{self.name} ({self.status})"

    @property
    def is_active(self):
        """Cek apakah key masih aktif dan belum expired."""
        if self.status != self.KeyStatus.ACTIVE:
            return False
        if self.valid_until and self.valid_until < timezone.now():
            return False
        return True

    def was_valid_at(self, when):
        """Apakah signature yang dibuat pada `when` masih sah.

        Key yang di-rotate / expired tetap sah untuk blok yang ditandatangani SEBELUM key itu
        dinonaktifkan (supaya rotasi tidak membatalkan riwayat). Key REVOKED (bocor) tidak
        pernah sah lagi, termasuk untuk blok lama.
        """
        if self.status == self.KeyStatus.REVOKED:
            return False
        if self.valid_until and when > self.valid_until:
            return False
        return not (self.deactivated_at and when > self.deactivated_at)


class AppendOnlyQuerySet(models.QuerySet):
    def _forbid(self, *args, **kwargs):
        raise LedgerWriteForbidden("Tabel ini append-only")

    update = delete = bulk_update = update_or_create = _forbid


class AppendOnlyModel(models.Model):
    """Boleh INSERT, tidak boleh UPDATE / DELETE lewat ORM."""
    objects = AppendOnlyQuerySet.as_manager()

    class Meta:
        abstract = True

    def save(self, *args, **kwargs):
        if not self._state.adding:
            raise LedgerWriteForbidden(f"{type(self).__name__} append-only")
        super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise LedgerWriteForbidden(f"{type(self).__name__} append-only")


class SealerKeyAuditLog(AppendOnlyModel):
    """Audit log untuk sealer key operations (register, rotate, expire, revoke, usage, split, recover).

    Bukan bagian ledger (managed=True) - ini hanya untuk audit trail dan compliance.
    Append-only: ditulis lewat ledger.services, tidak pernah diubah / dihapus lewat ORM.
    """
    class EventType(models.TextChoices):
        KEY_REGISTERED = 'key_registered', 'Key Registered'
        KEY_ROTATED = 'key_rotated', 'Key Rotated'
        KEY_EXPIRED = 'key_expired', 'Key Expired'
        KEY_REVOKED = 'key_revoked', 'Key Revoked'
        KEY_USED = 'key_used', 'Key Used'
        KEY_SPLIT = 'key_split', 'Key Split (Shamir)'
        KEY_RECOVERED = 'key_recovered', 'Key Recovered (Shamir)'

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    event_type = models.CharField(max_length=50, choices=EventType.choices)
    timestamp = models.DateTimeField(auto_now_add=True)

    # Actor info (siapa yang melakukan operasi)
    actor_user_id = models.CharField(max_length=100, null=True, blank=True)
    actor_email = models.CharField(max_length=255, null=True, blank=True)
    actor_ip = models.GenericIPAddressField(null=True, blank=True)
    actor_user_agent = models.TextField(null=True, blank=True)

    # Sealer info
    sealer_id = models.UUIDField(null=True, blank=True)
    sealer_name = models.CharField(max_length=100, null=True, blank=True)
    region = models.CharField(max_length=64, blank=True, default="")

    # Key info
    old_key_id = models.UUIDField(null=True, blank=True)
    new_key_id = models.UUIDField(null=True, blank=True)
    old_public_key = models.CharField(max_length=64, null=True, blank=True)
    new_public_key = models.CharField(max_length=64, null=True, blank=True)

    # Event details
    rotation_reason = models.CharField(max_length=100, null=True, blank=True)
    previous_valid_until = models.DateTimeField(null=True, blank=True)
    new_valid_until = models.DateTimeField(null=True, blank=True)
    block_at_rotation = models.BigIntegerField(null=True, blank=True)

    # Metadata
    metadata = models.JSONField(default=dict, blank=True)

    class Meta:
        db_table = 'ledger_sealer_audit_logs'
        ordering = ['-timestamp']
        indexes = [
            models.Index(fields=['event_type'], name='ledger_audit_event_idx'),
            models.Index(fields=['sealer_id'], name='ledger_audit_sealer_idx'),
            models.Index(fields=['timestamp'], name='ledger_audit_time_idx'),
        ]

    def __str__(self):
        return f"{self.event_type} - {self.sealer_name or 'Unknown'} at {self.timestamp}"


class BlockSignature(AppendOnlyModel):
    """Co-signature sebuah blok dari sealer lain (mis. region lain) SETELAH blok disegel.

    ledger_blocks append-only, jadi signature tambahan disimpan di sini. Signature menandatangani
    block_hash yang sama; verify_block_signatures() menggabungkan ini dengan Block.signatures.
    """
    id = models.BigAutoField(primary_key=True)
    block = models.ForeignKey(Block, on_delete=models.DO_NOTHING, db_constraint=False,
                              db_column='block_no', related_name='cosignatures')
    sealer = models.ForeignKey(SealerKey, on_delete=models.PROTECT, related_name='cosignatures')
    region = models.CharField(max_length=64, blank=True, default="")
    public_key = models.CharField(max_length=64)
    signature = models.CharField(max_length=128)
    signed_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = 'ledger_block_signatures'
        ordering = ['block_id', 'signed_at']
        constraints = [
            models.UniqueConstraint(fields=['block', 'sealer'], name='ledger_blocksig_unique'),
        ]

    def __str__(self):
        return f"block #{self.block_id} by {self.sealer_id} ({self.region or '-'})"


from .assets import Asset  # noqa: E402,F401  (daftarkan model Asset ke app ledger)
