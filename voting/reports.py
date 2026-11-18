"""Private, self-contained reports. Public scoreboards never include voter identities."""
import csv
import io
import json
from contextlib import contextmanager
from zipfile import ZipFile, ZIP_DEFLATED

from django.core.serializers.json import DjangoJSONEncoder
from django.db import connection, transaction
from django.utils import timezone
from django.utils.dateparse import parse_datetime

from .audit import result_votes, results, state
from .models import Round, RoundResultSnapshot


@contextmanager
def consistent_read():
    # A report can require many queries while voting continues. PostgreSQL gives
    # every query the same committed view, without holding up incoming votes.
    nested = connection.in_atomic_block
    with transaction.atomic():
        if connection.vendor == 'postgresql' and not nested:
            with connection.cursor() as cursor:
                cursor.execute('SET TRANSACTION ISOLATION LEVEL REPEATABLE READ')
        yield


def vote_details(rd):
    aliases = dict(rd.participants.values_list('source_participant_id', 'pk')) if rd.event_stage == 'qualification' else {}
    return [{
        **state(v), 'participant_name': v.participant.full_name,
        'created_at': v.created_at.isoformat(),
        'source_created_at': v.source_created_at.isoformat() if v.source_created_at else None,
        'result_participant_id': aliases.get(v.participant_id, v.participant_id),
        'choice_label': 'Нет' if v.choice == 'no' else 'Да' if v.choice == 'yes' else 'За участника',
    } for v in result_votes(rd).select_related('participant').order_by('created_at', 'pk')]


def round_payload(rd):
    return {**results(rd), 'snapshot_schema': 2, 'round_id': rd.pk,
            'round_name': rd.display_name, 'round_state': state(rd),
            'roster': [state(p) for p in rd.participants.all()], 'votes': vote_details(rd)}


def csv_cell(value):
    if isinstance(value, str) and value.lstrip()[:1] in ('=', '+', '-', '@'):
        return "'" + value
    return value


def csv_bytes(rows):
    output = io.StringIO(newline='')
    writer = csv.writer(output, delimiter=';')
    for row in rows:
        writer.writerow([csv_cell(cell) for cell in row])
    return output.getvalue().encode('utf-8-sig')


def vote_rows(data):
    yield ['Telegram ID', 'Участник', 'Выбор', 'Время (UTC)', 'Голос ID', 'Раунд ID', 'Участник ID', 'Исходный голос ID']
    for vote in data:
        yield [vote['user_telegram_id'], vote['participant_name'], vote['choice_label'],
               vote_time(vote),
               vote['id'], vote['round_id'], vote['participant_id'], vote['source_vote_id'] or '']


def vote_time(vote):
    created = vote['created_at']
    if isinstance(created, str):
        created = parse_datetime(created)
    return created.strftime('%d.%m.%Y %H:%M:%S')


