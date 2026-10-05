from .models import Member


def member_can_act(association_id, member_id):
    """Ledger authorization adapter for django-organizations' organization field."""
    return Member.objects.filter(
        pk=member_id,
        organization_id=association_id,
        deleted_at__isnull=True,
        organization__deleted_at__isnull=True,
    ).exists()
