# voting/views.py (обновлённый)
from django.shortcuts import render
from django.db.models import Count, Max, Q
from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework import status
from django.utils import timezone
from .models import Round, Participant, Vote, Campaign
from .serializers import ParticipantSerializer, VoteCreateSerializer, CampaignSerializer, RoundSerializer, \
    TransferWinnersSerializer, StartRoundSerializer, EndRoundSerializer
# Импорт для аутентификации
from rest_framework.authentication import TokenAuthentication
from rest_framework.permissions import IsAuthenticated, AllowAny

class CurrentRoundResults(APIView):
    permission_classes = [AllowAny]

    def get(self, request):
        try:
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
                    pass
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
                    .annotate(votes=Count("vote", filter=Q(vote__choice__isnull=True) | Q(vote__choice="yes"))) \
                    .order_by("-votes", "order_number", "full_name")
                results = [
                    {
                        "participant_order": p.order_number,
                        "participant_full_name": p.full_name,
                        "votes": p.votes,
                    } for p in participants_with_votes
                ]
                if current_round.type == "individual":
                    # Для индивидуального — только один столбец, максимум 1 участник
                    mid = len(results)  # всё в левую колонку
                    context["left_column"] = [{**item, "position": i + 1} for i, item in enumerate(results)]
                    context["right_column"] = []  # пустой правый столбец
                else:
                    # Обычная логика для стандартного
                    mid = (len(results) + 1) // 2
                    left = results[:mid]
                    right = results[mid:]
                    context["left_column"] = [{**item, "position": i + 1} for i, item in enumerate(left)]
                    context["right_column"] = [{**item, "position": mid + 1 + i} for i, item in enumerate(right)]
                context["total_votes"] = Vote.objects.filter(round=current_round).count()
            return render(request, "voting/results.html", context)
        except Exception as e:
            return Response({"error": str(e)}, status=500)

class AddVoteAPIView(APIView):
    permission_classes = [AllowAny]

    def post(self, request):
        try:
            serializer = VoteCreateSerializer(data=request.data)
            if serializer.is_valid():
                serializer.save()
                return Response({"status": "Голос учтён"}, status=status.HTTP_201_CREATED)
            return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)
        except Exception as e:
            return Response({"error": str(e)}, status=500)

class ActiveRoundParticipants(APIView):
    permission_classes = [AllowAny]

    def get(self, request):
        try:
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
                "round_type": round_obj.type,
                "participants": serializer.data
            })
        except Exception as e:
            return Response({"error": str(e)}, status=500)

class ActiveRoundInfo(APIView):
    permission_classes = [AllowAny]

    def get(self, request):
        try:
            # Сначала ищем текущий раунд (is_current=True)
            round_obj = Round.objects.filter(is_current=True, status="active").first()
            # Если нет текущего — берём последний активный
            if not round_obj:
                round_obj = Round.objects.filter(status="active").order_by("-started_at").first()
            if not round_obj:
                return Response({"error": "Активного раунда нет"}, status=404)
            user_id_str = request.GET.get("user_id")
            user_votes = None
            if user_id_str:
                try:
                    user_telegram_id = int(user_id_str)
                    user_votes = Vote.objects.filter(round=round_obj, user_telegram_id=user_telegram_id)
                except ValueError:
                    pass
            participants = Participant.objects.filter(round=round_obj).annotate(
                votes_count=Count("vote", filter=Q(vote__choice__isnull=True) | Q(vote__choice="yes"))
            ).order_by("-votes_count", "order_number", "full_name")
            data = {
                "round_id": round_obj.id,
                "round_name": str(round_obj),
                "round_type": round_obj.type,
                "status": round_obj.status,
                "participants": [
                    {
                        "id": p.id,
                        "order_number": p.order_number,
                        "full_name": p.full_name,
                        "description": p.description,
                        "votes": p.votes_count
                    } for p in participants
                ]
            }
            if user_votes:
                data["user_votes"] = [
                    {
                        "participant_id": vote.participant.id,
                        "participant_order": vote.participant.order_number,
                        "participant_name": vote.participant.full_name,
                        "choice": vote.choice,
                        "voted_at": vote.created_at.isoformat()
                    } for vote in user_votes
                ]
            return Response(data)
        except Exception as e:
            return Response({"error": str(e)}, status=500)

class ActiveRoundsList(APIView):
    permission_classes = [AllowAny]

    def get(self, request):
        try:
            rounds = Round.objects.filter(status="active").order_by("-started_at")
            serializer = RoundSerializer(rounds, many=True)
            if not serializer.data:
                return Response({"error": "Активных раундов нет"}, status=404)
            return Response({"rounds": serializer.data})
        except Exception as e:
            return Response({"error": str(e)}, status=500)

