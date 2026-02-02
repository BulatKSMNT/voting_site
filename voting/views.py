# voting/views.py (исправленный)
from django.shortcuts import render
from django.db.models import Count, Max
from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework import status
from django.utils import timezone
from .models import Round, Participant, Vote, Campaign
from .serializers import ParticipantSerializer, VoteCreateSerializer, CampaignSerializer, RoundSerializer

# Импорт для аутентификации
from rest_framework.authentication import TokenAuthentication
from rest_framework.permissions import IsAuthenticated, AllowAny


class CurrentRoundResults(APIView):
    permission_classes = [AllowAny]
    def get(self, request):
        active_rounds = Round.objects.filter(status="active").order_by("started_at")

        current_round = active_rounds.first() if active_rounds.exists() else None

        round_id_str = request.GET.get("round_id")
        if round_id_str:
            try:
                selected_id = int(round_id_str)
                current_round = active_rounds.filter(id=selected_id).first()
                if not current_round:
                    current_round = active_rounds.first()  # fallback
            except ValueError:
                pass  # если некорректный id → берём самый свежий

        context = {
            "round": current_round,
            "active_rounds": active_rounds,
            "selected_round_id": current_round.id if current_round else None,
            "total_votes": 0,
            "left_column": [],
            "right_column": [],
        }

        if current_round:
            participants_with_votes = Participant.objects.filter(round=current_round) \
                .annotate(votes=Count("vote")) \
                .order_by("-votes", "order_number", "full_name")

            results = [
                {
                    "participant_order": p.order_number,
                    "participant_full_name": p.full_name,
                    "votes": p.votes,
                }
                for p in participants_with_votes
            ]

            mid = (len(results) + 1) // 2
            left = results[:mid]
            right = results[mid:]

            context.update({
                "left_column": [{**item, "position": i + 1} for i, item in enumerate(left)],
                "right_column": [{**item, "position": mid + 1 + i} for i, item in enumerate(right)],
                "total_votes": Vote.objects.filter(round=current_round).count(),
            })

        return render(request, "voting/results.html", context)


class AddVoteAPIView(APIView):
    permission_classes = [AllowAny]
    def post(self, request):
        serializer = VoteCreateSerializer(data=request.data)
        if serializer.is_valid():
            serializer.save()
            return Response({"status": "Голос учтён"}, status=status.HTTP_201_CREATED)
        return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)


class ActiveRoundParticipants(APIView):
    permission_classes = [AllowAny]
    def get(self, request):
        round_obj = Round.objects.filter(status="active").order_by("-started_at").first()
        if not round_obj:
            return Response({
                "error_code": "no_active_round",
                "message": "Сейчас нет активного раунда. Голосование начнётся позже 🔥",
                "detail": "Следите за анонсами"
            }, status=200)

        participants = Participant.objects.filter(round=round_obj).order_by("order_number", "full_name")
        serializer = ParticipantSerializer(participants, many=True)

        return Response({
            "round_id": round_obj.id,
            "round_name": str(round_obj),
            "participants": serializer.data
        })


class ActiveRoundInfo(APIView):
    permission_classes = [AllowAny]

    def get(self, request):
        # Сначала ищем текущий раунд (is_current=True)
        round_obj = Round.objects.filter(is_current=True, status="active").first()

        # Если нет текущего — берём последний активный
        if not round_obj:
            round_obj = Round.objects.filter(status="active").order_by("-started_at").first()

        if not round_obj:
            return Response({"error": "Активного раунда нет"}, status=404)

        user_id_str = request.GET.get("user_id")
        user_vote = None
        if user_id_str:
            try:
                user_telegram_id = int(user_id_str)
                user_vote = Vote.objects.get(round=round_obj, user_telegram_id=user_telegram_id)
            except (ValueError, Vote.DoesNotExist):
                pass

        participants = Participant.objects.filter(round=round_obj).annotate(
            votes_count=Count("vote")
        ).order_by("-votes_count", "order_number", "full_name")

        data = {
            "round_id": round_obj.id,
            "round_name": str(round_obj),
            "status": round_obj.status,
            "participants": [
                {
                    "id": p.id,
                    "order_number": p.order_number,
                    "full_name": p.full_name,
                    "description": p.description,
                    "votes": p.votes_count
                }
                for p in participants
            ]
        }

        if user_vote:
            data["user_vote"] = {
                "participant_id": user_vote.participant.id,
                "participant_order": user_vote.participant.order_number,
                "participant_name": user_vote.participant.full_name,
                "voted_at": user_vote.created_at.isoformat()
            }

        return Response(data)


