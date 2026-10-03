import time

from django.core.management.base import BaseCommand

from snailstamp.apps.ledger.services import seal_next_block


class Command(BaseCommand):
    help = "Segel blok ledger (Merkle root per jendela waktu). Jalankan sebagai service tunggal."

    def add_arguments(self, parser):
        parser.add_argument("--once", action="store_true", help="kejar ketertinggalan lalu berhenti")
        parser.add_argument("--sleep", type=float, default=2.0)

    def handle(self, *args, **opts):
        while True:
            sealed = seal_next_block()
            if sealed:
                self.stdout.write(f"blok #{sealed.block_no} log={sealed.log_count} "
                                  f"{sealed.window_start:%H:%M:%S}-{sealed.window_end:%H:%M:%S}")
                continue                      # masih ada jendela tertinggal -> lanjut tanpa tidur
            if opts["once"]:
                return
            time.sleep(opts["sleep"])
