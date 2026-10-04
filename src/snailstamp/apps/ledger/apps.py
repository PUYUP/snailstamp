from django.apps import AppConfig


class LedgerConfig(AppConfig):
    name = "snailstamp.apps.ledger"
    label = "ledger"

    def ready(self) -> None:
        from . import checks  # noqa: F401  (daftarkan system check)
