# Block Sealer Cryptographic Signing (Multi-Signature & Key Rotation)

## Overview

Setiap block yang disegel oleh sealer sekarang ditandatangai secara kriptografis menggunakan Ed25519 untuk memastikan integritas dan authenticity. Mendukung:
- **Multi-signature**: Beberapa sealer bisa menandatangai satu block
- **Threshold signing**: Butuh minimum X signature untuk block valid
- **Key expiration**: Key bisa memiliki tanggal kadaluarsa otomatis
- **Key rotation**: Key bisa di-rotate tanpa downtime

Ini mencegah attacker yang memiliki akses ke database untuk membuat block palsu.

## Architecture

### Ed25519 Signature Scheme

- **Algorithm**: Ed25519 (EdDSA with Curve25519)
- **Key Size**: 32 bytes private key, 32 bytes public key, 64 bytes signature
- **Properties**: Fast, secure, deterministic signatures
- **Library**: PyNaCl (Python binding for libsodium)

### Block Structure

```python
class Block:
    block_no            # Block number
    window_start        # Window start time
    window_end          # Window end time
    log_count           # Number of logs in block
    merkle_root         # Merkle root of all logs
    prev_block_hash     # Hash of previous block
    block_hash          # Hash of this block
    sealed_at           # Timestamp when sealed
    signatures          # JSON array of sealer signatures for multi-sig
```

### Signatures Format

```json
[
  {
    "sealer_id": "uuid-of-sealer-1",
    "public_key": "public_key_hex_1",
    "signature": "signature_hex_1"
  },
  {
    "sealer_id": "uuid-of-sealer-2",
    "public_key": "public_key_hex_2",
    "signature": "signature_hex_2"
  }
]
```

### SealerKey Model

```python
class SealerKey:
    id                  # UUID
    name                # Nama sealer (mis: "sealer-1", "sealer-2")
    public_key          # Public key hex (64 chars)
    private_key_encrypted  # Private key encrypted (opsional)
    status              # ACTIVE / EXPIRED / REVOKED
    valid_from          # Key valid dari tanggal ini
    valid_until         # Key valid sampai tanggal ini (NULL = tidak pernah expired)
    threshold           # Minimum signature yang dibutuhkan (default 1)
    metadata            # Additional metadata
```

### Signature Process

1. **Generate Block Hash**: `block_hash = hash(prev_block_hash + merkle_root + window_start + window_end + log_count)`
2. **Multi-Sign**: Setiap sealer sign dengan private key mereka
3. **Store**: Save semua signatures di JSON field

### Verification Process

1. **Compute Block Hash**: Recalculate `block_hash` from block data
2. **Verify Signatures**: Verifikasi setiap signature dengan public key sealer
3. **Check Threshold**: Pastikan jumlah signature valid >= threshold
4. **Check Key Status**: Pastikan sealer masih aktif dan belum expired
5. **Chain Verification**: Verify block hash chain and merkle root

## Usage

### 1. Register Sealer Keys

```python
from snailstamp.apps.ledger.services import register_sealer, generate_sealer_keypair
from datetime import datetime, timedelta, timezone

# Generate key pair untuk sealer 1
private_key_1, public_key_1 = generate_sealer_keypair()

# Register sealer 1 dengan expiration 90 hari
sealer_1 = register_sealer(
    name="sealer-1",
    public_key_hex=public_key_1,
    valid_until=timezone.now() + timedelta(days=90),
    threshold=1
)

# Generate key pair untuk sealer 2
private_key_2, public_key_2 = generate_sealer_keypair()

# Register sealer 2 tanpa expiration (selamanya aktif)
sealer_2 = register_sealer(
    name="sealer-2",
    public_key_hex=public_key_2,
    valid_until=None,
    threshold=1
)

# Simpan private keys di environment variable (HARUS AMAN!)
# export BLOCK_SEALER_1_PRIVATE_KEY="private_key_1_hex"
# export BLOCK_SEALER_2_PRIVATE_KEY="private_key_2_hex"
```

**Important**: Private keys HARUS disimpan di secure environment (env var, secret manager, HSM). Jangan commit ke git!

### 2. Seal Block with Multi-Signature

