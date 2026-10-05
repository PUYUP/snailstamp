import time

from django.core.management.base import BaseCommand, CommandError

from snailstamp.apps.ledger.transaction_queue import process_batch


class Command(BaseCommand):
    help = "Process queued ledger transactions in priority/FIFO batches."

    def add_arguments(self, parser):
        parser.add_argument("--batch-size", type=int, default=100)
        parser.add_argument("--sleep", type=float, default=1.0,
                            help="idle polling interval in seconds; 0 means one pass")

    def handle(self, *args, **opts):
        if not 1 <= opts["batch_size"] <= 1000:
            raise CommandError("--batch-size harus 1..1000")
        if opts["sleep"] < 0:
            raise CommandError("--sleep tidak boleh negatif")
        while True:
            outcomes = process_batch(opts["batch_size"])
            for tx_id, status, error in outcomes:
                if status == "failed":
                    self.stderr.write(f"tx {tx_id} failed: {error}")
                else:
                    self.stdout.write(f"tx {tx_id} succeeded")
            if opts["sleep"] == 0:
                return
            if not outcomes:
                time.sleep(opts["sleep"])
