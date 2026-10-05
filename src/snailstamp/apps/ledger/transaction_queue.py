"""Opt-in transaction queue. Existing ``services`` calls remain synchronous and unchanged.

Use ``enqueue`` at an API boundary, then run ``manage.py process_ledger_queue`` from one or
more workers. Queue execution and the ledger mutation commit in the same DB transaction.
"""
from django.conf import settings
from django.db import transaction
from datetime import timedelta
from uuid import UUID

from django.utils import timezone

from . import services
from .models import QueuedTransaction

_OPERATIONS = {
    "send": {"collection_id", "payload"},
    "claim_transfer": {"token"},
    "cancel_send": {"collection_id"},
    "assign": {"collection_id", "new_holder_id"},
    "use": {"collection_id", "action_code", "payload"},
    "act": {"action_code", "tool_id", "target_id", "content", "content_hash", "payload"},
}
_REQUIRED = {
    "send": {"collection_id"}, "claim_transfer": {"token"},
    "cancel_send": {"collection_id"}, "assign": {"collection_id"},
    "use": {"collection_id"}, "act": {"action_code", "tool_id", "target_id"},
}


class QueueRateLimited(Exception):
    """The association has exceeded its configured transaction enqueue allowance."""


def _json_safe(value):
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, bytes):
        return {"__ledger_bytes_hex__": value.hex()}
    if isinstance(value, dict):
        return {str(k): _json_safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(v) for v in value]
    return value


def _restore(value):
    if isinstance(value, dict) and set(value) == {"__ledger_bytes_hex__"}:
        return bytes.fromhex(value["__ledger_bytes_hex__"])
    if isinstance(value, dict):
        return {k: _restore(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_restore(v) for v in value]
    return value


def enqueue(operation, association_id, member_id, *, priority=0, **payload):
    """Validate and queue an operation; priority is 0..100, with larger values first."""
    if operation not in _OPERATIONS:
        raise services.InvalidInput(f"operasi antrean tidak didukung: {operation!r}")
    if not isinstance(priority, int) or not 0 <= priority <= 100:
        raise services.InvalidInput("priority harus 0..100")
    unknown = payload.keys() - _OPERATIONS[operation]
    if unknown:
        raise services.InvalidInput(f"parameter tidak dikenal: {', '.join(sorted(unknown))}")
    missing = _REQUIRED[operation] - payload.keys()
    if missing:
        raise services.InvalidInput(f"parameter wajib belum diisi: {', '.join(sorted(missing))}")
    services._check_member(association_id, member_id)

    limit = getattr(settings, "LEDGER_QUEUE_RATE_LIMIT", 60)
    period = getattr(settings, "LEDGER_QUEUE_RATE_PERIOD_SECONDS", 60)
    # Serialize concurrent enqueues for the same association on PostgreSQL.
    from django.apps import apps
    Association = apps.get_model("tenant", "Association")
    with transaction.atomic():
        Association.objects.select_for_update().get(pk=association_id)
        since = timezone.now() - timedelta(seconds=period)
        recent = QueuedTransaction.objects.filter(association_id=association_id,
                                                   created_at__gte=since).count()
        if limit > 0 and recent >= limit:
            raise QueueRateLimited("batas transaksi per association terlampaui")
        return QueuedTransaction.objects.create(
            association_id=association_id, member_id=member_id,
            operation=operation, payload=_json_safe(payload), priority=priority,
        )


def _execute(item):
    common = (item.association_id, item.member_id)
    p = _restore(item.payload)
    if item.operation == "send":
        return services.send(p["collection_id"], *common, payload=p.get("payload"))
    if item.operation == "claim_transfer":
        return services.claim_transfer(p["token"], *common)
    if item.operation == "cancel_send":
        return services.cancel_send(p["collection_id"], *common)
    if item.operation == "assign":
        return services.assign(p["collection_id"], *common, new_holder_id=p.get("new_holder_id"))
    if item.operation == "use":
        return services.use(p["collection_id"], *common, action_code=p.get("action_code"),
                            payload=p.get("payload"))
    if item.operation == "act":
        return services.act(p["action_code"], p["tool_id"], p["target_id"], *common,
                            content=p.get("content"), content_hash=p.get("content_hash"),
                            payload=p.get("payload"))
    raise services.InvalidInput(f"operasi antrean tidak didukung: {item.operation!r}")


def process_batch(batch_size=100):
    """Process at most ``batch_size`` queued commands by priority then FIFO.

    Each command and its status update share a transaction. A failed command is rolled back
    and marked failed separately; it will not be silently replayed.
    """
    if not 1 <= batch_size <= 1000:
        raise ValueError("batch_size harus 1..1000")
    outcomes = []
    for _ in range(batch_size):
        item = None
        try:
            with transaction.atomic():
                item = (QueuedTransaction.objects.select_for_update(skip_locked=True)
                        .filter(status=QueuedTransaction.Status.PENDING)
                        .order_by("-priority", "created_at", "id").first())
                if item is None:
                    break
                result = _execute(item)
                item.status = QueuedTransaction.Status.SUCCEEDED
                item.result = _json_safe(result)
                item.finished_at = timezone.now()
                item.save(update_fields=["status", "result", "finished_at"])
            outcomes.append((item.pk, "succeeded", ""))
        except Exception as exc:
            if item is None:
                raise
            QueuedTransaction.objects.filter(pk=item.pk, status=QueuedTransaction.Status.PENDING).update(
                status=QueuedTransaction.Status.FAILED, error=str(exc)[:4000], finished_at=timezone.now())
            outcomes.append((item.pk, "failed", str(exc)))
    return outcomes
