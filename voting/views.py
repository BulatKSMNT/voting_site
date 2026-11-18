import csv
import io
import base64

from django.views import View
from django.shortcuts import render, get_object_or_404
from django.http import JsonResponse
from django.db import transaction, IntegrityError
from django.db.models import Count, Q, Max

from rest_framework import viewsets, status, mixins
from rest_framework.decorators import action
from rest_framework.response import Response
from rest_framework.permissions import AllowAny
from .permissions import VotingPermission, bind_identity
from .audit import record
from . import services, tournament
from .reports import campaign_archive, consistent_read
from .audit import results as round_results, displayed_participants
from rest_framework import serializers

from .models import Round, Participant, Vote, Campaign
from .serializers import (
    ParticipantSerializer,
    VoteCreateSerializer,
    CampaignSerializer,
    CampaignAccessSerializer,
    RoundSerializer,
)

def safe_csv_cell(value):
    if isinstance(value, str) and value[:1] in ("=", "+", "-", "@"):
        return "'" + value
    return value


class LockedModelViewSet(viewsets.ModelViewSet):
    @transaction.atomic
    def update(self, request, *args, **kwargs):
        return super().update(request, *args, **kwargs)

    @transaction.atomic
    def destroy(self, request, *args, **kwargs):
        return super().destroy(request, *args, **kwargs)

    def get_queryset(self):
        queryset = super().get_queryset()
        if self.action in {"update", "partial_update", "destroy"} and self.queryset.model is not Participant:
            queryset = queryset.select_for_update()
        return queryset


class CampaignViewSet(LockedModelViewSet):
    queryset = Campaign.objects.filter(is_active=True)
    serializer_class = CampaignSerializer
    permission_classes = [VotingPermission]

    @action(detail=False, methods=['get'])
    def my_access(self, request):
        """Only the authenticated bot may check its current Telegram sender."""
        actor = bind_identity(request)
        campaigns = self.get_queryset().order_by('order_number')
        campaign_id = request.query_params.get('campaign_id')
        if campaign_id is not None:
            class Selection(serializers.Serializer):
                campaign_id = serializers.IntegerField(min_value=1, max_value=2**63 - 1)
            selection = Selection(data={'campaign_id': campaign_id})
            selection.is_valid(raise_exception=True)
            campaigns = [get_object_or_404(campaigns, pk=selection.validated_data['campaign_id'])]
        return Response({'campaigns': [
            {'id': campaign.pk, 'name': campaign.name, 'can_vote': campaign.can_vote(actor['actor_id'])}
            for campaign in campaigns]})

    @action(detail=False, methods=['get'])
    def report_choices(self, request):
        return Response({'campaigns': list(Campaign.objects.order_by('-order_number').values('id', 'name', 'is_active'))})

    @action(detail=True, methods=['get'])
    def report(self, request, pk=None):
        with consistent_read():
            campaign = get_object_or_404(Campaign, pk=pk)
            content = campaign_archive(campaign)
        record('export.created', campaign_id=campaign.pk, details={'format': 'campaign_archive'})
        return Response({'filename': f'Отчет-мероприятие-{campaign.pk}.zip',
                         'content_base64': base64.b64encode(content).decode('ascii')})

    def perform_destroy(self, instance):
        if instance.rounds.exclude(event_stage='').exists():
            raise serializers.ValidationError('Архивируйте кампанию через is_active; история мероприятия сохраняется.')
        return super().perform_destroy(instance)

    @action(detail=True, methods=['get'])
    def event(self, request, pk=None):
        campaign = self.get_object()
        return Response({'campaign_id': campaign.pk, 'name': campaign.name, 'rounds': tournament.event_state(campaign.pk)})

    @action(detail=True, methods=['post'])
    def prepare_event(self, request, pk=None):
        campaign = self.get_object()
        return Response(tournament.prepare_event(campaign.pk, request.data.get('names'), request.data.get('winners_count', 8)))

    @action(detail=True, methods=['post'])
    def create_tour(self, request, pk=None):
        campaign = self.get_object()
        return Response(tournament.create_tour(campaign.pk, request.data.get('mode', 'individual'),
                        request.data.get('winners_count', 8), request.data.get('names')))

    @action(detail=True, methods=['patch'])
    @transaction.atomic
    def access(self, request, pk=None):
        """Merge list changes atomically; ordinary PATCH replaces the whole list."""
        changes = CampaignAccessSerializer(data=request.data)
        changes.is_valid(raise_exception=True)
        campaign = self.get_object()
        campaign = Campaign.objects.select_for_update().get(pk=campaign.pk)
        data = changes.validated_data
        fields = set()
        if 'access_mode' in data:
            campaign.access_mode = data['access_mode']
            fields.add('access_mode')
        if 'operation' in data:
            ids = set(campaign.allowed_telegram_ids)
            if data['operation'] == 'add':
                ids.update(data['telegram_ids'])
            else:
                ids.difference_update(data['telegram_ids'])
            campaign.allowed_telegram_ids = sorted(ids)
            fields.add('allowed_telegram_ids')
        campaign.save(update_fields=fields)
        return Response(self.get_serializer(campaign).data)

    @action(detail=False, methods=['get'], permission_classes=[AllowAny])
    def active_list(self, request):
        return Response({"campaigns": list(self.queryset.values("id", "name", "order_number", "is_active"))})


