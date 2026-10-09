from celery import shared_task
from django.conf import settings
from django.db import connection

from .transaction_queue import process_batch


@shared_task(name="snailstamp.ledger.process_queue_batch", ignore_result=True)
def process_queue_batch(batch_size=None):
    """Drain a bounded batch of pending DB transactions from a Celery worker."""
    if batch_size is None:
        batch_size = getattr(settings, "LEDGER_QUEUE_BATCH_SIZE", 100)
    return process_batch(batch_size)


@shared_task(name="snailstamp.ledger.refresh_ledger_snapshots", ignore_result=True)
def refresh_ledger_snapshots():
    """
    Task background untuk memperbarui materialized view ledger_collection_snapshots
    secara aman tanpa memblokir proses baca (menggunakan CONCURRENTLY).
    """
    # Menggunakan connection.cursor() karena REFRESH MATERIALIZED VIEW 
    # adalah perintah DDL/utility PostgreSQL yang jarang dibungkus ORM secara langsung.
    with connection.cursor() as cursor:
        cursor.execute("REFRESH MATERIALIZED VIEW CONCURRENTLY ledger_collection_snapshots;")
    
    return "Ledger snapshots refreshed successfully."
