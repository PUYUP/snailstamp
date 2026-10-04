# State Snapshot & Collection Rekonstruksi

## Overview

Fitur ini memungkinkan rekonstruksi penuh state Collection dari history log. Setiap log menyimpan JSON snapshot Collection state saat log dibuat, sehingga kita bisa melihat "sebelumnya ada apa saja" di setiap titik waktu.

## Konsep

### Blockchain Analogy

```
Block N   → Block N+1   → Block N+2
  ↓            ↓             ↓
State 1     State 2      State 3
  ↓            ↓             ↓
Log 1       Log 2        Log 3
```

Di SnailStamp:
- **Collection Model** = State terkini (mutable via services)
- **Log Model** = History lengkap dengan state snapshot (immutable, append-only)
- **state_snapshot** = JSON snapshot Collection state saat log dibuat

### Apa yang Disimpan di state_snapshot?

Setiap log menyimpan snapshot lengkap Collection state:

```json
{
  "id": 12345,
  "entry_id": 100,
  "serial_no": "STK-001",
  "owner_id": "uuid-association-1",
  "holder_id": "uuid-member-1",
  "state": 1,
  "kind": 1,
  "last_seq": 1,
  "tool_uses": 0,
  "target_acts": 0,
  "last_hash": "0a1b2c3d...",
  "created_at": "2024-01-01T10:00:00Z",
  "updated_at": "2024-01-01T10:00:00Z"
}
```

## Implementation

### 1. SQL Schema

Kolom `state_snapshot` ditambahkan ke tabel `ledger_logs`:

```sql
CREATE TABLE ledger_logs (
    collection_id   bigint      NOT NULL,
    actor_id        uuid        NOT NULL,
    actor_member_id uuid        NOT NULL,
    counterparty_id uuid,
    target_id       bigint,
    created_at      timestamptz NOT NULL,
    seq             integer     NOT NULL,
    target_seq      integer,
    event_type      smallint    NOT NULL,
    action_id       smallint,
    hash            bytea       NOT NULL,
    content_hash    bytea,
    payload         jsonb,
    state_snapshot  jsonb,      -- ← NEW COLUMN
    PRIMARY KEY (collection_id, seq)
) PARTITION BY HASH (collection_id);
```

### 2. PostgreSQL Function

Fungsi `ledger_collection_snapshot()` meng-generate JSON snapshot:

```sql
CREATE FUNCTION ledger_collection_snapshot(c ledger_collections) RETURNS jsonb
LANGUAGE sql STABLE AS $$
SELECT jsonb_build_object(
  'id', c.id,
  'entry_id', c.entry_id,
  'serial_no', c.serial_no,
  'owner_id', c.owner_id,
  'holder_id', c.holder_id,
  'state', c.state,
  'kind', c.kind,
  'last_seq', c.last_seq,
  'tool_uses', c.tool_uses,
  'target_acts', c.target_acts,
  'last_hash', encode(c.last_hash, 'hex'),
  'created_at', c.created_at,
  'updated_at', c.updated_at
) $$;
```

### 3. ledger_write_log Update

Fungsi `ledger_write_log()` di-update untuk menerima parameter `p_state_snapshot`:

```sql
CREATE FUNCTION ledger_write_log(
    c ledger_collections,
    p_event smallint,
    p_actor uuid,
    p_actor_member uuid,
    p_counterparty uuid,
    p_payload jsonb,
    p_ts timestamptz,
    p_target_id bigint DEFAULT NULL,
    p_target_seq int DEFAULT NULL,
    p_action_id smallint DEFAULT NULL,
    p_content_hash bytea DEFAULT NULL,
    p_state_snapshot jsonb DEFAULT NULL,  -- ← NEW PARAMETER
    OUT new_seq int,
    OUT new_hash bytea
)
```

Semua pemanggilan `ledger_write_log()` di-update untuk menyertakan `ledger_collection_snapshot(c)`.

### 4. Django Model

Field `state_snapshot` ditambahkan ke model `Log`:

```python
class Log(LedgerModel):
    # ... existing fields ...
    state_snapshot = models.JSONField(null=True)  # ← NEW FIELD
```

### 5. Python Services

Fungsi rekonstruksi ditambahkan ke `services.py`:

```python
def reconstruct_collection(collection_id, seq=None):
    """
    Rekonstruksi state Collection dari log history.

    Args:
        collection_id: ID collection
        seq: Seq log spesifik (None = state terkini)

    Returns:
        Dictionary yang merepresentasikan state Collection pada seq tersebut,
        atau None jika log tidak ditemukan.
    """
    if seq is None:
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
```

## Usage

### 1. Lihat State Collection di Seq Tertentu

```python
from snailstamp.apps.ledger.services import reconstruct_collection

# Lihat state saat log seq 3
state = reconstruct_collection(collection_id=12345, seq=3)
print(state)
# {
#   "id": 12345,
#   "owner_id": "uuid-association-1",
#   "holder_id": "uuid-member-1",
#   "state": 1,
#   ...
# }
```

### 2. Lihat State Terkini

```python
# Lihat state terkini (tanpa seq parameter)
state = reconstruct_collection(collection_id=12345)
print(state)
```

### 3. Lihat Seluruh History State

```python
from snailstamp.apps.ledger.services import reconstruct_collection_full

# Lihat seluruh history state
history = reconstruct_collection_full(collection_id=12345)
for seq, event_type, state in history:
    print(f"Seq {seq}: Event {event_type}")
    print(f"  Owner: {state['owner_id']}")
    print(f"  Holder: {state['holder_id']}")
    print(f"  State: {state['state']}")
```