class RoundViewSet(LockedModelViewSet):
    queryset = Round.objects.all()
    serializer_class = RoundSerializer
    permission_classes = [VotingPermission]

    def perform_destroy(self, instance):
        if instance.event_stage:
            raise serializers.ValidationError('Туры подготовленного мероприятия нельзя удалять по отдельности.')
        return super().perform_destroy(instance)

    @action(detail=True, methods=['post'])
    def add_entries(self, request, pk=None):
        return Response(tournament.add_entries(self.get_object().pk, request.data.get('names')))

    @action(detail=True, methods=['post'])
    def event_start(self, request, pk=None):
        return Response(tournament.start_round(self.get_object().pk))

    @action(detail=True, methods=['post'])
    def event_finish(self, request, pk=None):
        return Response(tournament.finish_round(self.get_object().pk))

    @action(detail=True, methods=['get'])
    def event_results(self, request, pk=None):
        return Response(round_results(self.get_object()))

    @action(detail=True, methods=['post'])
    def jury(self, request, pk=None):
        class ChoiceSerializer(serializers.Serializer):
            participant_id = serializers.IntegerField(min_value=1, allow_null=True, required=False, default=None)
            full_name = serializers.CharField(max_length=200, allow_blank=True, required=False, default='')
        data = ChoiceSerializer(data=request.data)
        data.is_valid(raise_exception=True)
        return Response(tournament.set_jury(self.get_object().pk, **data.validated_data))

    @action(detail=True, methods=['post'])
    def resolve_tie(self, request, pk=None):
        class TieSerializer(serializers.Serializer):
            participant_ids = serializers.ListField(child=serializers.IntegerField(min_value=1), allow_empty=False)
        data = TieSerializer(data=request.data)
        data.is_valid(raise_exception=True)
        return Response(tournament.resolve_tie(self.get_object().pk, data.validated_data['participant_ids']))

    def create(self, request, *args, **kwargs):
        data = request.data.copy()

        if not data.get('number'):
            campaign_id = data.get('campaign')
            if campaign_id:
                max_num = Round.objects.filter(campaign_id=campaign_id).aggregate(m=Max('number'))['m'] or 0
                data['number'] = max_num + 1

        serializer = self.get_serializer(data=data)
        serializer.is_valid(raise_exception=True)

        try:
            with transaction.atomic():
                serializer.save()
        except IntegrityError:
            return Response(
                {"error": "Раунд с таким номером уже существует в этой кампании."},
                status=status.HTTP_400_BAD_REQUEST
            )

        headers = self.get_success_headers(serializer.data)
        return Response(serializer.data, status=status.HTTP_201_CREATED, headers=headers)

    @action(detail=False, methods=['get'], permission_classes=[AllowAny])
    def active_info(self, request):
        round_obj = Round.objects.select_related('campaign').filter(is_current=True, status__in=["active", "published"]).first()

        if not round_obj:
            request._request.activity_reason = "no_active_round"
            return Response({"error": "Нет активных раундов", "code": "no_active_round"}, status=404)

        user_votes = []
        voter_id = None
        actor = bind_identity(request)
        user_id = actor["actor_id"] if actor["source"] == "bot" else request.GET.get("user_id")

        if request.user.is_authenticated and user_id and str(user_id).isdigit() and (actor["source"] == "bot" or actor["actor_role"] == "full"):
            voter_id = int(user_id)
            user_votes = Vote.objects.filter(round=round_obj, user_telegram_id=user_id)

        participants = Participant.objects.filter(round=round_obj).annotate(
            votes_count=Count("vote", filter=Q(vote__choice__isnull=True) | Q(vote__choice="yes"))
        ).order_by("-votes_count", "order_number")

        return Response({
            "round_id": round_obj.id,
            "campaign_id": round_obj.campaign_id,
            "access_mode": round_obj.campaign.access_mode,
            "can_vote": round_obj.campaign.can_vote(voter_id),
            "max_choices": round_obj.max_choices,
            "remaining_choices": max(0, round_obj.max_choices - len(user_votes)),
            "event_stage": round_obj.event_stage,
            "round_name": round_obj.display_name,
            "round_type": round_obj.type,
            "status": round_obj.status,
            "participants": ([{'id': p['id'], 'full_name': p['name'], 'votes': p['yes'], 'order_number': p['order_number']}
                              for p in displayed_participants(round_obj)] if round_obj.event_stage == 'qualification' else [
                {
                    "id": p.id,
                    "full_name": p.full_name,
                    "votes": p.votes_count,
                    "order_number": p.order_number
                }
                for p in participants
            ]),
            "user_votes": [
                {"participant_id": v.participant_id, "choice": v.choice}
                for v in user_votes
            ]
        })


    @action(detail=True, methods=['patch'], permission_classes=[VotingPermission])
    @transaction.atomic
    def update_winners_count(self, request, pk=None):
        """Изменяет количество призовых мест в раунде"""
        rd = self.get_object()
        return Response(tournament.configure_tour(rd.pk, request.data.get('winners_count')))

    @action(detail=True, methods=['post'])
    def end_and_transfer(self, request, pk=None):
        try:
            return Response(services.end_and_transfer(pk, request.data))
        except Round.DoesNotExist:
            return Response({"error": "Раунд не найден.", "code": "round_not_found"}, status=404)

    @action(detail=False, methods=['get'])
    def export_csv(self, request):
        output = io.StringIO()
        writer = csv.writer(output, delimiter=';', dialect='excel')

        writer.writerow(['Кампания', 'Раунд', 'Тип', 'Статус', 'Место', 'Участник', 'Голоса ЗА'])

        rounds = Round.objects.all().select_related('campaign').order_by('campaign__order_number', 'number')

        for r in rounds:
            participants = round_results(r)['participants']

            if not participants:
                writer.writerow([
                    safe_csv_cell(r.campaign.name),
                    f"№{r.number}",
                    r.get_type_display(),
                    r.get_status_display(),
                    '-',
                    'Нет участников',
                    0
                ])
                continue

            for i, p in enumerate(participants, 1):
                writer.writerow([
                    safe_csv_cell(r.campaign.name),
                    f"№{r.number}",
                    r.get_type_display(),
                    r.get_status_display(),
                    i,
                    safe_csv_cell(p['name']),
                    p['yes']
                ])

        record("export.created", details={"format": "legacy_csv", "scope": "all_rounds"})
        return Response({"csv_content": output.getvalue()})


