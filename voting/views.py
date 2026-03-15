from django.shortcuts import render
from rest_framework.views import APIView
from rest_framework import viewsets, status
from rest_framework.decorators import action
from rest_framework.response import Response
from rest_framework.permissions import IsAuthenticated, AllowAny
from django.db import transaction, IntegrityError
from django.db.models import Count, Q,Max
from django.utils import timezone
from .models import Round, Participant, Vote, Campaign
from .serializers import ParticipantSerializer, VoteCreateSerializer, CampaignSerializer, RoundSerializer


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
        # Делаем копию данных, так как request.data менять нельзя
        data = request.data.copy()

        # Если бот не прислал номер (выбрано "Авто")
        if not data.get('number'):
            campaign_id = data.get('campaign')
            if campaign_id:
                # Ищем максимальный номер раунда в этой кампании
                max_num = Round.objects.filter(campaign_id=campaign_id).aggregate(m=Max('number'))['m'] or 0
                data['number'] = max_num + 1  # Подставляем следующий

        # Теперь передаем данные с готовым номером в строгий валидатор DRF
        serializer = self.get_serializer(data=data)
        serializer.is_valid(raise_exception=True)
        self.perform_create(serializer)
        headers = self.get_success_headers(serializer.data)
        return Response(serializer.data, status=status.HTTP_201_CREATED, headers=headers)

    @action(detail=False, methods=['get'], permission_classes=[AllowAny])
    def active_info(self, request):
        """Инфо для пользователей (выводит активные ИЛИ опубликованные раунды)"""
        round_obj = Round.objects.filter(is_current=True, status__in=["active", "published"]).first()
        if not round_obj:
            round_obj = Round.objects.filter(status__in=["active", "published"]).order_by("-started_at").first()

        if not round_obj:
            return Response({"error": "Нет активных раундов"}, status=404)

        user_id = request.GET.get("user_id")
        user_votes = Vote.objects.filter(round=round_obj, user_telegram_id=user_id) if user_id else []

        participants = Participant.objects.filter(round=round_obj).annotate(
            votes_count=Count("vote", filter=Q(vote__choice__isnull=True) | Q(vote__choice="yes"))
        ).order_by("-votes_count", "order_number")

        return Response({
            "round_id": round_obj.id,
            "round_name": str(round_obj),
            "round_type": round_obj.type,
            "status": round_obj.status,  # Бот будет знать, активен он или published
            "participants": [
                {"id": p.id, "full_name": p.full_name, "votes": p.votes_count, "order_number": p.order_number} for p in
                participants],
            "user_votes": [{"participant_id": v.participant_id, "choice": v.choice} for v in user_votes]
        })

    @action(detail=True, methods=['post'])
    def end_and_transfer(self, request, pk=None):
        """Единая транзакция: Завершение + Перенос"""
        target_round_id = request.data.get("target_round_id")
        publish = request.data.get("publish", False)  # Опубликовать сразу?

        try:
            with transaction.atomic():
                # Блокируем строку раунда от изменений другими запросами
                round_obj = Round.objects.select_for_update().get(pk=pk)

                if round_obj.status in ["ended", "published"]:
                    return Response({"error": "Раунд уже завершен"}, status=400)

                # Завершаем текущий
                round_obj.status = "published" if publish else "ended"
                round_obj.ended_at = timezone.now()
                round_obj.save(update_fields=["status", "ended_at"])

                # Вычисляем победителей
                participants = Participant.objects.filter(round=round_obj).annotate(
                    v_count=Count("vote", filter=Q(vote__choice__isnull=True) | Q(vote__choice="yes"))
                ).order_by("-v_count")

                unique_votes = list(participants.values_list("v_count", flat=True).distinct())[:round_obj.winners_count]
                min_votes = min(unique_votes) if unique_votes else 0
                winners = participants.filter(v_count__gte=min_votes)

                transfer_count = 0
                # Если указан целевой раунд - переносим
                if target_round_id and winners.exists():
                    target_round = Round.objects.get(id=target_round_id, status="active")
                    new_participants = []
                    new_votes = []

                    for p in winners:
                        new_p = Participant.objects.create(
                            round=target_round,
                            full_name=p.full_name,
                            description=f"Победитель раунда {round_obj.number}"
                        )
                        transfer_count += 1

                        # Копируем голоса (только YES для individual, или все для standard)
                        voters = Vote.objects.filter(participant=p).filter(Q(choice="yes") | Q(choice__isnull=True))
                        for v in voters:
                            new_votes.append(
                                Vote(round=target_round, participant=new_p, user_telegram_id=v.user_telegram_id))

                    # Массовое сохранение голосов (очень быстро)
                    Vote.objects.bulk_create(new_votes, ignore_conflicts=True)

            return Response({
                "message": f"Раунд завершен! Перенесено победителей: {transfer_count}",
                "winners": [{"name": w.full_name, "votes": w.v_count} for w in winners]
            })

        except Exception as e:
            return Response({"error": str(e)}, status=500)


class VoteViewSet(viewsets.ModelViewSet):
    queryset = Vote.objects.all()
    serializer_class = VoteCreateSerializer
    permission_classes = [AllowAny]

    def create(self, request, *args, **kwargs):
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        try:
            # Пытаемся сохранить
            serializer.save()
            return Response({"status": "Голос учтён"}, status=status.HTTP_201_CREATED)
        except IntegrityError:
            # База данных поймала дубликат! (Высокая нагрузка)
            return Response({"error": "Вы уже проголосовали за этого участника."}, status=status.HTTP_400_BAD_REQUEST)


class ParticipantViewSet(viewsets.ModelViewSet):
    queryset = Participant.objects.all()
    serializer_class = ParticipantSerializer
    permission_classes = [IsAuthenticated]


class CurrentRoundResults(APIView):
    permission_classes = [AllowAny]

    def get(self, request):
        # Берем опубликованный или активный раунд
        active_rounds = Round.objects.filter(status__in=["active", "published"]).order_by("-started_at")
        current_round = active_rounds.first()

        round_id_str = request.GET.get("round_id")
        if round_id_str:
            current_round = active_rounds.filter(id=int(round_id_str)).first() or current_round

        context = {"round": current_round, "total_votes": 0, "left_column": [], "right_column": []}

        if current_round:
            participants = Participant.objects.filter(round=current_round).annotate(
                votes=Count("vote", filter=Q(vote__choice__isnull=True) | Q(vote__choice="yes"))
            ).order_by("-votes", "order_number")

            results = [{"participant_order": p.order_number, "participant_full_name": p.full_name, "votes": p.votes} for
                       p in participants]

            if current_round.type == "individual":
                context["left_column"] = results
            else:
                mid = (len(results) + 1) // 2
                context["left_column"] = results[:mid]
                context["right_column"] = results[mid:]

            context["total_votes"] = Vote.objects.filter(round=current_round).count()

        return render(request, "voting/results.html", context)