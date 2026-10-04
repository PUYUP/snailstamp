import os
import time

from django.core.management.base import BaseCommand, CommandError

from snailstamp.apps.ledger.services import seal_next_block


def _sealer_keys(specs):
    """--sealer SEALER_ID:ENV_VAR -> [{"sealer_id": ..., "private_key": ...}]"""
    keys = []
    for spec in specs:
        sealer_id, sep, env_var = spec.partition(":")
        if not sep or not os.environ.get(env_var):
            raise CommandError(f"--sealer {spec!r}: format SEALER_ID:ENV_VAR dan env var harus terisi")
        keys.append({"sealer_id": sealer_id, "private_key": os.environ[env_var].strip()})
    return keys or None


class Command(BaseCommand):
    help = "Segel blok ledger (Merkle root per jendela waktu). Jalankan sebagai service tunggal."

    def add_arguments(self, parser):
        parser.add_argument("--once", action="store_true", help="kejar ketertinggalan lalu berhenti")
        parser.add_argument("--sleep", type=float, default=2.0)
        parser.add_argument("--sealer", action="append", default=[], metavar="SEALER_ID:ENV_VAR",
                            help="tandatangani blok dengan key sealer dari env var (boleh diulang)")

    def handle(self, *args, **opts):
        keys = _sealer_keys(opts["sealer"])
        while True:
            sealed = seal_next_block(sealer_private_keys=keys)
            if sealed:
                self.stdout.write(f"blok #{sealed.block_no} log={sealed.log_count} "
                                  f"{sealed.window_start:%H:%M:%S}-{sealed.window_end:%H:%M:%S}")
                continue                      # masih ada jendela tertinggal -> lanjut tanpa tidur
            if opts["once"]:
                return
            time.sleep(opts["sleep"])
