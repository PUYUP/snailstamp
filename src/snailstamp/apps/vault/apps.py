from django.apps import AppConfig


class VaultConfig(AppConfig):
    name = 'snailstamp.apps.vault'

    def ready(self) -> None:
        import snailstamp.apps.vault.signals
