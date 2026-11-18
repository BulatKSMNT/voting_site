from django.db import transaction
from django.db.models import Count, Max, Q
from core.observability import context, event


def record(name, *, round_obj=None, campaign_id=None, participant_id=None, vote_id=None,
           round_id=None, outcome="success", reason_code="", details=None):
    from .models import AuditEvent
    actor = context.get()
    row = AuditEvent.objects.create(
        event=name, source=actor.get("source", "system"), actor_id=actor.get("actor_id"),
        actor_role=actor.get("actor_role", ""), request_id=actor.get("request_id", ""),
        campaign_id=round_obj.campaign_id if round_obj else campaign_id,
        round_id=round_obj.pk if round_obj else round_id,
        participant_id=participant_id, vote_id=vote_id, outcome=outcome,
        reason_code=reason_code, details=details or {})
    fields = {"audit_event_id": row.pk, "round_id": row.round_id, "campaign_id": row.campaign_id,
              "participant_id": participant_id, "vote_id": vote_id, "outcome": outcome,
              "reason_code": reason_code, **actor}
    transaction.on_commit(lambda: event(name, **fields))
    return row


def state(instance):
    return {field.attname: getattr(instance, field.attname) for field in instance._meta.concrete_fields}


def result_votes(round_obj):
    from .models import Vote
    if round_obj.event_stage == 'qualification':
        return Vote.objects.filter(round__next_round=round_obj, round__event_stage='performance')
    return Vote.objects.filter(round=round_obj)


def results(round_obj):
    from .models import Participant, RoundResultSnapshot
    participants = list(Participant.objects.filter(round=round_obj).annotate(
        v_count=Count("vote", filter=Q(vote__choice__isnull=True) | Q(vote__choice="yes")),
        no_count=Count("vote", filter=Q(vote__choice="no")),
        transferred=Count("vote", filter=Q(vote__source_vote_id__isnull=False)),
    ).order_by("-v_count", "order_number", "pk"))
    votes = result_votes(round_obj)
    if round_obj.event_stage == 'qualification':
        counts = {row['participant_id']: row for row in votes.values('participant_id').annotate(
            yes=Count('pk', filter=Q(choice='yes') | Q(choice__isnull=True)), no=Count('pk', filter=Q(choice='no')))}
        for p in participants:
            p.v_count = counts.get(p.source_participant_id, {}).get('yes', 0)
            p.no_count = counts.get(p.source_participant_id, {}).get('no', 0)
        participants.sort(key=lambda p: (-p.v_count, p.order_number, p.pk))
    # Completed transfers keep the rule recorded at the time of completion.
    historical_exact = False
    if round_obj.event_completed and round_obj.type != 'individual':
        algorithm = RoundResultSnapshot.objects.filter(round_id=round_obj.pk, data__cycle=round_obj.cycle).values_list('data__algorithm', flat=True).first()
        historical_exact = algorithm == 'event-exact-count-v1'
    exact_count = round_obj.event_stage == 'final' or historical_exact
    score_levels = sorted({p.v_count for p in participants}, reverse=True)
    score_ranks = {score: i for i, score in enumerate(score_levels, 1)}
    tie_candidates, tie_slots = [], 0
    if round_obj.type == "individual":
        winner_ids = {p.pk for p in participants[:1]}
    elif exact_count:
        count = round_obj.winners_count
        winner_ids = {p.pk for p in participants[:count]}
        if len(participants) > count and participants[count - 1].v_count == participants[count].v_count:
            cutoff = participants[count - 1].v_count
            above = [p for p in participants if p.v_count > cutoff]
            tied = [p for p in participants if p.v_count == cutoff]
            needed = count - len(above)
            chosen = set(round_obj.tie_break_ids)
            winner_ids = {p.pk for p in above}
            if len(chosen) == needed and chosen <= {p.pk for p in tied}:
                winner_ids.update(chosen)
                # Among equal scores, show the administrator's selected candidates first.
                participants.sort(key=lambda p: (-p.v_count, p.pk not in chosen, p.order_number, p.pk))
            else:
                tie_candidates = [{'id': p.pk, 'name': p.full_name, 'votes': p.v_count} for p in tied]
                tie_slots = needed
    else:
        # Top-N means N distinct score values, including every tied participant.
        selected_scores = set(score_levels[:round_obj.winners_count])
        winner_ids = {p.pk for p in participants if p.v_count in selected_scores}
    rows = [{"id": p.pk, "name": p.full_name, "position": index if exact_count or round_obj.type == "individual" else score_ranks[p.v_count], "order_number": p.order_number,
             "yes": p.v_count, "no": p.no_count, "transferred": p.transferred,
             "source_participant_id": p.source_participant_id, "auto_winner": p.pk in winner_ids,
             "jury_winner": p.pk == round_obj.jury_choice_id, "is_admin_choice": p.is_admin_choice,
             "winner": p.pk in winner_ids or p.pk == round_obj.jury_choice_id}
            for index, p in enumerate(participants, 1)]
    return {"campaign": round_obj.campaign.name, "round_number": round_obj.number,
            "type": round_obj.type, "status": round_obj.status, "cycle": round_obj.cycle,
            "event_stage": round_obj.event_stage, "event_completed": round_obj.event_completed,
            "max_choices": round_obj.max_choices, "jury_choice_id": round_obj.jury_choice_id, "jury_name": round_obj.jury_name,
            "tie_candidates": tie_candidates, "tie_slots": tie_slots,
            "winners_count": round_obj.winners_count, "started_at": round_obj.started_at,
            "ended_at": round_obj.ended_at, "algorithm": "event-exact-count-v1" if historical_exact else "final-single-winner-v1" if exact_count else "score-levels-v2",
            "total_votes": votes.count(),
            "unique_voters": votes.values("user_telegram_id").distinct().count(),
            "participants": rows}


