from django.views import View
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
from django.http import JsonResponse
import csv
import io


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
        action_type = request.data.get("action_type", "auto_individual")
        target_round_id = request.data.get("target_round_id")
        keep_votes = request.data.get("keep_votes", True)

        try:
            with transaction.atomic():
                round_obj = Round.objects.select_for_update().get(pk=pk)
                # 1. АВТОМАТИКА ДЛЯ ИНДИВИДУАЛЬНОГО РАУНДА
                if round_obj.type == "individual":
                    if round_obj.status == "ended":
                        return Response({"error": "Уже завершен"}, status=400)

                    round_obj.status = "ended"
                    round_obj.is_current = False
                    round_obj.ended_at = timezone.now()
                    round_obj.save()

                    p = Participant.objects.filter(round=round_obj).first()
                    if not p:
                        return Response(
                            {"is_individual": True, "message": "Индив. раунд завершен (участников не было)."})

                    # Ищем СТАНДАРТНЫЙ активный раунд (или создаем)
                    target_round = Round.objects.filter(campaign=round_obj.campaign, type="standard",
                                                        status="active").first()
                    if not target_round:
                        max_num = Round.objects.filter(campaign=round_obj.campaign).aggregate(m=Max('number'))['m'] or 0
                        target_round = Round.objects.create(campaign=round_obj.campaign, number=max_num + 1,
                                                            type="standard", status="active", winners_count=3)

                    # Переносим и сохраняем голоса ЗА
                    yes_votes = Vote.objects.filter(participant=p, choice="yes")
                    new_p = Participant.objects.create(round=target_round, full_name=p.full_name,
                                                       description=f"Из индив. раунда #{round_obj.number}")

                    Vote.objects.bulk_create([
                        Vote(round=target_round, participant=new_p, user_telegram_id=v.user_telegram_id)
                        for v in yes_votes
                    ], ignore_conflicts=True)

                    return Response({
                        "is_individual": True,
                        "message": f"🏁 Раунд завершен!\nУчастник <b>{p.full_name}</b> ({yes_votes.count()} голосов «ЗА») перенесен в Стандартный Раунд #{target_round.number}."
                    })

                # 2. ЗАВЕРШЕНИЕ СТАНДАРТНОГО РАУНДА (Возврат победителей боту)
                elif action_type == "end_standard":
                    round_obj.status = "published"  # Сразу публикуем результаты
                    round_obj.is_current = False
                    round_obj.ended_at = timezone.now()
                    round_obj.save()

                    participants = Participant.objects.filter(round=round_obj).annotate(
                        v_count=Count("vote", filter=Q(vote__choice__isnull=True) | Q(vote__choice="yes"))
                    ).order_by("-v_count")

                    unique_votes = list(participants.values_list("v_count", flat=True).distinct())[
                                   :round_obj.winners_count]
                    min_votes = min(unique_votes) if unique_votes else 0
                    winners = participants.filter(v_count__gte=min_votes)

                    winners_data = [{"id": w.id, "name": w.full_name, "votes": w.v_count} for w in winners]
                    return Response({
                        "is_individual": False,
                        "winners": winners_data,
                        "message": f"Раунд #{round_obj.number} завершен! Результаты на экране."
                    })

                # 3. ПЕРЕНОС ИЗ СТАНДАРТНОГО РАУНДА
                elif action_type == "transfer_standard":
                    target_round = Round.objects.get(id=target_round_id, type="standard", status="active")
                    winners_ids = request.data.get("winners_ids", [])
                    winners = Participant.objects.filter(id__in=winners_ids)

                    transfer_count = 0
                    for p in winners:
                        new_p = Participant.objects.create(round=target_round, full_name=p.full_name)
                        transfer_count += 1
                        if keep_votes:
                            voters = Vote.objects.filter(participant=p).filter(Q(choice="yes") | Q(choice__isnull=True))
                            Vote.objects.bulk_create([
                                Vote(round=target_round, participant=new_p, user_telegram_id=v.user_telegram_id)
                                for v in voters
                            ], ignore_conflicts=True)

                    return Response(
                        {"message": f"✅ Перенесено {transfer_count} финалистов в Раунд #{target_round.number}."})

        except Exception as e:
            return Response({"error": str(e)}, status=500)

    @action(detail=False, methods=['get'], permission_classes=[IsAuthenticated])
    def export_csv(self, request):
        """Собирает результаты всех кампаний и раундов в CSV формат"""
        output = io.StringIO()
        # Используем разделитель ';' — так русский Microsoft Excel сразу разбивает всё по колонкам
        writer = csv.writer(output, delimiter=';', dialect='excel')

        # Заголовки столбцов
        writer.writerow(['Кампания', 'Раунд', 'Тип', 'Статус', 'Место', 'Участник', 'Голоса ЗА'])

        # Получаем все раунды, сортируем по кампании и номеру
        rounds = Round.objects.all().select_related('campaign').order_by('campaign__order_number', 'number')

        for r in rounds:
            participants = Participant.objects.filter(round=r).annotate(
                votes=Count("vote", filter=Q(vote__choice__isnull=True) | Q(vote__choice="yes"))
            ).order_by("-votes")

            if not participants.exists():
                writer.writerow([r.campaign.name, f"№{r.number}", r.get_type_display(), r.get_status_display(), '-',
                                 'Нет участников', 0])
                continue

            for i, p in enumerate(participants, 1):
                writer.writerow(
                    [r.campaign.name, f"№{r.number}", r.get_type_display(), r.get_status_display(), i, p.full_name,
                     p.votes])

        return Response({"csv_content": output.getvalue()})


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

    def create(self, request, *args, **kwargs):
        # 1. Получаем ID раунда, куда бот хочет добавить человека
        round_id = request.data.get("round")

        if round_id:
            try:
                r = Round.objects.get(id=round_id)
                # 2. ЗАЩИТА: Если раунд Индивидуальный, и там УЖЕ есть 1 человек — блокируем!
                if r.type == "individual" and r.participants.count() >= 1:
                    return Response(
                        {"error": "В индивидуальном раунде может быть только 1 участник!"},
                        status=status.HTTP_400_BAD_REQUEST
                    )
            except Round.DoesNotExist:
                return Response({"error": "Раунд не найден."}, status=status.HTTP_404_NOT_FOUND)

        # 3. Если всё хорошо — сохраняем участника стандартным способом
        return super().create(request, *args, **kwargs)


class CurrentRoundResults(View):  # <-- Унаследовали от обычного View, а не от APIView
    def get(self, request):
        round_id_str = request.GET.get("round_id")
        current_round = None

        # 1. ЗАЩИТА: Если в URL указан раунд, берем его ТОЛЬКО если он не скрыт
        if round_id_str:
            try:
                current_round = Round.objects.filter(
                    id=int(round_id_str),
                    status__in=["active", "published"]  # <-- СТРОГАЯ ПРОВЕРКА СТАТУСА
                ).first()
            except ValueError:
                pass  # Защита, если кто-то руками введет ?round_id=абвгд

        # 2. Если по ссылке ничего не найдено (или раунд завершен), показываем текущий экран
        if not current_round:
            current_round = Round.objects.filter(is_current=True).first()

        # Список всех раундов для кнопок внизу
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
                {"position": i + 1, "participant_order": p.order_number, "participant_full_name": p.full_name,
                 "votes": p.votes} for i, p in enumerate(participants)]

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
                "right_column": context["right_column"]
            })

        return render(request, "voting/results.html", context)