```python
from snailstamp.apps.ledger.services import seal_next_block
from django.conf import settings

# Ambil private keys dari environment
sealer_1_key = settings.BLOCK_SEALER_1_PRIVATE_KEY
sealer_2_key = settings.BLOCK_SEALER_2_PRIVATE_KEY

# Seal block dengan multi-signature (2 sealer)
block = seal_next_block(
    window=timedelta(seconds=10),
    safety_lag=timedelta(seconds=2),
    sealer_private_keys=[
        {"sealer_id": sealer_1.id, "private_key": sealer_1_key},
        {"sealer_id": sealer_2.id, "private_key": sealer_2_key}
    ]
)

if block:
    print(f"Block {block.block_no} sealed with {len(block.signatures)} signatures")
    for sig in block.signatures:
        print(f"  Sealer: {sig['sealer_id']}")
else:
    print("Window not ready yet")
```

### 3. Seal Block with Single Signature (Legacy)

```python
# Legacy mode: single sealer (backward compatible)
block = seal_next_block(
    window=timedelta(seconds=10),
    safety_lag=timedelta(seconds=2),
    sealer_private_key=sealer_1_key  # Single hex string
)
```

### 4. Verify Blocks with Threshold

```python
from snailstamp.apps.ledger.services import verify_blocks

# Verifikasi semua block dengan threshold 2 (butuh 2 signature valid)
ok, broken_at = verify_blocks(verify_signature=True, threshold=2)

if ok:
    print("All blocks valid with threshold 2!")
else:
    print(f"Block verification failed at block {broken_at}")

# Verifikasi dengan threshold 1 (butuh minimal 1 signature)
ok, broken_at = verify_blocks(verify_signature=True, threshold=1)

# Verifikasi tanpa signature (backward compatibility)
ok, broken_at = verify_blocks(verify_signature=False)
```

### 5. Verify Single Block Signatures

```python
from snailstamp.apps.ledger.services import verify_block_signatures
from snailstamp.apps.ledger.models import Block

block = Block.objects.get(block_no=100)

# Verifikasi dengan threshold 2
valid, count = verify_block_signatures(block, threshold=2)

if valid:
    print(f"Block valid with {count} signatures (threshold 2)")
else:
    print(f"Block invalid: only {count} signatures valid")
```

### 6. Rotate Sealer Key

```python
from snailstamp.apps.ledger.services import rotate_sealer_key, generate_sealer_keypair
from datetime import datetime, timedelta, timezone

# Generate key pair baru
new_private, new_public = generate_sealer_keypair()

# Rotate key untuk sealer-1
new_sealer = rotate_sealer_key(
    sealer_id=sealer_1.id,
    new_public_key_hex=new_public,
    valid_until=timezone.now() + timedelta(days=90)
)

# Update environment variable dengan private key baru
# export BLOCK_SEALER_1_PRIVATE_KEY="new_private_key_hex"

print(f"Old key expired, new key: {new_sealer.id}")
```

### 7. Expire Old Keys

```python
from snailstamp.apps.ledger.services import expire_old_keys

# Mark semua key yang sudah expired sebagai EXPIRED
count = expire_old_keys()
print(f"Expired {count} keys")
```

### 8. Get Active Sealers

```python
from snailstamp.apps.ledger.services import get_active_sealers

# Ambil semua sealer yang aktif
active_sealers = get_active_sealers()

for sealer in active_sealers:
    print(f"{sealer.name}: {sealer.public_key} (valid until {sealer.valid_until})")
```

## Configuration

### Environment Variables

Tambahkan ke `.env`:

```bash
# Sealer 1 private key (hex string, 64 chars untuk 32 bytes)
BLOCK_SEALER_1_PRIVATE_KEY=your_private_key_hex_1

# Sealer 2 private key (hex string, 64 chars untuk 32 bytes)
BLOCK_SEALER_2_PRIVATE_KEY=your_private_key_hex_2

# Threshold default untuk multi-sig (berapa banyak signature dibutuhkan)
BLOCK_SIGNATURE_THRESHOLD=2
```

### Settings Configuration

Tambahkan ke `settings.py`:

```python
# Load from environment
BLOCK_SEALER_1_PRIVATE_KEY = env('BLOCK_SEALER_1_PRIVATE_KEY', default=None)
BLOCK_SEALER_2_PRIVATE_KEY = env('BLOCK_SEALER_2_PRIVATE_KEY', default=None)
BLOCK_SIGNATURE_THRESHOLD = env('BLOCK_SIGNATURE_THRESHOLD', default=1, cast=int)
```

