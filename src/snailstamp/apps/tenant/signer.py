import nacl.signing
import nacl.encoding

from cryptography.exceptions import InvalidSignature
import base64, hashlib, json, os
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from snailstamp.apps.vault.models import CollectionSignature
from snailstamp.apps.tenant.models import Certificate, Association, Member
from django.conf import settings
from django.db import transaction
from django.utils import timezone
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey


def public_key_to_pem(hex_key: str) -> str:
    pk = Ed25519PublicKey.from_public_bytes(bytes.fromhex(hex_key))
    return pk.public_bytes(
        serialization.Encoding.PEM,
        serialization.PublicFormat.SubjectPublicKeyInfo,
    ).decode()


@transaction.atomic
def create_certificate(
    association: "Association",
    member: "Member"    
) -> "Certificate":
    existing = (
        Certificate.objects.select_for_update()
        .filter(association=association, member=member)
        .order_by("-key_version")
    )
    last = existing.first()
    version = (last.key_version + 1) if last else 1
    existing.filter(is_active=True).update(is_active=False)

    signing_key = nacl.signing.SigningKey.generate()
    raw_private_key = bytes(signing_key)  # seed 32 byte
    public_key_hex = signing_key.verify_key.encode(nacl.encoding.HexEncoder).decode()

    iv = os.urandom(12)
    encrypted = AESGCM(_master_key()).encrypt(
        iv, raw_private_key, _aad(association.id, member.id, version)
    )

    return Certificate.objects.create(
        association=association,
        member=member,
        public_key=public_key_hex,
        encrypted_private_key=encrypted.hex(),
        encryption_iv=iv.hex(),
        key_version=version,
        is_active=True,
    )


def get_active_certificate(
    association: "Association",
    member: "Member"
) -> "Certificate":
    return Certificate.objects.filter(
        association=association,
        member=member,
        is_active=True
    ).order_by("-key_version").get()


@transaction.atomic
def sign_collection(collection) -> "CollectionSignature":
    cert = get_active_certificate(collection.association, collection.member)
    signing_key = _load_signing_key(cert)   # dekripsi + AAD terjadi di sini
    payload = _build_payload(collection)
    digest = hashlib.sha256(payload).hexdigest()
    signature = signing_key.sign(payload).signature   # 64 byte

    return CollectionSignature.objects.create(
        collection=collection,
        certificate=cert,
        payload_hash=digest,
        signature=base64.b64encode(signature).decode(),
        signed_at=timezone.now(),
    )


def verify_collection(collection, sig: "CollectionSignature | None" = None) -> bool:
    sig = sig or collection.signatures.select_related("certificate").order_by("-signed_at").first()
    if not sig:
        return False

    public = Ed25519PublicKey.from_public_bytes(bytes.fromhex(sig.certificate.public_key))
    try:
        public.verify(base64.b64decode(sig.signature), _build_payload(collection))
        return True
    except InvalidSignature:
        return False


# ---------------------------
# PRIVATE UTILS
# ---------------------------

def _aad(association_id, member_id, version: int) -> bytes:
    return f"{association_id}:{member_id}:{version}".encode()


def _master_key() -> bytes:
    key = bytes.fromhex(settings.CERTIFICATE_ENCRYPTION_KEY)
    if len(key) != 32:
        raise ValueError("CERTIFICATE_ENCRYPTION_KEY harus 32 byte (64 hex)")
    return key


def _build_payload(collection) -> bytes:
    """Hanya field yang tidak boleh berubah. JANGAN masukkan 'status'."""
    data = {
        "v": 1,
        "collection_id": str(collection.pk),
        "collection_type": collection.collection_type.slug,
        "association_id": str(collection.association_id),
        "assigner_id": str(collection.assigner_id),
        "name": collection.name,
        "slug": collection.slug,
        "properties": collection.properties,
        "created_at": collection.created_at.isoformat(),  # sesuaikan dengan field di TimeMixin
    }
    # canonical: key terurut, tanpa spasi, supaya hash selalu sama
    return json.dumps(data, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()


def _load_signing_key(cert) -> nacl.signing.SigningKey:
    seed = AESGCM(_master_key()).decrypt(
        bytes.fromhex(cert.encryption_iv),
        bytes.fromhex(cert.encrypted_private_key),
        _aad(cert.association_content_type_id, cert.association_object_id, cert.key_version),  # AAD di sini
    )
    return nacl.signing.SigningKey(seed)
