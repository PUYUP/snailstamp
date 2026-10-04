from django.apps import AppConfig


class LedgerConfig(AppConfig):
    name = "snailstamp.apps.ledger"
    label = "Ledger"

    def ready(self) -> None:
        pass