## Security Considerations

### 1. Private Key Protection

- Private keys HARUS disimpan di secure environment (env var, secret manager, HSM)
- Jangan commit private key ke git
- Jangan log private key
- Gunakan key rotation otomatis untuk security

### 2. Key Rotation

#### Manual Rotation

Jika private key compromised:

```python
# 1. Generate key pair baru
new_private, new_public = generate_sealer_keypair()

# 2. Rotate key di database
new_sealer = rotate_sealer_key(
    sealer_id=sealer_1.id,
    new_public_key_hex=new_public,
    valid_until=timezone.now() + timedelta(days=90)
)

# 3. Update environment variable
# export BLOCK_SEALER_1_PRIVATE_KEY="new_key..."

# 4. Restart sealer service
# Block selanjutnya akan menggunakan key baru
```

#### Automatic Rotation (Scheduled Job)

Setup Celery task untuk automatic rotation:

```python
from celery import shared_task
from datetime import timedelta, timezone

@shared_task
def auto_rotate_sealer_keys():
    """Rotate key yang akan expired dalam 7 hari."""
    seven_days = timezone.now() + timedelta(days=7)

    # Cari key yang akan expired
    keys_to_rotate = SealerKey.objects.filter(
        status=SealerKey.KeyStatus.ACTIVE,
        valid_until__isnull=False,
        valid_until__lte=seven_days
    )

    for key in keys_to_rotate:
        # Generate key baru
        new_private, new_public = generate_sealer_keypair()

        # Rotate key
        new_key = rotate_sealer_key(
            sealer_id=key.id,
            new_public_key_hex=new_public,
            valid_until=timezone.now() + timedelta(days=90)
        )

        # Update environment variable (via API atau manual)
        print(f"Rotated {key.name} -> {new_key.id}")

# Schedule di celery beat
# CELERYBEAT_SCHEDULE = {
#     'auto-rotate-sealer-keys': {
#         'task': 'path.to.auto_rotate_sealer_keys',
#         'schedule': crontab(hour=0, minute=0),  # Setiap hari
#     },
# }
```

### 3. Multi-Signer Threshold

Setup threshold untuk multi-signature:

```python
# Butuh 2 dari 3 sealer untuk sign block
sealer_1 = register_sealer(name="sealer-1", public_key_hex=pk1, threshold=2)
sealer_2 = register_sealer(name="sealer-2", public_key_hex=pk2, threshold=2)
sealer_3 = register_sealer(name="sealer-3", public_key_hex=pk3, threshold=2)

# Seal dengan 2 sealer (minimal untuk threshold 2)
block = seal_next_block(
    sealer_private_keys=[
        {"sealer_id": sealer_1.id, "private_key": private_1},
        {"sealer_id": sealer_2.id, "private_key": private_2}
    ]
)

# Verifikasi dengan threshold 2
ok, broken_at = verify_blocks(threshold=2)
```

### 4. Key Expiration

Keys dengan expiration date otomatis di-mark sebagai EXPIRED:

```python
# Register key dengan expiration 30 hari
sealer = register_sealer(
    name="temp-sealer",
    public_key_hex=public_key,
    valid_until=timezone.now() + timedelta(days=30)
)

# Scheduled job untuk expire old keys
@shared_task
def expire_old_keys_job():
    count = expire_old_keys()
    print(f"Expired {count} keys")
```

### 5. Signature Absence

Block tanpa signature (old blocks atau sealer tanpa key) masih valid tapi tidak terverifikasi secara kriptografis. Gunakan `verify_signature=False` untuk compatibility.

## Migration

### For Existing Blocks

Block yang sudah ada sebelum fitur ini akan memiliki `signatures` = NULL atau kosong. Ini normal dan tidak menurunkan validitas block.

### For New Blocks

Block baru akan disertai multi-signature jika `sealer_private_keys` disediakan ke `seal_next_block()`.

### Migration Steps

1. Run migration untuk update schema:
```bash
python manage.py migrate
```

2. Register sealer keys:
```python
from snailstamp.apps.ledger.services import register_sealer, generate_sealer_keypair

private, public = generate_sealer_keypair()
sealer = register_sealer(name="sealer-1", public_key_hex=public)
```

3. Update environment variables dengan private keys

