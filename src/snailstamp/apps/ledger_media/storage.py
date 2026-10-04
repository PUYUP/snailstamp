"""Pembungkus tipis boto3 untuk S3 / S3-compatible. Tidak ada logika bisnis di sini."""
from functools import lru_cache

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError

from . import conf


@lru_cache(maxsize=8)
def _client(endpoint_url, region, key_id, secret, addressing):
    creds = {"aws_access_key_id": key_id, "aws_secret_access_key": secret} if key_id and secret else {}
    return boto3.client("s3", endpoint_url=endpoint_url, region_name=region, **creds,
                        config=Config(signature_version="s3v4", s3={"addressing_style": addressing}))


class S3Storage:
    def __init__(self):
        self.bucket = conf.get("BUCKET")
        self.client = _client(conf.get("ENDPOINT_URL"), conf.get("REGION"), conf.get("ACCESS_KEY_ID"),
                              conf.get("SECRET_ACCESS_KEY"), conf.get("ADDRESSING_STYLE"))

    # --- unggah (dipakai klien lewat presigned URL) -------------------------------------
    def presign_put(self, key, content_type, ttl):
        return self.client.generate_presigned_url(
            "put_object", Params={"Bucket": self.bucket, "Key": key, "ContentType": content_type}, ExpiresIn=ttl)

    def create_multipart(self, key, content_type):
        return self.client.create_multipart_upload(Bucket=self.bucket, Key=key, ContentType=content_type)["UploadId"]

    def presign_part(self, key, upload_id, part_number, ttl):
        return self.client.generate_presigned_url(
            "upload_part", ExpiresIn=ttl,
            Params={"Bucket": self.bucket, "Key": key, "UploadId": upload_id, "PartNumber": part_number})

    def complete_multipart(self, key, upload_id, parts):
        parts = sorted(({"PartNumber": int(p["PartNumber"]), "ETag": p["ETag"]} for p in parts),
                       key=lambda p: p["PartNumber"])
        self.client.complete_multipart_upload(Bucket=self.bucket, Key=key, UploadId=upload_id,
                                              MultipartUpload={"Parts": parts})

    def abort_multipart(self, key, upload_id):
        try:
            self.client.abort_multipart_upload(Bucket=self.bucket, Key=key, UploadId=upload_id)
        except ClientError as e:
            if e.response["Error"]["Code"] != "NoSuchUpload":
                raise

    # --- operasi sisi server -------------------------------------------------------------
    def head(self, key):
        """{'size', 'content_type'} atau None bila tidak ada.
        Catatan: tanpa izin s3:ListBucket, S3 membalas 403 (bukan 404) untuk key yang tak ada."""
        try:
            r = self.client.head_object(Bucket=self.bucket, Key=key)
        except ClientError as e:
            if e.response["Error"]["Code"] in ("404", "NoSuchKey", "NotFound"):
                return None
            raise
        return {"size": r["ContentLength"], "content_type": r.get("ContentType")}

    def copy(self, src_key, dst_key, content_type):
        """Salin di sisi S3 (tanpa lalu-lintas ke server; multipart otomatis untuk file besar).
        Content-Type diganti dengan tipe yang SUDAH divalidasi, bukan yang dikirim klien."""
        self.client.copy({"Bucket": self.bucket, "Key": src_key}, self.bucket, dst_key,
                         ExtraArgs={"ContentType": content_type, "MetadataDirective": "REPLACE"})

    def delete(self, key):
        self.client.delete_object(Bucket=self.bucket, Key=key)

    def iter_chunks(self, key, chunk_size=1 << 20):
        body = self.client.get_object(Bucket=self.bucket, Key=key)["Body"]
        try:
            yield from body.iter_chunks(chunk_size)
        finally:
            body.close()

    # --- unduh (dipakai klien lewat presigned URL) ---------------------------------------
    def presign_get(self, key, ttl, content_type, disposition):
        return self.client.generate_presigned_url(
            "get_object", ExpiresIn=ttl,
            Params={"Bucket": self.bucket, "Key": key,
                    "ResponseContentType": content_type, "ResponseContentDisposition": disposition})
