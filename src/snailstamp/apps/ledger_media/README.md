# ledger_media — foto & video untuk collection, tersimpan di S3

Byte file ada di S3. Ledger hanya menyimpan **sha256**-nya, plus siapa (association + member) dan kapan.
`MediaObject` adalah tabel bantu (boleh berubah); buktinya selalu ada di ledger (log `USE`).

```
klien ──request_upload──▶ Django ──(presigned URL)──▶ klien ──PUT langsung──▶ S3 uploads/<id>
klien ──finalize_upload──▶ Django: salin ke media/<id> → hash di sana → ledger.use(content_hash) + READY
klien ──get_download_url─▶ Django ──(presigned GET, 5 menit)──▶ klien ◀── S3 media/<id>
```

File tidak pernah lewat Django (aman untuk video berukuran GB). App ini terpisah dari `ledger` karena
`MediaObject` mutable (status, hapus) sedangkan seluruh tabel ledger append-only, dan agar `boto3`
bukan dependensi ledger. `ledger` tidak mengenal `ledger_media`.

## Pasang
1. `pip install boto3`
2. `INSTALLED_APPS += ["snailstamp.apps.ledger_media"]` (setelah `ledger`)
3. Settings (hanya `BUCKET` wajib; selebihnya lihat `conf.py`):
   ```python
   LEDGER_MEDIA = {"BUCKET": "snailstamp-media", "REGION": "ap-southeast-3"}
   # MinIO / S3-compatible: tambahkan "ENDPOINT_URL": "http://minio:9000", "ADDRESSING_STYLE": "path"
   # Kredensial: bawaan = rantai boto3 (IAM role / env). Atau "ACCESS_KEY_ID" + "SECRET_ACCESS_KEY".
   ```
4. `python manage.py migrate`. Migrasi ini bergantung pada `("tenant", "__latest__")`, bukan nama migrasi tertentu.
5. Aksi `attach` (id 7) ada di `ledger/sql/0002_seed_registry.sql`. Bila registry Anda sendiri, buat aksi dengan kode itu
   atau ubah `LEDGER_MEDIA["ACTION"]`.
6. Jadwalkan `cleanup_media` (mis. tiap jam) dan `audit_media` (mis. harian; `--rehash` mingguan).

## Menyiapkan bucket (contoh; sesuaikan, belum diuji terhadap AWS sungguhan, hanya moto)
* **Block all public access** aktif; default encryption aktif. Semua akses lewat presigned URL.
* **Izin IAM** identitas yang menandatangani URL: `s3:PutObject`, `s3:GetObject`, `s3:DeleteObject`,
  `s3:AbortMultipartUpload`, `s3:ListMultipartUploadParts` pada `bucket/*`, **dan `s3:ListBucket` pada bucket**.
  Tanpa `ListBucket`, S3 membalas 403 (bukan 404) untuk key yang tak ada, sehingga finalisasi salah membaca "belum diunggah".
* **Lifecycle** pada prefix `uploads/`: kedaluwarsakan objek 1 hari dan `AbortIncompleteMultipartUpload` 1 hari
  (jaring pengaman bila `cleanup_media` tak jalan).
* **CORS** bila klien browser: izinkan `PUT` dan `GET`, header `Content-Type`, dan **expose header `ETag`**
  (multipart butuh ETag tiap part).

## Alur pemakaian
```python
from snailstamp.apps.ledger_media import services as media

# 1. minta izin unggah (validasi tipe, ukuran, keanggotaan, kepemilikan collection)
r = media.request_upload(association_id, member_id, collection_id, "foto ibu.jpg", "image/jpeg", size)
#   r["mode"] == "single"   -> PUT file ke r["url"] dengan header r["headers"]
#   r["mode"] == "multipart"-> PUT tiap part ke r["parts"][i]["url"], kumpulkan ETag
#                              (URL kedaluwarsa? media.presign_parts(media_id, association_id, member_id, [3, 4]))

# 2. setelah unggah selesai
m = media.finalize_upload(r["media_id"], association_id, member_id)                  # single
m = media.finalize_upload(r["media_id"], association_id, member_id,                  # multipart
                          parts=[{"PartNumber": 1, "ETag": '"..."'}, ...])

# 3. baca / daftar / hapus
media.get_download_url(m.id, association_id, member_id)      # {"url", "expires_in", "content_type", "sha256", ...}
media.list_media(collection_id, association_id, member_id)    # urut sesuai urutan log di ledger
media.erase_media(m.id, association_id, member_id)           # hapus byte; bukti di ledger tetap
```
Galat: `NotFound`, `Forbidden`, `InvalidState`, `InvalidInput` (dari `ledger.services`), ditambah
`NotUploaded` dan `InvalidParts` (keduanya **bisa diulang**: media tetap `PENDING`, part yang sudah terunggah tidak dibuang).
`finalize_upload` idempoten dan aman dipanggil ulang. View/API (DRF dsb.) tidak disertakan: tipis saja,
cukup memetakan galat di atas ke status HTTP dan memastikan user yang login memang pemegang `member_id` itu.

