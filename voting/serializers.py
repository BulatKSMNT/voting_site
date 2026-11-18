from rest_framework import serializers
from rest_framework.exceptions import PermissionDenied
from django.db import transaction
from .access import normalize_telegram_ids, VOTER_NOT_ALLOWED
from .permissions import identity
from .models import Vote, Participant, Round, Campaign


class ParticipantSerializer(serializers.ModelSerializer):
    def validate(self, data):
        rd = data.get("round", getattr(self.instance, "round", None))
        if (rd and rd.event_stage) or (self.instance and self.instance.round.event_stage):
            raise serializers.ValidationError("Состав мероприятия задаётся через /event. Дополнительного финалиста выбирает главный администратор.")
        if rd and rd.type == "individual":
            others = rd.participants.all()
            if self.instance:
                others = others.exclude(pk=self.instance.pk)
            if others.exists():
                raise serializers.ValidationError("В индивидуальном раунде может быть только 1 участник!")
        if self.instance and rd.pk != self.instance.round_id and self.instance.vote_set.exists():
            raise serializers.ValidationError("Участника с голосами переносите через операцию переноса.")
        return data

    @transaction.atomic
    def create(self, validated_data):
        Round.objects.select_for_update().get(pk=validated_data['round'].pk)
        self.validate(validated_data)
        return super().create(validated_data)

    @transaction.atomic
    def update(self, instance, validated_data):
        ids = {instance.round_id, validated_data.get('round', instance.round).pk}
        list(Round.objects.select_for_update().filter(pk__in=ids).order_by('pk'))
        self.validate(validated_data)
        return super().update(instance, validated_data)

    class Meta:
        model = Participant
        fields = ["id", "round", "order_number", "full_name", "description"]
        read_only_fields = ["id", "order_number"]


class VoteCreateSerializer(serializers.ModelSerializer):
    class Meta:
        model = Vote
        fields = ["round", "participant", "user_telegram_id", "choice"]
        validators = []  # отключаем стандартный unique_together validator DRF

    def validate(self, data):
        request = self.context.get("request")
        if request:
            actor = identity(request)
            if actor["source"] == "bot" and actor["actor_id"] != data["user_telegram_id"]:
                raise serializers.ValidationError("Telegram ID не совпадает с отправителем.", code="actor_mismatch")
        round_obj = data["round"]
        participant = data["participant"]
        user_telegram_id = data["user_telegram_id"]
        choice = data.get("choice")

        if choice == "":
            data["choice"] = None
            choice = None

        if round_obj.status != "active":
            raise serializers.ValidationError("Раунд не активен. Голосование закрыто.", code="round_closed")

        if not round_obj.campaign.can_vote(user_telegram_id):
            raise PermissionDenied(VOTER_NOT_ALLOWED, code="voter_not_allowed")

        if participant.round_id != round_obj.id:
            raise serializers.ValidationError("Участник не принадлежит указанному раунду.", code="participant_mismatch")

        if round_obj.type == "individual":
            if choice not in ("yes", "no"):
                raise serializers.ValidationError("Для индивидуального раунда нужно передать choice=yes или choice=no.")
        else:
            if choice not in (None, "yes"):
                raise serializers.ValidationError("Для стандартного раунда допустим только голос «за».")

        # Своя понятная проверка дубликата
        if Vote.objects.filter(
            round=round_obj,
            participant=participant,
            user_telegram_id=user_telegram_id
        ).exists():
            raise serializers.ValidationError("Вы уже проголосовали за этого участника.", code="duplicate")

        if round_obj.event_stage == 'qualification':
            raise serializers.ValidationError("В первом туре голосуйте за отдельные выступления.", code="round_closed")
        if round_obj.type == 'standard' and Vote.objects.filter(round=round_obj, user_telegram_id=user_telegram_id).count() >= round_obj.max_choices:
            raise serializers.ValidationError(
                f"Можно выбрать не более {round_obj.max_choices} участников в этом раунде.", code="vote_limit_reached")

        return data


    @transaction.atomic
    def create(self, validated_data):
        # Campaign -> Round is also the lock order used by end_and_transfer.
        # Admission changes and accepting a vote must commit in a defined order.
        campaign = Campaign.objects.select_for_update().get(pk=validated_data["round"].campaign_id)
        validated_data["round"] = Round.objects.select_for_update().get(pk=validated_data["round"].pk)
        if validated_data["round"].campaign_id != campaign.pk:
            raise serializers.ValidationError("Кампания раунда изменилась. Откройте голосование заново.", code="round_changed")
        validated_data["round"].campaign = campaign
        validated_data["participant"] = Participant.objects.get(pk=validated_data["participant"].pk)
        self.validate(validated_data)
        return super().create(validated_data)


class RoundSerializer(serializers.ModelSerializer):
    campaign_name = serializers.CharField(source="campaign.name", read_only=True)
    campaign_order_number = serializers.IntegerField(source="campaign.order_number", read_only=True)
    number = serializers.IntegerField(required=False)
    participant_name = serializers.SerializerMethodField()
    can_edit_jury = serializers.SerializerMethodField()
    display_name = serializers.CharField(read_only=True)

    def get_can_edit_jury(self, obj):
        return obj.type == 'standard' and obj.event_stage in {'', 'semifinal'} and (
            obj.next_round_id is None or obj.next_round.status == 'pending')

    def validate(self, data):
        if not self.instance and data.get('campaign') and data['campaign'].rounds.exclude(event_stage='').exists():
            raise serializers.ValidationError('Туры этого мероприятия создаются через /event.')
        if self.instance and self.instance.event_stage:
            forbidden = set(data) - {'is_current'}
            if forbidden:
                raise serializers.ValidationError("Турами мероприятия управляйте через меню /event: запуск и завершение.")
        if 'max_choices' in data and not 1 <= data['max_choices'] <= 3:
            raise serializers.ValidationError({'max_choices': 'Допустимо от 1 до 3.'})
        return data

    def get_participant_name(self, obj):
        if obj.type == "individual":
            p = obj.participants.first()
            return p.full_name if p else "Пусто"
        return None

    class Meta:
        model = Round
        fields = "__all__"
        read_only_fields = ["started_at", "ended_at", "cycle", "event_stage", "next_round", "event_completed", "tie_break_ids", "jury_choice", "jury_name"]


class TelegramIDsField(serializers.JSONField):
    def to_internal_value(self, data):
        try:
            return normalize_telegram_ids(super().to_internal_value(data))
        except ValueError as exc:
            raise serializers.ValidationError(str(exc)) from exc


class CampaignAccessSerializer(serializers.Serializer):
    access_mode = serializers.ChoiceField(choices=Campaign.ACCESS_MODES, required=False)
    operation = serializers.ChoiceField(choices=["add", "remove"], required=False)
    telegram_ids = TelegramIDsField(required=False)

    def validate(self, data):
        if not data or bool("operation" in data) != bool("telegram_ids" in data):
            raise serializers.ValidationError("Укажите access_mode или operation (add/remove) вместе с telegram_ids.")
        if "telegram_ids" in data and not data["telegram_ids"]:
            raise serializers.ValidationError("Укажите хотя бы один Telegram ID.")
        return data


class CampaignSerializer(serializers.ModelSerializer):
    allowed_telegram_ids = TelegramIDsField(required=False)

    class Meta:
        model = Campaign
        fields = "__all__"
        read_only_fields = ["order_number", "created_at"]
