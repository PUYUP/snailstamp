# ledger — ledger append-only bergaya blockchain di atas PostgreSQL + Django

Item apa pun (pena, jurnal, surat pos, perangko, kamera, rol film, piringan hitam, ...) bisa
dibuat, berpindah tangan, dipakai, dan dikenai aksi. Seluruh riwayat tiap item adalah hash chain
append-only; blok Merkle berkala mengikatnya secara global.

## Identitas: association → member → user

```
association ──< member >── user
```

| Peran | Siapa | Di ledger |
|---|---|---|
| **Pemilik** | association | `entries.issuer_id`, `collections.owner_id`, `holdings.owner_id`, `logs.actor_id`, `logs.counterparty_id` |
| **Pelaku** | member (atas nama association-nya) | `entries.issuer_member_id`, `logs.actor_member_id`, `transfer_tokens.from_member_id` / `claimed_by_member_id` |
| **User** | orang di balik akun | **tidak pernah masuk ledger** |

Kenapa: bila kepemilikan menempel ke user dan user meninggal, koleksinya terkunci. Dengan
association, koleksi bisa dibagi (kakak, adik, kerabat) dan bisa diwariskan tanpa menyentuh riwayat:

* **Pewarisan / berbagi di dalam association** = tambahkan member baru. Tidak ada yang ditulis ke ledger;
  item tetap milik association yang sama, dan log berikutnya mencatat member baru itu.
* **Pewarisan ke association lain** = `send` lalu `claim_transfer` biasa. Riwayat lama tetap utuh.
* **Siapa melakukan apa** selalu terbaca: member tiap aksi masuk ke HASH log, jadi tidak bisa ditulis
  ulang tanpa merusak chain. Pembuat entry: `Entry.issuer_member`. Pembuat tiap item: `item_creator()` (log MINT).

## Pasang
1. Salin folder `ledger/` ke project, tambahkan `"snailstamp.apps.ledger"` ke `INSTALLED_APPS`
   (Django >= 5.2, PostgreSQL >= 14; diuji di Django 6.1 + PostgreSQL 16).
2. `python manage.py migrate` — `0001_schema` membuat tabel/partisi/trigger/fungsi dari `sql/0001_schema.sql`,
   `0002_seed_registry` mengisi registry contoh, `0003_state` hanya state model Django.
3. Pengecekan keanggotaan (lihat bawah). Bawaan: `tenant.Member` punya FK `association`.
4. Jalankan `python manage.py seal_ledger --sealer SEALER_ID:ENV_VAR` sebagai satu service (menyegel blok
   tiap ~10 dtk; blok wajib ditandatangani), `anchor_ledger` untuk anchor checkpoint ke luar DB, dan
   `check_forks` di monitoring (lihat `BLOCK_SIGNING.md`).
5. Disarankan: role `ledger_app` yang hanya punya SELECT + EXECUTE; set `idle_in_transaction_session_timeout`.

### Pengecekan keanggotaan (`LEDGER_MEMBER_CHECK`)
Ledger tidak punya FK ke tabel tenant, jadi DB **tidak bisa** tahu apakah member X anggota association Y.
Setiap fungsi tulis di `services` memeriksanya dulu. Bawaannya (`services.default_member_check`)
mencari `tenant.Member(pk=member_id, association_id=association_id)`. Bila model Anda berbeda, atau
butuh syarat tambahan (member aktif, belum keluar), tulis fungsi dan tunjuk di settings:
```python
LEDGER_MEMBER_CHECK = "snailstamp.apps.tenant.ledger_authz.member_can_act"   # (association_id, member_id) -> bool
```
View/API Anda tetap bertugas memastikan **user yang login memang pemegang member itu**.

## API (`ledger.services`)
`*_id` = association; `*_member_id` = member yang bertindak.

