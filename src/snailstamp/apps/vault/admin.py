from django.contrib import admin
from snailstamp.apps.vault.models import (
    CollectionType,
    Collection,
    CollectionSignature,
)

admin.site.register(CollectionType)
admin.site.register(Collection)
admin.site.register(CollectionSignature)