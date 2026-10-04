from django.apps import AppConfig


class TenantConfig(AppConfig):
    name = 'snailstamp.apps.tenant'
    label = 'tenant'

    def ready(self) -> None:
        pass