class ActiveCampaignsList(APIView):
    permission_classes = [AllowAny]

    def get(self, request):
        try:
            campaigns = Campaign.objects.filter(is_active=True).order_by('order_number')
            serializer = CampaignSerializer(campaigns, many=True)
            return Response({
                "campaigns": serializer.data,
                "total": campaigns.count()
            })
        except Exception as e:
            return Response({"error": str(e)}, status=500)

class CreateCampaignAPIView(APIView):
    authentication_classes = [TokenAuthentication]
    permission_classes = [IsAuthenticated]

    def post(self, request):
        try:
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
        except Exception as e:
            return Response({"error": str(e)}, status=500)

class StartRoundAPIView(APIView):
    authentication_classes = [TokenAuthentication]
    permission_classes = [IsAuthenticated]

    def post(self, request):
        try:
            print("Полученные данные в API:", request.data)
            serializer = StartRoundSerializer(data=request.data)
            if not serializer.is_valid():
                print("Ошибки валидации:", serializer.errors)
                return Response(serializer.errors, status=400)
            data = serializer.validated_data
            campaign = Campaign.objects.get(id=data["campaign_id"])
            number = data.get("number")
            if number is None:
                max_number = Round.objects.filter(campaign=campaign).aggregate(max_num=Max('number'))['max_num'] or 0
                number = max_number + 1
            winners_count = data["winners_count"]
            round_type = data["type"]
            round_obj = Round.objects.create(
                campaign=campaign,
                number=number,
                status="active",
                winners_count=winners_count,
                type=round_type
            )
            return Response({
                "status": "ok",
                "round_id": round_obj.id,
                "round_number": round_obj.number,
                "round_type": round_obj.type,
                "message": f"Раунд №{round_obj.number} ({round_obj.get_type_display()}) запущен"
            })
        except Campaign.DoesNotExist:
            return Response({"error": "Кампания не найдена"}, status=404)
        except Exception as e:
            print("Исключение в StartRoundAPIView:", str(e))
            return Response({"error": str(e)}, status=500)

class EndRoundAPIView(APIView):
    authentication_classes = [TokenAuthentication]
    permission_classes = [IsAuthenticated]

    def post(self, request):
        try:
            serializer = EndRoundSerializer(data=request.data)
            if not serializer.is_valid():
                return Response(serializer.errors, status=400)

            round_id = serializer.validated_data["round_id"]
            round_obj = Round.objects.get(id=round_id)

            if round_obj.status == "ended":
                return Response({"error": "Раунд уже завершён"}, status=400)

            round_obj.status = "ended"
            round_obj.ended_at = timezone.now()
            round_obj.save(update_fields=["status", "ended_at"])

            # ── ТВОЯ ОРИГИНАЛЬНАЯ ЛОГИКА ПОДСЧЁТА ──
            participants_with_votes = Participant.objects.filter(round=round_obj) \
                .annotate(votes_count=Count("vote", filter=Q(vote__choice__isnull=True) | Q(vote__choice="yes"))) \
                .order_by("-votes_count")

            if not participants_with_votes:
                return Response({
                    "status": "ok",
                    "message": f"Раунд #{round_obj.number} завершён",
                    "winners_count": round_obj.winners_count,
                    "winners": [],
                    "round_type": round_obj.type,
                    "ended_round_campaign_id": round_obj.campaign.id
                })

            unique_votes = participants_with_votes.values_list("votes_count", flat=True).distinct()
            top_n_scores = list(unique_votes)[:round_obj.winners_count]

            if not top_n_scores:
                min_votes = 0
            elif len(top_n_scores) < round_obj.winners_count:
                min_votes = min(top_n_scores)
            else:
                min_votes = top_n_scores[-1]

            winners = participants_with_votes.filter(votes_count__gte=min_votes)
            winners_data = []

            for p in winners:
                winner_dict = {
                    "participant_id": p.id,
                    "participant_order": p.order_number,
                    "full_name": p.full_name,
                    "votes": p.votes_count,
                }
                if round_obj.type == "individual":
                    yes_voters = Vote.objects.filter(
                        participant=p, choice="yes"
                    ).values_list("user_telegram_id", flat=True)
                    winner_dict["yes_voters"] = list(yes_voters)
                winners_data.append(winner_dict)

            response_data = {
                "status": "ok",
                "message": f"Раунд #{round_obj.number} завершён",
                "winners_count": round_obj.winners_count,
                "winners": winners_data,
                "round_type": round_obj.type,
                "ended_round_campaign_id": round_obj.campaign.id
            }
            return Response(response_data)

        except Round.DoesNotExist:
            return Response({"error": "Раунд не найден"}, status=404)
        except Exception as e:
            import traceback
            traceback.print_exc()           # ← чтобы в консоли видеть точную причину
            return Response({"error": str(e)}, status=500)

