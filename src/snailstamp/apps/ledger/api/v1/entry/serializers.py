from rest_framework import serializers
from rest_framework.exceptions import PermissionDenied
from snailstamp.apps.ledger.models import Entry
from snailstamp.apps.tenant.models import Member


class BaseEntrySerializer(serializers.ModelSerializer):
    class Meta:
        model = Entry
        fields = "__all__"


class CreateEntrySerializer(serializers.Serializer):
    issuer_id = serializers.UUIDField(required=True)
    reason = serializers.CharField(max_length=255)
    supply = serializers.IntegerField(min_value=1)
    metadata = serializers.JSONField(required=False, default=dict)
    kind = serializers.IntegerField()

    class Meta(BaseEntrySerializer.Meta):
        fields = [
            'issuer_id',
            'reason',
            'supply',
            'metadata',
            'kind', # an integer representing the kind of entry
        ]

    def validate(self, attrs):
        request = self.context["request"]
        member = Member.objects.filter(
            user_id=request.user.id,
            organization_id=attrs["issuer_id"],
        ).first()
        if member is None:
            raise PermissionDenied("Member is not available to this user")

        attrs["issuer_member_id"] = member.id
        return attrs


class UpdateEntrySerializer(serializers.Serializer):
    reason = serializers.CharField(max_length=255)
    supply = serializers.IntegerField(min_value=1)
    metadata = serializers.JSONField(required=False, default=dict)
    kind = serializers.IntegerField()

    class Meta(BaseEntrySerializer.Meta):
        fields = [
            'reason',
            'supply',
            'metadata',
            'kind', # an integer representing the kind of entry
        ]

    def validate(self, attrs):
        attrs['issuer_id'] = self.instance.issuer_id
        attrs['issuer_member_id'] = self.instance.issuer_member_id
        return attrs
