from django.apps import AppConfig


class TenantConfig(AppConfig):
    name = 'snailstamp.apps.tenant'
    label = 'Tenant'

    def ready(self) -> None:
        pass
