from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from ....ledger import services as ledger
from ... import services
from ...models import MediaObject


class Command(BaseCommand):
    help = ("Audit media: cocokkan tiap media READY dengan ledger, dan (--rehash) dengan byte di S3. "
            "Jalankan berkala (cron). Exit code != 0 bila ada ketidakcocokan.")

    def add_arguments(self, parser):
        parser.add_argument("--rehash", action="store_true",
                            help="unduh & hitung ulang sha256 tiap file dari S3 (mahal, tapi mendeteksi file rusak/diganti)")
        parser.add_argument("--limit", type=int, default=0, help="maksimum media yang diperiksa (0 = semua)")
        parser.add_argument("--collection", type=int, help="periksa satu collection saja")

    def handle(self, *args, rehash, limit, collection, **opts):
        qs = MediaObject.objects.filter(status=MediaObject.Status.READY)
        if collection:
            qs = qs.filter(collection_id=collection)
        qs = qs.order_by("last_verified_at", "created_at")      # yang paling lama belum diperiksa lebih dulu
        if limit:
            qs = qs[:limit]

        checked, bad, chains = 0, [], {}
        for media in qs.iterator():
            ok, detail = services.verify_media(media, rehash=rehash)
            if ok:                                                # rantai ledger-nya juga: sekali per collection
                if media.collection_id not in chains:
                    chains[media.collection_id] = ledger.verify_chain(media.collection_id)
                valid, seq, why = chains[media.collection_id]
                if not valid:
                    ok, detail = False, f"hash chain collection rusak di seq {seq}: {why}"
            checked += 1
            if ok:
                MediaObject.objects.filter(pk=media.pk).update(last_verified_at=timezone.now())
            else:
                bad.append((media.id, detail))
                self.stderr.write(f"TIDAK COCOK {media.id} (collection {media.collection_id}): {detail}")

        self.stdout.write(f"diperiksa={checked} bermasalah={len(bad)} rehash={'ya' if rehash else 'tidak'}")
        if bad:
            raise CommandError(f"{len(bad)} media bermasalah")
