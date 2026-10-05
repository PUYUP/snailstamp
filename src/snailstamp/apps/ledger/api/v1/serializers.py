from rest_framework import serializers

from snailstamp.apps.ledger.models import Action, Collection, Kind, Log, QueuedTransaction


class KindSerializer(serializers.ModelSerializer):
    class Meta:
        model = Kind
        fields = ("id", "code", "label", "max_as_tool", "max_as_target", "restricted")


class ActionSerializer(serializers.ModelSerializer):
    class Meta:
        model = Action
        fields = ("id", "code", "label")


class CollectionSerializer(serializers.ModelSerializer):
    entry_id = serializers.IntegerField(read_only=True)
    owner_id = serializers.UUIDField(read_only=True)
    holder_id = serializers.UUIDField(read_only=True, allow_null=True)
    kind_id = serializers.IntegerField(read_only=True)
    kind_code = serializers.CharField(source="kind.code", read_only=True)

    class Meta:
        model = Collection
        fields = ("id", "entry_id", "serial_no", "owner_id", "holder_id", "kind_id",
                  "kind_code", "state", "last_seq", "tool_uses", "target_acts", "created_at",
                  "updated_at")


class LogSerializer(serializers.ModelSerializer):
    collection_id = serializers.IntegerField(read_only=True)
    action_id = serializers.IntegerField(read_only=True, allow_null=True)
    actor_id = serializers.UUIDField(read_only=True)
    actor_member_id = serializers.UUIDField(read_only=True)
    counterparty_id = serializers.UUIDField(read_only=True, allow_null=True)
    hash = serializers.SerializerMethodField()
    content_hash = serializers.SerializerMethodField()

    class Meta:
        model = Log
        fields = ("collection_id", "seq", "event_type", "action_id", "actor_id",
                  "actor_member_id", "counterparty_id", "target_id", "target_seq",
                  "created_at", "hash", "content_hash", "payload")

    def get_hash(self, obj):
        return bytes(obj.hash).hex()

    def get_content_hash(self, obj):
        return bytes(obj.content_hash).hex() if obj.content_hash is not None else None


class QueueTransactionInputSerializer(serializers.Serializer):
    association_id = serializers.UUIDField()
    member_id = serializers.UUIDField()
    operation = serializers.ChoiceField(choices=(
        "create_item", "send", "claim_transfer", "cancel_send", "assign", "use", "act",
    ))
    priority = serializers.IntegerField(min_value=0, max_value=100, default=0)
    payload = serializers.JSONField(default=dict)

    def validate_payload(self, value):
        if not isinstance(value, dict):
            raise serializers.ValidationError("payload harus berupa object JSON")
        return value


class QueuedTransactionSerializer(serializers.ModelSerializer):
    class Meta:
        model = QueuedTransaction
        fields = ("id", "association_id", "member_id", "operation", "payload", "priority",
                  "status", "result", "error", "created_at", "finished_at")
        read_only_fields = fields