| Fungsi | Arti |
|---|---|
| `create_item(issuer_id, issuer_member_id, alasan, jumlah, "pen")` | entry (alasan) + lahirkan item jenis tsb |
| `send(coll, actor_id, actor_member_id)` / `claim_transfer(token, actor_id, actor_member_id)` / `cancel_send(...)` | kirim → terima (dua langkah; "dalam pengiriman" = surat di jalan) |
| `use(coll, actor_id, actor_member_id, "read")` | aksi tunggal oleh pemilik |
| `act("write", alat, sasaran, actor_id, actor_member_id, content=...)` | dua item bertemu, dicatat di DUA chain |
| `item_history / item_uses / item_acted_on / item_creator` | riwayat; `item_creator` = log MINT (association + member pembuat) |
| `verify_chain`, `verify_blocks`, `build_proof` + `verify_proof` | audit; jalankan KEDUANYA secara berkala |

Isi (teks, foto) tidak pernah masuk ledger, hanya sha256-nya (`verify_content`).
Kesalahan: `NotFound` / `Forbidden` / `InvalidState` / `InvalidInput`.

## Menambah hobi baru = INSERT (tanpa ubah skema/kode)
```sql
INSERT INTO ledger_kinds (id, code, label, max_as_tool, max_as_target, restricted)
VALUES (11, 'sketchbook', 'buku sketsa', NULL, 40, false);        -- muat 40 aksi
INSERT INTO ledger_kinds (id, code, label) VALUES (12, 'pencil', 'pensil');
INSERT INTO ledger_actions (id, code, label) VALUES (7, 'draw', 'menggambar');
INSERT INTO ledger_action_rules (action_id, tool_kind, target_kind, target_access)
VALUES (7, 12, 11, 1);                                            -- pensil menggambar di buku sketsa
```
* `max_as_tool`  : batas pemakaian sebagai alat (perangko = 1).  `max_as_target`: batas aksi sebagai sasaran (rol film = 36).
* `target_access`: 1 pemilik sasaran | 2 siapa pun, saat sasaran DIKIRIM (cap pos) | 3 siapa pun, sasaran aktif (buku tamu).
* `restricted`  : hanya association di `ledger_kind_issuers` yang boleh membuat entry jenis itu (cegah cap pos palsu).
* **id kind/aksi masuk ke hash: tetap, jangan diubah atau dipakai ulang.** Trigger menolak ubah/hapus.

## Tes & reset
* `python manage.py test snailstamp.apps.ledger` — skenario keluarga, otorisasi member, append-only,
  deteksi penulisan ulang member, sealer + bukti Merkle. Tidak butuh data tenant.
* Reset **development**: `sql/dev_reset.sql` (lihat petunjuk di dalamnya). Migrasi skema sengaja tidak
  bisa di-reverse: ledger production tidak boleh di-rollback.

## Batas yang disengaja
* `verify_chain` memeriksa struktur, state, penghitung, tautan silang (termasuk member di kedua sisi),
  dan bahwa MINT dilakukan association penerbit entry. Ia TIDAK memeriksa ulang registry (aturan boleh
  berubah; riwayat lama tetap sah) dan TIDAK memeriksa keanggotaan member pada saat itu.
* **Jangan hapus baris Member/Association** yang pernah muncul di ledger: UUID-nya ada di hash dan tak ada
  FK yang melindungi. Nonaktifkan saja, dan simpan riwayat keanggotaan (`joined_at` / `left_at`) di tenant
  bila perlu membuktikan "member X berhak bertindak saat itu".
* Association yang hanya punya satu admin menjadi yatim bila admin itu meninggal. Siapkan jalur pemulihan
  di luar ledger (mis. minimal dua admin, atau kontak pewaris).
* Tiap baris log memuat `actor_member_id` (+16 byte). Tidak ada index per member; bila butuh
  "aktivitas member X" di skala besar, tambahkan `(actor_member_id, created_at)` dengan sadar biayanya.
* Penempelan fisik (perangko menempel di surat dan ikut berpindah) belum dimodelkan: perangko bekas
  tetap item terpisah (filatelis memang mengoleksinya).
* Superuser database tetap bisa menulis ulang semuanya (chain akan rusak dan terdeteksi);
  publikasikan `block_hash` ke luar untuk bukti tak-terbantahkan.
