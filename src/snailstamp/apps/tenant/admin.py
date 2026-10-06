import organizations.admin  # pastikan sudah terdaftar sebelum di-unregister
from django.contrib import admin
from django.contrib.auth.admin import UserAdmin as BaseUserAdmin
from django.contrib.auth.models import User
from snailstamp.apps.tenant.models import (
    Association,
    Member,
    Owner,
    Invitation,
    User as CustomUser,
)


class OwnerInline(admin.StackedInline):
    model = Owner


class AssociationAdmin(admin.ModelAdmin):
    model = Association
    # inlines = [OwnerInline]


class UserAdmin(BaseUserAdmin):
    """Custom User admin with Django's default UserAdmin functionality"""
    list_display = ('username', 'email', 'first_name', 'last_name', 'is_staff')
    list_filter = ('is_staff', 'is_superuser', 'is_active', 'groups')
    search_fields = ('username', 'first_name', 'last_name', 'email')
    ordering = ('username',)
    filter_horizontal = ('groups', 'user_permissions',)

    fieldsets = (
        (None, {'fields': ('username', 'password')}),
        ('Personal info', {'fields': ('first_name', 'last_name', 'email')}),
        ('Permissions', {
            'fields': ('is_active', 'is_staff', 'is_superuser', 'groups', 'user_permissions'),
        }),
        ('Important dates', {'fields': ('last_login', 'date_joined')}),
    )

    add_fieldsets = (
        (None, {
            'classes': ('wide',),
            'fields': ('username', 'email', 'password1', 'password2'),
        }),
    )


admin.site.register(Association, AssociationAdmin)
admin.site.register(Member)
admin.site.register(Owner)
admin.site.register(Invitation)
admin.site.register(CustomUser, UserAdmin)

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
