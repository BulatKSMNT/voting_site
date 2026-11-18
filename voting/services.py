import hashlib
import json
from html import escape
from django.db import transaction
from django.db.models import Max, Q
from django.utils import timezone
from rest_framework.exceptions import ValidationError
from .models import Campaign, Round, Participant, Vote, RoundOperation
from .audit import record, results, vote_event


def copy_participant(source, target, keep_votes, key, *, individual=False):
    participant = Participant.objects.create(
        round=target, full_name=source.full_name, source_participant_id=source.pk, transfer_key=key,
        description=f"Из индив. раунда #{source.round.number}" if individual else "")
    source_votes = source.vote_set.filter(choice="yes") if individual else source.vote_set.filter(Q(choice="yes") | Q(choice__isnull=True))
    votes = []
    if keep_votes:
        votes = Vote.objects.bulk_create([
            Vote(round=target, participant=participant, user_telegram_id=v.user_telegram_id,
                 source_vote_id=v.pk, source_created_at=v.source_created_at or v.created_at, transfer_key=key)
            for v in source_votes
        ])
        for vote in votes:
            vote_event(vote, "vote.transferred")
    record("participant.transferred", round_obj=source.round, participant_id=source.pk,
           details={"operation_key": key, "target_round_id": target.pk, "target_participant_id": participant.pk,
                    "keep_votes": keep_votes, "votes_count": len(votes)})
    record("participant.received", round_obj=target, participant_id=participant.pk,
           details={"operation_key": key, "source_round_id": source.round_id,
                    "source_participant_id": source.pk, "votes_count": len(votes)})
    return participant, len(votes)


@transaction.atomic
def end_and_transfer(round_id, data):
    if Round.objects.filter(pk=round_id).exclude(event_stage='').exists():
        from .tournament import finish_round
        if data.get('action_type') == 'transfer_standard':
            raise ValidationError('В мероприятии перенос выполняется автоматически при завершении тура.')
        return finish_round(round_id)
    campaign_id = Round.objects.values_list("campaign_id", flat=True).get(pk=round_id)
    # A single lock order prevents opposing transfers deadlocking in a campaign.
    Campaign.objects.select_for_update().get(pk=campaign_id)
    rd = Round.objects.select_for_update().select_related("campaign").get(pk=round_id)
    action = "auto_individual" if rd.type == "individual" else data.get("action_type", "auto_individual")
    if action not in {"auto_individual", "end_standard", "transfer_standard"} or (rd.type != "individual" and action == "auto_individual"):
        raise ValidationError({"error": "Неизвестный action_type"})
    keep = data.get("keep_votes", True)
    if not isinstance(keep, bool):
        raise ValidationError({"error": "keep_votes должен быть boolean."})
    try:
        target_id = int(data["target_round_id"]) if data.get("target_round_id") else None
        winner_ids = sorted(set(int(value) for value in data.get("winners_ids", [])))
    except (ValueError, TypeError):
        raise ValidationError({"error": "Некорректный ID раунда или участника."})
    signature = {"round": rd.pk, "cycle": rd.cycle, "action": action}
    if action == "end_standard":
        current = results(rd)
        signature.update(winners_count=rd.winners_count, participants=current['participants'],
                         total_votes=current['total_votes'], unique_voters=current['unique_voters'])
    else:
        signature.update(target=target_id, winners=winner_ids if action == "transfer_standard" else [],
                         keep=keep if action == "transfer_standard" else True)
    key = hashlib.sha256(json.dumps(signature, sort_keys=True).encode()).hexdigest()
    previous = RoundOperation.objects.filter(key=key).first()
    if previous:
        return previous.response

    if action == "end_standard":
        rd.status, rd.is_current, rd.ended_at = "published", False, timezone.now()
        rd.save()
        rows = results(rd)["participants"]
        response = {"is_individual": False,
                    "winners": [{"id": p["id"], "name": p["name"], "votes": p["yes"]} for p in rows if p["winner"]],
                    "message": f"Раунд #{rd.number} завершен! Результаты на экране."}
    else:
        if action == "auto_individual" and rd.status == "ended":
            raise ValidationError({"error": "Уже завершен"})
        if target_id == rd.pk:
            raise ValidationError({"error": "Нельзя переносить участников в тот же самый раунд."})
        targets = Round.objects.select_for_update().filter(campaign=rd.campaign, type="standard", status__in=["active", "published"])
        if target_id:
            target = targets.get(pk=target_id)
        elif action == "auto_individual":
            target = targets.first()
        else:
            raise ValidationError({"error": "Укажите целевой раунд."})

        if action == "auto_individual":
            rd.status, rd.is_current, rd.ended_at = "ended", False, timezone.now()
            rd.save()
            p = rd.participants.first()
            if p:
                if not target:
                    number = (rd.campaign.rounds.aggregate(m=Max("number"))["m"] or 0) + 1
                    target = Round.objects.create(campaign=rd.campaign, number=number, type="standard", status="published", winners_count=3)
                _, votes = copy_participant(p, target, True, key, individual=True)
                response = {"is_individual": True, "winners": [{"id": p.pk, "name": p.full_name, "votes": votes}],
                            "message": f"🏁 Раунд завершен!\nУчастник <b>{escape(p.full_name)}</b> ({votes} голосов «ЗА») перенесен в Стандартный Раунд #{target.number}."}
            else:
                response = {"is_individual": True, "message": "Индив. раунд завершен (участников не было)."}
        else:
            participants = list(rd.participants.filter(pk__in=winner_ids))
            for p in participants:
                copy_participant(p, target, keep, key)
            response = {"message": f"✅ Перенесено {len(participants)} финалистов в Раунд #{target.number}."}
    RoundOperation.objects.create(key=key, round_id=rd.pk, cycle=rd.cycle, action=action, response=response)
    return response
