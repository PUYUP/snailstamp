from django.contrib import admin
from snailstamp.apps.tenant.models import (
    User,
    Issuer,
    Member,
    Owner,
    Invitation
)

admin.site.register(User)
admin.site.register(Issuer)
admin.site.register(Member)
admin.site.register(Owner)
admin.site.register(Invitation)