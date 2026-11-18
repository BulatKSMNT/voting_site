"""Tour controls built on the existing rounds, preserving votes and history."""
import hashlib

from django.db import transaction
from django.utils import timezone
from rest_framework.exceptions import ValidationError

from .audit import record, results, snapshot
from .models import Campaign, Round, Participant, RoundOperation


def locked_round(pk):
    campaign_id = Round.objects.values_list('campaign_id', flat=True).get(pk=pk)
    Campaign.objects.select_for_update().get(pk=campaign_id)
    return Round.objects.select_for_update().get(pk=pk)


def event_state(campaign_id):
    rounds = Round.objects.filter(campaign_id=campaign_id).order_by('number').prefetch_related('participants')
    return [{'id': r.pk, 'number': r.number, 'stage': r.event_stage, 'status': r.status,
             'completed': r.event_completed, 'is_current': r.is_current, 'next_round': r.next_round_id,
             'parent_tour': r.next_round_id if r.event_stage == 'performance' else None,
             'name': r.display_name, 'type': r.type, 'winners_count': r.winners_count,
             'max_choices': r.max_choices} for r in rounds]


def clean_names(names, *, allow_empty=False):
    if not isinstance(names, list) or len(names) > 100 or (not names and not allow_empty):
        raise ValidationError('Передайте от 1 до 100 имён; каждое с новой строки.')
    if any(not isinstance(n, str) or not n.strip() or len(n.strip()) > 200 for n in names):
        raise ValidationError('Имя каждого участника должно содержать от 1 до 200 символов.')
    names = [n.strip() for n in names]
    if len({n.casefold() for n in names}) != len(names):
        raise ValidationError('В списке повторяются имена. Уточните имена однофамильцев.')
    return names


def validate_count(value):
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= 100:
        raise ValidationError('Число лучших уровней баллов (N): целое число от 1 до 100.')
    return value


@transaction.atomic
def create_tour(campaign_id, mode='individual', winners_count=8, names=None):
    campaign = Campaign.objects.select_for_update().get(pk=campaign_id, is_active=True)
    if mode not in {'individual', 'standard'}:
        raise ValidationError('Выберите индивидуальный или общий тур.')
    validate_count(winners_count)
    names = clean_names([] if names is None else names, allow_empty=True)
    stage = {'individual': 'qualification', 'standard': 'semifinal'}[mode]
    board = Round.objects.create(campaign=campaign, type='standard', event_stage=stage,
                                 status='published' if mode == 'individual' else 'pending',
                                 winners_count=winners_count, max_choices=3)
    if names:
        add_entries(board.pk, names)
    record('tour.created', round_obj=board, details={'mode': mode, 'winners_count': board.winners_count})
    return {'rounds': event_state(campaign.pk), 'tour_id': board.pk, 'qualification_id': board.pk}


@transaction.atomic
def add_entries(pk, names):
    rd = locked_round(pk)
    names = clean_names(names)
    if rd.event_stage not in {'qualification', 'semifinal'} or rd.ended_at or rd.event_completed:
        raise ValidationError('Добавление доступно в незавершённом индивидуальном или ещё не открытом общем туре.')
    if rd.event_stage == 'semifinal' and (rd.status != 'pending' or rd.votes.exists()):
        raise ValidationError('Состав общего тура можно менять только до открытия голосования.')
    existing = {n.casefold() for n in rd.participants.values_list('full_name', flat=True)}
    if existing.intersection(n.casefold() for n in names):
        raise ValidationError('Такое имя уже есть в туре. Уточните имя однофамильца.')
    if len(existing) + len(names) > 100:
        raise ValidationError('В одном туре может быть не более 100 участников.')
    for name in names:
        source = None
        if rd.event_stage == 'qualification':
            performance = Round.objects.create(campaign=rd.campaign, type='individual', event_stage='performance',
                                               status='pending', winners_count=1, max_choices=1, next_round=rd)
            source = Participant.objects.create(round=performance, full_name=name)
        Participant.objects.create(round=rd, full_name=name, source_participant_id=source.pk if source else None)
    record('tour.entries_added', round_obj=rd, details={'names': names})
    return {'message': f'Добавлено участников: {len(names)}.', 'tour_id': rd.pk}


@transaction.atomic
def configure_tour(pk, winners_count):
    rd = locked_round(pk)
    validate_count(winners_count)
    if rd.event_stage == 'performance' or rd.ended_at or rd.event_completed:
        raise ValidationError('Число уровней баллов меняется у тура до его завершения.')
    if rd.event_stage == 'final' and winners_count != 1:
        raise ValidationError('В финале выбирается один победитель.')
    rd.winners_count = winners_count
    rd.save(update_fields=['winners_count'])
    return {'message': f'В следующий тур проходят топ-{winners_count}.', 'winners_count': winners_count}


