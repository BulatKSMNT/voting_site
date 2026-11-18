"""A viewer can check their own campaign access before a round is open."""
from html import escape

from aiogram import F
from aiogram.filters import Command
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton

import api
import lexicon


async def show_access(message, campaign):
    text = (f'✅ Доступ к голосованию на мероприятии «{escape(campaign["name"])}» разрешён.'
            if campaign['can_vote'] else lexicon.MSG_ACCESS_DENIED)
    await message.answer(text, parse_mode='HTML')


def register_voter_access(dp, keyboard):
    @dp.message(Command('check_access'))
    @dp.message(F.text == lexicon.BTN_CHECK_ACCESS)
    async def check_access(message, state):
        await state.clear()
        try:
            campaigns = (await api.request('GET', 'campaigns/my_access'))['campaigns']
            if not campaigns:
                await message.answer('Пока нет доступных мероприятий. Проверьте доступ позже.', reply_markup=keyboard)
            elif len(campaigns) == 1:
                await show_access(message, campaigns[0])
            else:
                rows = [[InlineKeyboardButton(text=c['name'], callback_data=f"access_check_{c['id']}")] for c in campaigns]
                for start in range(0, len(rows), 50):
                    await message.answer('Выберите мероприятие для проверки доступа:',
                                         reply_markup=InlineKeyboardMarkup(inline_keyboard=rows[start:start+50]))
        except api.APIError:
            await message.answer('Не удалось проверить доступ. Попробуйте ещё раз позже.', reply_markup=keyboard)

    @dp.callback_query(F.data.regexp(r'^access_check_[0-9]+$'))
    async def select_access_campaign(call):
        await call.answer()
        try:
            # Fetch again: an administrator may have changed admission since the menu opened.
            campaigns = (await api.request('GET', f"campaigns/my_access?campaign_id={int(call.data.split('_')[-1])}"))['campaigns']
            await show_access(call.message, campaigns[0])
        except api.APIError as exc:
            text = ('Это мероприятие больше недоступно. Нажмите «Проверить доступ» ещё раз.' if exc.status == 404 else
                    'Не удалось проверить доступ. Попробуйте ещё раз позже.')
            await call.message.answer(text, reply_markup=keyboard)
