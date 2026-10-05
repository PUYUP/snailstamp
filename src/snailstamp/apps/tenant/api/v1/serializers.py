from rest_framework import serializers


class MembershipSerializer(serializers.Serializer):
    association_id = serializers.UUIDField()
    association_name = serializers.CharField()
    member_id = serializers.UUIDField()
    is_admin = serializers.BooleanField()
