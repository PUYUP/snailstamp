"""Sumber private key sealer untuk management command: env var atau file share Shamir."""
import os
from pathlib import Path

from django.core.management.base import CommandError

from snailstamp.apps.ledger import services as svc


def add_key_source_arguments(parser):
    parser.add_argument("--sealer-id", required=True, help="UUID SealerKey")
    src = parser.add_mutually_exclusive_group(required=True)
    src.add_argument("--key-env", help="nama env var berisi private key hex")
    src.add_argument("--share-file", action="append", default=[],
                     help="file berisi share Shamir (boleh diulang; boleh banyak share per file)")


def read_shares(paths):
    shares = []
    for path in paths:
        shares += [line.strip() for line in Path(path).read_text().splitlines() if line.strip()]
    return shares


def load_private_key(opts):
    """Return private key hex. Dari share: direkonstruksi di memori, dicatat KEY_RECOVERED."""
    if opts.get("key_env"):
        value = os.environ.get(opts["key_env"])
        if not value:
            raise CommandError(f"env var {opts['key_env']} kosong")
        return value.strip()
    try:
        return svc.recover_sealer_private_key(opts["sealer_id"], read_shares(opts["share_file"]),
                                              actor_info={"user_id": "management-command"})
    except svc.LedgerError as exc:
        raise CommandError(str(exc)) from exc
