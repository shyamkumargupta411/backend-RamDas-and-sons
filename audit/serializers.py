from rest_framework import serializers


class AuditLogSerializer(serializers.Serializer):
    """Flattened audit log entry across all history-tracked models."""

    id = serializers.CharField(source="history_id")
    model = serializers.CharField()
    object_id = serializers.IntegerField()
    object_repr = serializers.CharField()
    action_type = serializers.CharField(source="history_type")
    action = serializers.SerializerMethodField()
    user = serializers.CharField(source="history_user.username", default=None)
    user_id = serializers.IntegerField(source="history_user_id", default=None)
    timestamp = serializers.DateTimeField(source="history_date")
    changes = serializers.SerializerMethodField()

    ACTION_MAP = {"+": "Created", "~": "Updated", "-": "Deleted"}

    def get_action(self, obj):
        return self.ACTION_MAP.get(obj.history_type, obj.history_type)

    def get_changes(self, obj):
        prev = obj.prev_record
        if prev is None:
            return None
        delta = obj.diff_against(prev)
        return {
            "added": delta.added,
            "removed": delta.removed,
            "changed": delta.changed,
        }