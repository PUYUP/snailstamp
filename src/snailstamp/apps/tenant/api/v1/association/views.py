from django.core.exceptions import PermissionDenied
from rest_framework.decorators import permission_classes
from drf_spectacular.utils import extend_schema
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView
from rest_framework import generics

from snailstamp.apps.tenant.models import Member, Association
from django.utils.translation import gettext_lazy as _
from django.db import transaction

from .serializers import (
    MembershipSerializer,
    AssociationCreateSerializer
)


class MyAssociationsView(APIView):
    permission_classes = [IsAuthenticated]

    @extend_schema(responses={200: MembershipSerializer(many=True)})
    def get(self, request, *args, **kwargs):
        memberships = (Member.objects.select_related("organization")
                       .filter(user=request.user, deleted_at__isnull=True,
                               organization__deleted_at__isnull=True)
                       .order_by("organization__name"))
        return Response([{
            "association_id": str(member.organization_id),
            "association_name": member.organization.name,
            "member_id": str(member.pk),
            "is_admin": member.is_admin,
        } for member in memberships])


class ListCreateAssociationView(generics.ListCreateAPIView):
    queryset = Association.objects.all()
    serializer_class = AssociationCreateSerializer
    permission_classes = (IsAuthenticated,)

    @transaction.atomic
    def perform_create(self, serializer):
        instance = serializer.save()
        instance.add_user(self.request.user, is_admin=True)
        return instance

    def get_queryset(self):
        return super().get_queryset().prefetch_related("users")

    def filter_queryset(self, queryset):
        # only member can see associations
        queryset = queryset.filter(users__in=[self.request.user])
        return queryset


class RetrieveUpdateDestroyAssociationView(generics.RetrieveUpdateDestroyAPIView):
    queryset = Association.objects.all()
    serializer_class = AssociationCreateSerializer
    permission_classes = (IsAuthenticated,)

    def perform_update(self, serializer):
        # only admin can update
        if serializer.instance.users.filter(id__in=[self.request.user.id], tenant_member__is_admin=False).exists():
            raise PermissionDenied
        serializer.save()

    def perform_destroy(self, instance):
        # only admin can delete
        if instance.users.filter(id__in=[self.request.user.id], tenant_member__is_admin=False).exists():
            raise PermissionDenied
        instance.delete()
