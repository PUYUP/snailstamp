from django.apps import AppConfig


class LedgerConfig(AppConfig):
    name = "snailstamp.apps.ledger"

    def ready(self) -> None:
        pass
