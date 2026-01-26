from rest_framework import serializers
from .models import Vote, Participant, Round, Campaign


class ParticipantSerializer(serializers.ModelSerializer):
    order_number = serializers.IntegerField(read_only=True)

    class Meta:
        model = Participant
        fields = ["id", "order_number", "full_name", "description"]


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


class CampaignSerializer(serializers.ModelSerializer):
    order_number = serializers.IntegerField(read_only=True)

    class Meta:
        model = Campaign
        fields = ["id", "order_number", "name", "admin_telegram_id", "is_active"]


class RoundSerializer(serializers.ModelSerializer):
    campaign_name = serializers.CharField(source="campaign.name", read_only=True)
    campaign_order_number = serializers.IntegerField(source="campaign.order_number", read_only=True)

    class Meta:
        model = Round
        fields = ["id", "number", "campaign_name", "campaign_order_number", "status", "started_at", "ended_at"]