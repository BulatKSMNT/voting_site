import csv
import io
import logging

from django.views import View
from django.shortcuts import render
from django.http import JsonResponse
from django.db import transaction, IntegrityError
from django.db.models import Count, Q, Max
from django.utils import timezone

from rest_framework import viewsets, status, mixins
from rest_framework.decorators import action
from rest_framework.response import Response
from rest_framework.permissions import IsAuthenticated, AllowAny

from .models import Round, Participant, Vote, Campaign
from .serializers import (
    ParticipantSerializer,
    VoteCreateSerializer,
    CampaignSerializer,
    RoundSerializer,
)

logger = logging.getLogger(__name__)


def safe_csv_cell(value):
    if isinstance(value, str) and value[:1] in ("=", "+", "-", "@"):
        return "'" + value
    return value


class CampaignViewSet(viewsets.ModelViewSet):
    queryset = Campaign.objects.filter(is_active=True)
    serializer_class = CampaignSerializer
    permission_classes = [IsAuthenticated]

    @action(detail=False, methods=['get'], permission_classes=[AllowAny])
    def active_list(self, request):
        return Response({"campaigns": self.get_serializer(self.queryset, many=True).data})


class RoundViewSet(viewsets.ModelViewSet):
    queryset = Round.objects.all()
    serializer_class = RoundSerializer
    permission_classes = [IsAuthenticated]

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
        round_obj = Round.objects.filter(is_current=True, status__in=["active", "published"]).first()

        if not round_obj:
            return Response({"error": "Нет активных раундов"}, status=404)

        user_votes = []
        user_id = request.GET.get("user_id")

        if request.user and request.user.is_authenticated and user_id:
            user_votes = Vote.objects.filter(round=round_obj, user_telegram_id=user_id)

        participants = Participant.objects.filter(round=round_obj).annotate(
            votes_count=Count("vote", filter=Q(vote__choice__isnull=True) | Q(vote__choice="yes"))
        ).order_by("-votes_count", "order_number")

        return Response({
            "round_id": round_obj.id,
            "round_name": str(round_obj),
            "round_type": round_obj.type,
            "status": round_obj.status,
            "participants": [
                {
                    "id": p.id,
                    "full_name": p.full_name,
                    "votes": p.votes_count,
                    "order_number": p.order_number
                }
                for p in participants
            ],
            "user_votes": [
                {"participant_id": v.participant_id, "choice": v.choice}
                for v in user_votes
            ]
        })


    @action(detail=True, methods=['patch'], permission_classes=[IsAuthenticated])
    def update_winners_count(self, request, pk=None):
        """Изменяет количество призовых мест в раунде"""
        try:
            round_obj = Round.objects.get(pk=pk)
            new_count = request.data.get("winners_count")

            if not new_count or int(new_count) < 1:
                return Response({"error": "Укажите корректное число победителей (от 1)"}, status=400)

            round_obj.winners_count = int(new_count)
            round_obj.save(update_fields=["winners_count"])

            return Response({"message": f"✅ В Раунде #{round_obj.number} теперь {new_count} призовых мест!"})
        except Round.DoesNotExist:
            return Response({"error": "Раунд не найден."}, status=404)
        except ValueError:
            return Response({"error": "Количество должно быть числом."}, status=400)

    @action(detail=True, methods=['post'])
    def end_and_transfer(self, request, pk=None):
        action_type = request.data.get("action_type", "auto_individual")
        target_round_id = request.data.get("target_round_id")
        keep_votes = request.data.get("keep_votes", True)

        try:
            with transaction.atomic():
                round_obj = Round.objects.select_for_update().get(pk=pk)

                if round_obj.type == "individual":
                    if round_obj.status == "ended":
                        return Response({"error": "Уже завершен"}, status=400)

                    round_obj.status = "ended"
                    round_obj.is_current = False
                    round_obj.ended_at = timezone.now()
                    round_obj.save()

                    p = Participant.objects.filter(round=round_obj).first()
                    if not p:
                        return Response({
                            "is_individual": True,
                            "message": "Индив. раунд завершен (участников не было)."
                        })

                    if target_round_id:
                        # Ищем среди active И published
                        target_round = Round.objects.get(
                            id=target_round_id,
                            campaign=round_obj.campaign,
                            type="standard",
                            status__in=["active", "published"]
                        )
                    else:
                        target_round = Round.objects.filter(
                            campaign=round_obj.campaign,
                            type="standard",
                            status__in=["active", "published"]
                        ).first()

                        if not target_round:
                            max_num = Round.objects.filter(campaign=round_obj.campaign).aggregate(m=Max('number'))['m'] or 0
                            target_round = Round.objects.create(
                                campaign=round_obj.campaign,
                                number=max_num + 1,
                                type="standard",
                                status="published",  # <--- СОЗДАЕМ ОПУБЛИКОВАННЫМ
                                winners_count=3
                            )

                    yes_votes = Vote.objects.filter(participant=p, choice="yes")
                    new_p = Participant.objects.create(
                        round=target_round,
                        full_name=p.full_name,
                        description=f"Из индив. раунда #{round_obj.number}"
                    )

                    Vote.objects.bulk_create([
                        Vote(round=target_round, participant=new_p, user_telegram_id=v.user_telegram_id)
                        for v in yes_votes
                    ], ignore_conflicts=True)

                    return Response({
                        "is_individual": True,
                        "winners": [
                            {
                                "id": p.id,
                                "name": p.full_name,
                                "votes": yes_votes.count()
                            }
                        ],
                        "message": (
                            f"🏁 Раунд завершен!\n"
                            f"Участник <b>{p.full_name}</b> "
                            f"({yes_votes.count()} голосов «ЗА») перенесен в Стандартный Раунд #{target_round.number}."
                        )
                    })

                elif action_type == "end_standard":
                    round_obj.status = "published"
                    round_obj.is_current = False
                    round_obj.ended_at = timezone.now()
                    round_obj.save()

                    participants = Participant.objects.filter(round=round_obj).annotate(
                        v_count=Count("vote", filter=Q(vote__choice__isnull=True) | Q(vote__choice="yes"))
                    ).order_by("-v_count", "order_number")

                    unique_votes = list(participants.values_list("v_count", flat=True).distinct())[:round_obj.winners_count]
                    min_votes = min(unique_votes) if unique_votes else 0
                    winners = participants.filter(v_count__gte=min_votes)

                    winners_data = [{"id": w.id, "name": w.full_name, "votes": w.v_count} for w in winners]
                    return Response({
                        "is_individual": False,
                        "winners": winners_data,
                        "message": f"Раунд #{round_obj.number} завершен! Результаты на экране."
                    })

                elif action_type == "transfer_standard":
                    target_round = Round.objects.get(id=target_round_id, type="standard", status__in=["active", "published"])
                    if target_round.id == round_obj.id:
                        return Response({"error": "Нельзя переносить участников в тот же самый раунд."}, status=400)
                    if target_round.campaign_id != round_obj.campaign_id:
                        return Response({"error": "Нельзя переносить участников в раунд другой кампании."}, status=400)

                    winners_ids = request.data.get("winners_ids", [])
                    winners = Participant.objects.filter(id__in=winners_ids, round=round_obj)

                    transfer_count = 0
                    for p in winners:
                        new_p = Participant.objects.create(round=target_round, full_name=p.full_name)
                        transfer_count += 1

                        if keep_votes:
                            voters = Vote.objects.filter(participant=p).filter(
                                Q(choice="yes") | Q(choice__isnull=True)
                            )
                            Vote.objects.bulk_create([
                                Vote(round=target_round, participant=new_p, user_telegram_id=v.user_telegram_id)
                                for v in voters
                            ], ignore_conflicts=True)

                    return Response({
                        "message": f"✅ Перенесено {transfer_count} финалистов в Раунд #{target_round.number}."
                    })

                return Response({"error": "Неизвестный action_type"}, status=400)

        except Round.DoesNotExist:
            return Response({"error": "Раунд не найден."}, status=404)
        except Exception:
            logger.exception("end_and_transfer failed")
            return Response({"error": "Внутренняя ошибка сервера"}, status=500)

    @action(detail=False, methods=['get'])
    def export_csv(self, request):
        output = io.StringIO()
        writer = csv.writer(output, delimiter=';', dialect='excel')

        writer.writerow(['Кампания', 'Раунд', 'Тип', 'Статус', 'Место', 'Участник', 'Голоса ЗА'])

        rounds = Round.objects.all().select_related('campaign').order_by('campaign__order_number', 'number')

        for r in rounds:
            participants = Participant.objects.filter(round=r).annotate(
                votes=Count("vote", filter=Q(vote__choice__isnull=True) | Q(vote__choice="yes"))
            ).order_by("-votes", "order_number")

            if not participants.exists():
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
                    safe_csv_cell(p.full_name),
                    p.votes
                ])

        return Response({"csv_content": output.getvalue()})


