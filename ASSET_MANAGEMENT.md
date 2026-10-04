# Asset Management Documentation

## Overview

Sistem asset management untuk SnailStamp memungkinkan upload dan tracking file (foto, video, dll) yang terkait dengan Collection. Integritas file diverifikasi melalui ledger menggunakan content hash (SHA256).

## Architecture

### Konsep Utama

1. **1 Collection = 1 File**
   - Setiap collection merepresentasikan satu item (foto/video)
   - Entry = batch/collection dari banyak file

2. **Content-Addressable Storage**
   - File disimpan dengan nama = SHA256 hash-nya
   - Deduplication otomatis: file dengan hash sama tidak duplikat
   - Path structure: `assets/ab/cdef1234567890...` (2 karakter pertama sebagai folder)

3. **Two-Layer System**
   - **Asset Model** (non-ledger): Index untuk lookup cepat, tracking version
   - **Ledger Log** (ACTED_ON): Bukti integritas yang immutable

4. **Storage Backend**
   - Development: Local filesystem
   - Production: S3 (AWS) atau compatible storage

## Setup

### 1. Environment Variables

Tambahkan ke `.env`:

```bash
# Development (local storage)
USE_S3=False

# Production (S3 storage)
USE_S3=True
AWS_STORAGE_BUCKET_NAME=your-bucket-name
AWS_S3_REGION_NAME=us-east-1
AWS_S3_CUSTOM_DOMAIN=  # Optional: CloudFront CDN
AWS_ACCESS_KEY_ID=your-access-key
AWS_SECRET_ACCESS_KEY=your-secret-key
```

### 2. Migration

Jalankan migration untuk membuat tabel `ledger_assets`:

```bash
hatch run manage.py migrate
```

## Usage

### 1. Upload File Baru

```python
from snailstamp.apps.ledger.services import upload_asset
from django.core.files.uploadedfile import SimpleUploadedFile

# Upload file (dari Django request)
with open('photo.jpg', 'rb') as f:
    file_obj = SimpleUploadedFile(
        name='photo.jpg',
        content=f.read(),
        content_type='image/jpeg'
    )

# Upload ke ledger
asset_id, seq = upload_asset(
    collection_id=collection_id,  # ID collection yang terkait
    actor_id=association_id,      # ID association pemilik
    actor_member_id=member_id,   # ID member yang upload
    file_obj=file_obj,
    original_filename='photo.jpg',
    mime_type='image/jpeg',
    metadata={'description': 'Foto produk', 'tags': ['product', 'photo']},
    action_code='upload',
    replace_existing=False
)

print(f"Asset ID: {asset_id}, Log Seq: {seq}")
```

### 2. Upload dengan Replace (Versioning)

```python
# Upload versi baru, mengganti versi lama
asset_id, seq = upload_asset(
    collection_id=collection_id,
    actor_id=association_id,
    actor_member_id=member_id,
    file_obj=file_obj,
    original_filename='photo_v2.jpg',
    mime_type='image/jpeg',
    replace_existing=True  # Ganti asset lama yang active
)
```

### 3. Verifikasi File

```python
from snailstamp.apps.ledger.services import verify_asset

# Verifikasi file sama dengan yang dicatat di ledger
valid, asset_id = verify_asset(
    collection_id=collection_id,
    seq=seq,  # Seq log ACTED_ON
    file_obj='path/to/file.jpg'  # atau file object
)

if valid:
    print(f"File valid! Asset ID: {asset_id}")
else:
    print("File tidak valid atau log tidak ditemukan")
```

### 4. Get Asset Info

```python
from snailstamp.apps.ledger.services import get_asset, get_asset_versions

# Ambil asset terbaru untuk collection
asset = get_asset(collection_id)
if asset:
    print(f"Filename: {asset.original_filename}")
    print(f"Size: {asset.file_size} bytes")
    print(f"Hash: {asset.content_hash}")
    print(f"Version: {asset.version}")
    print(f"URL: {asset.file_url}")

# Ambil semua versi asset
versions = get_asset_versions(collection_id)
for asset in versions:
    print(f"v{asset.version}: {asset.original_filename} ({asset.status})")
```

