from django.db.models.signals import post_save
from django.dispatch import receiver
from snailstamp.apps.tenant.signer import create_certificate
from snailstamp.apps.tenant.models import Member
from organizations.models import Organization

@receiver(post_save, sender=Member)
def handle_member_created(sender, instance: "Organization", created, **kwargs):
    if created:
        create_certificate(instance.organization, instance)