4. Update sealer service untuk menggunakan multi-signature

## Troubleshooting

### Block Verification Failed

Jika `verify_blocks()` return False:

1. Cek apakah block data integrity valid (merkle root, hash chain)
2. Cek apakah cukup signature valid untuk threshold
3. Cek apakah sealer masih aktif dan belum expired
4. Gunakan `verify_signature=False` untuk debug

### Threshold Not Met

Jika signature valid tapi threshold tidak terpenuhi:

1. Cek berapa banyak signature yang dibutuhkan (threshold)
2. Cek berapa banyak signature valid (gunakan `verify_block_signatures()`)
3. Tambahkan signature dari sealer lain jika perlu

### Invalid Signature

Jika signature invalid:

1. Pastikan public key yang digunakan untuk verification benar
2. Pastikan block tidak dimodifikasi setelah disegel
3. Cek apakah private key sealer masih valid (tidak compromised)
4. Cek apakah sealer sudah di-rotate dan key lama sudah expired

### Sealer Expired

Jika sealer expired:

1. Cek `valid_until` di SealerKey
2. Gunakan `expire_old_keys()` untuk mark expired keys
3. Rotate key dengan `rotate_sealer_key()`
4. Update environment variable dengan private key baru

### Private Key Not Found

Jika `seal_next_block()` tidak men-generate signature:

1. Pastikan environment variable untuk private keys di-set
2. Pastikan private key format benar (hex string, 64 chars)
3. Pastikan PyNaCl terinstall (`pip install pynacl`)
4. Cek apakah sealer_id valid dan aktif

## Audit Log Key Rotation

Semua operasi key dicatat di `SealerKeyAuditLog` (tabel `ledger_sealer_audit_logs`, append-only:
`save()` pada baris lama, `delete()`, `QuerySet.update()/delete()` melempar `LedgerWriteForbidden`).

| Event | Ditulis oleh | Isi penting |
|---|---|---|
| `key_registered` | `register_sealer()` | `new_key_id`, `new_public_key`, `region` |
| `key_rotated` | `rotate_sealer_key()` | `old_key_id` -> `new_key_id`, public key lama/baru, `rotation_reason`, `block_at_rotation` |
| `key_revoked` | `revoke_sealer_key()` | `rotation_reason`, `block_at_rotation` |
| `key_expired` | `expire_old_keys()` | `previous_valid_until` |
| `key_used` | `seal_next_block()` / `cosign_block()` | `block_at_rotation` = block_no, `metadata.mode` = `seal` / `cosign` |
| `key_split` / `key_recovered` | Shamir (lihat bawah) | jumlah share, threshold, sidik jari share (bukan share-nya) |

Semua fungsi menerima `actor_info={"user_id", "email", "ip", "user_agent"}`.

```python
from snailstamp.apps.ledger.services import rotate_sealer_key, revoke_sealer_key, sealer_audit_trail

new_key = rotate_sealer_key(sealer.id, new_public, reason="compromised",
                            actor_info={"user_id": str(request.user.pk), "ip": request.META["REMOTE_ADDR"]})
revoke_sealer_key(other.id, reason="leaked")
for log in sealer_audit_trail(sealer_name="sealer-1"):
    print(log.timestamp, log.event_type, log.rotation_reason, log.block_at_rotation)
```

**Rotasi tidak membatalkan riwayat.** Key yang di-rotate/expired tetap sah untuk blok yang
ditandatangani sebelum `deactivated_at` / `valid_until` (`SealerKey.was_valid_at()`). Key
`REVOKED` (bocor) tidak pernah dihitung lagi, termasuk untuk blok lama. Signature hanya dihitung
jika public key-nya cocok dengan `SealerKey` terdaftar, dan satu public key hanya dihitung sekali.

## Multi-Region Sealer

Satu proses `seal_ledger` (pemegang advisory lock) menyegel blok; sealer di region lain
menambahkan **co-signature** atas `block_hash` yang sama ke `ledger_block_signatures`
(`ledger_blocks` append-only). Co-signer memverifikasi ulang Merkle root dari log sebelum
menandatangani.

