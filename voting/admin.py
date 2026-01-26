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
    list_display = ("user_telegram_id", "participant", "round", "created_at")
    list_filter = ("round",)
    search_fields = ("user_telegram_id",)