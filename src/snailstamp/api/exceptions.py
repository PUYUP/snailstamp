from rest_framework.exceptions import NotFound, PermissionDenied, Throttled, ValidationError
from rest_framework.views import exception_handler as drf_exception_handler

from snailstamp.apps.ledger import services
from snailstamp.apps.ledger.transaction_queue import QueueRateLimited


def exception_handler(exc, context):
    if isinstance(exc, QueueRateLimited):
        exc = Throttled(detail=str(exc))
    elif isinstance(exc, services.NotFound):
        exc = NotFound(str(exc))
    elif isinstance(exc, services.Forbidden):
        exc = PermissionDenied(str(exc))
    elif isinstance(exc, services.InvalidInput):
        exc = ValidationError(str(exc))
    elif isinstance(exc, services.InvalidState):
        exc = ValidationError(str(exc), code="invalid_state")
    return drf_exception_handler(exc, context)
