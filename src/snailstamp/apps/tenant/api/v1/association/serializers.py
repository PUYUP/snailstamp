from rest_framework import serializers
from snailstamp.apps.tenant.models import Association, Member


class MembershipSerializer(serializers.Serializer):
    association_id = serializers.UUIDField()
    association_name = serializers.CharField()
    member_id = serializers.UUIDField()
    is_admin = serializers.BooleanField()


class AssociationBaseSerializer(serializers.ModelSerializer):
    class Meta:
        model = Association
        fields = '__all__'


class AssociationCreateSerializer(AssociationBaseSerializer):
    class Meta(AssociationBaseSerializer.Meta):
        fields = ['name', 'slug']

    def to_representation(self, instance, *args, **kwargs):
        serializer = AssociationBaseSerializer(
            instance=instance,
            context={'request': self.context.get('request')}
        )
        return serializer.data