## Apa yang dijamin, dan bagaimana
| Jaminan | Mekanisme |
|---|---|
| Hash di ledger = byte yang tersimpan | file disalin ke `media/<id>` (tak ada presigned URL yang bisa menulis ke sana) **baru** di-hash di sana |
| Tipe file jujur | isi diperiksa (magic number) terhadap tipe yang diklaim; daftar tipe diizinkan sempit (tanpa html/svg/pdf); `Content-Type` S3 dan header unduh dipaksa dari data tervalidasi |
| Ukuran jujur | ukuran aktual (HEAD dan jumlah byte yang di-stream) harus sama dengan yang dideklarasikan dan di bawah batas tipe |
| Atomik | `ledger.use` dan penandaan `READY` dalam satu transaksi: keduanya berhasil atau tidak sama sekali |
| Tak ada finalisasi ganda | transisi `PENDING→PROCESSING` atomik; yang macet > `PROCESSING_STALE` boleh diambil alih |
| Worker lambat tak merusak | `FAILED` hanya dari status yang diizinkan; media `READY` milik worker lain tak ditimpa dan filenya tak dihapus |
| Pelaku tercatat | actor di log = (association, member) peminta unggahan; hanya peminta yang boleh finalisasi |
| Akses mengikuti pemilik | baca/hapus dicek ke pemilik collection **saat ini**: diwariskan = media ikut, tanpa menyalin file |
| Nama file bukan data ledger | hanya `media id`, tipe, dan ukuran di payload; nama file di tabel saja (bisa dihapus) |
| Bukti utuh setelah hapus | `erase_media` menghapus byte; hash, waktu, pelaku di ledger tetap, `verify_chain` tetap valid |

`audit_media` mencocokkan tiap media dengan ledger (hash, association, member, payload), memverifikasi rantai
collection-nya, dan dengan `--rehash` menghitung ulang sha256 dari S3 (mendeteksi file rusak atau diganti).
Exit code ≠ 0 bila ada ketidakcocokan.

## Kebijakan yang perlu Anda sadari
* **Warisan**: penerima baru melihat semua media lama. Pemilik lama kehilangan akses.
* **Hapus**: hanya pemilik saat ini (anggota mana pun), tak peduli siapa yang mengunggah. Tak bisa di-undelete;
  hash di ledger membuktikan file itu pernah ada tanpa membocorkan isinya.
* **`kinds.max_as_tool`**: lampiran dicatat lewat `ledger.use`, yang menghitung sebagai pemakaian alat. Jenis item
  berbatas pakai (mis. perangko = 1) akan menolak lampiran setelah batasnya habis (media jadi `FAILED`).
  Pakai jenis tanpa batas (`max_as_tool` NULL) sebagai wadah media.
* **Tidak ada** pemrosesan turunan (thumbnail, transcode), pemindaian virus, atau penghapusan EXIF. Hash dihitung
  atas byte persis yang diunggah; bila Anda mengubah byte (hapus EXIF), unggah ulang hasilnya. Turunan boleh dibuat
  sendiri dan tidak di-hash.
* Tabel `MediaObject` bukan bukti. Jangan percaya `sha256` di sana tanpa `verify_media` / `audit_media`.

## Tes
`pip install "moto[server]"` lalu `python manage.py test snailstamp.apps.ledger_media`. Tes memakai server S3 tiruan
lewat HTTP sungguhan (presigned PUT/GET benar-benar dipakai). Tanpa moto, tes alur S3 dilewati.
Keterbatasan: moto gagal memproses PUT part multipart lewat presigned URL, jadi tes multipart mengunggah part
lewat API dan hanya memeriksa bentuk URL part-nya. Perilaku nyata presigned part terhadap S3/MinIO sungguhan belum diuji.
