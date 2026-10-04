import time

from django.core.management.base import BaseCommand, CommandError

from snailstamp.apps.ledger import services as svc

from ._sealer_keys import add_key_source_arguments, load_private_key


class Command(BaseCommand):
    help = ("Co-sign blok yang sudah disegel memakai key sealer region ini (multi-region sealer). "
            "Jalankan satu proses per sealer/region.")

    def add_arguments(self, parser):
        add_key_source_arguments(parser)
        parser.add_argument("--once", action="store_true", help="tandatangani yang tertunda lalu berhenti")
        parser.add_argument("--sleep", type=float, default=5.0)
        parser.add_argument("--limit", type=int, default=100)

    def handle(self, *args, **opts):
        private_key = load_private_key(opts)
        actor = {"user_id": "cosign_ledger"}
        while True:
            pending = list(svc.pending_cosign_blocks(opts["sealer_id"], limit=opts["limit"]))
            signed = 0
            for block in pending:
                try:
                    sig = svc.cosign_block(block.block_no, opts["sealer_id"], private_key, actor_info=actor)
                except svc.InvalidState as exc:
                    self.stderr.write(f"blok #{block.block_no}: {exc}")
                    continue
                except svc.LedgerError as exc:
                    raise CommandError(str(exc)) from exc
                signed += 1
                self.stdout.write(f"co-sign blok #{block.block_no} region={sig.region or '-'}")
            if signed and len(pending) == opts["limit"]:
                continue
            if opts["once"]:
                return
            time.sleep(opts["sleep"])