@transaction.atomic
def prepare_event(campaign_id, names, winners_count=8):
    campaign = Campaign.objects.select_for_update().get(pk=campaign_id, is_active=True)
    names = clean_names(names)
    validate_count(winners_count)
    existing = campaign.rounds.filter(event_stage='qualification').first()
    if existing:
        old = list(existing.participants.order_by('order_number').values_list('full_name', flat=True))
        if old != names or existing.winners_count != winners_count:
            raise ValidationError('Тур уже существует. Добавьте участников или настройте топ-N в его карточке.')
        return {'rounds': event_state(campaign.pk), 'qualification_id': existing.pk}
    prepared = create_tour(campaign_id, 'individual', winners_count, names)
    board = Round.objects.get(pk=prepared['tour_id'])
    if not campaign.rounds.filter(status='active').exists():
        board.is_current = True
        board.save(update_fields=['is_current'])
    return {'rounds': event_state(campaign.pk), 'qualification_id': board.pk}


@transaction.atomic
def start_round(pk):
    rd = locked_round(pk)
    if not rd.event_stage:
        if rd.status == 'ended':
            raise ValidationError('Архивный раунд открывает главный администратор командой /resurrect_round.')
        rd.status, rd.is_current = 'active', True
        rd.save(update_fields=['status', 'is_current'])
        return {'message': 'Голосование открыто.', 'round_id': rd.pk}
    if rd.event_stage not in {'performance', 'semifinal', 'final'} or rd.event_completed or rd.ended_at:
        raise ValidationError('Этот тур нельзя запустить. Завершённые туры мероприятия не открываются заново.')
    if not rd.participants.exists():
        raise ValidationError('Сначала добавьте участника в раунд.')
    if rd.status == 'active':
        rd.is_current = True
        rd.save(update_fields=['is_current'])
        return {'message': 'Голосование уже запущено.', 'round_id': rd.pk}
    if Round.objects.filter(campaign=rd.campaign, status='active').exclude(pk=rd.pk).exists():
        raise ValidationError('Сначала завершите текущее голосование кампании.')
    if rd.event_stage == 'performance' and rd.next_round.ended_at:
        raise ValidationError('Первый тур уже завершён.')
    rd.status, rd.is_current = 'active', True
    rd.save(update_fields=['status', 'is_current'])
    return {'message': 'Голосование запущено.', 'round_id': rd.pk}


def sync_extra_finalist(source, target):
    """Exactly one optional administrator entry, only while the final is pending."""
    old = target.participants.filter(is_admin_choice=True)
    for entry in old:
        entry.delete()
    if source.jury_choice_id:
        candidate = Participant.objects.get(pk=source.jury_choice_id, round=source)
        if target.participants.filter(source_participant_id=candidate.pk).exists():
            return  # The chosen candidate already qualified by votes.
        Participant.objects.create(round=target, full_name=candidate.full_name,
                                   source_participant_id=candidate.pk, is_admin_choice=True)
    elif source.jury_name:
        if target.participants.filter(full_name__iexact=source.jury_name).exists():
            raise ValidationError('Такой финалист уже есть в списке.')
        Participant.objects.create(round=target, full_name=source.jury_name, is_admin_choice=True)


@transaction.atomic
def set_jury(pk, participant_id=None, full_name=''):
    rd = locked_round(pk)
    if rd.type != 'standard' or rd.event_stage not in {'', 'semifinal'}:
        raise ValidationError('Дополнительный победитель выбирается в общем голосовании.')
    if participant_id and full_name:
        raise ValidationError('Выберите участника или введите имя нового, но не оба варианта.')
    if full_name and (rd.event_stage != 'semifinal' or not isinstance(full_name, str) or not 2 <= len(full_name.strip().split()) or len(full_name.strip()) > 200):
        raise ValidationError('Введите фамилию и имя (до 200 символов) для дополнительного финалиста.')
    if participant_id and not Participant.objects.filter(pk=participant_id, round=rd).exists():
        raise ValidationError('Участник должен принадлежать выбранному раунду.')
    if full_name and rd.participants.filter(full_name__iexact=full_name.strip()).exists():
        raise ValidationError('Этот участник уже есть во втором туре. Выберите его из списка.')
    if rd.event_completed and participant_id in {p['id'] for p in results(rd)['participants'] if p['auto_winner']}:
        raise ValidationError('Этот участник уже прошёл по голосам. Выберите дополнительного участника.')
    target = Round.objects.select_for_update().get(pk=rd.next_round_id) if rd.next_round_id else None
    if target and (target.status != 'pending' or target.votes.exists()):
        raise ValidationError('Состав финала нельзя менять после запуска голосования.')
    if rd.jury_choice_id == participant_id and rd.jury_name == full_name.strip():
        return {'message': 'Выбор уже сохранён.'}
    rd.jury_choice_id, rd.jury_name = participant_id, full_name.strip()
    rd.save(update_fields=['jury_choice', 'jury_name'])
    if target:
        sync_extra_finalist(rd, target)
    record('round.jury_selected', round_obj=rd, participant_id=participant_id,
           details={'full_name': rd.jury_name, 'final_round_id': rd.next_round_id})
    if rd.ended_at:
        snapshot(rd)
    return {'message': 'Выбор главного администратора сохранён.', 'final_round_id': rd.next_round_id}


