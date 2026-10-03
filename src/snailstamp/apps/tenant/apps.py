from django.apps import AppConfig


class TenantConfig(AppConfig):
    name = 'snailstamp.apps.tenant'

    def ready(self) -> None:
        import snailstamp.apps.tenant.signals