### 5. Download File

```python
from snailstamp.apps.ledger.services import get_asset

asset = get_asset(collection_id)
if asset:
    file_obj = asset.get_file()
    content = file_obj.read()
    # Gunakan content sesuai kebutuhan
```

## API Integration Example

### REST API Endpoint (Django REST Framework)

```python
from rest_framework import serializers
from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework.parsers import MultiPartParser
from snailstamp.apps.ledger.services import upload_asset, verify_asset, get_asset

class AssetUploadSerializer(serializers.Serializer):
    file = serializers.FileField()
    collection_id = serializers.UUIDField()
    metadata = serializers.JSONField(required=False)

class AssetUploadView(APIView):
    parser_classes = [MultiPartParser]

    def post(self, request):
        serializer = AssetUploadSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        # Ambil member dari request (dari authentication)
        member_id = request.user.member.id
        association_id = request.user.member.association_id

        # Upload file
        asset_id, seq = upload_asset(
            collection_id=serializer.validated_data['collection_id'],
            actor_id=association_id,
            actor_member_id=member_id,
            file_obj=serializer.validated_data['file'],
            original_filename=serializer.validated_data['file'].name,
            mime_type=serializer.validated_data['file'].content_type,
            metadata=serializer.validated_data.get('metadata', {}),
        )

        return Response({
            'asset_id': asset_id,
            'seq': seq,
            'message': 'File uploaded successfully'
        })

class AssetVerifyView(APIView):
    def post(self, request):
        collection_id = request.data['collection_id']
        seq = request.data['seq']
        file_obj = request.FILES['file']

        valid, asset_id = verify_asset(collection_id, seq, file_obj)

        return Response({
            'valid': valid,
            'asset_id': asset_id
        })
```

## Storage Configuration

### Local Storage (Development)

File disimpan di:
```
/media/assets/ab/cdef1234567890...
```

### S3 Storage (Production)

File disimpan di S3 dengan structure:
```
s3://bucket-name/assets/ab/cdef1234567890...
```

URL pattern:
```
https://bucket-name.s3.region.amazonaws.com/assets/ab/cdef1234567890...
```

Dengan CloudFront CDN (opsional):
```
https://cdn.yourdomain.com/assets/ab/cdef1234567890...
```

## Security Considerations

1. **Private Storage**: Default ACL S3 = private (bukan public)
2. **Signed URLs**: Gunakan presigned URL untuk temporary access
3. **Content Hash**: Integritas file diverifikasi via ledger, bukan URL
4. **Version Tracking**: Semua versi file tercatat, tidak bisa dihapus secara diam-diam

## Performance Optimization

1. **Deduplication**: File dengan hash sama tidak duplikat di storage
2. **Lazy Loading**: Asset model tidak termasuk dalam ledger query
3. **Indexing**: Index pada (collection, version), content_hash, status
4. **Chunked Upload**: Untuk file besar, implement chunked upload (future)

## Troubleshooting

### Migration Error

Jika mengalami error saat migration karena AUTH_USER_MODEL:

```bash
# Temporarily disable admin in settings
# Run migration
# Re-enable admin
```

### File Upload Failed

1. Cek storage backend configuration
2. Cek permissions (local filesystem atau S3 credentials)
3. Cek file size limit

### Verification Failed

1. Pastikan file yang diverifikasi sama persis dengan yang diupload
2. Cek seq log yang benar (gunakan `item_history(collection_id)`)
3. Pastikan log event type = ACTED_ON

## Future Enhancements

- [ ] Chunked upload untuk file besar
- [ ] Presigned URL untuk temporary access
- [ ] Thumbnail generation untuk images
- [ ] Video transcoding
- [ ] CDN integration
- [ ] Asset deletion dengan soft delete