def displayed_participants(round_obj, summary=None):
    rows = (summary if summary is not None else results(round_obj))['participants']
    if round_obj.event_stage == 'qualification':
        return [p for p in rows if p['auto_winner']]
    if round_obj.event_stage == 'semifinal' and round_obj.event_completed:
        selected = [p for p in rows if p['winner']]
        if round_obj.jury_name and round_obj.next_round_id:
            # A finalist entered by name has no votes in this source tour.
            # Include the actual transferred entrant without creating a source participant.
            from .models import Participant
            extra = Participant.objects.filter(round_id=round_obj.next_round_id,
                full_name=round_obj.jury_name, is_admin_choice=True, source_participant_id__isnull=True).first()
            if extra:
                selected.append({'id': extra.pk, 'name': extra.full_name,
                    'position': max((p['position'] for p in selected), default=0) + 1,
                    'order_number': extra.order_number, 'yes': 0, 'no': 0, 'transferred': 0,
                    'source_participant_id': None, 'auto_winner': False, 'jury_winner': True,
                    'is_admin_choice': True, 'winner': True})
        return selected
    # While voting is open, and in the final, show the entire roster.
    return rows


def snapshot(round_obj):
    from .models import RoundResultSnapshot
    from .reports import round_payload
    version = (RoundResultSnapshot.objects.filter(round_id=round_obj.pk).aggregate(v=Max("version"))["v"] or 0) + 1
    return RoundResultSnapshot.objects.create(campaign_id=round_obj.campaign_id, round_id=round_obj.pk,
                                               version=version, data=round_payload(round_obj))


def vote_event(vote, name="vote.accepted"):
    return record(name, round_obj=vote.round, participant_id=vote.participant_id, vote_id=vote.pk,
                  details=state(vote))


def api_exception_handler(exc, ctx):
    from rest_framework.views import exception_handler
    from .permissions import bind_identity
    from .models import Round, Participant
    response = exception_handler(exc, ctx)
    request = ctx.get("request")
    if response is not None:
        def first_code(codes):
            if isinstance(codes, dict):
                return first_code(next(iter(codes.values()), "request_rejected"))
            if isinstance(codes, list):
                return first_code(codes[0]) if codes else "request_rejected"
            return str(codes)
        code = first_code(exc.get_codes()) if hasattr(exc, "get_codes") else "request_rejected"
        if isinstance(response.data, dict):
            response.data.setdefault("code", code)
            response.data["request_id"] = getattr(request, "request_id", "")
        if request and request.method not in {"GET", "HEAD", "OPTIONS"}:
            bind_identity(request)
            view = ctx['view']
            raw = request.data.get("round")
            if not raw and view.basename == 'round':
                raw = view.kwargs.get('pk')
            if not raw and view.basename == 'participant':
                raw = Participant.objects.filter(pk=view.kwargs.get('pk')).values_list('round_id', flat=True).first()
            valid_id = str(raw).isdigit() and len(str(raw)) <= 18
            rd = Round.objects.filter(pk=raw).first() if valid_id else None
            if rd:
                record("vote.rejected" if view.basename == 'vote' else "request.rejected",
                       round_obj=rd, outcome="rejected", reason_code=code,
                       details={"status": response.status_code, "action": view.action, "errors": response.data})
    return response
