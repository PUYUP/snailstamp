from django.db.models.signals import post_save
from django.dispatch import receiver
from snailstamp.apps.tenant.signer import sign_collection
from snailstamp.apps.ledger.models import Collection


@receiver(post_save, sender=Collection)
def handle_collection_created(sender, instance, created, **kwargs):
    if created:
        sign_collection(instance)