class AddParticipantAPIView(APIView):
    authentication_classes = [TokenAuthentication]
    permission_classes = [IsAuthenticated]

    def post(self, request):
        try:
            round_id = request.data.get("round_id")
            full_name = request.data.get("full_name")
            description = request.data.get("description", "")
            if not round_id or not full_name:
                return Response({"error": "round_id и full_name обязательны"}, status=400)
            round_obj = Round.objects.get(id=int(round_id))
            if round_obj.status != "active":
                return Response({"error": "Раунд не активен"}, status=400)
            # Для individual — опционально ограничить на одного, но не обязательно (если несколько — ок)
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
        except Exception as e:
            return Response({"error": str(e)}, status=500)

# Новый эндпоинт: установить текущий раунд
class SetCurrentRoundAPIView(APIView):
    authentication_classes = [TokenAuthentication]
    permission_classes = [IsAuthenticated]

    def post(self, request):
        try:
            round_id = request.data.get("round_id")
            if not round_id:
                return Response({"error": "round_id обязателен"}, status=400)
            # Снимаем флаг со всех
            Round.objects.filter(is_current=True).update(is_current=False)
            # Ставим на выбранный
            round_obj = Round.objects.get(id=round_id, status="active")
            round_obj.is_current = True
            round_obj.save()
            return Response({"status": "ok", "message": f"Раунд {round_obj} теперь текущий"})
        except Round.DoesNotExist:
            return Response({"error": "Раунд не найден или не активен"}, status=404)
        except Exception as e:
            return Response({"error": str(e)}, status=500)

# Новый эндпоинт: получить ID текущего раунда (если нужно)
class GetCurrentRoundAPIView(APIView):
    permission_classes = [AllowAny]

    def get(self, request):
        try:
            round_obj = Round.objects.filter(is_current=True, status="active").first()
            if not round_obj:
                round_obj = Round.objects.filter(status="active").order_by("-started_at").first()
            if not round_obj:
                return Response({"current_round_id": None})
            return Response({"current_round_id": round_obj.id})
        except Exception as e:
            return Response({"error": str(e)}, status=500)

class TransferWinnersAPIView(APIView):
    authentication_classes = [TokenAuthentication]
    permission_classes = [IsAuthenticated]

    def post(self, request):
        try:
            serializer = TransferWinnersSerializer(data=request.data)
            if not serializer.is_valid():
                return Response(serializer.errors, status=400)

            data = serializer.validated_data
            round_id = data["round_id"]
            target_round_id = data["target_round_id"]

            round_obj = Round.objects.get(id=round_id)
            if round_obj.status != "ended":
                return Response(
                    {"error": "Исходный раунд должен быть завершён для переноса"},
                    status=400
                )

            target_round = Round.objects.get(
                id=target_round_id,
                status="active",
                type="standard"
            )

            participants_with_votes = Participant.objects.filter(round=round_obj) \
                .annotate(votes_count=Count("vote", filter=Q(vote__choice__isnull=True) | Q(vote__choice="yes"))) \
                .order_by("-votes_count")

            if not participants_with_votes.exists():
                return Response({
                    "status": "ok",
                    "message": "В раунде нет участников с голосами — перенос не требуется",
                    "transferred": 0,
                    "transferred_votes": 0
                })

            unique_votes = participants_with_votes.values_list("votes_count", flat=True).distinct()
            top_n_scores = list(unique_votes)[:round_obj.winners_count]

            if not top_n_scores:
                min_votes = 0
            elif len(top_n_scores) < round_obj.winners_count:
                min_votes = min(top_n_scores)
            else:
                min_votes = top_n_scores[-1]

            winners = participants_with_votes.filter(votes_count__gte=min_votes)

            transfer_count = 0
            total_transferred_votes = 0

            for p in winners:
                yes_voters = Vote.objects.filter(
                    participant=p, choice="yes"
                ).values_list("user_telegram_id", flat=True)

                votes_count = len(yes_voters)
                total_transferred_votes += votes_count

                new_participant = Participant.objects.create(
                    round=target_round,
                    full_name=p.full_name,
                    description=(
                        f"Перенесён из индивидуального раунда №{round_obj.number} "
                        f"(перенесено {votes_count} голосов «Да»)"
                    )
                )

                for user_tg_id in yes_voters:
                    if not Vote.objects.filter(
                        round=target_round,
                        participant=new_participant,
                        user_telegram_id=user_tg_id
                    ).exists():
                        Vote.objects.create(
                            round=target_round,
                            participant=new_participant,
                            user_telegram_id=user_tg_id,
                            choice=None
                        )

                transfer_count += 1

            return Response({
                "status": "ok",
                "message": f"Перенесено {transfer_count} участников с {total_transferred_votes} голосами в раунд №{target_round.number}",
                "transferred": transfer_count,
                "transferred_votes": total_transferred_votes,
                "target_round_id": target_round.id,
                "target_round_number": target_round.number
            })

        except Round.DoesNotExist:
            return Response({"error": "Исходный или целевой раунд не найден"}, status=404)
        except Exception as e:
            import traceback
            traceback.print_exc()           # ← для диагностики в консоли
            return Response({"error": f"Ошибка при переносе: {str(e)}"}, status=500)