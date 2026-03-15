from rest_framework import serializers
from .models import Vote, Participant, Round, Campaign

class ParticipantSerializer(serializers.ModelSerializer):
    class Meta:
        model = Participant
        fields = ["id","round", "order_number", "full_name", "description"]

class VoteCreateSerializer(serializers.ModelSerializer):
    class Meta:
        model = Vote
        fields = ["round", "participant", "user_telegram_id", "choice"]

    def validate(self, data):
        round_obj = data["round"]
        if round_obj.status != "active":
            raise serializers.ValidationError("Раунд не активен. Голосование закрыто.")
        return data

class RoundSerializer(serializers.ModelSerializer):
    campaign_name = serializers.CharField(source="campaign.name", read_only=True)
    campaign_order_number = serializers.IntegerField(source="campaign.order_number", read_only=True)
    number = serializers.IntegerField(required=False)
    participant_name = serializers.SerializerMethodField()

    def get_participant_name(self, obj):
        # Если раунд индивидуальный, вытягиваем имя единственного участника
        if obj.type == "individual":
            p = obj.participants.first()
            return p.full_name if p else "Пусто"
        return None

    class Meta:
        model = Round
        fields = "__all__"

class CampaignSerializer(serializers.ModelSerializer):
    class Meta:
        model = Campaign
        fields = "__all__"
