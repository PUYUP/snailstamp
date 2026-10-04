import json

from django.core.management.base import BaseCommand, CommandError

from snailstamp.apps.ledger import services as svc


class Command(BaseCommand):
    help = ("Bandingkan checkpoint di backend anchor eksternal dengan blok di DB. "
            "Exit code != 0 bila ada fork / rewrite / rollback.")

    def add_arguments(self, parser):
        parser.add_argument("--skip-chain", action="store_true",
                            help="jangan jalankan verify_blocks() atas seluruh chain (lebih cepat)")

    def handle(self, *args, **opts):
        findings = svc.detect_forks(verify_chain=not opts["skip_chain"])
        for f in findings:
            self.stderr.write(json.dumps(f.to_dict()))
        if findings:
            raise CommandError(f"{len(findings)} temuan fork/integritas")
        self.stdout.write("OK: DB konsisten dengan semua checkpoint")
