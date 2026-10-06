import organizations.admin  # pastikan sudah terdaftar sebelum di-unregister
from django.contrib import admin
from django.contrib.auth.models import User
from snailstamp.apps.tenant.models import (
    Association,
    Member,
    Owner,
    Invitation,
)


class OwnerInline(admin.StackedInline):
    model = Owner


class AssociationAdmin(admin.ModelAdmin):
    model = Association
    # inlines = [OwnerInline]


admin.site.register(Association, AssociationAdmin)
admin.site.register(Member)
admin.site.register(Owner)
admin.site.register(Invitation)

# Remove organizations models from django admin
admin.site.unregister(organizations.models.Organization)
admin.site.unregister(organizations.models.OrganizationUser)
admin.site.unregister(organizations.models.OrganizationOwner)
admin.site.unregister(organizations.models.OrganizationInvitation)

# Remove default Django User if it exists (we use custom User)
try:
    admin.site.unregister(User)
except admin.sites.NotRegistered:
    pass
