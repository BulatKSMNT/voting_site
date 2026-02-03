# voting/serializers.py (обновлённый)
from rest_framework import serializers
from .models import Vote, Participant, Round, Campaign

class ParticipantSerializer(serializers.ModelSerializer):
    order_number = serializers.IntegerField(read_only=True)

    class Meta:
        model = Participant
        fields = ["id", "order_number", "full_name", "description"]

class VoteCreateSerializer(serializers.ModelSerializer):
    choice = serializers.ChoiceField(choices=Vote.VOTE_CHOICES, required=False, allow_null=True)

    class Meta:
        model = Vote
        fields = ["round", "participant", "user_telegram_id", "choice"]

    def validate(self, data):
        round_obj = data["round"]
        if round_obj.status != "active":
            raise serializers.ValidationError("Раунд не активен")
        if Vote.objects.filter(
            round=round_obj,
            user_telegram_id=data["user_telegram_id"],
            participant=data["participant"]
        ).exists():
            raise serializers.ValidationError("Вы уже проголосовали за этого участника в этом раунде. Один голос на участника!")
        if round_obj.type == "individual":
            if "choice" not in data or data["choice"] not in ["yes", "no"]:
                raise serializers.ValidationError("Для индивидуального раунда требуется choice: 'yes' или 'no'")
        else:
            if "choice" in data and data["choice"]:
                raise serializers.ValidationError("Для стандартного раунда choice не требуется")
        return data

class CampaignSerializer(serializers.ModelSerializer):
    order_number = serializers.IntegerField(read_only=True)

    class Meta:
        model = Campaign
        fields = ["id", "order_number", "name", "admin_telegram_id", "is_active"]

class RoundSerializer(serializers.ModelSerializer):
    campaign_name = serializers.CharField(source="campaign.name", read_only=True)
    campaign_order_number = serializers.IntegerField(source="campaign.order_number", read_only=True)
    type = serializers.CharField(read_only=True)

    class Meta:
        model = Round
        fields = ["id", "number", "campaign_name", "campaign_order_number", "status", "started_at", "ended_at", "is_current", "winners_count", "type"]

class StartRoundSerializer(serializers.Serializer):
    campaign_id = serializers.IntegerField(required=True)
    number = serializers.IntegerField(required=False, min_value=1)
    winners_count = serializers.IntegerField(required=False, min_value=1, default=3)
    type = serializers.ChoiceField(choices=Round.ROUND_TYPES, default="standard")

class EndRoundSerializer(serializers.Serializer):
    round_id = serializers.IntegerField(required=True)

class TransferWinnersSerializer(serializers.Serializer):
    round_id = serializers.IntegerField(required=True)
    target_round_id = serializers.IntegerField(required=True)