class VoteViewSet(mixins.CreateModelMixin, viewsets.GenericViewSet):
    queryset = Vote.objects.all()
    serializer_class = VoteCreateSerializer
    permission_classes = [VotingPermission]
    http_method_names = ['post']

    def create(self, request, *args, **kwargs):
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        try:
            vote = serializer.save()
            return Response({"status": "Голос учтён", "vote_id": vote.pk}, status=status.HTTP_201_CREATED)
        except IntegrityError:
            record("vote.rejected", round_obj=serializer.validated_data["round"], outcome="rejected", reason_code="duplicate")
            return Response(
                {"code": "duplicate", "error": "Вы уже проголосовали за этого участника."},
                status=status.HTTP_400_BAD_REQUEST
            )


class ParticipantViewSet(LockedModelViewSet):
    queryset = Participant.objects.all()
    serializer_class = ParticipantSerializer
    permission_classes = [VotingPermission]

    def perform_destroy(self, instance):
        if instance.round.event_stage:
            raise serializers.ValidationError('Состав тура мероприятия нельзя удалять вручную.')
        return super().perform_destroy(instance)

    def create(self, request, *args, **kwargs):
        round_id = request.data.get("round")

        if round_id:
            try:
                r = Round.objects.get(id=round_id)
                if r.type == "individual" and r.participants.count() >= 1:
                    return Response(
                        {"error": "В индивидуальном раунде может быть только 1 участник!"},
                        status=status.HTTP_400_BAD_REQUEST
                    )
            except Round.DoesNotExist:
                return Response({"error": "Раунд не найден."}, status=status.HTTP_404_NOT_FOUND)

        return super().create(request, *args, **kwargs)


