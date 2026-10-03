from django.contrib import admin
from snailstamp.apps.vault.models import (
    Certificate,
    CollectionType,
    Collection,
)

admin.site.register(Certificate)
admin.site.register(CollectionType)
admin.site.register(Collection)