class ActiveRoundsList(APIView):
    permission_classes = [AllowAny]
    def get(self, request):
        rounds = Round.objects.filter(status="active").order_by("-started_at")
        serializer = RoundSerializer(rounds, many=True)
        if not serializer.data:
            return Response({"error": "Активных раундов нет"}, status=404)
        return Response({"rounds": serializer.data})


class ActiveCampaignsList(APIView):
    permission_classes = [AllowAny]
    def get(self, request):
        campaigns = Campaign.objects.filter(is_active=True).order_by('order_number')
        serializer = CampaignSerializer(campaigns, many=True)
        return Response({
            "campaigns": serializer.data,
            "total": campaigns.count()
        })


class CreateCampaignAPIView(APIView):
    authentication_classes = [TokenAuthentication]
    permission_classes = [IsAuthenticated]

    def post(self, request):
        name = request.data.get("name")
        admin_telegram_id = request.data.get("admin_telegram_id")

        if not name or not admin_telegram_id:
            return Response({"error": "name и admin_telegram_id обязательны"}, status=400)

        campaign = Campaign.objects.create(
            name=name.strip(),
            admin_telegram_id=int(admin_telegram_id)
        )
        return Response({
            "status": "ok",
            "campaign_id": campaign.id,
            "campaign_order_number": campaign.order_number,
            "message": f"Кампания #{campaign.order_number} '{name}' создана"
        })


class StartRoundAPIView(APIView):
    authentication_classes = [TokenAuthentication]
    permission_classes = [IsAuthenticated]

    def post(self, request):
        campaign_id = request.data.get("campaign_id")
        if not campaign_id:
            return Response({"error": "campaign_id обязателен"}, status=400)

        try:
            campaign = Campaign.objects.get(id=int(campaign_id))
            number = request.data.get("number")
            if number is None:
                max_number = Round.objects.filter(campaign=campaign).aggregate(
                    max_num=Max('number')
                )['max_num'] or 0
                number = max_number + 1

            winners_count = request.data.get("winners_count", 3)  # Исправлено: добавлено чтение winners_count из request, default=3

            round_obj = Round.objects.create(
                campaign=campaign,
                number=int(number),
                status="active",
                winners_count=winners_count  # Исправлено: сохранение winners_count в модель
            )
            return Response({
                "status": "ok",
                "round_id": round_obj.id,
                "round_number": round_obj.number,
                "message": f"Раунд №{round_obj.number} запущен в кампании {campaign.name}"
            })
        except Campaign.DoesNotExist:
            return Response({"error": "Кампания не найдена"}, status=404)
        except Exception as e:
            return Response({"error": str(e)}, status=400)


