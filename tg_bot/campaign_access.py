"""Full-admin campaign admission settings, available in a private chat."""
from html import escape

from aiogram import F
from aiogram.filters import Command
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton, BufferedInputFile

import api
from voting.access import parse_telegram_ids


class CampaignAccessStates(StatesGroup):
    enter_ids = State()


def access_menu(campaign):
    cid = campaign['id']
    private = campaign['access_mode'] == 'private'
    count = len(campaign['allowed_telegram_ids'])
    text = (
        f"<b>{escape(campaign['name'])}</b>\n"
        f"Режим: {'приватный' if private else 'общедоступный'}\n"
        f"Telegram ID в списке: {count}\n\n"
        "Настройки действуют для всех раундов этой кампании. Уже принятые голоса сохраняются."
    )
    if private and not count:
        text += "\n⚠️ Список пуст: сейчас голосовать не сможет никто."
    if not private:
        text += "\nГолосовать могут все; сохранённый список пока не применяется."
    buttons = [
        [InlineKeyboardButton(text=('✓ ' if not private else '') + 'Общедоступный', callback_data=f'ca_mode_{cid}_public'),
         InlineKeyboardButton(text=('✓ ' if private else '') + 'Приватный', callback_data=f'ca_mode_{cid}_private')],
        [InlineKeyboardButton(text='Добавить ID', callback_data=f'ca_add_{cid}'),
         InlineKeyboardButton(text='Удалить ID', callback_data=f'ca_remove_{cid}')],
        [InlineKeyboardButton(text='Скачать список ID', callback_data=f'ca_list_{cid}')],
        [InlineKeyboardButton(text='Готово', callback_data='ca_done')],
    ]
    return text, InlineKeyboardMarkup(inline_keyboard=buttons)


async def show_menu(message, campaign):
    text, keyboard = access_menu(campaign)
    await message.answer(text, reply_markup=keyboard, parse_mode='HTML')


async def show_error(message, exc):
    if exc.status == 404:
        text = 'Кампания больше недоступна. Выберите другую через /campaign_access.'
    elif exc.status == 403:
        text = 'Настройки доступа доступны только главным администраторам.'
    else:
        text = 'Не удалось выполнить запрос. Попробуйте ещё раз.'
    await message.answer(text)


def register_campaign_access(dp, full_access):
    @dp.message(Command('campaign_access'), full_access)
    async def command(message, state):
        if message.chat.type != 'private':
            await message.answer('Для настройки доступа отправьте /campaign_access в личный чат с ботом.')
            return
        await state.clear()
        try:
            campaigns = await api.request('GET', 'campaigns')
        except api.APIError as exc:
            await show_error(message, exc)
            return
        if not campaigns:
            await message.answer('Сначала создайте кампанию: /create_campaign.')
            return
        # Split large lists to stay within Telegram's keyboard size limit.
        for start in range(0, len(campaigns), 50):
            keyboard = InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text=f"#{c['order_number']} {c['name']}", callback_data=f"ca_open_{c['id']}")]
                for c in campaigns[start:start + 50]
            ])
            await message.answer('Выберите кампанию для настройки доступа:', reply_markup=keyboard)

    @dp.callback_query(F.data.regexp(r'^ca_open_[0-9]+$'), F.message.chat.type == 'private', full_access)
    async def open_campaign(call, state):
        await call.answer()
        await state.clear()
        try:
            campaign = await api.request('GET', f"campaigns/{call.data.split('_')[2]}")
            await show_menu(call.message, campaign)
        except api.APIError as exc:
            await show_error(call.message, exc)

    @dp.callback_query(F.data.regexp(r'^ca_mode_[0-9]+_(public|private)$'), F.message.chat.type == 'private', full_access)
    async def set_mode(call, state):
        await call.answer()
        await state.clear()
        _, _, cid, mode = call.data.split('_')
        try:
            campaign = await api.request('PATCH', f'campaigns/{cid}/access', {'access_mode': mode})
            await show_menu(call.message, campaign)
        except api.APIError as exc:
            await show_error(call.message, exc)

    @dp.callback_query(F.data.regexp(r'^ca_(add|remove)_[0-9]+$'), F.message.chat.type == 'private', full_access)
    async def enter_ids(call, state):
        await call.answer()
        _, operation, cid = call.data.split('_')
        await state.clear()
        await state.update_data(campaign_id=int(cid), operation=operation)
        await state.set_state(CampaignAccessStates.enter_ids)
        action = 'добавить' if operation == 'add' else 'удалить'
        await call.message.answer(
            f'Отправьте Telegram ID, которые нужно {action}: по одному на строку или через пробел, запятую. '
            'Можно отправлять список частями, каждый раз нажимая «Добавить ID». '
            'Пользователь может узнать свой ID командой /myid. Для отмены: /campaign_access.',
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text='Отмена', callback_data=f'ca_open_{cid}')]
            ]),
        )

    @dp.message(CampaignAccessStates.enter_ids, F.chat.type == 'private', F.text, ~F.text.startswith('/'), full_access)
    async def save_ids(message, state):
        try:
            ids = parse_telegram_ids(message.text)
            if not ids:
                raise ValueError('Укажите хотя бы один Telegram ID.')
        except ValueError as exc:
            await message.answer(str(exc))
            return
        data = await state.get_data()
        try:
            campaign = await api.request('PATCH', f"campaigns/{data['campaign_id']}/access",
                                         {'operation': data['operation'], 'telegram_ids': ids})
        except api.APIError as exc:
            await show_error(message, exc)
            return
        await state.clear()
        await show_menu(message, campaign)

    @dp.callback_query(F.data.regexp(r'^ca_list_[0-9]+$'), F.message.chat.type == 'private', full_access)
    async def download_ids(call):
        await call.answer()
        cid = call.data.split('_')[2]
        try:
            campaign = await api.request('GET', f'campaigns/{cid}')
        except api.APIError as exc:
            await show_error(call.message, exc)
            return
        ids = campaign['allowed_telegram_ids']
        if not ids:
            await call.message.answer('Список Telegram ID пуст.')
            return
        document = BufferedInputFile(('\n'.join(map(str, ids)) + '\n').encode('utf-8'), filename=f'campaign-{cid}-telegram-ids.txt')
        await call.message.answer_document(document, caption=f'Telegram ID: {len(ids)}')

    @dp.callback_query(F.data == 'ca_done', F.message.chat.type == 'private', full_access)
    async def done(call, state):
        await state.clear()
        await call.answer()
        await call.message.edit_text('Настройки доступа сохранены. Открыть снова: /campaign_access.')
