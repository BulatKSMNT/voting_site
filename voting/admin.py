import csv
import json
from contextlib import contextmanager
from django.contrib import admin
from django.core.exceptions import PermissionDenied
from django.core.paginator import Paginator
from django.db import transaction
from django.http import HttpResponse
from django.shortcuts import get_object_or_404, redirect
from django.template.response import TemplateResponse
from django.urls import path, reverse
from django.utils.html import format_html
from core.observability import context
from .models import Campaign, Round, Participant, Vote, AuditEvent, RoundResultSnapshot, RoundOperation
from .audit import record, results, result_votes
from .forms import CampaignAdminForm, EventPrepareForm, EventControlForm
from . import tournament
from rest_framework.exceptions import ValidationError


@contextmanager
def admin_context(request):
    token = context.set({**context.get(), 'source': 'admin', 'actor_id': request.user.pk, 'actor_role': 'staff'})
    try:
        yield
    finally:
        context.reset(token)


class AuditedAdmin(admin.ModelAdmin):
    def save_model(self, request, obj, form, change):
        with admin_context(request), transaction.atomic():
            super().save_model(request, obj, form, change)

    def delete_model(self, request, obj):
        with admin_context(request), transaction.atomic():
            super().delete_model(request, obj)

    def delete_queryset(self, request, queryset):
        with admin_context(request), transaction.atomic():
            if queryset.model is Campaign:
                list(queryset.select_for_update().order_by('pk'))
                rounds = Round.objects.filter(campaign_id__in=queryset.values('pk'))
            elif queryset.model is Round:
                rounds = queryset
            else:
                rounds = Round.objects.filter(pk__in=queryset.values('round_id'))
            list(rounds.select_for_update().order_by('pk'))
            super().delete_queryset(request, queryset)


@admin.register(Campaign)
class CampaignAdmin(AuditedAdmin):
    form = CampaignAdminForm
    list_display = ('name', 'admin_telegram_id', 'access_mode', 'allowed_voters_count', 'event_link', 'is_active', 'created_at')
    list_filter = ('is_active', 'access_mode')
    search_fields = ('name',)

    @admin.display(description='Мероприятие')
    def event_link(self, obj):
        return format_html('<a href="{}">Подготовить / открыть туры</a>', reverse('admin:voting_campaign_event', args=[obj.pk]))

    def has_delete_permission(self, request, obj=None):
        return super().has_delete_permission(request, obj) and not (obj and obj.rounds.exclude(event_stage='').exists())

    def get_actions(self, request):
        actions = super().get_actions(request)
        actions.pop('delete_selected', None)
        return actions

    def get_urls(self):
        return [path('<int:pk>/event/', self.admin_site.admin_view(self.event), name='voting_campaign_event')] + super().get_urls()

    def event(self, request, pk):
        campaign = get_object_or_404(Campaign, pk=pk)
        if not (self.has_change_permission(request, campaign) and request.user.has_perm('voting.add_round') and request.user.has_perm('voting.add_participant')):
            raise PermissionDenied
        form = EventPrepareForm(request.POST or None)
        rounds = campaign.rounds.exclude(event_stage='performance').order_by('number')
        if request.method == 'POST' and form.is_valid():
            try:
                with admin_context(request):
                    tournament.create_tour(pk, form.cleaned_data['mode'] or 'individual', form.cleaned_data['winners_count'] or 8,
                                           [n.strip() for n in form.cleaned_data['names'].splitlines() if n.strip()])
                return redirect('admin:voting_campaign_event', pk)
            except ValidationError as exc:
                form.add_error(None, str(exc.detail))
        return TemplateResponse(request, 'admin/voting/event_controls.html', {
            **self.admin_site.each_context(request), 'title': f'Мероприятие: {campaign.name}', 'form': form if not campaign.rounds.exclude(event_stage='').filter(event_completed=False).exists() else None,
            'rounds': rounds, 'opts': self.model._meta,
        })

    @admin.display(description='ID в списке')
    def allowed_voters_count(self, obj):
        return len(obj.allowed_telegram_ids)

    def save_model(self, request, obj, form, change):
        with admin_context(request), transaction.atomic():
            # Do not overwrite an access change made while an unrelated field was edited.
            obj.save(update_fields=form.changed_data if change else None)


def csv_cell(value):
    if isinstance(value, str) and value.lstrip()[:1] in ('=', '+', '-', '@'):
        return "'" + value
    return value


