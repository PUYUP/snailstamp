"""
Shamir's Secret Sharing di atas GF(256) untuk distribusi private key sealer.

Secret (mis. private key Ed25519 32 byte) dipecah menjadi `n` share; minimal `k` share
dibutuhkan untuk merekonstruksi. Kurang dari `k` share tidak membocorkan informasi apa pun
tentang secret. Setiap byte secret dibagi secara independen (polinom derajat k-1, koefisien acak
dari `secrets`), aritmetika memakai polinom AES (x^8 + x^4 + x^3 + x + 1).

Format share (teks, aman untuk disalin ke secret manager / kertas):

    ss1-<set_id>-<k>-<x>-<payload_hex>-<checksum>

* set_id   : 8 hex acak per pemecahan -> mencegah mencampur share dari pemecahan berbeda.
* k        : threshold.
* x        : indeks share (1..255).
* checksum : 8 hex pertama sha256 dari bagian sebelumnya -> mendeteksi salah ketik.
"""
import hashlib
import secrets

VERSION = "ss1"

_EXP = [0] * 512
_LOG = [0] * 256
_x = 1
for _i in range(255):
    _EXP[_i] = _x
    _LOG[_x] = _i
    _y = _x << 1                       # kali generator 3 = x ^ (x * 2)
    if _y & 0x100:
        _y ^= 0x11B
    _x ^= _y
for _i in range(255, 512):
    _EXP[_i] = _EXP[_i - 255]


class ShamirError(ValueError):
    pass


def _mul(a, b):
    if a == 0 or b == 0:
        return 0
    return _EXP[_LOG[a] + _LOG[b]]


def _div(a, b):
    if b == 0:
        raise ZeroDivisionError("pembagian dengan nol di GF(256)")
    if a == 0:
        return 0
    return _EXP[(_LOG[a] - _LOG[b]) % 255]


def _eval(coeffs, x):
    """Horner: coeffs[0] = secret byte."""
    result = 0
    for c in reversed(coeffs):
        result = _mul(result, x) ^ c
    return result


def _checksum(body):
    return hashlib.sha256(body.encode()).hexdigest()[:8]


def _encode(set_id, k, x, payload):
    body = f"{VERSION}-{set_id}-{k}-{x}-{payload.hex()}"
    return f"{body}-{_checksum(body)}"


def parse_share(share):
    """Return (set_id, k, x, payload_bytes). Raise ShamirError bila format/checksum salah."""
    parts = share.strip().split("-")
    if len(parts) != 6 or parts[0] != VERSION:
        raise ShamirError("format share tidak dikenal")
    set_id, k, x, payload_hex, checksum = parts[1:]
    if _checksum("-".join(parts[:5])) != checksum:
        raise ShamirError("checksum share tidak cocok (salah ketik / rusak)")
    try:
        k, x, payload = int(k), int(x), bytes.fromhex(payload_hex)
    except ValueError as exc:
        raise ShamirError("isi share tidak valid") from exc
    if not 1 <= x <= 255 or not 2 <= k <= 255 or not payload:
        raise ShamirError("isi share di luar rentang")
    return set_id, k, x, payload


def split_secret(secret, shares, threshold):
    """Pecah `secret` (bytes) menjadi `shares` share; butuh `threshold` share untuk rekonstruksi."""
    if not isinstance(secret, (bytes, bytearray)) or not secret:
        raise ShamirError("secret harus bytes tidak kosong")
    if not 2 <= threshold <= shares <= 255:
        raise ShamirError("harus 2 <= threshold <= shares <= 255")
    set_id = secrets.token_hex(4)
    polys = [[b] + [secrets.randbelow(256) for _ in range(threshold - 1)] for b in secret]
    return [_encode(set_id, threshold, x, bytes(_eval(p, x) for p in polys))
            for x in range(1, shares + 1)]


def combine_shares(shares):
    """Rekonstruksi secret dari >= threshold share (urutan bebas). Return bytes."""
    parsed = [parse_share(s) for s in shares]
    if not parsed:
        raise ShamirError("tidak ada share")
    set_ids = {p[0] for p in parsed}
    thresholds = {p[1] for p in parsed}
    lengths = {len(p[3]) for p in parsed}
    if len(set_ids) != 1:
        raise ShamirError("share berasal dari pemecahan yang berbeda")
    if len(thresholds) != 1 or len(lengths) != 1:
        raise ShamirError("share tidak konsisten")
    unique = {}
    for _, _, x, payload in parsed:
        if x in unique and unique[x] != payload:
            raise ShamirError(f"share #{x} ganda dengan isi berbeda")
        unique[x] = payload
    k = thresholds.pop()
    if len(unique) < k:
        raise ShamirError(f"butuh minimal {k} share berbeda, baru ada {len(unique)}")
    points = sorted(unique.items())[:k]
    xs = [x for x, _ in points]
    weights = []                       # koefisien Lagrange di x = 0
    for i, xi in enumerate(xs):
        w = 1
        for j, xj in enumerate(xs):
            if i != j:
                w = _mul(w, _div(xj, xj ^ xi))
        weights.append(w)
    out = bytearray(lengths.pop())
    for (_, payload), w in zip(points, weights):
        for idx, byte in enumerate(payload):
            out[idx] ^= _mul(byte, w)
    return bytes(out)


def share_fingerprint(share):
    """Sidik jari share untuk audit log (share asli TIDAK pernah disimpan)."""
    return hashlib.sha256(share.strip().encode()).hexdigest()[:16]
