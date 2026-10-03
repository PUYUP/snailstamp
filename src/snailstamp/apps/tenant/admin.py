import organizations
from django.contrib import admin
from snailstamp.apps.tenant.models import (
    User,
    Association,
    Member,
    Owner,
    Invitation,
    Certificate,
)


class OwnerInline(admin.StackedInline):
    model = Owner


class AssociationAdmin(admin.ModelAdmin):
    model = Association
    # inlines = [OwnerInline]


admin.site.register(User)
admin.site.register(Association, AssociationAdmin)
admin.site.register(Member)
admin.site.register(Owner)
admin.site.register(Invitation)
admin.site.register(Certificate)

# Remove organizations models from django admin
admin.site.unregister(organizations.models.Organization)
admin.site.unregister(organizations.models.OrganizationUser)
admin.site.unregister(organizations.models.OrganizationOwner)
admin.site.unregister(organizations.models.OrganizationInvitation)