class VoteViewSet(mixins.CreateModelMixin, viewsets.GenericViewSet):
    queryset = Vote.objects.all()
    serializer_class = VoteCreateSerializer
    permission_classes = [IsAuthenticated]
    http_method_names = ['post']

    def create(self, request, *args, **kwargs):
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        try:
            serializer.save()
            return Response({"status": "Голос учтён"}, status=status.HTTP_201_CREATED)
        except IntegrityError:
            return Response(
                {"error": "Вы уже проголосовали за этого участника."},
                status=status.HTTP_400_BAD_REQUEST
            )


class ParticipantViewSet(viewsets.ModelViewSet):
    queryset = Participant.objects.all()
    serializer_class = ParticipantSerializer
    permission_classes = [IsAuthenticated]

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

        active_rounds = Round.objects.filter(status__in=["active", "published"]).order_by("started_at")

        context = {
            "round": current_round,
            "active_rounds": active_rounds,
            "selected_round_id": current_round.id if current_round else None,
            "total_votes": 0,
            "left_column": [],
            "right_column": []
        }

        if current_round:
            participants = Participant.objects.filter(round=current_round).annotate(
                votes=Count("vote", filter=Q(vote__choice__isnull=True) | Q(vote__choice="yes"))
            ).order_by("-votes", "order_number")

            results = [
                {
                    "position": i + 1,
                    "participant_order": p.order_number,
                    "participant_full_name": p.full_name,
                    "votes": p.votes
                }
                for i, p in enumerate(participants)
            ]

            if current_round.type == "individual":
                context["left_column"] = results
            else:
                mid = (len(results) + 1) // 2
                context["left_column"] = results[:mid]
                context["right_column"] = results[mid:]

            context["total_votes"] = Vote.objects.filter(round=current_round).count()

        if request.GET.get("ajax") == "1":
            return JsonResponse({
                "left_column": context["left_column"],
                "right_column": context["right_column"],
                "total_votes": context["total_votes"]
            })

        return render(request, "voting/results.html", context)