### 4. Rekonstruksi Collection ke Database (Recovery)

Jika tabel `ledger_collections` rusak atau hilang, bisa direkonstruksi dari log:

```python
def recover_collection_from_logs(collection_id):
    """Rekonstruksi Collection dari log history."""
    from snailstamp.apps.ledger.models import Collection
    from snailstamp.apps.ledger.services import reconstruct_collection

    # Ambil state terkini dari log
    state = reconstruct_collection(collection_id)

    if state is None:
        raise Exception("Log tidak ditemukan")

    # Buat/update Collection dari state
    collection, created = Collection.objects.update_or_create(
        id=state['id'],
        defaults={
            'entry_id': state['entry_id'],
            'serial_no': state['serial_no'],
            'owner_id': state['owner_id'],
            'holder_id': state['holder_id'],
            'state': state['state'],
            'kind': state['kind'],
            'last_seq': state['last_seq'],
            'tool_uses': state['tool_uses'],
            'target_acts': state['target_acts'],
            'last_hash': bytes.fromhex(state['last_hash']),
            'created_at': state['created_at'],
            'updated_at': state['updated_at'],
        }
    )

    return collection
```

### 5. Audit Trail: Lihat Perubahan Owner

```python
def track_owner_changes(collection_id):
    """Lihat riwayat perubahan owner."""
    history = reconstruct_collection_full(collection_id)
    
    owner_changes = []
    prev_owner = None
    
    for seq, event_type, state in history:
        current_owner = state['owner_id']
        if prev_owner is not None and current_owner != prev_owner:
            owner_changes.append({
                'seq': seq,
                'event': event_type,
                'from': prev_owner,
                'to': current_owner,
                'timestamp': state['updated_at']
            })
        prev_owner = current_owner
    
    return owner_changes
```

### 6. Time Travel: Lihat State di Tanggal Tertentu

```python
def get_state_at_time(collection_id, target_time):
    """Lihat state Collection pada waktu tertentu."""
    from django.utils import timezone
    from snailstamp.apps.ledger.models import Log
    
    # Cari log terakhir sebelum target_time
    log = Log.objects.filter(
        collection_id=collection_id,
        created_at__lte=target_time
    ).order_by('-seq').first()
    
    if log and log.state_snapshot:
        return log.state_snapshot
    return None

# Contoh:
state = get_state_at_time(
    collection_id=12345,
    target_time=timezone.datetime(2024, 1, 15, 12, 0, 0, tzinfo=timezone.utc)
)
```

## Benefits

### 1. Audit Trail Lengkap
- Bisa melihat state Collection di setiap titik waktu
- Buka track perubahan owner, holder, state, dll
- Bukti lengkap untuk compliance/audit

### 2. Disaster Recovery
- Jika tabel `ledger_collections` rusak/hilang
- Bisa direkonstruksi 100% dari log history
- Tidak ada data yang hilang

### 3. Debugging
- Bisa melihat state sebelum dan sesudah error
- Buka track perubahan yang menyebabkan masalah
- Time travel debugging

### 4. Analytics
- Analisis perubahan state over time
- Statistik ownership transfer
- Pattern analysis (mis: holder change frequency)

### 5. True Blockchain Experience
- Append-only history dengan state snapshot
- Bisa rekonstruksi penuh dari genesis
- Bukti integritas via hash chain

## Performance Considerations

### Storage
- Setiap log menyimpan ~500-1000 byte JSON snapshot
- Untuk 1 juta log = ~500-1000 MB tambahan
- Trade-off: storage vs. query speed

### Query Speed
- Tidak perlu replay seluruh log untuk mendapatkan state
- Cukup 1 query untuk mendapatkan state di seq tertentu
- Significantly faster daripada replay dari genesis

### Indexing
- Tidak perlu index khusus untuk state_snapshot
- Query by (collection_id, seq) sudah efisien via PK

## Migration

### For Existing Databases

Jika database sudah ada log tanpa state_snapshot:

```sql
-- Backfill state_snapshot untuk log yang sudah ada
UPDATE ledger_logs l
SET state_snapshot = ledger_collection_snapshot(c)
FROM ledger_collections c
WHERE l.collection_id = c.id
  AND l.state_snapshot IS NULL;
```

### New Databases

Run migration:
```bash
hatch run manage.py migrate
```

## Troubleshooting

### state_snapshot NULL pada Log Lama

Jika log lama tidak punya state_snapshot:
```python
# Fallback: replay dari genesis
state = reconstruct_collection(collection_id)
if state is None:
    # Manual replay dari log pertama
    logs = Log.objects.filter(collection_id=collection_id).order_by('seq')
    # Replay logic...
```

### Memory Usage untuk History Besar

Untuk collection dengan ribuan log:
```python
# Gunakan iterator untuk memory efficient
def reconstruct_collection_full_iter(collection_id):
    logs = Log.objects.filter(collection_id=collection_id).order_by('seq').iterator()
    for log in logs:
        if log.state_snapshot:
            yield (log.seq, log.event_type, log.state_snapshot)
```

## Future Enhancements

- [ ] Compression untuk state_snapshot (JSONB compression)
- [ ] Delta encoding (hanya menyimpan field yang berubah)
- [ ] Partial snapshot (hanya field penting)
- [ ] State snapshot validation (cross-check dengan Collection)
- [ ] Automated recovery tools
- [ ] Export/import state history