@admin.register(Round)
class RoundAdmin(AuditedAdmin):
    list_display = ('campaign', 'number', 'tour_title', 'status', 'started_at', 'ended_at', 'event_link', 'report_link')
    list_filter = ('status', 'campaign', 'type')
    list_select_related = ('campaign',)
    readonly_fields = ('report_link', 'event_link', 'cycle', 'jury_choice', 'jury_name')

    @admin.display(description='Тур / раунд')
    def tour_title(self, obj):
        return obj.display_name

    def formfield_for_foreignkey(self, db_field, request, **kwargs):
        if db_field.name == 'campaign' and not request.resolver_match.kwargs.get('object_id'):
            kwargs['queryset'] = Campaign.objects.exclude(rounds__event_stage__in=['qualification', 'performance', 'semifinal', 'final'])
        return super().formfield_for_foreignkey(db_field, request, **kwargs)

    @admin.display(description='Управление')
    def event_link(self, obj):
        if not obj or not obj.pk:
            return '—'
        return format_html('<a href="{}">Управление туром / выбор администратора</a>', reverse('admin:voting_round_event', args=[obj.pk]))

    def get_readonly_fields(self, request, obj=None):
        if obj and obj.event_stage:
            return tuple(f.name for f in self.model._meta.fields if f.name not in {'is_current', 'event_stage'}) + ('tour_title', 'event_link', 'report_link')
        return super().get_readonly_fields(request, obj)

    def save_model(self, request, obj, form, change):
        if change and obj.event_stage:
            with admin_context(request), transaction.atomic():
                obj.save(update_fields=['is_current'])
        else:
            super().save_model(request, obj, form, change)

    def has_delete_permission(self, request, obj=None):
        return super().has_delete_permission(request, obj) and not (obj and obj.event_stage)

    def get_actions(self, request):
        actions = super().get_actions(request)
        actions.pop('delete_selected', None)
        return actions

    @admin.display(description='Результаты и история')
    def report_link(self, obj):
        if not obj or not obj.pk:
            return '—'
        return format_html('<a href="{}">Открыть карточку голосования</a>', reverse('admin:voting_round_report', args=[obj.pk]))

    def get_urls(self):
        return [path('<int:pk>/report/', self.admin_site.admin_view(self.report), name='voting_round_report'),
                path('<int:pk>/event/', self.admin_site.admin_view(self.event), name='voting_round_event')] + super().get_urls()

    def event(self, request, pk):
        rd = get_object_or_404(Round, pk=pk)
        if not self.has_change_permission(request, rd):
            raise PermissionDenied
        form = EventControlForm(request.POST or None, round_obj=rd)
        if request.method == 'POST' and form.is_valid():
            data = form.cleaned_data
            try:
                with admin_context(request):
                    if data['action'] == 'start':
                        result = tournament.start_round(pk)
                    elif data['action'] == 'finish':
                        result = tournament.finish_round(pk)
                    elif data['action'] == 'add':
                        if not (request.user.has_perm('voting.add_round') and request.user.has_perm('voting.add_participant')):
                            raise PermissionDenied
                        result = tournament.add_entries(pk, [n.strip() for n in data['names'].splitlines() if n.strip()])
                    elif data['action'] == 'configure':
                        result = tournament.configure_tour(pk, data['winners_count'])
                    elif data['action'] == 'tie':
                        result = tournament.resolve_tie(pk, [p.pk for p in data['tied']])
                    else:
                        result = tournament.set_jury(pk, data['participant'].pk if data['participant'] and data['action'] == 'jury' else None,
                                                     data['full_name'] if data['action'] == 'jury' else '')
                self.message_user(request, result['message'])
                return redirect('admin:voting_round_event', pk)
            except ValidationError as exc:
                form.add_error(None, str(exc.detail))
        return TemplateResponse(request, 'admin/voting/event_controls.html', {
            **self.admin_site.each_context(request), 'title': f'Управление: {rd.display_name}', 'form': form if form.fields['action'].choices else None,
            'round': rd, 'summary': results(rd), 'rounds': rd.previous_rounds.filter(event_stage='performance').order_by('number') if rd.event_stage == 'qualification' else [], 'opts': self.model._meta,
        })

    def report(self, request, pk):
        rd = get_object_or_404(Round.objects.select_related('campaign'), pk=pk)
        if not (self.has_view_permission(request, rd) and request.user.has_perm('voting.view_vote')
                and request.user.has_perm('voting.view_auditevent') and request.user.has_perm('voting.view_roundresultsnapshot')):
            raise PermissionDenied
        current = results(rd)
        snapshots = RoundResultSnapshot.objects.filter(round_id=rd.pk)
        selected = snapshots.filter(pk=request.GET.get('snapshot')).first() if request.GET.get('snapshot', '').isdigit() else None
        shown = selected.data if selected else current
        votes = result_votes(rd).select_related('participant').order_by('-created_at', '-pk')
        events = AuditEvent.objects.filter(round_id=rd.pk)
        export = request.GET.get('export')
        if export in {'results', 'votes', 'events'}:
            response = HttpResponse(content_type='text/csv; charset=utf-8')
            response['Content-Disposition'] = f'attachment; filename="round-{rd.pk}-{export}.csv"'
            response.write('\ufeff')
            writer = csv.writer(response, delimiter=';')
            if export == 'results':
                writer.writerow(['Раунд ID', 'Версия снимка', 'Место', 'Участник', 'За', 'Против', 'Перенесено', 'Победитель'])
                for p in shown['participants']:
                    writer.writerow([rd.pk, selected.version if selected else 'текущее состояние', p['position'], csv_cell(p['name']), p['yes'], p['no'], p['transferred'], p['winner']])
            elif export == 'votes':
                writer.writerow(['ID', 'Участник', 'Telegram ID', 'Выбор', 'Записан', 'Исходный голос ID', 'Исходное время', 'Операция'])
                for v in votes.iterator():
                    writer.writerow([v.pk, csv_cell(v.participant.full_name), v.user_telegram_id, v.choice or 'yes', v.created_at.isoformat(), v.source_vote_id or '', v.source_created_at.isoformat() if v.source_created_at else '', v.transfer_key])
            else:
                writer.writerow(['Время', 'Событие', 'Источник', 'Инициатор ID', 'Роль', 'Результат', 'Причина', 'Request ID', 'Данные'])
                for e in events.iterator():
                    writer.writerow([e.created_at.isoformat(), e.event, e.source, e.actor_id, e.actor_role, e.outcome, e.reason_code, e.request_id, csv_cell(json.dumps(e.details, ensure_ascii=False))])
            with admin_context(request):
                record('export.created', round_obj=rd, details={'format': export, 'snapshot_id': selected.pk if selected else None})
            return response
        return TemplateResponse(request, 'admin/voting/round_report.html', {
            **self.admin_site.each_context(request), 'title': f'История: {rd}', 'round': rd,
            'summary': shown, 'selected_snapshot': selected, 'snapshots': snapshots,
            'votes': Paginator(votes, 50).get_page(request.GET.get('votes_page')),
            'events': Paginator(events, 50).get_page(request.GET.get('events_page')),
            'opts': self.model._meta,
        })


