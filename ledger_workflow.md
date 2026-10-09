# Panduan Workflow Ledger: Entry, Collection, dan Content

Dokumen ini menjelaskan alur kerja (workflow) lengkap untuk berinteraksi dengan sistem ledger/semi-blockchain di aplikasi ini. Karena ledger bersifat *append-only* (tidak bisa dihapus/diubah secara sembarangan), penting untuk memahami urutan operasinya.

## Konsep Dasar

1. **Entry**: Cetak biru (blueprint) atau alasan dasar pembuatan sesuatu (contoh: "Cetak 1000 stiker edisi terbatas").
2. **Collection (Item)**: Benda spesifik yang lahir dari sebuah Entry (contoh: Stiker #1, Stiker #2).
3. **Log (History)**: Catatan permanen dari setiap kejadian yang terjadi pada sebuah Collection (pindah tangan, dipasangi foto, dll).
4. **Read Model (Content/Asset)**: Tabel pendamping (seperti `Content` atau `Asset`) yang berelasi *One-to-One* dengan Collection. Fungsinya murni untuk **mempercepat query di UI/Frontend**, sedangkan *Source of Truth* (sumber kebenaran sejarah) tetap berada di tabel Log.

---

## Alur Kerja (Workflow)

### 1. Membuat Entry (Cetak Biru)
Sebelum Anda bisa memiliki item, Anda harus mendefinisikan *entry*-nya. Entry akan menentukan maksimal jumlah item yang bisa ada (`supply`).

```python
from snailstamp.apps.ledger import services

def buat_blueprint_stiker(issuer_id, member_id):
    # 1. Definisikan metadata dasar (Immutable setelah di-mint)
    metadata = {
        "bahan": "Vinyl Hologram",
        "tahun_rilis": 2026,
        "seri": "A1"
    }
    
    # 2. Buat Entry
    entry_id = services.create_entry(
        issuer_id=issuer_id,          # ID Asosiasi / Organisasi pembuat
        issuer_member_id=member_id,   # ID Member yang sedang login
        reason="Cetak stiker promosi batch 1", 
        supply=1000,                  # Maksimal stiker yang bisa dicetak
        metadata=metadata,
        kind="sticker"                # Kode jenis dari ledger_kinds registry
    )
    
    return entry_id
```
> [!NOTE] 
> Selama item belum di-mint (`minted_count == 0`), Anda masih bisa mengedit *entry* ini menggunakan `services.update_entry()`. Begitu item pertama lahir, *entry* akan dibekukan selamanya.

### 2. Melahirkan Collection (Minting)
Setelah *entry* dibuat, Anda "melahirkan" item aslinya (Collection) ke dunia.

```python
from snailstamp.apps.ledger import services

def cetak_item_stiker(entry_id, issuer_id, member_id):
    # Melahirkan 100 item pertama dari total supply (batching)
    # Ini akan membuat 100 baris di tabel `ledger_collections`
    # dan 100 baris log MINT di tabel `ledger_logs`.
    
    jumlah_berhasil = services.mint_batch(
        entry_id=entry_id,
        issuer_id=issuer_id,
        issuer_member_id=member_id,
        batch=100, 
        prefix="STK-" # Serial Number akan dimulai dengan STK-
    )
    
    print(f"Berhasil mencetak {jumlah_berhasil} stiker!")
```

### 3. Menambahkan Keterangan / Content (Read Model)
Katakanlah pengguna (yang sekarang memiliki stiker tersebut) ingin menambahkan cerita atau deskripsi pribadi (Content). Operasi ini mengubah "state" dinamis dari item tersebut, maka harus dicatat di Ledger *dan* tabel Content.

```python
from snailstamp.apps.ledger import services

def tambah_cerita_ke_stiker(collection_id, owner_id, member_id, judul, isi_cerita):
    # Fungsi ini (add_or_update_content) sudah membungkus dua aksi sekaligus 
    # dalam satu transaksi database:
    # 1. Menulis sejarah "user menulis cerita" ke tabel `ledger_logs`
    # 2. Menyimpan/Update teksnya ke tabel `Content` untuk kemudahan query
    
    content_id, log_seq = services.add_or_update_content(
        collection_id=collection_id,
        actor_id=owner_id,
        actor_member_id=member_id,
        title=judul,
        body=isi_cerita,
        format="markdown",
        action_code="write"
    )
    
    return content_id
```

### 4. Menambahkan File / Foto (Asset)
Mirip dengan Content, penambahan file fisik diverifikasi *hash*-nya agar *tamper-proof*.

```python
from snailstamp.apps.ledger import services

def upload_foto_ke_stiker(collection_id, owner_id, member_id, file_obj):
    # Proses ini secara otomatis:
    # 1. Menghitung SHA256 file
    # 2. Meng-upload file ke Storage (S3 / Local)
    # 3. Mencatat aksi ACTED_ON beserta hash-nya ke `ledger_logs`
    # 4. Membuat record di tabel `Asset` (Read Model)
    
    asset_id, log_seq = services.upload_asset(
        collection_id=collection_id,
        actor_id=owner_id,
        actor_member_id=member_id,
        file_obj=file_obj,
        action_code="affix"  # Contoh: aksi 'menempelkan' foto
    )
    
    return asset_id
```

---

## Ringkasan Konsep CQRS (Command Query Responsibility Segregation)

Arsitektur yang kita gunakan memisahkan alur **Tulis (Command)** dan **Baca (Query)**:

- **Cara TULIS yang salah:** `Content.objects.create(...)` atau `Asset.objects.create(...)`. Jangan pernah melakukan ini langsung.
- **Cara TULIS yang benar:** Lewat fungsi di `services.py` (`services.add_or_update_content()`, `services.upload_asset()`, atau `services.use()`). Ini memastikan setiap mutasi data tercatat permanen di Ledger (Log).

- **Cara BACA yang salah:** Membongkar dan merekonstruksi `ledger_logs` setiap kali ingin menampilkan halaman di Web UI. (Sangat lambat).
- **Cara BACA yang benar:** Lakukan query biasa ke Read Model seperti `Collection.objects.select_related('content', 'asset')`. Ini sangat cepat karena Read Model hanya berisi *state* terkini.