```python
sgp = register_sealer("sealer-sgp", pub_sgp, region="ap-southeast-1")
jkt = register_sealer("sealer-jkt", pub_jkt, region="ap-southeast-3")

# region JKT menyegel
seal_next_block(sealer_private_keys=[{"sealer_id": jkt.id, "private_key": priv_jkt}])
# region SGP co-sign semua blok yang belum ia tandatangani
for block in pending_cosign_blocks(sgp.id):
    cosign_block(block.block_no, sgp.id, priv_sgp)

verify_blocks(threshold=2, min_regions=2)        # butuh 2 signature dari 2 region berbeda
```

Sebagai service:

```bash
# region utama
python manage.py seal_ledger --sealer <JKT_ID>:BLOCK_SEALER_JKT_PRIVATE_KEY
# tiap region lain
python manage.py cosign_ledger --sealer-id <SGP_ID> --key-env BLOCK_SEALER_SGP_PRIVATE_KEY
```

Settings opsional: `LEDGER_SEALER_REGION` (default region untuk `register_sealer`),
`BLOCK_SIGNATURE_THRESHOLD`, `LEDGER_SEALER_MIN_REGIONS` (default untuk `verify_blocks` /
`verify_block_signatures`). Blok lama tanpa signature sama sekali tetap lolos verifikasi.

## Shamir's Secret Sharing

Private key sealer bisa dipecah menjadi N share (butuh K untuk rekonstruksi) dan dibagikan ke
custodian berbeda; kurang dari K share tidak membocorkan apa pun. Implementasi GF(256) ada di
`ledger/shamir.py`. Format share: `ss1-<set_id>-<k>-<x>-<hex>-<checksum>`.

```python
shares = split_sealer_private_key(sealer.id, private_hex, shares=5, threshold=3)
private_hex = recover_sealer_private_key(sealer.id, [shares[0], shares[2], shares[4]])
```

Hasil rekonstruksi diverifikasi terhadap public key sealer (share salah/tercampur ditolak). Audit
log hanya menyimpan sidik jari share.

```bash
python manage.py sealer_shamir split --sealer-id <ID> --key-env BLOCK_SEALER_1_PRIVATE_KEY \
    --shares 5 --threshold 3 --out-dir /secure/shares      # satu file per share, mode 0600
python manage.py sealer_shamir check --sealer-id <ID> --share-file a.txt --share-file b.txt --share-file c.txt
# sealer/co-signer bisa langsung memakai share (key direkonstruksi di memori, dicatat key_recovered)
python manage.py cosign_ledger --sealer-id <ID> --share-file a.txt --share-file b.txt --share-file c.txt
```

## Signature Wajib & Anchoring (Fork Detection)

### Signature wajib

`LEDGER_REQUIRE_BLOCK_SIGNATURES` (default `True`):

* `seal_next_block()` menolak menyegel tanpa key, atau dengan key yang bukan milik `SealerKey` aktif
  terdaftar (`InvalidInput`). Key legacy tanpa `sealer_id` dicocokkan lewat public key.
* `verify_blocks()` menganggap blok tanpa signature valid sebagai rusak. Superuser DB yang menulis
  ulang chain sekarang juga butuh private key sealer (yang bisa dipecah dengan Shamir).

### Anchoring checkpoint ke luar DB

Superuser DB + pencuri key masih bisa membangun chain baru yang konsisten. Pertahanannya: secara
berkala terbitkan **checkpoint** blok terbaru ke penyimpanan di luar database
(`ledger/anchors.py`). Checkpoint = `block_no`, `block_hash`, `prev_block_hash`, `window_end_us`, dan
semua signature sealer atas blok itu. Karena `block_hash` mengikat seluruh blok sebelumnya, satu
checkpoint mengunci seluruh riwayat sampai titik itu.

```python
# settings
LEDGER_ANCHOR_INTERVAL = 100                     # anchor tiap 100 blok (~17 menit @10 dtk)
LEDGER_ANCHOR_BACKENDS = [
    {"NAME": "worm", "BACKEND": "snailstamp.apps.ledger.anchors.FileAnchorBackend",
     "OPTIONS": {"path": "/mnt/worm/snailstamp-anchors.jsonl"}},
    {"NAME": "notary", "BACKEND": "snailstamp.apps.ledger.anchors.HttpAnchorBackend",
     "OPTIONS": {"url": "https://notary.example.com/anchors", "token_env": "LEDGER_ANCHOR_TOKEN"}},
]
```