class EventDataAdmin(AuditedAdmin):
    """Managed event rosters and votes are changed through the checked workflow."""
    def has_change_permission(self, request, obj=None):
        return super().has_change_permission(request, obj) and not (obj and obj.round.event_stage)

    def has_delete_permission(self, request, obj=None):
        return super().has_delete_permission(request, obj) and not (obj and obj.round.event_stage)

    def get_actions(self, request):
        actions = super().get_actions(request)
        actions.pop('delete_selected', None)
        return actions

    def formfield_for_foreignkey(self, db_field, request, **kwargs):
        if db_field.name == 'round':
            kwargs['queryset'] = Round.objects.filter(event_stage='')
        return super().formfield_for_foreignkey(db_field, request, **kwargs)


@admin.register(Participant)
class ParticipantAdmin(EventDataAdmin):
    list_display = ('full_name', 'round', 'source_participant_id')
    list_filter = ('round__campaign', 'round')
    search_fields = ('full_name',)
    list_select_related = ('round', 'round__campaign')
    readonly_fields = ('source_participant_id', 'transfer_key')


@admin.register(Vote)
class VoteAdmin(EventDataAdmin):
    list_display = ('user_telegram_id', 'participant', 'round', 'choice', 'created_at', 'source_vote_id')
    list_filter = ('round__campaign', 'round', 'choice', 'created_at')
    search_fields = ('user_telegram_id', 'participant__full_name')
    list_select_related = ('participant', 'round', 'round__campaign')
    readonly_fields = ('source_vote_id', 'source_created_at', 'transfer_key', 'created_at')


class ReadOnlyAdmin(admin.ModelAdmin):
    def get_readonly_fields(self, request, obj=None):
        return [f.name for f in self.model._meta.fields]

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(AuditEvent)
class AuditEventAdmin(ReadOnlyAdmin):
    list_display = ('created_at', 'event', 'campaign_id', 'round_id', 'actor_id', 'source', 'outcome', 'reason_code')
    list_filter = ('event', 'source', 'outcome', 'created_at')
    search_fields = ('=campaign_id', '=round_id', '=participant_id', '=actor_id', 'request_id')
    date_hierarchy = 'created_at'


@admin.register(RoundResultSnapshot)
class SnapshotAdmin(ReadOnlyAdmin):
    list_display = ('round_id', 'campaign_id', 'version', 'created_at')
    list_filter = ('created_at',)
    search_fields = ('=round_id', '=campaign_id')


@admin.register(RoundOperation)
class OperationAdmin(ReadOnlyAdmin):
    list_display = ('round_id', 'cycle', 'action', 'created_at')
    list_filter = ('action',)
    search_fields = ('=round_id', 'key')
