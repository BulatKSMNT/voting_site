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
        round_obj = Round.objects.filter(status="active").order_by("-started_at").first()
        if not round_obj:
            return render(request, "voting/results.html", {"round": None})

        participants_with_votes = Participant.objects.filter(round=round_obj).annotate(
            votes=Count("vote")
        ).order_by("-votes", "order_number", "full_name")

        results = [
            {
                "participant_order": p.order_number,
                "participant_full_name": p.full_name,
                "votes": p.votes,
            }
            for p in participants_with_votes
        ]

        print(results)

        context = {
            "round": round_obj,
            "results": results,
            "total_votes": Vote.objects.filter(round=round_obj).count(),
        }
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
            return Response({"error": "Активного раунда нет"}, status=404)

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
                "message": f"Раунд #{round_obj.number} запущен в кампании #{campaign.order_number} {campaign.name}"
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

            winners = Participant.objects.filter(round=round_obj).annotate(
                votes_count=Count("vote")
            ).order_by("-votes_count", "order_number", "full_name")[:round_obj.winners_count]

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