Atau lewat env (`core/settings/base.py`): `LEDGER_ANCHOR_FILE`, `LEDGER_ANCHOR_URL` (+ `LEDGER_ANCHOR_TOKEN`),
`LEDGER_ANCHOR_INTERVAL`, `LEDGER_REQUIRE_BLOCK_SIGNATURES`.

* `FileAnchorBackend` — satu baris JSON per checkpoint (append + fsync). Taruh di volume yang TIDAK
  bisa ditulis oleh operator DB: WORM / S3 Object Lock yang di-mount / replika off-site.
* `HttpAnchorBackend` — `POST url` (body = checkpoint JSON, respons = receipt) dan `GET url`
  (JSON array semua checkpoint). Cocok untuk layanan notaris / region lain / pihak ketiga.
* Backend lain (OpenTimestamps, transparency log, ...): turunkan `AnchorBackend`, implementasikan
  `publish()` dan `checkpoints()`.

`anchor_blocks()` memverifikasi ulang chain (termasuk signature) sejak anchor terakhir sebelum
menerbitkan, jadi ledger yang sudah rusak tidak pernah di-anchor. Tabel `ledger_block_anchors`
(`BlockAnchor`, append-only) hanya indeks lokal; deteksi selalu membaca dari backend eksternal.

### Deteksi fork

```python
from snailstamp.apps.ledger.services import anchor_blocks, detect_forks

anchor_blocks()                     # biasanya lewat command anchor_ledger
for f in detect_forks():            # [] = DB cocok dengan semua checkpoint
    print(f.kind, f.block_no, f.backend, f.detail)
```

| `kind` | Arti |
|---|---|
| `missing_block` | Blok yang pernah di-anchor tidak ada lagi di DB (rollback / truncate) |
| `hash_mismatch` | `block_hash` di DB berbeda dari checkpoint (riwayat ditulis ulang) |
| `equivocation` | Key sealer yang sama menandatangani dua versi blok yang sama -> key bocor / sealer curang |
| `anchor_conflict` | Dua backend menyimpan checkpoint berbeda untuk blok yang sama |
| `invalid_checkpoint` | Checkpoint tanpa signature valid dari sealer terdaftar (backend dirusak) |
| `chain_invalid` | `verify_blocks()` gagal (Merkle / tautan / signature) |
| `backend_error` | Backend tidak bisa dibaca / isinya rusak |

```bash
python manage.py anchor_ledger              # service: anchor tiap --sleep detik (default 60)
python manage.py anchor_ledger --once --force
python manage.py check_forks                # exit code != 0 bila ada temuan -> pasang di cron/monitoring
```

## Performance Impact

- **Signing**: ~0.1ms per signature (Ed25519 sangat cepat)
- **Verification**: ~0.1ms per signature
- **Storage**: +64 bytes per signature per block
- **Multi-sig overhead**: Linear dengan jumlah sealer (2 sealer = 2x verification time)
- **Key management**: Minimal overhead untuk SealerKey table
- **Negligible** untuk sistem dengan throughput normal dan jumlah sealer terbatas (2-5 sealer)

## Best Practices

1. **Generate key pair sekali per sealer**, jangan generate per block
2. **Backup private key** di secure location (multiple backups)
3. **Setup automatic key rotation** (scheduled job) untuk security
4. **Gunakan threshold yang sesuai** (2-of-3 untuk fault tolerance, 3-of-5 untuk high security)
5. **Test signature verification** di staging sebelum production
6. **Monitor signature verification** di logs untuk detect anomalies
7. **Document key rotation procedure** untuk emergency
8. **Use key expiration** untuk limit exposure jika key compromised
9. **Monitor sealer status** untuk detect expired/revoked keys
10. **Keep at least 1 active sealer** untuk avoid downtime

## Future Enhancements

- [x] Multi-signature (threshold signing untuk multi-sealer)
- [x] Key expiration dan automatic rotation
- [ ] Signature verification cache untuk high-throughput
- [ ] Hardware Security Module (HSM) integration
- [ ] Key whitelisting untuk trusted sealers
- [ ] Webhook notification saat key expired
- [x] Audit log untuk key rotation events
- [x] Multi-region sealer setup
- [x] Shamir's Secret Sharing untuk private key distribution
- [x] Signature wajib untuk setiap blok
- [x] Anchoring checkpoint eksternal + fork detection
- [ ] Backend anchor OpenTimestamps / transparency log
