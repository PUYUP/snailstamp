import json
import socket
import time
import urllib.request

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from snailstamp.apps.ledger import anchors
from snailstamp.apps.ledger import services as svc


class Command(BaseCommand):
    help = ("Watchtower per region: simpan checkpoint sendiri (LEDGER_WITNESS_BACKENDS), deteksi fork, "
            "dan alert bila sealer mati / co-sign tertinggal. --once: exit code != 0 bila ada temuan.")

    def add_arguments(self, parser):
        parser.add_argument("--once", action="store_true", help="satu putaran (untuk cron) lalu berhenti")
        parser.add_argument("--sleep", type=float, default=60.0)
        parser.add_argument("--verify-every", type=int, default=60,
                            help="jalankan verify_blocks() penuh tiap N putaran (selalu saat --once)")
        parser.add_argument("--webhook", default=None,
                            help="URL alert (POST JSON); default settings.LEDGER_ALERT_WEBHOOK")

    def handle(self, *args, **opts):
        webhook = opts["webhook"] if opts["webhook"] is not None else getattr(settings, "LEDGER_ALERT_WEBHOOK", "")
        backends = anchors.get_backends("LEDGER_WITNESS_BACKENDS")
        interval = getattr(settings, "LEDGER_WITNESS_INTERVAL", 1)
        rounds = 0
        while True:
            full = opts["once"] or rounds % max(1, opts["verify_every"]) == 0
            findings = self._round(backends, interval, full)
            rounds += 1
            for f in findings:
                self.stderr.write(json.dumps(f.to_dict()))
            if findings and webhook:
                self._alert(webhook, findings)
            if opts["once"]:
                if findings:
                    raise CommandError(f"{len(findings)} temuan")
                self.stdout.write("OK")
                return
            time.sleep(opts["sleep"])

    def _round(self, backends, interval, verify_chain):
        findings = []
        if backends:
            try:
                for anchor in svc.anchor_blocks(backends=backends, interval=interval):
                    self.stdout.write(json.dumps({"witness": anchor.backend, "block_no": anchor.block_no,
                                                  "block_hash": anchor.block_hash}))
            except svc.LedgerError as exc:
                findings.append(svc.ForkFinding("witness_refused", None, "", str(exc)))
            except anchors.AnchorError as exc:
                findings.append(svc.ForkFinding("backend_error", None, "", str(exc)))
        findings += svc.detect_forks(backends=backends, verify_chain=verify_chain)
        findings += svc.ledger_health()
        return findings

    def _alert(self, url, findings):
        body = json.dumps({"host": socket.gethostname(), "findings": [f.to_dict() for f in findings]}).encode()
        req = urllib.request.Request(url, data=body, method="POST", headers={"Content-Type": "application/json"})
        try:
            urllib.request.urlopen(req, timeout=10).close()
        except OSError as exc:
            self.stderr.write(f"alert ke {url} gagal: {exc}")
