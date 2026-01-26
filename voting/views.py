from django.shortcuts import render
from django.db.models import Count
from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework import status
from .models import Round, Participant, Vote, Campaign
from .serializers import ParticipantSerializer, VoteCreateSerializer
from django.utils import timezone  # только если нужно для ended_at, но можно убрать
from .serializers import CampaignSerializer

class CurrentRoundResults(APIView):
    """HTML-страница результатов текущего активного раунда"""
    def get(self, request):
        round_obj = Round.objects.filter(status="active").order_by("-started_at").first()
        if not round_obj:
            return render(request, "voting/results.html", {"round": None})
        participants_with_votes = Participant.objects.filter(round=round_obj).annotate(
            votes=Count("vote")  # ← vote — это related_name от Vote к Participant
        ).order_by("-votes", "full_name")
        results = [
            {
                "participant__full_name": p.full_name,
                "votes": p.votes,
            }
            for p in participants_with_votes
        ]
        # Топ по голосам
        # results = Vote.objects.filter(round=round_obj) \
        #     .values("participant__full_name") \
        #     .annotate(votes=Count("id")) \
        #     .order_by("-votes")

        # Все участники по алфавиту
        participants = Participant.objects.filter(round=round_obj).order_by("full_name")

        context = {
            "round": round_obj,
            "results": list(results),
            "participants": participants,
            "total_votes": Vote.objects.filter(round=round_obj).count(),
        }
        return render(request, "voting/results.html", context)


class AddVoteAPIView(APIView):
    """Добавление голоса (POST от бота)"""
    def post(self, request):
        serializer = VoteCreateSerializer(data=request.data)
        if serializer.is_valid():
            serializer.save()
            return Response({"status": "Голос учтён"}, status=status.HTTP_201_CREATED)
        return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)


class ActiveRoundParticipants(APIView):
    """Список участников активного раунда (для кнопок голосования)"""
    def get(self, request):
        round_obj = Round.objects.filter(status="active").order_by("-started_at").first()
        if not round_obj:
            return Response({"error": "Активного раунда нет"}, status=status.HTTP_404_NOT_FOUND)

        participants = Participant.objects.filter(round=round_obj).order_by("full_name")
        serializer = ParticipantSerializer(participants, many=True)

        return Response({
            "round_id": round_obj.id,
            "round_name": str(round_obj),
            "participants": serializer.data
        })


class ActiveRoundInfo(APIView):
    """Полная информация об активном раунде + статус голоса текущего пользователя"""
    def get(self, request):
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
        ).order_by("-votes_count", "full_name")

        data = {
            "round_id": round_obj.id,
            "round_name": str(round_obj),
            "status": round_obj.status,
            "participants": [
                {
                    "id": p.id,
                    "full_name": p.full_name,
                    "description": p.description,
                    "votes": p.votes_count
                }
                for p in participants
            ]
        }

        if user_vote:
            data["user_vote"] = {
                "participant_id": user_vote.participant_id,
                "participant_name": user_vote.participant.full_name,
                "voted_at": user_vote.created_at.isoformat()
            }

        return Response(data)


class ActiveRoundsList(APIView):
    """Список всех активных раундов (для выбора в админ-командах бота)"""
    def get(self, request):
        rounds = Round.objects.filter(status="active").order_by("-started_at")
        data = [
            {
                "round_id": r.id,
                "round_name": str(r),
                "campaign_name": r.campaign.name,
                "campaign_id": r.campaign.id,
                "participants_count": r.participants.count()
            }
            for r in rounds
        ]

        if not data:
            return Response({"error": "Активных раундов нет"}, status=404)

        return Response(data)


# voting/views.py — добавь класс для создания Campaign, измени StartRoundAPIView

class CreateCampaignAPIView(APIView):
    """Создать новую кампанию"""
    def post(self, request):
        name = request.data.get("name")
        admin_telegram_id = request.data.get("admin_telegram_id")

        if not name or not admin_telegram_id:
            return Response({"error": "name и admin_telegram_id обязательны"}, status=400)

        try:
            campaign = Campaign.objects.create(
                name=name,
                admin_telegram_id=int(admin_telegram_id),
                is_active=True
            )
            return Response({
                "status": "ok",
                "campaign_id": campaign.id,
                "message": f"Кампания '{name}' создана"
            })
        except Exception as e:
            return Response({"error": str(e)}, status=400)


class StartRoundAPIView(APIView):
    """Создать и запустить новый раунд (автоматический number, без duration)"""
    def post(self, request):
        campaign_id = request.data.get("campaign_id")
        # number теперь опциональный — если не передан, берём max +1

        if not campaign_id:
            return Response({"error": "campaign_id обязателен"}, status=400)

        try:
            campaign = Campaign.objects.get(id=int(campaign_id))
            number = request.data.get("number")
            if number is None:
                max_number = Round.objects.filter(campaign=campaign).aggregate(max_num=max('number'))['max_num'] or 0
                number = max_number + 1

            round_obj = Round.objects.create(
                campaign=campaign,
                number=int(number),
                status="active",
                # без duration_minutes
            )
            return Response({
                "status": "ok",
                "round_id": round_obj.id,
                "message": f"Раунд {number} запущен в кампании {campaign.name}"
            })
        except Campaign.DoesNotExist:
            return Response({"error": "Кампания не найдена"}, status=404)
        except Exception as e:
            return Response({"error": str(e)}, status=400)

# EndRoundAPIView — без изменений, уже возвращает winners
class EndRoundAPIView(APIView):
    """Завершить раунд и вернуть топ-N победителей"""
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

            # Получаем топ-N победителей
            winners = Participant.objects.filter(round=round_obj).annotate(
                votes_count=Count("vote")
            ).order_by("-votes_count", "full_name")[:round_obj.winners_count]

            winners_data = [
                {
                    "participant_id": p.id,
                    "full_name": p.full_name,
                    "votes": p.votes_count,
                }
                for p in winners
            ]

            return Response({
                "status": "ok",
                "message": f"Раунд {round_obj} завершён",
                "winners_count": round_obj.winners_count,
                "winners": winners_data,
                "ended_round_campaign_id": round_obj.campaign.id
            })
        except Round.DoesNotExist:
            return Response({"error": "Раунд не найден"}, status=404)


class AddParticipantAPIView(APIView):
    """Добавить участника в раунд"""
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
                "message": f"Участник {full_name} добавлен"
            })
        except Round.DoesNotExist:
            return Response({"error": "Раунд не найден"}, status=404)

class ActiveCampaignsList(APIView):
    def get(self, request):
        print(Campaign.objects.filter(is_active=True).exists())
        campaigns = Campaign.objects.filter(is_active=True).order_by('-created_at')
        data = [
            {
                "id": c.id,
                "name": c.name,
                "admin_telegram_id": c.admin_telegram_id,
                "rounds_count": c.rounds.count(),
            }
            for c in campaigns
        ]
        print(data)
        return Response({
            "campaigns": data,
            "total": len(data)
        })