@transaction.atomic
def resolve_tie(pk, participant_ids):
    rd = locked_round(pk)
    if rd.event_stage != 'final' or not rd.ended_at or rd.event_completed:
        raise ValidationError('Равенство разрешается вручную только в завершённом голосовании финала. В остальных турах проходят все с баллами из топ-N.')
    summary = results(rd)
    candidates = {p['id'] for p in summary['tie_candidates']}
    if len(participant_ids) != len(set(participant_ids)) or len(participant_ids) != summary['tie_slots'] or not set(participant_ids) <= candidates or not candidates:
        raise ValidationError(f"Выберите ровно {summary['tie_slots']} участников из списка с равными голосами.")
    rd.tie_break_ids = participant_ids
    rd.save(update_fields=['tie_break_ids'])
    record('round.tie_resolved', round_obj=rd, details={'selected_participant_ids': participant_ids})
    return finish_round(rd.pk)


@transaction.atomic
def finish_round(pk):
    rd = locked_round(pk)
    if not rd.event_stage:
        from .services import end_and_transfer
        return end_and_transfer(pk, {'action_type': 'end_standard' if rd.type == 'standard' else 'auto_individual'})
    key = hashlib.sha256(f'event-finish:{rd.pk}:{rd.cycle}'.encode()).hexdigest()
    previous = RoundOperation.objects.filter(key=key).first()
    if previous:
        return previous.response
    if not rd.participants.exists():
        raise ValidationError('В туре нет участников.')
    if rd.status == 'pending':
        raise ValidationError('Голосование ещё не запускалось.')
    if rd.event_stage == 'qualification' and rd.previous_rounds.filter(event_stage='performance', event_completed=False).exists():
        raise ValidationError('Сначала проведите и завершите все индивидуальные выступления.')
    if not rd.ended_at:
        rd.status = 'ended' if rd.event_stage == 'performance' else 'published'
        rd.ended_at = timezone.now()
        rd.is_current = rd.event_stage != 'performance'
        rd.save(update_fields=['status', 'ended_at', 'is_current'])
    summary = results(rd)
    if summary['tie_slots']:
        return {'message': 'Голосование закрыто. Главный администратор должен разрешить равенство голосов.',
                'needs_tiebreak': True, 'round_id': rd.pk, 'tie_slots': summary['tie_slots'],
                'tie_candidates': summary['tie_candidates']}
    auto = [p for p in summary['participants'] if p['auto_winner']]
    target = None
    if rd.event_stage == 'performance':
        target = Round.objects.select_for_update().get(pk=rd.next_round_id)
        target.is_current = True
        target.save(update_fields=['is_current'])
    elif rd.event_stage in {'qualification', 'semifinal'}:
        stage = 'semifinal' if rd.event_stage == 'qualification' else 'final'
        # Reuse a later, empty tour prepared by an administrator. Never overwrite a roster.
        target = Round.objects.select_for_update(of=('self',)).filter(
            campaign=rd.campaign, type='standard', event_stage=stage, status='pending',
            event_completed=False, ended_at__isnull=True, number__gt=rd.number,
            participants__isnull=True, votes__isnull=True, previous_rounds__isnull=True,
        ).order_by('number').first()
        if target is None:
            target = Round.objects.create(campaign=rd.campaign, type='standard', event_stage=stage, status='pending',
                                          winners_count=2 if stage == 'semifinal' else 1, max_choices=3 if stage == 'semifinal' else 1)
        for p in auto:
            Participant.objects.create(round=target, full_name=p['name'], source_participant_id=p['id'])
        if stage == 'final':
            sync_extra_finalist(rd, target)
        rd.next_round = target
    rd.event_completed = True
    rd.save(update_fields=['event_completed', 'next_round'])
    snapshot(rd)
    response = {'message': ('Индивидуальный раунд завершён. Результат учтён в таблице тура.' if rd.event_stage == 'performance' else 'Тур завершён.') + (' Следующий тур подготовлен; голоса начинаются с нуля.' if target and target.event_stage != 'qualification' else ''),
                'round_id': rd.pk, 'next_round_id': target.pk if target else None,
                'winners': [{'id': p['id'], 'name': p['name'], 'votes': p['yes']} for p in auto], 'needs_tiebreak': False}
    RoundOperation.objects.create(key=key, round_id=rd.pk, cycle=rd.cycle, action='event_finish', response=response)
    record('event.round_completed', round_obj=rd, details=response)
    return response
