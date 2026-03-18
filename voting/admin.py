from django.contrib import admin
from .models import Campaign, Round, Participant, Vote


@admin.register(Campaign)
class CampaignAdmin(admin.ModelAdmin):
    list_display = ("name", "admin_telegram_id", "is_active", "created_at")
    list_filter = ("is_active",)


@admin.register(Round)
class RoundAdmin(admin.ModelAdmin):
    list_display = ("campaign", "number", "status", "started_at", "ended_at")
    list_filter = ("status", "campaign")


@admin.register(Participant)
class ParticipantAdmin(admin.ModelAdmin):
    list_display = ("full_name", "round", "description_short")
    list_filter = ("round",)
    search_fields = ("full_name",)

    def description_short(self, obj):
        return obj.description[:60] + "..." if obj.description else "-"


@admin.register(Vote)
class VoteAdmin(admin.ModelAdmin):
    # Используем наши новые функции вместо стандартных полей
    list_display = ("user_telegram_id", "get_participant_name", "get_round_info", "choice", "created_at")
    list_filter = ("round__campaign", "round") # Удобный фильтр сбоку
    search_fields = ("user_telegram_id", "participant__full_name")

    @admin.display(description='Участник')
    def get_participant_name(self, obj):
        # Выводим ФИО участника
        return obj.participant.full_name

    @admin.display(description='Раунд')
    def get_round_info(self, obj):
        # Выводим номер раунда и название кампании
        return f"Раунд №{obj.round.number} ({obj.round.campaign.name})"
