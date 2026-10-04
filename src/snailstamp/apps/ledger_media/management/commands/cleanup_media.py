from django.core.management.base import BaseCommand

from ... import services


class Command(BaseCommand):
    help = ("Tandai FAILED unggahan yang tak pernah difinalisasi (melewati PENDING_TTL) dan hapus sisa objeknya "
            "di S3, termasuk multipart yang menggantung. Jalankan berkala (cron), mis. tiap jam. "
            "Pasang juga lifecycle rule S3 pada prefix uploads/ sebagai jaring pengaman (lihat README).")

    def handle(self, *args, **opts):
        self.stdout.write(f"dibersihkan={services.expire_pending()}")
