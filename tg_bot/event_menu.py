"""Operator controls for the event, with server-side checks on every operation."""
from html import escape

from aiogram import F
from aiogram.filters import Command
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
import api


class EventStates(StatesGroup):
    names = State()
    create_count = State()
    winners_count = State()
    add_names = State()
    jury_name = State()
    tie = State()


def keyboard(rows):
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=label, callback_data=data) for label, data in row] for row in rows])


async def error(message, exc):
    text = str(exc) if 0 < exc.status < 500 else 'Сервис временно недоступен. Попробуйте ещё раз.'
    await message.answer(text)


async def show_campaign(message, cid, full):
    data = await api.request('GET', f'campaigns/{cid}/event')
    rows = []
    for rd in data['rounds']:
        if rd.get('parent_tour'):
            continue
        mark = '✓' if rd['completed'] else '▶' if rd['status'] == 'active' else '·'
        rows.append([(f"{mark} {rd['name']}", f"ev_round_{rd['id']}")])
    if full:
        rows.insert(0, [('Создать тур', f'ev_new_{cid}')])
        if not data['rounds']:
            rows.append([('Загрузить сразу список выступающих', f'ev_prepare_{cid}')])
    for start in range(0, max(len(rows), 1), 50):
        await message.answer(f"<b>{escape(data['name'])}</b>\nВыберите тур. В индивидуальном туре откройте раунд с нужной фамилией.",
                             reply_markup=keyboard(rows[start:start + 50]), parse_mode='HTML')


async def show_round(message, rid, full):
    rd = await api.request('GET', f'rounds/{rid}')
    summary = await api.request('GET', f'rounds/{rid}/event_results')
    stage = rd['event_stage']
    text = f"<b>{escape(rd.get('display_name', 'Раунд №' + str(rd['number'])))}</b>\nСтатус: {rd['status']}\n"
    if rd['type'] == 'standard':
        text += f"Проходят по уровням баллов: топ-{rd['winners_count']}\n"
        if stage != 'qualification':
            text += f"Максимум выбранных участников: {rd['max_choices']}\n"
    text += '\n'.join(f"{p['position']}. {escape(p['name'][:70])} — {p['yes']} за, {p['no']} против"
                      + (' ★' if rd['event_completed'] and p['winner'] else '') for p in summary['participants'][:25])
    if len(summary['participants']) > 25:
        text += '\nОстальные участники и полные имена — в админке.'
    jury_name = rd['jury_name'] or next((p['name'] for p in summary['participants'] if p['id'] == rd['jury_choice']), '')
    if jury_name:
        text += f'\n\nВыбор главного администратора: {escape(jury_name)}'
    rows = []
    if stage == 'qualification':
        data = await api.request('GET', f"campaigns/{rd['campaign']}/event")
        for child in data['rounds']:
            if child.get('parent_tour') == rid:
                mark = '✓' if child['completed'] else '▶' if child['status'] == 'active' else '·'
                rows.append([(f"{mark} {child['name']}", f"ev_round_{child['id']}")])
        text += '\n\nВыберите раунд по ФИО, откройте голосование ДА/НЕТ, затем завершите этот раунд. Тур завершайте после всех выступлений.'
    if full and not rd['event_completed'] and not rd['ended_at']:
        if stage == 'qualification' or (stage == 'semifinal' and rd['status'] == 'pending'):
            rows.append([('Добавить участника / список', f'ev_add_{rid}')])
        if rd['type'] == 'standard' and stage != 'final':
            rows.append([('Настроить уровни баллов: топ-N', f'ev_count_{rid}')])
    if stage:
        if rd['status'] == 'pending':
            rows.append([('Открыть голосование ДА/НЕТ' if stage == 'performance' else 'Открыть общее голосование', f'ev_start_{rid}')])
        elif not rd['event_completed'] and (not rd['ended_at'] or not summary['tie_slots']):
            label = 'Завершить этот раунд' if stage == 'performance' else 'Завершить ТУР и перенести топ-N'
            rows.append([(label, f'ev_end_{rid}')])
        if summary['tie_slots'] and rd['ended_at'] and not rd['event_completed']:
            text += f"\n\nРавенство голосов. Нужно выбрать {summary['tie_slots']} из списка равных."
            if full:
                rows.append([('Разрешить равенство голосов', f'ev_tie_{rid}')])
    else:
        if rd['status'] in {'pending', 'published'}:
            rows.append([('Открыть голосование', f'ev_start_{rid}')])
        if rd['status'] in {'active', 'published'}:
            rows.append([('Завершить раунд / перенести победителей', f'ev_legacyend_{rid}')])
    if full and rd.get('can_edit_jury', rd['type'] == 'standard' and stage in {'', 'semifinal'}):
        rows.append([('Дополнительный победитель: выбрать участника', f'ev_jury_{rid}')])
        if stage == 'semifinal':
            rows.append([('Дополнительный финалист: фамилия и имя', f'ev_name_{rid}')])
        if rd['jury_choice'] or rd['jury_name']:
            rows.append([('Убрать дополнительного победителя', f'ev_clearjury_{rid}')])
    if rd['next_round']:
        rows.append([('К туру и остальным раундам' if stage == 'performance' else 'Открыть следующий тур', f"ev_round_{rd['next_round']}")])
    rows.append([('К мероприятию', f"ev_camp_{rd['campaign']}")])
    for start in range(0, max(len(rows), 1), 50):
        await message.answer(text if start == 0 else 'Продолжение списка раундов:', reply_markup=keyboard(rows[start:start+50]), parse_mode='HTML')