class EndRoundAPIView(APIView):
    authentication_classes = [TokenAuthentication]
    permission_classes = [IsAuthenticated]

    def post(self, request):
        round_id = request.data.get("round_id")
        if not round_id:
            return Response({"error": "round_id обязателен"}, status=400)

        try:
            round_obj = Round.objects.get(id=int(round_id))
            if round_obj.status == "ended":
                return Response({"error": "Раунд уже завершён"}, status=400)

            round_obj.status = "ended"
            round_obj.ended_at = timezone.now()
            round_obj.save(update_fields=["status", "ended_at"])

            # Считаем голоса по участникам
            participants_with_votes = Participant.objects.filter(round=round_obj).annotate(
                votes_count=Count("vote")
            ).order_by("-votes_count")

            if not participants_with_votes:
                return Response({
                    "status": "ok",
                    "message": f"Раунд #{round_obj.number} завершён",
                    "winners_count": round_obj.winners_count,
                    "winners": []
                })

            # Получаем уникальные значения голосов, отсортированные по убыванию
            unique_votes = participants_with_votes.values_list("votes_count", flat=True).distinct()

            # Берём топ-n уникальных баллов (или все, если меньше n)
            top_n_scores = list(unique_votes)[:round_obj.winners_count]

            if not top_n_scores:
                min_votes = 0
            elif len(top_n_scores) < round_obj.winners_count:
                # Если уникальных баллов меньше n — берём минимальный из имеющихся
                min_votes = min(top_n_scores)
            else:
                # Нормальный случай: берём n-й по величине
                min_votes = top_n_scores[-1]

            # Все участники с баллами >= min_votes
            winners = participants_with_votes.filter(votes_count__gte=min_votes)

            winners_data = [
                {
                    "participant_id": p.id,
                    "participant_order": p.order_number,
                    "full_name": p.full_name,
                    "votes": p.votes_count,
                }
                for p in winners
            ]

            return Response({
                "status": "ok",
                "message": f"Раунд #{round_obj.number} завершён",
                "winners_count": round_obj.winners_count,
                "winners": winners_data,
                "ended_round_campaign_id": round_obj.campaign.id
            })
        except Round.DoesNotExist:
            return Response({"error": "Раунд не найден"}, status=404)

class AddParticipantAPIView(APIView):
    authentication_classes = [TokenAuthentication]
    permission_classes = [IsAuthenticated]

    def post(self, request):
        round_id = request.data.get("round_id")
        full_name = request.data.get("full_name")
        description = request.data.get("description", "")

        if not round_id or not full_name:
            return Response({"error": "round_id и full_name обязательны"}, status=400)

        try:
            round_obj = Round.objects.get(id=int(round_id))
            if round_obj.status != "active":
                return Response({"error": "Раунд не активен"}, status=400)

            participant = Participant.objects.create(
                round=round_obj,
                full_name=full_name.strip().title(),
                description=description.strip()
            )
            return Response({
                "status": "ok",
                "participant_id": participant.id,
                "participant_order": participant.order_number,
                "message": f"Участник #{participant.order_number} {full_name} добавлен"
            })
        except Round.DoesNotExist:
            return Response({"error": "Раунд не найден"}, status=404)


# Новый эндпоинт: установить текущий раунд
class SetCurrentRoundAPIView(APIView):
    authentication_classes = [TokenAuthentication]
    permission_classes = [IsAuthenticated]

    def post(self, request):
        round_id = request.data.get("round_id")
        if not round_id:
            return Response({"error": "round_id обязателен"}, status=400)

        try:
            # Снимаем флаг со всех
            Round.objects.filter(is_current=True).update(is_current=False)
            # Ставим на выбранный
            round_obj = Round.objects.get(id=round_id, status="active")
            round_obj.is_current = True
            round_obj.save()
            return Response({"status": "ok", "message": f"Раунд {round_obj} теперь текущий"})
        except Round.DoesNotExist:
            return Response({"error": "Раунд не найден или не активен"}, status=404)


# Новый эндпоинт: получить ID текущего раунда (если нужно)
class GetCurrentRoundAPIView(APIView):
    permission_classes = [AllowAny]

    def get(self, request):
        round_obj = Round.objects.filter(is_current=True, status="active").first()
        if not round_obj:
            round_obj = Round.objects.filter(status="active").order_by("-started_at").first()
        if not round_obj:
            return Response({"current_round_id": None})
        return Response({"current_round_id": round_obj.id})