class CurrentRoundResults(View):
    def get(self, request):
        round_id_str = request.GET.get("round_id")
        current_round = None

        if round_id_str:
            try:
                current_round = Round.objects.filter(
                    id=int(round_id_str),
                    status__in=["active", "published"]
                ).first()
            except ValueError:
                pass

        if not current_round:
            current_round = Round.objects.filter(
                is_current=True,
                status__in=["active", "published"]
            ).first()

        if current_round and current_round.event_stage == 'performance':
            current_round = current_round.next_round
        active_rounds = Round.objects.filter(status__in=["active", "published"]).exclude(event_stage='performance').order_by("started_at")

        context = {
            "round": current_round,
            "active_rounds": active_rounds,
            "selected_round_id": current_round.id if current_round else None,
            "total_votes": 0,
            "left_column": [],
            "right_column": []
        }

        if current_round:
            summary = round_results(current_round)
            # Presentation order only: extra entrants are last; score ranks,
            # winners, transfers and archived reports retain the scoring order.
            participants = sorted(displayed_participants(current_round, summary),
                                  key=lambda p: bool(p['jury_winner'] or p['is_admin_choice']))
            context['stage_label'] = current_round.display_name if current_round.event_stage else ''
            context['needs_tiebreak'] = bool(summary['tie_slots'] and current_round.ended_at)

            results = [
                {
                    "position": p["position"],
                    "participant_order": p['order_number'],
                    "participant_full_name": p['name'],
                    "votes": p['yes'],
                    "winner": current_round.event_completed and p['winner'],
                    "jury_winner": p['jury_winner'],
                }
                for p in participants
            ]

            if current_round.type == "individual":
                context["left_column"] = results
            else:
                mid = (len(results) + 1) // 2
                context["left_column"] = results[:mid]
                context["right_column"] = results[mid:]

            context["total_votes"] = summary['total_votes']

        if request.GET.get("ajax") == "1":
            return JsonResponse({
                "round_id": current_round.pk if current_round else None,
                "round_type": current_round.type if current_round else "",
                "stage_label": context.get("stage_label", ""),
                "needs_tiebreak": context.get('needs_tiebreak', False),
                "left_column": context["left_column"],
                "right_column": context["right_column"],
                "total_votes": context["total_votes"]
            })

        return render(request, "voting/results.html", context)
