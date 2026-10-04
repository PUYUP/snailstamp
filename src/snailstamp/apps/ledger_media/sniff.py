"""Deteksi tipe file dari byte awalnya (magic number). Tipe yang DIKLAIM klien tidak dipercaya:
file harus benar-benar berbentuk seperti yang diklaim sebelum masuk ledger."""

_HEIC_BRANDS = {b"heic", b"heix", b"hevc", b"hevx", b"heim", b"heis", b"mif1", b"msf1"}
_NOT_ALLOWED_BRANDS = {b"avif", b"avis"}          # AVIF berbungkus mirip HEIC; belum diizinkan


def sniff(head: bytes):
    """Kembalikan tipe MIME kanonik, atau None bila tidak dikenali."""
    if head.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if head.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if head.startswith((b"GIF87a", b"GIF89a")):
        return "image/gif"
    if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return "image/webp"
    if head[:4] == b"\x1a\x45\xdf\xa3":
        return "video/webm"
    if head[4:8] == b"ftyp":
        brand = head[8:12]
        if brand in _NOT_ALLOWED_BRANDS:
            return None
        if brand in _HEIC_BRANDS:
            return "image/heic"
        if brand == b"qt  ":
            return "video/quicktime"
        return "video/mp4"
    return None


def matches(declared: str, sniffed):
    """Tipe yang diklaim cocok dengan isi? (heif dan heic satu keluarga)"""
    canon = {"image/heif": "image/heic"}
    return sniffed is not None and canon.get(declared, declared) == sniffed
