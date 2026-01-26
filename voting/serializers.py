from rest_framework import serializers
from .models import Vote, Participant, Round, Campaign


class ParticipantSerializer(serializers.ModelSerializer):
    class Meta:
        model = Participant
        fields = ["id", "full_name", "description"]


class SimpleVoteSerializer(serializers.ModelSerializer):
    participant_name = serializers.CharField(source="participant.full_name", read_only=True)

    class Meta:
        model = Vote
        fields = ["id", "participant", "participant_name", "user_telegram_id", "created_at"]


class VoteCreateSerializer(serializers.ModelSerializer):
    class Meta:
        model = Vote
        fields = ["round", "participant", "user_telegram_id"]

    def validate(self, data):
        round_obj = data["round"]
        if round_obj.status != "active":
            raise serializers.ValidationError("Раунд не активен")
        if Vote.objects.filter(round=round_obj, user_telegram_id=data["user_telegram_id"]).exists():
            raise serializers.ValidationError("Вы уже голосовали в этом раунде")
        return data


class WinnerSerializer(serializers.Serializer):
    participant_id = serializers.IntegerField()
    full_name = serializers.CharField()
    votes = serializers.IntegerField()

class CampaignSerializer(serializers.ModelSerializer):
    class Meta:
        model = Campaign
        fields = ['id', 'name', 'admin_telegram_id', 'is_active']