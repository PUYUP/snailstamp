from rest_framework import generics
from rest_framework.response import Response
from rest_framework.permissions import IsAuthenticated
from django.db import transaction
from django.shortcuts import get_object_or_404
from drf_spectacular.utils import OpenApiExample, OpenApiResponse, extend_schema
from snailstamp.apps.ledger.services import create_entry, update_entry
from snailstamp.apps.ledger.models import Entry
from .serializers import CreateEntrySerializer, UpdateEntrySerializer, BaseEntrySerializer


class ListCreateEntryView(generics.ListCreateAPIView):
    queryset = Entry.objects.all()
    permission_classes = (IsAuthenticated,)
    serializer_class = CreateEntrySerializer

    @extend_schema(
        tags=["Ledger"],
        summary="Create a ledger entry",
        description=(
            "Records a reason and a total supply on behalf of an issuer association. "
            "The call is synchronous: the entry exists as soon as it returns, but it "
            "contains no items yet. Items are created later by minting. The entry's "
            "content hash is computed by the database."
        ),
        request=CreateEntrySerializer,
        responses={
            201: OpenApiResponse(
                response=BaseEntrySerializer,
                description="Entry created. No items exist until minting.",
            ),
            400: OpenApiResponse(
                description="Invalid request body, or the ledger rejected the entry "
                            "(e.g. supply < 1, empty reason, unknown kind)."
            ),
            401: OpenApiResponse(description="Authentication credentials missing or invalid."),
            403: OpenApiResponse(
                description="The member is not available to this user, or the kind is "
                            "restricted to official issuers."
            ),
        },
        examples=[
            OpenApiExample(
                "Print 1000 stickers",
                value={
                    "issuer_id": "3f2b8c1e-5a4d-4e0b-9a7c-1d2e3f4a5b6c",
                    "reason": "Saya mencetak 1000 stiker untuk dijual",
                    "supply": 1000,
                    "kind": 0,
                    "metadata": {},
                },
                request_only=True,
            ),
            OpenApiExample(
                "Created",
                value={"entry_id": "42"},
                response_only=True,
                status_codes=["201"],
            ),
        ],
    )
    @transaction.atomic
    def create(self, request, *args, **kwargs):
        serializer = CreateEntrySerializer(data=request.data, context={"request": request})
        serializer.is_valid(raise_exception=True)
        entry = create_entry(**serializer.validated_data)

        # get entry instance to serialize it
        instance = get_object_or_404(Entry, pk=entry)
        entry_serializer = BaseEntrySerializer(instance=instance)
        return Response(entry_serializer.data, status=201)


class RetrieveUpdateEntryView(generics.RetrieveUpdateAPIView):
    queryset = Entry.objects.all()
    permission_classes = (IsAuthenticated,)
    serializer_class = BaseEntrySerializer

    @extend_schema(
            tags=["Ledger"],
            summary="Retrieve or update a ledger entry",
    )
    @transaction.atomic
    def partial_update(self, request, pk=None, *args, **kwargs):
        # check entry exists and is owned by the user
        entry = get_object_or_404(Entry, pk=pk)
        serializer = UpdateEntrySerializer(
            data=request.data,
            instance=entry,
            context={"request": request}
        )
        serializer.is_valid(raise_exception=True)
        entry = update_entry(pk, **serializer.validated_data)
    
        # get entry instance to serialize it
        instance = get_object_or_404(Entry, pk=entry)
        entry_serializer = BaseEntrySerializer(instance=instance)
        return Response(entry_serializer.data, status=201)