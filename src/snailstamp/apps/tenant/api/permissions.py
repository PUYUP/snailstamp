from rest_framework.exceptions import PermissionDenied

from snailstamp.apps.tenant.models import Member


def require_member(user, association_id, member_id):
    """Return an active membership only when it belongs to the authenticated user."""
    try:
        return Member.objects.select_related("organization").get(
            pk=member_id,
            user=user,
            organization_id=association_id,
            deleted_at__isnull=True,
            organization__deleted_at__isnull=True,
        )
    except Member.DoesNotExist:
        raise PermissionDenied("member bukan milik user atau association ini") from None


def require_association_member(user, association_id):
    if not Member.objects.filter(
        user=user,
        organization_id=association_id,
        deleted_at__isnull=True,
        organization__deleted_at__isnull=True,
    ).exists():
        raise PermissionDenied("user bukan anggota association ini")
