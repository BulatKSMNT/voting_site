from rest_framework import serializers
from .models import Vote, Participant, Round, Campaign


class ParticipantSerializer(serializers.ModelSerializer):
    class Meta:
        model = Participant
        fields = ["id", "round", "order_number", "full_name", "description"]
        read_only_fields = ["id", "order_number"]


class VoteCreateSerializer(serializers.ModelSerializer):
    class Meta:
        model = Vote
        fields = ["round", "participant", "user_telegram_id", "choice"]

    def validate(self, data):
        round_obj = data["round"]
        participant = data["participant"]
        choice = data.get("choice")

        # Нормализуем пустую строку
        if choice == "":
            data["choice"] = None
            choice = None

        if round_obj.status != "active":
            raise serializers.ValidationError("Раунд не активен. Голосование закрыто.")

        if participant.round_id != round_obj.id:
            raise serializers.ValidationError("Участник не принадлежит указанному раунду.")

        if round_obj.type == "individual":
            if choice not in ("yes", "no"):
                raise serializers.ValidationError("Для индивидуального раунда нужно передать choice=yes или choice=no.")
        else:
            # В стандартном раунде бот не передает choice, это считается голосом "за"
            if choice not in (None, "yes"):
                raise serializers.ValidationError("Для стандартного раунда допустим только голос 'за'.")

        return data


class RoundSerializer(serializers.ModelSerializer):
    campaign_name = serializers.CharField(source="campaign.name", read_only=True)
    campaign_order_number = serializers.IntegerField(source="campaign.order_number", read_only=True)
    number = serializers.IntegerField(required=False)
    participant_name = serializers.SerializerMethodField()

    def get_participant_name(self, obj):
        if obj.type == "individual":
            p = obj.participants.first()
            return p.full_name if p else "Пусто"
        return None

    class Meta:
        model = Round
        fields = "__all__"
        read_only_fields = ["started_at", "ended_at"]


class CampaignSerializer(serializers.ModelSerializer):
    class Meta:
        model = Campaign
        fields = "__all__"
        read_only_fields = ["order_number", "created_at"]
