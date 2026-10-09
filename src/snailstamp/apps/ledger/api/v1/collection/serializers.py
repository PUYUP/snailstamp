from rest_framework import serializers
from snailstamp.apps.ledger.models_content import Content


class CreateCollectionSerializer(serializers.Serializer):
    entry_id = serializers.IntegerField(required=True, help_text="ID dari Entry induk (misal ID 'Mesin Cetak')")
    body = serializers.CharField(required=True, style={'base_template': 'textarea.html'})
    format = serializers.ChoiceField(choices=Content.TextFormat.choices, default=Content.TextFormat.PLAIN)
    metadata = serializers.JSONField(required=False, allow_null=True)
