import json
import time

from django.core.management.base import BaseCommand, CommandError

from snailstamp.apps.ledger import anchors
from snailstamp.apps.ledger import services as svc


class Command(BaseCommand):
    help = "Terbitkan checkpoint blok ke backend anchor eksternal (settings.LEDGER_ANCHOR_BACKENDS)."

    def add_arguments(self, parser):
        parser.add_argument("--once", action="store_true", help="anchor sekali lalu berhenti")
        parser.add_argument("--force", action="store_true", help="abaikan LEDGER_ANCHOR_INTERVAL")
        parser.add_argument("--sleep", type=float, default=60.0)

    def handle(self, *args, **opts):
        if not anchors.get_backends():
            raise CommandError("LEDGER_ANCHOR_BACKENDS kosong; tidak ada tujuan anchor")
        while True:
            try:
                created = svc.anchor_blocks(force=opts["force"])
            except (svc.LedgerError, anchors.AnchorError) as exc:
                raise CommandError(str(exc)) from exc
            for anchor in created:
                self.stdout.write(json.dumps({"block_no": anchor.block_no, "block_hash": anchor.block_hash,
                                              "backend": anchor.backend, "receipt": anchor.receipt}))
            if opts["once"]:
                return
            time.sleep(opts["sleep"])
