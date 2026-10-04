from pathlib import Path

from django.core.management.base import BaseCommand, CommandError

from snailstamp.apps.ledger import services as svc

from ._sealer_keys import add_key_source_arguments, load_private_key, read_shares


class Command(BaseCommand):
    help = "Shamir's Secret Sharing untuk private key sealer: split (pecah) / check (uji rekonstruksi)."

    def add_arguments(self, parser):
        sub = parser.add_subparsers(dest="action", required=True)
        split = sub.add_parser("split", help="pecah private key menjadi N share (butuh K)")
        add_key_source_arguments(split)
        split.add_argument("--shares", type=int, required=True, help="jumlah share (N)")
        split.add_argument("--threshold", type=int, required=True, help="share minimum (K)")
        split.add_argument("--out-dir", help="tulis satu file per share (mode 0600) alih-alih stdout")

        check = sub.add_parser("check", help="uji bahwa share cukup & cocok dengan public key sealer")
        check.add_argument("--sealer-id", required=True)
        check.add_argument("--share-file", action="append", required=True)

    def handle(self, *args, **opts):
        actor = {"user_id": "sealer_shamir"}
        try:
            if opts["action"] == "split":
                self._split(opts, actor)
            else:
                svc.recover_sealer_private_key(opts["sealer_id"], read_shares(opts["share_file"]),
                                               actor_info=actor)
                self.stdout.write(self.style.SUCCESS("OK: share merekonstruksi private key sealer"))
        except svc.LedgerError as exc:
            raise CommandError(str(exc)) from exc

    def _split(self, opts, actor):
        shares = svc.split_sealer_private_key(opts["sealer_id"], load_private_key(opts),
                                              opts["shares"], opts["threshold"], actor_info=actor)
        if not opts["out_dir"]:
            for share in shares:
                self.stdout.write(share)
            return
        out = Path(opts["out_dir"])
        out.mkdir(parents=True, exist_ok=True)
        for i, share in enumerate(shares, 1):
            path = out / f"sealer-{opts['sealer_id']}-share-{i}.txt"
            path.touch(mode=0o600, exist_ok=False)
            path.write_text(share + "\n")
            self.stdout.write(str(path))
