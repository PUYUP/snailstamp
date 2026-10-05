from celery import shared_task
from django.conf import settings

from .transaction_queue import process_batch


@shared_task(name="snailstamp.ledger.process_queue_batch", ignore_result=True)
def process_queue_batch(batch_size=None):
    """Drain a bounded batch of pending DB transactions from a Celery worker."""
    if batch_size is None:
        batch_size = getattr(settings, "LEDGER_QUEUE_BATCH_SIZE", 100)
    return process_batch(batch_size)
