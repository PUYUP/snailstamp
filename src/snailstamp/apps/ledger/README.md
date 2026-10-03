# ledger — ledger append-only bergaya blockchain di atas PostgreSQL + Django

Item apa pun (pena, jurnal, surat pos, perangko, kamera, rol film, piringan hitam, ...) bisa
dibuat, berpindah tangan, dipakai, dan dikenai aksi. Seluruh riwayat tiap item adalah hash chain
append-only; blok Merkle berkala mengikatnya secara global.

## Pasang
1. Salin folder `ledger/` ke project, tambahkan `"ledger"` ke `INSTALLED_APPS` (Django >= 5.2, PostgreSQL >= 14).
2. `python manage.py migrate`  (0001 skema, 0002 registry contoh, 0003 state model)
3. Jalankan `python manage.py seal_ledger` sebagai satu service (menyegel blok tiap ~10 dtk).
4. Disarankan: role `ledger_app` yang hanya punya SELECT + EXECUTE; set `idle_in_transaction_session_timeout`.

## API (`ledger.services`)
| Fungsi | Arti |
|---|---|
| `create_item(user, alasan, jumlah, "pen")` | entry (alasan) + lahirkan item jenis tsb |
| `send / receive / cancel_send` | kirim -> terima (dua langkah; "dalam pengiriman" = surat di jalan) |
| `use(item, user, "read")` | aksi tunggal oleh pemilik |
| `act("write", alat, sasaran, user, content=...)` | dua item bertemu, dicatat di DUA chain |
| `item_history / item_uses / item_acted_on` | riwayat |
| `verify_chain`, `verify_blocks`, `build_proof` + `verify_proof` | audit; jalankan KEDUANYA secara berkala |

Isi (teks, foto) tidak pernah masuk ledger, hanya sha256-nya (`verify_content`).

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
* `restricted`  : hanya user di `ledger_kind_issuers` yang boleh membuat entry jenis itu (cegah cap pos palsu).
* **id kind/aksi masuk ke hash: tetap, jangan diubah atau dipakai ulang.** Trigger menolak ubah/hapus.

## Batas yang disengaja
* `verify_chain` memeriksa struktur, state, penghitung, dan tautan silang; ia TIDAK memeriksa ulang
  registry (aturan boleh berubah; riwayat lama tetap sah).
* Penempelan fisik (perangko menempel di surat dan ikut berpindah) belum dimodelkan: perangko bekas
  tetap item terpisah (filatelis memang mengoleksinya).
* Superuser database tetap bisa menulis ulang semuanya; publikasikan `block_hash` ke luar untuk bukti tak-terbantahkan.