def make_archive(campaign, rounds, snapshots):
    """Only tour totals in the summary; a performance's votes appear once."""
    top_level = [r for r in rounds if r['event_stage'] != 'performance']
    # A single performance exported from its card should still have a summary.
    if len(rounds) == 1:
        top_level = rounds
    result_rows = [['Мероприятие', 'Тур', 'Статус', 'Место', 'Участник', 'За', 'Против',
                    'Проходит по баллам', 'Выбор администратора']]
    ballots = [['Тур', *next(vote_rows([]))]]
    seen_votes = set()
    for rd in top_level:
        title = rd.get('round_name', f"Раунд №{rd['round_number']}")
        status = 'Завершён' if rd['event_completed'] or rd.get('ended_at') else dict(Round.STATUS_CHOICES).get(rd['status'], rd['status'])
        for p in rd['participants']:
            result_rows.append([campaign['name'], title, status, p['position'], p['name'], p['yes'], p['no'],
                                'Да' if p['auto_winner'] else 'Нет', 'Да' if p['jury_winner'] or p['is_admin_choice'] else 'Нет'])
        if rd.get('jury_name'):
            result_rows.append([campaign['name'], title, status, '', rd['jury_name'], '', '', 'Нет', 'Добавлен по имени'])
        for vote in rd.get('votes', []):
            if vote['id'] not in seen_votes:
                ballots.append([title, *list(vote_rows([vote]))[1]])
                seen_votes.add(vote['id'])
    # Preserve orphaned/legacy performance votes even if no parent table exists.
    for rd in rounds:
        for vote in rd.get('votes', []):
            if vote['id'] not in seen_votes:
                ballots.append([rd.get('round_name', f"Раунд №{rd['round_number']}"), *list(vote_rows([vote]))[1]])
                seen_votes.add(vote['id'])
    backup = {'schema': 'voting-report-v1', 'exported_at': timezone.now(),
              'campaign': campaign, 'rounds': rounds, 'snapshots': snapshots}
    readme = (
        'Результаты.csv — итоговая таблица по турам выбранного мероприятия.\n'
        'Голоса.csv — кто за кого голосовал: Telegram ID, участник, выбор и время UTC.\n'
        'Индивидуальные выступления входят в свой тур; их голоса не дублируются.\n'
        'Общее голосование: «За участника». Индивидуальное: «Да» или «Нет».\n'
        'При переносе старых голосов указан исходный голос ID. Это не новый выбор зрителя.\n'
        'До завершения тура места и проходящие участники предварительные.\n'
        'Финалист, введённый по имени, отмечен «Добавлен по имени», без приписанных голосов.\n'
        'CSV открываются в Excel: кодировка UTF-8, разделитель — точка с запятой.\n\n'
        'Данные.json — исходные голоса, составы, настройки подсчёта и сохранённые снимки.\n'
        'rounds — состояние на момент выгрузки (или выбранный снимок в карточке раунда).\n'
        'snapshots — история завершений. Это разные версии, складывать их голоса нельзя.\n'
        'Новые снимки содержат votes; старые снимки без votes хранят только итоговые числа.\n'
        'Для пересчёта одного тура берите только его votes, группируйте по result_participant_id.\n'
        'choice=null и choice=yes дают 1 балл «За»; choice=no учитывается отдельно «Против».\n'
        'Участники без голосов остаются с нулём. Победители и решение администратора сохранены отдельно.\n'
        'Для проверки файла: python manage.py recount_report Данные.json\n'
        'Архив позволяет восстановить таблицу результатов; автоматического импорта в рабочую БД нет.\n'
        'Для полного восстановления сервера нужна резервная копия PostgreSQL.\n'
    )
    if any('votes' not in rd for rd in rounds):
        readme = 'ВНИМАНИЕ: выбран старый снимок без отдельных голосов. CSV голосов пуст; пересчёт этой версии невозможен.\n\n' + readme
    output = io.BytesIO()
    with ZipFile(output, 'w', ZIP_DEFLATED) as archive:
        archive.writestr('Результаты.csv', csv_bytes(result_rows))
        archive.writestr('Голоса.csv', csv_bytes(ballots))
        archive.writestr('Данные.json', json.dumps(backup, cls=DjangoJSONEncoder, ensure_ascii=False, indent=2))
        archive.writestr('Как читать отчёт.txt', readme)
    return output.getvalue()


def campaign_archive(campaign):
    rounds = [round_payload(rd) for rd in campaign.rounds.select_related('campaign').order_by('number')]
    snapshots = list(RoundResultSnapshot.objects.filter(campaign_id=campaign.pk).order_by('round_id', 'version').values())
    return make_archive({'id': campaign.pk, 'name': campaign.name}, rounds, snapshots)


def recount(data):
    """Rebuild score totals from the file alone, and compare with stored totals."""
    if data.get('schema') != 'voting-report-v1':
        raise ValueError('Неизвестный формат отчёта.')
    rows = []
    for rd in data['rounds']:
        if 'votes' not in rd:
            raise ValueError(f"Раунд {rd['round_id']}: старый снимок без отдельных голосов.")
        counts = {p['id']: {'yes': 0, 'no': 0} for p in rd['participants']}
        seen = set()
        for vote in rd['votes']:
            if vote['id'] in seen or vote['result_participant_id'] not in counts or vote['choice'] not in (None, 'yes', 'no'):
                raise ValueError('Повторный голос, неизвестный участник или неверный выбор в файле.')
            seen.add(vote['id'])
            counts[vote['result_participant_id']]['no' if vote['choice'] == 'no' else 'yes'] += 1
        if len(seen) != rd['total_votes']:
            raise ValueError(f"Раунд {rd['round_id']}: количество голосов не совпало.")
        for p in rd['participants']:
            c = counts[p['id']]
            if (c['yes'], c['no']) != (p['yes'], p['no']):
                raise ValueError(f"Раунд {rd['round_id']}, участник {p['id']}: итоги не совпали с голосами.")
            rows.append([rd['round_id'], rd.get('round_name', ''), p['id'], p['name'], c['yes'], c['no']])
    return rows
