import json

from django.core.management.base import BaseCommand

from snailstamp.apps.ledger import services as svc


class Command(BaseCommand):
    help = ("Cetak {public_key: region} sealer tepercaya (belum di-revoke) sebagai JSON, untuk dipin "
            "light client dan dipakai di verify_proof(proof, trusted_keys=...).")

    def handle(self, *args, **opts):
        self.stdout.write(json.dumps(svc.trusted_sealer_keys(), indent=2, sort_keys=True))
