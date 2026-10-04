"""Pengaturan ledger_media dari settings.LEDGER_MEDIA (dict). Hanya BUCKET yang wajib.

    LEDGER_MEDIA = {
        "BUCKET": "snailstamp-media",
        "REGION": "ap-southeast-3",
        # "ENDPOINT_URL": "http://minio:9000", "ADDRESSING_STYLE": "path",   # MinIO / S3-compatible
        # "ACCESS_KEY_ID": "...", "SECRET_ACCESS_KEY": "...",                 # default: rantai kredensial boto3
    }
"""
from django.conf import settings
from django.core.exceptions import ImproperlyConfigured

MiB = 1024 * 1024
GiB = 1024 * MiB

DEFAULTS = {
    "BUCKET": None,
    "ENDPOINT_URL": None,
    "REGION": None,
    "ACCESS_KEY_ID": None,
    "SECRET_ACCESS_KEY": None,
    "ADDRESSING_STYLE": "auto",          # "path" untuk MinIO
    "UPLOAD_PREFIX": "uploads/",         # klien boleh menulis di sini (presigned URL)
    "MEDIA_PREFIX": "media/",            # HANYA server yang menulis di sini; di sinilah file di-hash
    "UPLOAD_URL_TTL": 900,               # detik
    "DOWNLOAD_URL_TTL": 300,
    "SINGLE_PUT_MAX": 100 * MiB,         # di atas ini: multipart
    "PART_SIZE": 16 * MiB,               # minimum S3 = 5 MiB; maksimum 10.000 part
    "PENDING_TTL": 24 * 3600,            # unggahan yang tak pernah difinalisasi dianggap kedaluwarsa
    "PROCESSING_STALE": 900,             # finalisasi yang macet lebih lama dari ini boleh diambil alih
    "ACTION": "attach",                  # kode aksi registry untuk lampiran (ledger_use)
    # tipe yang diizinkan -> ukuran maksimum (byte). Tipe aktif (html/svg/pdf) sengaja tidak ada.
    "ALLOWED_TYPES": {
        "image/jpeg": 25 * MiB, "image/png": 25 * MiB, "image/gif": 25 * MiB, "image/webp": 25 * MiB,
        "image/heic": 25 * MiB, "image/heif": 25 * MiB,
        "video/mp4": 2 * GiB, "video/quicktime": 2 * GiB, "video/webm": 2 * GiB,
    },
}


def get(name):
    value = getattr(settings, "LEDGER_MEDIA", {}).get(name, DEFAULTS[name])
    if name == "BUCKET" and not value:
        raise ImproperlyConfigured("settings.LEDGER_MEDIA['BUCKET'] wajib diisi")
    return value
