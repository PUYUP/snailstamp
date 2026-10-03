from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from django.conf import settings

def sign_collection_payload(issuer_instance, payload_text):
    """
    Mendekripsi private key, membuat tanda tangan, lalu membuang private key dari RAM

    Args:
        issuer_instance: Instance dari model yang memiliki private key
        payload_text: Teks yang akan ditandatangani (contoh: f"{new_collection.id}:{new_collection.content_type}|{issuer.id}:{issuer.content_type}|{new_collection.created_at.timestamp()}")

    Returns:
        str: Tanda tangan dalam bentuk hex
    """

    # 1. Ambil Master Key dari Environment Variable Django (settings.py)
    master_key = bytes.fromhex(settings.VAULT_ENCRYPTION_KEY)
    aesgcm = AESGCM(master_key)

    # 2. Dekripsi private key di dalam memori (RAM)
    iv = bytes.fromhex(issuer_instance.encryption_iv)
    encrypted_key = bytes.fromhex(issuer_instance.encrypted_private_key)
    raw_private_key = aesgcm.decrypt(iv, encrypted_key, associated_data=None)

    # 3. Lakukan proses penandatanganan menggunakan Ed25519
    # (Menggunakan library seperti PyNaCl / pynacl)
    import nacl.signing
    signing_key = nacl.signing.SigningKey(raw_private_key)
    signed_b64 = signing_key.sign(payload_text.encode()).signature.hex()

    # 4. Hapus kunci privat dari memori secara eksplisit (opsional tapi disarankan)
    del raw_private_key
    del signing_key

    return signed_b64
