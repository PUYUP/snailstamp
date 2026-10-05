from django.shortcuts import get_object_or_404
from drf_spectacular.utils import OpenApiParameter, OpenApiResponse, extend_schema
from rest_framework import generics, serializers, status
from rest_framework.exceptions import ValidationError
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from snailstamp.apps.ledger import services
from snailstamp.apps.ledger.models import Action, Collection, Kind, QueuedTransaction
from snailstamp.apps.ledger.transaction_queue import enqueue
from snailstamp.apps.tenant.api.permissions import require_association_member, require_member
from snailstamp.apps.tenant.models import Member

from .serializers import (
    ActionSerializer,
    CollectionSerializer,
    KindSerializer,
    LogSerializer,
    QueueTransactionInputSerializer,
    QueuedTransactionSerializer,
)


class KindListView(generics.ListAPIView):
    permission_classes = [IsAuthenticated]
    serializer_class = KindSerializer
    queryset = Kind.objects.order_by("id")
    pagination_class = None


class ActionListView(generics.ListAPIView):
    permission_classes = [IsAuthenticated]
    serializer_class = ActionSerializer
    queryset = Action.objects.order_by("id")
    pagination_class = None


class CollectionListView(generics.ListAPIView):
    permission_classes = [IsAuthenticated]
    serializer_class = CollectionSerializer

    def get_queryset(self):
        association_id = self.request.query_params.get("association_id")
        if not association_id:
            raise ValidationError({"association_id": "parameter ini wajib diisi"})
        association_id = serializers.UUIDField().run_validation(association_id)
        require_association_member(self.request.user, association_id)
        return (Collection.objects.filter(owner_id=association_id)
                .select_related("kind").order_by("-created_at", "-id"))

    @extend_schema(parameters=[OpenApiParameter("association_id", str, required=True,
                                                 description="UUID association pemilik")])
    def get(self, request, *args, **kwargs):
        return super().get(request, *args, **kwargs)


class CollectionHistoryView(APIView):
    permission_classes = [IsAuthenticated]

    @extend_schema(responses={200: LogSerializer(many=True), 404: OpenApiResponse(description="Item not found")})
    def get(self, request, collection_id):
        active_associations = Member.objects.filter(
            user=request.user, deleted_at__isnull=True, organization__deleted_at__isnull=True,
        ).values("organization_id")
        collection = get_object_or_404(
            Collection.objects.only("id", "owner_id").filter(owner_id__in=active_associations),
            pk=collection_id,
        )
        logs = services.item_history(collection_id)
        return Response(LogSerializer(logs, many=True).data)


class QueueTransactionView(APIView):
    permission_classes = [IsAuthenticated]

    @extend_schema(
        request=QueueTransactionInputSerializer,
        responses={202: QueuedTransactionSerializer, 400: OpenApiResponse(description="Invalid request"),
                   403: OpenApiResponse(description="Member is not available to this user"),
                   429: OpenApiResponse(description="Association rate limit exceeded")},
        description=("Queues a ledger operation for asynchronous execution. Status can be checked "
                     "using the returned transaction ID. Priority ranges from 0 to 100."),
    )
    def post(self, request):
        serializer = QueueTransactionInputSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        association_id = data["association_id"]
        member_id = data["member_id"]
        require_member(request.user, association_id, member_id)
        payload = data["payload"]
        self._validate_operation_payload(data["operation"], payload)
        queued = enqueue(data["operation"], association_id, member_id,
                         priority=data["priority"], **payload)
        return Response(QueuedTransactionSerializer(queued).data, status=status.HTTP_202_ACCEPTED)

    @staticmethod
    def _validate_operation_payload(operation, payload):
        required = {
            "create_item": {"reason", "quantity", "kind_code"},
            "send": {"collection_id"},
            "claim_transfer": {"token"},
            "cancel_send": {"collection_id"},
            "assign": {"collection_id"},
            "use": {"collection_id"},
            "act": {"action_code", "tool_id", "target_id"},
        }[operation]
        missing = required - payload.keys()
        if missing:
            raise ValidationError({"payload": f"parameter wajib belum diisi: {', '.join(sorted(missing))}"})


class QueuedTransactionDetailView(APIView):
    permission_classes = [IsAuthenticated]

    @extend_schema(responses={200: QueuedTransactionSerializer, 404: OpenApiResponse(description="Not found")})
    def get(self, request, transaction_id):
        active_associations = Member.objects.filter(
            user=request.user, deleted_at__isnull=True, organization__deleted_at__isnull=True,
        ).values("organization_id")
        item = get_object_or_404(QueuedTransaction.objects.filter(association_id__in=active_associations),
                                 pk=transaction_id)
        return Response(QueuedTransactionSerializer(item).data)
