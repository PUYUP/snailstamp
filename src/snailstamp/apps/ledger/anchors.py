"""
Anchor checkpoint blok ke penyimpanan di LUAR database ledger.

Checkpoint = (block_no, block_hash, prev_block_hash, window_end_us, signatures). Karena block_hash
mengikat seluruh blok sebelumnya, satu checkpoint mengunci seluruh riwayat sampai blok itu.
Superuser DB yang menulis ulang chain tidak bisa ikut mengubah checkpoint yang sudah diterbitkan
ke luar, jadi services.detect_forks() akan menemukan perbedaannya.

Backend dipilih lewat settings:

    LEDGER_ANCHOR_BACKENDS = [
        {"NAME": "worm", "BACKEND": "snailstamp.apps.ledger.anchors.FileAnchorBackend",
         "OPTIONS": {"path": "/mnt/worm/snailstamp-anchors.jsonl"}},
        {"NAME": "notary", "BACKEND": "snailstamp.apps.ledger.anchors.HttpAnchorBackend",
         "OPTIONS": {"url": "https://notary.example.com/anchors", "token_env": "LEDGER_ANCHOR_TOKEN"}},
    ]

Backend baru cukup mewarisi AnchorBackend dan mengimplementasikan publish() + checkpoints().
"""
import json
import os
import urllib.request
from dataclasses import asdict, dataclass, field
from pathlib import Path

from django.conf import settings
from django.utils.module_loading import import_string


class AnchorError(Exception):
    """Backend anchor tidak bisa ditulis / dibaca, atau isinya rusak."""


@dataclass(frozen=True)
class Checkpoint:
    block_no: int
    block_hash: str                 # hex
    prev_block_hash: str            # hex
    window_end_us: int
    signatures: list = field(default_factory=list)   # [{"public_key", "signature", "region"}]
    v: int = 1

    def to_dict(self):
        return asdict(self)

    @classmethod
    def from_dict(cls, data):
        try:
            return cls(block_no=int(data["block_no"]), block_hash=str(data["block_hash"]),
                       prev_block_hash=str(data["prev_block_hash"]), window_end_us=int(data["window_end_us"]),
                       signatures=list(data.get("signatures") or []), v=int(data.get("v", 1)))
        except (KeyError, TypeError, ValueError) as exc:
            raise AnchorError(f"checkpoint tidak valid: {exc}") from exc


class AnchorBackend:
    def __init__(self, name, **options):
        self.name = name
        self.options = options

    def publish(self, checkpoint):
        """Terbitkan checkpoint. Return receipt (teks) yang disimpan di BlockAnchor."""
        raise NotImplementedError

    def checkpoints(self):
        """Semua checkpoint yang pernah diterbitkan, dibaca dari sumber EKSTERNAL (bukan DB ledger)."""
        raise NotImplementedError


class FileAnchorBackend(AnchorBackend):
    """Satu baris JSON per checkpoint. Taruh di volume terpisah / WORM / replika off-site."""

    @property
    def path(self):
        return Path(self.options["path"])

    def publish(self, checkpoint):
        line = json.dumps(checkpoint.to_dict(), sort_keys=True, separators=(",", ":")) + "\n"
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            fd = os.open(self.path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o644)
            try:
                os.write(fd, line.encode("utf-8"))
                os.fsync(fd)
            finally:
                os.close(fd)
        except OSError as exc:
            raise AnchorError(f"{self.name}: gagal menulis {self.path}: {exc}") from exc
        return f"file:{self.path}#block={checkpoint.block_no}"

    def checkpoints(self):
        if not self.path.exists():
            return []
        try:
            lines = self.path.read_text(encoding="utf-8").splitlines()
        except OSError as exc:
            raise AnchorError(f"{self.name}: gagal membaca {self.path}: {exc}") from exc
        result = []
        for no, line in enumerate(lines, 1):
            if not line.strip():
                continue
            try:
                result.append(Checkpoint.from_dict(json.loads(line)))
            except (ValueError, AnchorError) as exc:
                raise AnchorError(f"{self.name}: baris {no} rusak: {exc}") from exc
        return result


class HttpAnchorBackend(AnchorBackend):
    """POST checkpoint (JSON) ke `url`; GET `url` mengembalikan list checkpoint (JSON array).

    OPTIONS: url, token_env (nama env var berisi bearer token, opsional), timeout (detik, default 10).
    """

    def _request(self, method, body=None):
        headers = {"Content-Type": "application/json", "Accept": "application/json"}
        token_env = self.options.get("token_env")
        if token_env and os.environ.get(token_env):
            headers["Authorization"] = f"Bearer {os.environ[token_env]}"
        req = urllib.request.Request(self.options["url"], data=body, method=method, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=self.options.get("timeout", 10)) as resp:
                return resp.read().decode("utf-8")
        except OSError as exc:              # URLError / HTTPError / timeout
            raise AnchorError(f"{self.name}: {method} {self.options['url']} gagal: {exc}") from exc

    def publish(self, checkpoint):
        body = json.dumps(checkpoint.to_dict(), sort_keys=True).encode("utf-8")
        return self._request("POST", body).strip()[:2000] or f"http:{self.options['url']}"

    def checkpoints(self):
        try:
            data = json.loads(self._request("GET") or "[]")
        except ValueError as exc:
            raise AnchorError(f"{self.name}: respons bukan JSON: {exc}") from exc
        if not isinstance(data, list):
            raise AnchorError(f"{self.name}: respons harus JSON array")
        return [Checkpoint.from_dict(item) for item in data]


def get_backends():
    """Instans backend dari settings.LEDGER_ANCHOR_BACKENDS (list kosong = anchoring nonaktif)."""
    backends = []
    for i, conf in enumerate(getattr(settings, "LEDGER_ANCHOR_BACKENDS", None) or []):
        cls = import_string(conf["BACKEND"])
        backends.append(cls(conf.get("NAME") or f"anchor{i}", **(conf.get("OPTIONS") or {})))
    return backends