def register_event_menu(dp, full_access, admin_access, is_full_admin):
    @dp.message(Command('event'), admin_access)
    async def command(message, state):
        await state.clear()
        try:
            data = await api.request('GET', 'campaigns/active_list')
            rows = [[(c['name'], f"ev_camp_{c['id']}")] for c in data['campaigns']]
            await message.answer('Выберите мероприятие. Для нового сначала /create_campaign.', reply_markup=keyboard(rows))
        except api.APIError as exc:
            await error(message, exc)

    @dp.message(Command('jury_winner'), full_access)
    async def jury_command(message, state):
        await state.clear()
        try:
            rounds = await api.request('GET', 'rounds')
            rows = [[(f"{r['campaign_name']} · Раунд №{r['number']}", f"ev_round_{r['id']}")]
                    for r in rounds if r['type'] == 'standard' and r['event_stage'] in {'', 'semifinal'}]
            await message.answer('Выберите общее голосование:', reply_markup=keyboard(rows))
        except api.APIError as exc:
            await error(message, exc)

    @dp.callback_query(F.data.regexp(r'^ev_camp_[0-9]+$'), admin_access)
    async def campaign(call, state):
        await call.answer()
        await state.clear()
        try:
            await show_campaign(call.message, int(call.data.split('_')[2]), is_full_admin(call.from_user.id))
        except api.APIError as exc:
            await error(call.message, exc)

    @dp.callback_query(F.data.regexp(r'^ev_round_[0-9]+$'), admin_access)
    async def round_card(call, state):
        await call.answer()
        await state.clear()
        try:
            await show_round(call.message, int(call.data.split('_')[2]), is_full_admin(call.from_user.id))
        except api.APIError as exc:
            await error(call.message, exc)

    @dp.callback_query(F.data.regexp(r'^ev_new(_[0-9]+)?$'), full_access)
    async def choose_tour_type(call, state):
        await call.answer()
        await state.clear()
        try:
            parts = call.data.split('_')
            if len(parts) == 3:
                cid = int(parts[2])
            else:
                data = await api.request('GET', 'campaigns/active_list')
                campaigns = data['campaigns']
                if not campaigns:
                    await call.message.answer('Сначала создайте мероприятие: /create_campaign.')
                    return
                if len(campaigns) != 1:
                    await call.message.answer('В каком мероприятии создать тур?', reply_markup=keyboard([
                        [(c['name'], f"ev_new_{c['id']}")] for c in campaigns]))
                    return
                cid = campaigns[0]['id']
            await call.message.answer('Выберите тип тура:', reply_markup=keyboard([
                [('Общий', f'ev_create_standard_{cid}')],
                [('Индивидуальный (ДА/НЕТ)', f'ev_create_individual_{cid}')],
                [('Назад к мероприятию', f'ev_camp_{cid}')],
            ]))
        except api.APIError as exc:
            await error(call.message, exc)

    @dp.callback_query(F.data.regexp(r'^ev_create_(individual|standard)_[0-9]+$'), full_access)
    async def create(call, state):
        await call.answer()
        _, _, mode, cid = call.data.split('_')
        await state.clear()
        await state.update_data(campaign_id=int(cid), mode=mode)
        await state.set_state(EventStates.create_count)
        await call.message.answer('Сколько лучших уровней баллов отбирать? Введите N. Например, топ-2 при баллах 10, 10, 8, 8, 5 пропускает первых четверых. Отмена: /event.')

    @dp.message(EventStates.create_count, F.text, ~F.text.startswith('/'), full_access)
    async def create_save(message, state):
        if not message.text.strip().isdigit() or not 1 <= int(message.text.strip()) <= 100:
            await message.answer('Введите целое число от 1 до 100.')
            return
        data = await state.get_data()
        try:
            result = await api.request('POST', f"campaigns/{data['campaign_id']}/create_tour",
                                       {'mode': data['mode'], 'winners_count': int(message.text.strip())})
            await state.clear()
            await show_round(message, result['tour_id'], True)
        except api.APIError as exc:
            await error(message, exc)

    @dp.callback_query(F.data.regexp(r'^ev_(add|count)_[0-9]+$'), full_access)
    async def edit_tour(call, state):
        await call.answer()
        _, action, rid = call.data.split('_')
        await state.clear()
        await state.update_data(round_id=int(rid))
        await state.set_state(EventStates.add_names if action == 'add' else EventStates.winners_count)
        text = ('Пришлите фамилию и имя или список, каждый с новой строки. Для каждого участника индивидуального тура будет создан отдельный раунд ДА/НЕТ.'
                if action == 'add' else 'Введите число лучших уровней баллов N от 1 до 100. Все участники с одинаковыми проходными баллами проходят вместе.')
        await call.message.answer(text + ' Отмена: /event.')

    @dp.message(EventStates.add_names, F.text, ~F.text.startswith('/'), full_access)
    async def add_names(message, state):
        data = await state.get_data()
        try:
            result = await api.request('POST', f"rounds/{data['round_id']}/add_entries",
                                       {'names': [n.strip() for n in message.text.splitlines() if n.strip()]})
            await state.clear()
            await message.answer(result['message'])
            await show_round(message, data['round_id'], True)
        except api.APIError as exc:
            await error(message, exc)

    @dp.message(EventStates.winners_count, F.text, ~F.text.startswith('/'), full_access)
    async def count_save(message, state):
        if not message.text.strip().isdigit() or not 1 <= int(message.text.strip()) <= 100:
            await message.answer('Введите целое число от 1 до 100.')
            return
        data = await state.get_data()
        try:
            result = await api.request('PATCH', f"rounds/{data['round_id']}/update_winners_count",
                                       {'winners_count': int(message.text.strip())})
            await state.clear()
            await message.answer(result['message'])
            await show_round(message, data['round_id'], True)
        except api.APIError as exc:
            await error(message, exc)

    @dp.callback_query(F.data.regexp(r'^ev_prepare_[0-9]+$'), full_access)
    async def prepare(call, state):
        await call.answer()
        await state.clear()
        await state.update_data(campaign_id=int(call.data.split('_')[2]))
        await state.set_state(EventStates.names)
        await call.message.answer('Пришлите список выступающих: фамилия и имя, каждый с новой строки (для вашего мероприятия — 25 человек). Отмена: /event.')

    @dp.message(EventStates.names, F.text, ~F.text.startswith('/'), full_access)
    async def save_names(message, state):
        data = await state.get_data()
        names = [line.strip() for line in message.text.splitlines() if line.strip()]
        try:
            await api.request('POST', f"campaigns/{data['campaign_id']}/prepare_event", {'names': names})
            await state.clear()
            await show_campaign(message, data['campaign_id'], True)
        except api.APIError as exc:
            await error(message, exc)

    @dp.callback_query(F.data.regexp(r'^ev_(start|end)_[0-9]+$'), admin_access)
    async def transition(call, state):
        await call.answer()
        await state.clear()
        _, action, rid = call.data.split('_')
        try:
            response = await api.request('POST', f"rounds/{rid}/event_{'start' if action == 'start' else 'finish'}", {})
            await call.message.answer(response['message'])
            await show_round(call.message, int(rid), is_full_admin(call.from_user.id))
        except api.APIError as exc:
            await error(call.message, exc)

    @dp.callback_query(F.data.regexp(r'^ev_jury_[0-9]+$'), full_access)
    async def jury_list(call, state):
        await call.answer()
        await state.clear()
        rid = call.data.split('_')[2]
        try:
            data = await api.request('GET', f'rounds/{rid}/event_results')
            rows = [[(p['name'], f"ev_pick_{rid}_{p['id']}")] for p in data['participants']
                    if not (data['event_completed'] and p['auto_winner'])]
            await call.message.answer('Выберите одного дополнительного победителя независимо от голосов:', reply_markup=keyboard(rows))
        except api.APIError as exc:
            await error(call.message, exc)

    @dp.callback_query(F.data.regexp(r'^ev_(pick|clearjury)_[0-9]+(_[0-9]+)?$'), full_access)
    async def jury_pick(call):
        await call.answer()
        parts = call.data.split('_')
        rid = parts[2]
        payload = {'participant_id': int(parts[3])} if parts[1] == 'pick' and len(parts) == 4 else {'participant_id': None}
        try:
            data = await api.request('POST', f'rounds/{rid}/jury', payload)
            await call.message.answer(data['message'])
            await show_round(call.message, int(rid), True)
        except api.APIError as exc:
            await error(call.message, exc)

    @dp.callback_query(F.data.regexp(r'^ev_name_[0-9]+$'), full_access)
    async def enter_name(call, state):
        await call.answer()
        await state.clear()
        await state.update_data(round_id=int(call.data.split('_')[2]))
        await state.set_state(EventStates.jury_name)
        await call.message.answer('Введите фамилию и имя одного дополнительного финалиста. Новый выбор заменит предыдущего дополнительного участника. Отмена: /event.')

    @dp.message(EventStates.jury_name, F.text, ~F.text.startswith('/'), full_access)
    async def save_name(message, state):
        data = await state.get_data()
        try:
            response = await api.request('POST', f"rounds/{data['round_id']}/jury", {'full_name': message.text.strip()})
            await state.clear()
            await message.answer(response['message'])
            await show_round(message, data['round_id'], True)
        except api.APIError as exc:
            await error(message, exc)

    async def tie_menu(message, data):
        chosen = data['selected']
        rows = [[(('✓ ' if p['id'] in chosen else '') + f"{p['name']} ({p['votes']})", f"evt_{p['id']}")]
                for p in data['candidates']]
        rows.append([('Подтвердить выбор и завершить тур', 'ev_tie_save')])
        rows.append([('Отмена', f"ev_round_{data['round_id']}")])
        await message.answer(f"Выбрано {len(chosen)} из {data['slots']}. Выберите участников с равными голосами:", reply_markup=keyboard(rows))

    @dp.callback_query(F.data.regexp(r'^ev_tie_[0-9]+$'), full_access)
    async def choose_tie(call, state):
        await call.answer()
        rid = int(call.data.split('_')[2])
        try:
            summary = await api.request('GET', f'rounds/{rid}/event_results')
            data = {'round_id': rid, 'candidates': summary['tie_candidates'], 'slots': summary['tie_slots'], 'selected': []}
            await state.set_state(EventStates.tie)
            await state.set_data(data)
            await tie_menu(call.message, data)
        except api.APIError as exc:
            await error(call.message, exc)

    @dp.callback_query(F.data.regexp(r'^evt_[0-9]+$'), EventStates.tie, full_access)
    async def toggle_tie(call, state):
        await call.answer()
        data = await state.get_data()
        pid = int(call.data.split('_')[1])
        if pid not in {p['id'] for p in data['candidates']}:
            return
        selected = set(data['selected'])
        if pid in selected:
            selected.remove(pid)
        elif len(selected) < data['slots']:
            selected.add(pid)
        await state.update_data(selected=sorted(selected))
        await tie_menu(call.message, await state.get_data())

    @dp.callback_query(F.data == 'ev_tie_save', EventStates.tie, full_access)
    async def confirm_tie(call, state):
        await call.answer()
        data = await state.get_data()
        try:
            response = await api.request('POST', f"rounds/{data['round_id']}/resolve_tie", {'participant_ids': data['selected']})
            await state.clear()
            await call.message.answer(response['message'])
            await show_round(call.message, data['round_id'], True)
        except api.APIError as exc:
            await error(call.message, exc)
