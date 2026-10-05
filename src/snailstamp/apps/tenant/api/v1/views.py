from drf_spectacular.utils import extend_schema
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from snailstamp.apps.tenant.models import Member

from .serializers import MembershipSerializer


class MyAssociationsView(APIView):
    permission_classes = [IsAuthenticated]

    @extend_schema(responses={200: MembershipSerializer(many=True)})
    def get(self, request):
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
