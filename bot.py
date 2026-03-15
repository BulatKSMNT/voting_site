import asyncio
import logging
import requests
import time
from typing import List, Dict
from aiogram import Bot, Dispatcher, types
from aiogram.filters import Command
from aiogram.types import (
    Message, InlineKeyboardMarkup, InlineKeyboardButton, CallbackQuery, ReplyKeyboardMarkup, KeyboardButton
)
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
# Поддержка .env
from decouple import config

import aiohttp

# ──────────────────────────────────────────────
# НАСТРОЙКИ (из .env)
# ──────────────────────────────────────────────
BOT_TOKEN = config("TELEGRAM_TOKEN")
DJANGO_API_TOKEN = config("DJANGO_API_TOKEN")  # Токен из Django authtoken
DJANGO_API_BASE = "http://127.0.0.1:8000"
API_VOTE_URL = f"{DJANGO_API_BASE}/api/vote/"
API_ACTIVE_PARTICIPANTS = f"{DJANGO_API_BASE}/api/active-participants"
API_ACTIVE_ROUND_INFO = f"{DJANGO_API_BASE}/api/active-round-info"
API_ACTIVE_ROUNDS = f"{DJANGO_API_BASE}/api/active-rounds"
API_START_ROUND = f"{DJANGO_API_BASE}/api/start-round/"
API_END_ROUND = f"{DJANGO_API_BASE}/api/end-round/"
API_ADD_PARTICIPANT = f"{DJANGO_API_BASE}/api/add-participant/"
API_CREATE_CAMPAIGN = f"{DJANGO_API_BASE}/api/create-campaign/"
API_ACTIVE_CAMPAIGNS = f"{DJANGO_API_BASE}/api/active-campaigns/"
API_SET_CURRENT_ROUND = f"{DJANGO_API_BASE}/api/set-current-round/"
API_GET_CURRENT_ROUND = f"{DJANGO_API_BASE}/api/get-current-round/"
API_TRANSFER_WINNERS = f"{DJANGO_API_BASE}/api/transfer-winners/"

ADMIN_IDS = [1251634923, ]
#1401411234
# Заголовки
PUBLIC_HEADERS = {"Content-Type": "application/json"}
ADMIN_HEADERS = {"Authorization": f"Token {DJANGO_API_TOKEN}", "Content-Type": "application/json"}

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher(storage=MemoryStorage())

session: aiohttp.ClientSession = None
# ──────────────────────────────────────────────
# Инициализация и закрытие сессии
# ──────────────────────────────────────────────

async def on_startup():
    global session
    session = aiohttp.ClientSession()
    logger.info("aiohttp сессия создана")

async def on_shutdown():
    global session
    if session and not session.closed:
        await session.close()
    logger.info("aiohttp сессия закрыта")

# Прикрепляем хуки (важно!)
dp.startup.register(on_startup)
dp.shutdown.register(on_shutdown)

# ──────────────────────────────────────────────
# Вспомогательные асинхронные функции для запросов
# ──────────────────────────────────────────────

async def api_get(url: str, headers: dict = PUBLIC_HEADERS, timeout: int = 8) -> dict:
    async with session.get(url, headers=headers, timeout=timeout) as resp:
        if resp.status >= 400:
            text = await resp.text()
            raise aiohttp.ClientResponseError(
                resp.request_info, resp.history,
                status=resp.status, message=text
            )
        return await resp.json()

async def api_post(url: str, json_data: dict, headers: dict = ADMIN_HEADERS, timeout: int = 10) -> dict:
    async with session.post(url, json=json_data, headers=headers, timeout=timeout) as resp:
        if resp.status >= 400:
            try:
                error_data = await resp.json()
            except:
                error_data = {"detail": await resp.text()}
            raise aiohttp.ClientResponseError(
                resp.request_info, resp.history,
                status=resp.status, message=str(error_data)
            )
        return await resp.json()
# ──────────────────────────────────────────────
# СОСТОЯНИЯ FSM
# ──────────────────────────────────────────────
class AddParticipantStates(StatesGroup):
    choose_campaign = State()
    choose_round = State()
    waiting_for_name = State()

class StartRoundStates(StatesGroup):
    choose_campaign = State()
    choose_type = State()  # Новый: выбор типа раунда
    enter_round_number = State()
    enter_winners_count = State()
    enter_new_campaign_name = State()

class EndRoundStates(StatesGroup):
    choose_campaign = State()
    choose_round = State()

class TransferWinnersStates(StatesGroup):
    choose_action = State()  # та же / новая / существующий
    choose_existing_round = State()  # Выбор существующего раунда в той же кампании
    enter_new_campaign_name = State()
    choose_target_round = State()  # Новый: для individual — выбор target standard round

# ──────────────────────────────────────────────
# ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ
# ──────────────────────────────────────────────
def is_admin(user_id: int) -> bool:
    return user_id in ADMIN_IDS

async def get_active_campaigns() -> List[Dict]:
    try:
        data = await api_get(API_ACTIVE_CAMPAIGNS)
        return data.get("campaigns", [])
    except Exception as e:
        logger.error(f"Ошибка получения кампаний: {e}")
        return []

async def get_active_rounds(round_type: str = None) -> List[Dict]:
    try:
        data = await api_get(API_ACTIVE_ROUNDS)
        rounds = data.get("rounds", [])
        if round_type:
            rounds = [rd for rd in rounds if rd.get("type") == round_type]
        return rounds
    except Exception as e:
        logger.error(f"Ошибка получения раундов: {e}")
        return []

async def get_rounds_for_campaign(campaign_id: int, round_type: str = None) -> List[Dict]:
    rounds = await get_active_rounds()
    filtered = [rd for rd in rounds if rd.get("campaign_order_number") == campaign_id]
    if round_type:
        filtered = [rd for rd in filtered if rd.get("type") == round_type]
    return filtered

async def transfer_winners_to_round(winners: List[Dict], target_round_id: int) -> str:
    if not winners:
        return "Нет победителей для переноса."
    success_count = 0
    errors = []
    for winner in winners:
        yes_voters_str = ", ".join(map(str, winner.get("yes_voters", [])))
        payload = {
            "round_id": target_round_id,
            "full_name": winner["full_name"],
            "description": f"Из индивидуального раунда (голосов: {winner['votes']}). Yes voters: {yes_voters_str}"
        }
        try:
            await api_post(API_ADD_PARTICIPANT, payload)
            success_count += 1
        except Exception as e:
            errors.append(f"{winner['full_name']}: {str(e)}")
        await asyncio.sleep(0.07)  # Для масштаба, чтобы не перегружать
    if errors:
        return f"Добавлено {success_count}/{len(winners)}. Ошибки: {', '.join(errors)}"
    return f"Все {success_count} победителей добавлены успешно!"

# ──────────────────────────────────────────────
# ОБЩИЕ КОМАНДЫ
# ──────────────────────────────────────────────
vote_keyboard = ReplyKeyboardMarkup(
    keyboard=[[KeyboardButton(text="Проголосовать")], ],
    resize_keyboard=True, one_time_keyboard=False
)

@dp.message(Command("start"))
async def cmd_start(message: Message):
    await message.answer(
        "Привет! 🔥\n\n"
        "Битва ведущих начинается, и твой голос решает всё.\n"
        "Нажимай кнопку внизу и голосуй ⬇️\n",
        reply_markup=vote_keyboard
    )

@dp.message(lambda message: message.text == "Проголосовать")
async def cmd_vote_button(message: Message):
    # Просто перенаправляем на команду /vote
    await cmd_show_participants(message)

@dp.message(Command("help"))
async def cmd_help(message: Message):
    user_id = message.from_user.id
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Как проголосовать", callback_data="help_vote")],
        [InlineKeyboardButton(text="Мой Telegram ID", callback_data="help_myid")],
    ])
    if is_admin(user_id):
        kb.inline_keyboard.extend([
            [InlineKeyboardButton(text="Запуск раунда", callback_data="help_start_round")],
            [InlineKeyboardButton(text="Завершение раунда", callback_data="help_end_round")],
            [InlineKeyboardButton(text="Добавление участников", callback_data="help_add_participant")],
            [InlineKeyboardButton(text="Изменение текущего раунда", callback_data="help_set_current_round")],
        ])
    await message.answer("📖 Помощь — выберите пункт:", reply_markup=kb)
    await message.answer("Используй клавиатуру внизу для быстрого доступа", reply_markup=vote_keyboard)

@dp.callback_query(lambda c: c.data.startswith("help_"))
async def process_help_callback(callback: CallbackQuery):
    topic = callback.data.split("_")[1]
    texts = {
        "vote": "Напишите /vote — бот покажет участников с номерами и ФИО. Нажмите на кнопку — проголосуете.",
        "start": "Админ-команда /start_round — выбираете кампанию из списка, номер раунда (или авто).",
        "end": "Админ-команда /end_round — выбираете раунд, завершаете, решаете куда перенести победителей.",
        "add": "Админ-команда /add_participant — выбираете кампанию → раунд → добавляете участников по одному.",
        "set": "Админ-команда /set_current_round — выбираете кампанию → раунд → добавляете участников по одному.",
        "myid": "Напишите /myid — бот покажет ваш телеграмм ID"
    }
    text = texts.get(topic, "Подробностей пока нет.")
    await callback.message.answer(text)
    await callback.answer()

@dp.message(Command("myid"))
async def cmd_myid(message: Message):
    await message.answer(f"Ваш Telegram ID: **{message.from_user.id}**", reply_markup=vote_keyboard)

# ──────────────────────────────────────────────
# ГОЛОСОВАНИЕ
# ──────────────────────────────────────────────
@dp.message(Command("vote", "list", "participants"))
async def cmd_show_participants(message: Message):
    user_id = message.from_user.id
    url = f"{API_ACTIVE_ROUND_INFO}?user_id={user_id}"
    try:
        data = await api_get(url)
        if not data.get("round_id"):
            msg = data.get("message") or "Активного раунда сейчас нет."
            await message.answer(
                f"{msg}\n\nПриходи, как только запустят новый раунд!",
                reply_markup=vote_keyboard
            )
            return
        round_name = data["round_name"]
        round_type = data.get("round_type", "standard")
        participants = data["participants"]
        user_votes = data.get("user_votes", [])  # Список всех голосов
        print(round_name)
        text = ""
        #text = f"<b>{round_name}</b>\n\n"
        kb = InlineKeyboardMarkup(inline_keyboard=[])
        if round_type == "individual":
            text += "Готовы ли вы пригласить на свое мероприятие такого ведущего, как\n"
            if len(participants) == 0:
                text += "Участников пока нет\n"
            else:
                for p in participants:
                    full_name = p.get('full_name', '???')
                    description = p.get('description', '').strip()
                    # Основное сообщение — имя и описание крупно
                    text += f"<b>{full_name}</b>?"
                    # if description:
                    #     text += f"{description}\n"
                    #text += f"Голосов «Да»: {p['votes']}\n\n"
                    # Если пользователь уже голосовал — добавляем информацию
                    user_vote = next((v for v in user_votes if v["participant_id"] == p["id"]), None)
                    if user_vote:
                        choice_upper = user_vote.get('choice', '').upper()
                        # participant_name = user_vote.get('participant_name', '???')
                        text += f"\n\nВы уже проголосовали "
                        if choice_upper == 'YES':
                            text += f"за данного ведущего\n\n"
                        else:
                            text += f"против данного ведущего\n\n"
                # Кнопки — только Да / Нет, с отметкой если голосовал
                for p in participants:
                    row = []
                    # Кнопка "Да"
                    da_text = "Да"
                    user_vote = next((v for v in user_votes if v["participant_id"] == p["id"]), None)
                    if user_vote and user_vote.get("choice") == "yes":
                        da_text += " ❤️"
                    row.append(
                        InlineKeyboardButton(
                            text=da_text,
                            callback_data=f"vote_{data['round_id']}_{p['id']}_yes"
                        )
                    )
                    # Кнопка "Нет"
                    net_text = "Нет"
                    if user_vote and user_vote.get("choice") == "no":
                        net_text += " 💔"
                    row.append(
                        InlineKeyboardButton(
                            text=net_text,
                            callback_data=f"vote_{data['round_id']}_{p['id']}_no"
                        )
                    )
                    kb.inline_keyboard.append(row)
        else:
            # Standard: список кнопок для множественного выбора
            voted_participant_ids = [vote["participant_id"] for vote in user_votes]
            text += "Вы можете голосовать за нескольких (по 1 на каждого).\n"
            text += "Выберите участников (можно нескольких):\n"
            for p in participants:
                btn_text = f"#{p['order_number']} {p.get('full_name', '?')}" #({p['votes']} голосов)
                if p["id"] in voted_participant_ids:
                    btn_text += " ❤️   "
                kb.inline_keyboard.append([InlineKeyboardButton(text=btn_text, callback_data=f"vote_{data['round_id']}_{p['id']}")])
            #text = f"Сейчас не проходит голосование. \n\nПриходи, как только запустят!❤️️"
        await message.answer(text, reply_markup=kb, parse_mode="HTML")
    except Exception as e:
        logger.error(f"Ошибка загрузки раунда: {e}")
        await message.answer(
            "Не удалось проверить, идёт ли сейчас раунд 😕\nПопробуй позже.",
            reply_markup=vote_keyboard
        )

@dp.callback_query(lambda c: c.data.startswith("vote_"))
async def process_vote_callback(callback: CallbackQuery):
    try:
        parts = callback.data.split("_")
        round_id = int(parts[1])
        participant_id = int(parts[2])
        choice = parts[3] if len(parts) > 3 else None
    except Exception:
        await callback.answer("Ошибка кнопки 😕", show_alert=True)
        return

    user_id = callback.from_user.id
    payload = {
        "round": round_id,
        "participant": participant_id,
        "user_telegram_id": user_id
    }
    if choice:
        payload["choice"] = choice

    try:
        # Пытаемся отдать голос
        await api_post(API_VOTE_URL, payload, PUBLIC_HEADERS, timeout=8)
        await callback.answer("Голос учтён! Спасибо! ❤️", show_alert=True)

    except aiohttp.ClientResponseError as e:
        msg = "Не удалось проголосовать 😔"
        is_already_voted = False

        try:
            error_data = await e.response.json()
            error_text = error_data.get("non_field_errors", [str(error_data)])[0]
        except Exception:
            error_text = str(e)

        lower_text = error_text.lower()
        if "unique" in lower_text or "уже проголосовал" in lower_text:
            msg = "Вы уже проголосовали. Голос нельзя изменить"
            is_already_voted = True

        await callback.answer(msg, show_alert=True)

        # Если это не дубль — выходим без обновления
        if not is_already_voted:
            return

    except Exception as e:
        logger.error(f"Неожиданная ошибка при голосовании: {e}")
        await callback.answer("Что-то пошло не так... Попробуй позже", show_alert=True)
        return

    # ──────────────────────────────────────────────
    # Обновляем список участников (без лишней кнопки)
    # ──────────────────────────────────────────────
    try:
        url = f"{API_ACTIVE_ROUND_INFO}?user_id={user_id}"
        fresh_data = await api_get(url)

        if not fresh_data.get("round_id"):
            await callback.message.edit_text(
                "Активного раунда больше нет 😔",
                reply_markup=None
            )
            return

        round_name = fresh_data["round_name"]
        round_type = fresh_data.get("round_type", "standard")
        participants = fresh_data["participants"]
        user_votes = fresh_data.get("user_votes", [])

        #text = f"<b>{round_name}</b>\n\n"
        text = ""
        kb = InlineKeyboardMarkup(inline_keyboard=[])

        if round_type == "individual":
            text += "Готовы ли вы пригласить на свое мероприятие такого ведущего, как\n"
            if not participants:
                text += "Участников пока нет\n"
            else:
                for p in participants:
                    full_name = p.get('full_name', '???')
                    description = p.get('description', '').strip()
                    text += f"<b>{full_name}</b>?"
                    # if description:
                    #     text += f"{description}\n"
                    # text += f"Голосов «Да»: {p['votes']}\n\n"

            for p in participants:
                row = []
                da_text = "Да"
                user_vote = next((v for v in user_votes if v.get("participant_id") == p["id"]), None)
                if user_vote and user_vote.get("choice") == "yes":
                    text += "\n\nВы уже проголосовали за данного ведущего"
                    da_text += " ❤️"
                row.append(InlineKeyboardButton(
                    text=da_text,
                    callback_data=f"vote_{fresh_data['round_id']}_{p['id']}_yes"
                ))

                net_text = "Нет"
                if user_vote and user_vote.get("choice") == "no":
                    text += "\n\nВы уже проголосовали против данного ведущего"
                    net_text += " 💔"
                row.append(InlineKeyboardButton(
                    text=net_text,
                    callback_data=f"vote_{fresh_data['round_id']}_{p['id']}_no"
                ))
                kb.inline_keyboard.append(row)

        else:
            voted_participant_ids = [v["participant_id"] for v in user_votes]
            text += "Вы можете голосовать за нескольких (по 1 на каждого).\n"
            text += "Выберите участников (можно нескольких):\n"
            for p in participants:
                btn_text = f"#{p['order_number']} {p.get('full_name', '?')}" # ({p['votes']} голосов)
                if p["id"] in voted_participant_ids:
                    btn_text += " ❤️    "
                kb.inline_keyboard.append([InlineKeyboardButton(
                    text=btn_text,
                    callback_data=f"vote_{fresh_data['round_id']}_{p['id']}"
                )])


        # Редактируем сообщение
        await callback.message.edit_text(
            text,
            reply_markup=kb,
            parse_mode="HTML"
        )

    except Exception as refresh_err:
        logger.error(f"Ошибка обновления списка после голосования: {refresh_err}", exc_info=True)
        # Не трогаем сообщение, просто тихое уведомление
        await callback.answer(
            "Голос учтён, но список не обновился — нажми /vote для обновления",
            show_alert=False
        )

@dp.callback_query(lambda c: c.data == "refresh_participants")
async def refresh_participants(callback: CallbackQuery):
    fake_message = types.Message(
        message_id=callback.message.message_id,
        from_user=callback.from_user,
        chat=callback.message.chat,
        date=int(time.time()),
        text="/vote"
    )
    await cmd_show_participants(fake_message)
    await callback.message.delete()
    await callback.answer("Обновлено!")

# ──────────────────────────────────────────────
# АДМИН: ДОБАВЛЕНИЕ УЧАСТНИКОВ
# ──────────────────────────────────────────────
@dp.message(Command("add_participant"))
async def cmd_add_participant_start(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        await message.answer("Только для админов", reply_markup=vote_keyboard)
        return
    campaigns = await get_active_campaigns()
    if not campaigns:
        await message.answer("Нет активных кампаний.", reply_markup=vote_keyboard)
        return
    kb = InlineKeyboardMarkup(inline_keyboard=[])
    for c in campaigns:
        kb.inline_keyboard.append([
            InlineKeyboardButton(text=f"#{c['order_number']} {c['name']}", callback_data=f"addp_camp_{c['id']}")
        ])
    kb.inline_keyboard.append([InlineKeyboardButton(text="Отмена", callback_data="cancel")])
    await message.answer("Выберите кампанию:", reply_markup=kb)
    await state.set_state(AddParticipantStates.choose_campaign)

@dp.callback_query(lambda c: c.data.startswith("addp_camp_") or c.data == "cancel")
async def process_addp_campaign(callback: CallbackQuery, state: FSMContext):
    if callback.data == "cancel":
        await callback.message.edit_text("Отменено.")
        await state.clear()
        await callback.answer()
        return
    try:
        camp_id = int(callback.data.split("_")[-1])
    except:
        await callback.answer("Ошибка", show_alert=True)
        return
    rounds = await get_rounds_for_campaign(camp_id)
    if not rounds:
        await callback.message.edit_text("Нет раундов в кампании.")
        await state.clear()
        return
    kb = InlineKeyboardMarkup(inline_keyboard=[])
    for rd in rounds:
        kb.inline_keyboard.append([
            InlineKeyboardButton(text=f"Раунд #{rd['number']}", callback_data=f"addp_round_{rd['id']}")
        ])
    kb.inline_keyboard.append([InlineKeyboardButton(text="Отмена", callback_data="cancel")])
    await callback.message.edit_text("Выберите раунд:", reply_markup=kb)
    await state.update_data(campaign_id=camp_id)
    await state.set_state(AddParticipantStates.choose_round)
    await callback.answer()

@dp.callback_query(lambda c: c.data.startswith("addp_round_"))
async def process_addp_round(callback: CallbackQuery, state: FSMContext):
    try:
        round_id = int(callback.data.split("_")[-1])
    except:
        await callback.answer("Ошибка", show_alert=True)
        return
    await state.update_data(round_id=round_id)
    await callback.message.edit_text(
        "Отправляйте участников по одному:\nФИО [описание в скобках]\n\n"
        "Пример: Иванов Иван (хороший спикер)\n\n"
        "Готово / отмена — завершить."
    )
    await state.set_state(AddParticipantStates.waiting_for_name)
    await callback.answer()

@dp.message(AddParticipantStates.waiting_for_name)
async def process_add_participant_name(message: Message, state: FSMContext):
    txt = message.text.strip().lower()
    if txt in ("готово", "всё", "стоп", "отмена"):
        await message.answer("Добавление завершено.", reply_markup=vote_keyboard)
        await state.clear()
        return
    full_name = txt.title()
    description = ""
    if "(" in txt and ")" in txt:
        parts = txt.split("(", 1)
        full_name = parts[0].strip().title()
        description = parts[1].rstrip(")").strip()
    if not full_name:
        await message.answer("ФИО пустое. Попробуйте снова.", reply_markup=vote_keyboard)
        return
    data = await state.get_data()
    round_id = data.get("round_id")
    payload = {
        "round_id": round_id,
        "full_name": full_name,
        "description": description
    }
    try:
        await api_post(API_ADD_PARTICIPANT, payload)
        text = (f"Добавлен: {full_name} 👍\n"
                "Что дальше?\n "
                "Напиши «стоп» или «готово», чтобы завершить\n "
                "/vote — посмотреть, как выглядит раунд сейчас")

        await message.answer(text, reply_markup=vote_keyboard)
    except Exception as e:
        await message.answer(f"Ошибка: {str(e)}", reply_markup=vote_keyboard)

# ──────────────────────────────────────────────
# АДМИН: ЗАПУСК РАУНДА
# ──────────────────────────────────────────────
@dp.message(Command("start_round"))
async def cmd_start_round(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        await message.answer("Только для админов", reply_markup=vote_keyboard)
        return
    campaigns = await get_active_campaigns()
    kb = InlineKeyboardMarkup(inline_keyboard=[])
    for c in campaigns:
        kb.inline_keyboard.append([
            InlineKeyboardButton(text=f"#{c['order_number']} {c['name']}", callback_data=f"sr_camp_{c['id']}")
        ])
    kb.inline_keyboard.append([
        InlineKeyboardButton(text="Новая кампания", callback_data="sr_new_camp")
    ])
    await message.answer("Выберите кампанию:", reply_markup=kb)
    await state.set_state(StartRoundStates.choose_campaign)

@dp.callback_query(lambda c: c.data.startswith("sr_camp_") or c.data == "sr_new_camp")
async def process_sr_campaign(callback: CallbackQuery, state: FSMContext):
    if callback.data == "sr_new_camp":
        await callback.message.edit_text("Введите название новой кампании:")
        await state.set_state(StartRoundStates.enter_new_campaign_name)
        await callback.answer()
        return
    try:
        camp_id = int(callback.data.split("_")[-1])
    except:
        await callback.answer("Ошибка", show_alert=True)
        return
    await state.update_data(campaign_id=camp_id)
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Стандартный", callback_data="sr_type_standard")],
        [InlineKeyboardButton(text="Индивидуальный", callback_data="sr_type_individual")]
    ])
    await callback.message.edit_text("Выберите тип раунда:", reply_markup=kb)
    await state.set_state(StartRoundStates.choose_type)
    await callback.answer()

@dp.callback_query(lambda c: c.data.startswith("sr_type_"))
async def process_sr_type(callback: CallbackQuery, state: FSMContext):
    round_type = callback.data.split("_")[-1]
    await state.update_data(type=round_type)
    await callback.message.edit_text(
        "Запустить раунд с автоматическим номером?\n"
        "Или введите номер (например: 5)"
    )
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Авто-номер", callback_data="sr_auto")]
    ])
    await callback.message.edit_reply_markup(reply_markup=kb)
    await state.set_state(StartRoundStates.enter_round_number)
    await callback.answer()

@dp.callback_query(lambda c: c.data == "sr_auto")
@dp.message(StartRoundStates.enter_round_number)
async def process_sr_round_number(event: CallbackQuery | Message, state: FSMContext):
    data = await state.get_data()
    campaign_id = data.get("campaign_id")
    round_type = data.get("type")
    payload = {"campaign_id": campaign_id, "type": round_type}
    if isinstance(event, Message):
        try:
            num = int(event.text.strip())
            payload["number"] = num
        except:
            await event.answer("Число не распознано → авто.")
            payload["number"] = None  # Авто
    await state.update_data(payload=payload)  # Сохраняем payload для следующего шага
    await (event.message if isinstance(event, CallbackQuery) else event).answer(
        "Сколько победителей выбрать? (по умолчанию 3)"
    )
    await state.set_state(StartRoundStates.enter_winners_count)

@dp.message(StartRoundStates.enter_winners_count)
async def process_sr_winners_count(message: Message, state: FSMContext):
    data = await state.get_data()
    campaign_id = data.get("campaign_id")
    if not campaign_id:
        await message.answer("Ошибка: не найдена кампания. Начните заново с /start_round", reply_markup=vote_keyboard)
        await state.clear()
        return
    print("Данные состояния перед созданием раунда:", data)
    winners = data.get("winners", [])  # может быть пустым при обычном старте
    is_auto_transfer = data.get("is_auto_transfer", False)
    round_number = data.get("number")  # может быть None
    round_type = data.get("type") or "standard"  # ← защита от None
    try:
        winners_count = int(message.text.strip())
        if winners_count < 1:
            winners_count = 3
    except ValueError:
        winners_count = 3
        await message.answer("Не удалось распознать число → используем 3 по умолчанию.", reply_markup=vote_keyboard)
    payload = {
        "campaign_id": campaign_id,
        "winners_count": winners_count,
        "type": round_type
    }
    if round_number is not None:
        payload["number"] = round_number
    print("Отправляем в /api/start-round/: ", payload)
    try:
        resp = await api_post(API_START_ROUND, payload)
        round_id = resp.get("round_id")
        msg = (f"✅ Раунд создан (победителей: {winners_count}, тип: {round_type}) "
               f"\n Для добавления участников /add_participant")
        if is_auto_transfer and winners:
            result = await transfer_winners_to_round(winners, round_id)
            msg += f"\n{result}"
        elif is_auto_transfer:
            msg += "\n(победители не найдены — перенос пропущен)"

        msg += "\n\nЧто дальше?\n"
        msg += "• /add_participant — добавить участников в этот раунд\n"
        msg += "• /vote — посмотреть, как выглядит раунд для пользователей\n"
        msg += "• /end_round — когда захочешь завершить раунд\n"
        msg += "• /set_current_round — если нужно переключить текущий раунд"

        await message.answer(msg, reply_markup=vote_keyboard)
    except Exception as e:
        await message.answer(f"Ошибка создания раунда: {str(e)}", reply_markup=vote_keyboard)
    await state.clear()

@dp.message(StartRoundStates.enter_new_campaign_name)
async def process_sr_new_campaign(message: Message, state: FSMContext):
    name = message.text.strip()
    if not name:
        await message.answer("Название пустое.", reply_markup=vote_keyboard)
        return
    payload = {"name": name, "admin_telegram_id": message.from_user.id}
    try:
        data = await api_post(API_CREATE_CAMPAIGN, payload)
        await message.answer(f"Кампания #{data['campaign_order_number']} создана. Запускаем раунд...", reply_markup=vote_keyboard)
        round_payload = {"campaign_id": data["campaign_id"]}
        await state.update_data(payload=round_payload)
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="Стандартный", callback_data="sr_type_standard")],
            [InlineKeyboardButton(text="Индивидуальный", callback_data="sr_type_individual")]
        ])
        await message.answer("Выберите тип раунда:", reply_markup=kb)
        await state.set_state(StartRoundStates.choose_type)
    except Exception as e:
        await message.answer(f"Ошибка: {e}", reply_markup=vote_keyboard)
        await state.clear()

# ──────────────────────────────────────────────
# ЗАВЕРШЕНИЕ РАУНДА + ПЕРЕНОС
# ──────────────────────────────────────────────
@dp.message(Command("end_round"))
async def cmd_end_round(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        await message.answer("Только для админов", reply_markup=vote_keyboard)
        return
    campaigns = await get_active_campaigns()
    kb = InlineKeyboardMarkup(inline_keyboard=[])
    for c in campaigns:
        kb.inline_keyboard.append([
            InlineKeyboardButton(text=f"#{c['order_number']} {c['name']}", callback_data=f"er_camp_{c['id']}")
        ])
    await message.answer("Выберите кампанию:", reply_markup=kb)
    await state.set_state(EndRoundStates.choose_campaign)

@dp.callback_query(lambda c: c.data.startswith("er_camp_"))
async def process_er_campaign(callback: CallbackQuery, state: FSMContext):
    try:
        camp_id = int(callback.data.split("_")[-1])
    except:
        await callback.answer("Ошибка", show_alert=True)
        return
    rounds = await get_rounds_for_campaign(camp_id)
    active = [r for r in rounds if r["status"] == "active"]
    if not active:
        await callback.message.edit_text("Нет активных раундов в этой кампании.")
        await state.clear()
        return
    kb = InlineKeyboardMarkup(inline_keyboard=[])
    for rd in active:
        kb.inline_keyboard.append([
            InlineKeyboardButton(text=f"Раунд #{rd['number']} ({rd['type']})", callback_data=f"er_round_{rd['id']}")
        ])
    await callback.message.edit_text("Выберите раунд для завершения:", reply_markup=kb)
    await state.set_state(EndRoundStates.choose_round)
    await callback.answer()

@dp.callback_query(lambda c: c.data.startswith("er_round_"))
async def process_er_round(callback: CallbackQuery, state: FSMContext):
    # Защита от повторных нажатий
    data = await state.get_data()
    if data.get("round_ended", False):
        await callback.answer("Раунд уже завершён", show_alert=True)
        return
    try:
        round_id = int(callback.data.split("_")[-1])
    except Exception:
        await callback.answer("Неверные данные кнопки", show_alert=True)
        return
    # Помечаем, что начали обработку
    await state.update_data(processing_round=round_id)
    payload = {"round_id": round_id}
    try:
        resp = await api_post(API_END_ROUND, payload)
        # Успех → фиксируем завершение
        await state.update_data(round_ended=True, processing_round=None)
        winners = resp.get("winners", [])
        campaign_id = resp.get("ended_round_campaign_id")
        round_type = resp.get("round_type", "standard")
        await state.update_data(
            campaign_id=campaign_id,
            winners=winners,
            ended_round_id=round_id
        )
        text = f"Раунд завершён.\nПобедители:\n"
        if winners:
            text += "\n".join([f"{w['full_name']} ({w['votes']} да)" for w in winners])
        else:
            text += "Победителей нет."
        kb = InlineKeyboardMarkup(inline_keyboard=[])
        if round_type == "individual":
            standard_rounds = await get_rounds_for_campaign(campaign_id, "standard")
            if standard_rounds:
                text += "\n\nВыберите стандартный раунд для переноса:"
                for rd in standard_rounds:
                    kb.inline_keyboard.append([
                        InlineKeyboardButton(
                            text=f"Раунд #{rd['number']} (камп. {rd['campaign_order_number']})",
                            callback_data=f"trans_target_{rd['id']}"
                        )
                    ])
            else:
                text += "\n\nНет подходящих раундов для переноса."
            kb.inline_keyboard.append([InlineKeyboardButton(text="Не переносить", callback_data="trans_skip")])
        else:
            # старая логика для стандартного
            active_rounds = await get_rounds_for_campaign(campaign_id)
            active = [r for r in active_rounds if r["status"] == "active" and r["id"] != round_id]
            if active:
                kb.inline_keyboard.append([InlineKeyboardButton(text="В существующий раунд", callback_data="trans_existing")])
            kb.inline_keyboard.extend([
                [InlineKeyboardButton(text="В ту же кампанию (новый раунд)", callback_data="trans_same")],
                [InlineKeyboardButton(text="В новую кампанию", callback_data="trans_new")],
                [InlineKeyboardButton(text="Не переносить", callback_data="trans_skip")],
            ])
        # Обновляем сообщение и убираем старую клавиатуру
        await callback.message.edit_text(text, reply_markup=kb)
        await callback.answer("Раунд завершён!")
    except aiohttp.ClientResponseError as e:
        error_text = "Неизвестная ошибка"
        try:
            error_data = await e.response.json() if hasattr(e.response, 'json') else {}
            error_text = error_data.get("error") or error_data.get("detail") or str(
                error_data) or await e.response.text()
        except Exception:
            error_text = str(e)

        lower_text = error_text.lower()

        if "уже завершён" in lower_text or "уже был завершён" in lower_text:
            await state.update_data(round_ended=True, processing_round=None)
            await callback.message.edit_text(
                "Раунд уже был завершён ранее.",
                reply_markup=InlineKeyboardMarkup(inline_keyboard=[])
            )
            await callback.answer("Уже завершено")
        else:
            await callback.message.edit_text(
                f"Ошибка при завершении раунда:\n{error_text}\n\nПопробуйте ещё раз или проверьте статус раунда."
            )
            await callback.answer("Ошибка сервера", show_alert=True)

    except Exception as e:
        logger.error(f"Критическая ошибка в process_er_round: {e}", exc_info=True)
        await callback.message.edit_text(
            "Произошла критическая ошибка. Попробуйте позже или перезапустите команду /end_round."
        )
        await callback.answer("Что-то сломалось 😔", show_alert=True)

    # В любом случае очищаем состояние при ошибке
    #await state.clear()
    await callback.answer()  # завершаем callback, чтобы убрать "часики"

@dp.callback_query(lambda c: c.data.startswith("trans_target_"))
async def process_transfer_target(callback: CallbackQuery, state: FSMContext):
    try:
        target_round_id = int(callback.data.split("_")[-1])
    except:
        await callback.answer("Ошибка", show_alert=True)
        return
    data = await state.get_data()
    ended_round_id = data.get("ended_round_id")
    payload = {
        "round_id": ended_round_id,
        "target_round_id": target_round_id
    }
    try:
        resp = await api_post(API_TRANSFER_WINNERS, payload)

        text = resp.get("message", "Перенос выполнен успешно! 🎉")

        text += "\n\nЧто дальше?\n"
        text += "• /add_participant — добавить ещё участников (если нужно)\n"
        text += "• /vote — проверить, как теперь выглядит раунд\n"
        text += "• /end_round — завершить следующий раунд\n"
        text += "• /start_round — запустить новый раунд"
        await callback.message.edit_text(text)
    except Exception as e:
        await callback.message.edit_text(f"Ошибка переноса: {str(e)}")
    await state.clear()
    await callback.answer()

@dp.callback_query(lambda c: c.data == "trans_existing")
async def process_transfer_existing(callback: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    campaign_id = data.get("campaign_id")
    winners = data.get("winners", [])
    rounds = await get_rounds_for_campaign(campaign_id)
    active = [r for r in rounds if r["status"] == "active"]
    if not active:
        await callback.message.edit_text("Нет активных раундов для переноса.")
        await state.clear()
        await callback.answer()
        return
    kb = InlineKeyboardMarkup(inline_keyboard=[])
    for rd in active:
        kb.inline_keyboard.append([
            InlineKeyboardButton(text=f"Раунд #{rd['number']}", callback_data=f"trans_exist_round_{rd['id']}")
        ])
    kb.inline_keyboard.append([InlineKeyboardButton(text="Отмена", callback_data="trans_skip")])
    await callback.message.edit_text("Выберите активный раунд для переноса:", reply_markup=kb)
    await state.set_state(TransferWinnersStates.choose_existing_round)
    await callback.answer()

@dp.callback_query(lambda c: c.data.startswith("trans_exist_round_"))
async def process_transfer_existing_round(callback: CallbackQuery, state: FSMContext):
    try:
        target_round_id = int(callback.data.split("_")[-1])
    except:
        await callback.answer("Ошибка", show_alert=True)
        return
    data = await state.get_data()
    winners = data.get("winners", [])
    result = await transfer_winners_to_round(winners, target_round_id)

    text = f"Победители перенесены в выбранный раунд. {result}\n\n"

    text += "Отлично! 🎉 Что дальше?\n"
    text += "• /vote — посмотреть обновлённый список участников\n"
    text += "• /add_participant — добавить кого-то ещё вручную\n"
    text += "• /end_round — завершить этот раунд позже\n"
    text += "• /start_round — если нужно запустить ещё один"

    await callback.message.edit_text(text)
    await state.clear()
    await callback.answer()

@dp.callback_query(lambda c: c.data == "trans_same")
async def process_transfer_same(callback: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    winners = data.get("winners", [])
    campaign_id = data.get("campaign_id")
    if not winners:
        await callback.message.edit_text("Нет победителей для переноса.")
        await state.clear()
        await callback.answer()
        return
    await state.update_data(
        campaign_id=campaign_id,
        winners=winners,
        is_auto_transfer=True  # метка, что это перенос
    )
    await callback.message.edit_text(
        "Создаём новый раунд в этой же кампании.\n"
        "Сколько победителей будет в новом раунде? (по умолчанию 3)"
    )
    await state.set_state(StartRoundStates.enter_winners_count)
    await callback.answer()

@dp.callback_query(lambda c: c.data == "trans_new")
async def process_transfer_new(callback: CallbackQuery, state: FSMContext):
    await callback.message.edit_text("Введите название новой кампании:")
    await state.set_state(TransferWinnersStates.enter_new_campaign_name)
    await callback.answer()

@dp.message(TransferWinnersStates.enter_new_campaign_name)
async def process_transfer_new_campaign(message: Message, state: FSMContext):
    name = message.text.strip()
    if not name:
        await message.answer("Название пустое.", reply_markup=vote_keyboard)
        return
    data = await state.get_data()
    winners = data.get("winners", [])
    payload = {"name": name, "admin_telegram_id": message.from_user.id}
    try:
        camp = await api_post(API_CREATE_CAMPAIGN, payload)
        campaign_id = camp["campaign_id"]
        await state.update_data(
            campaign_id=campaign_id,
            winners=winners,
            is_auto_transfer=True
        )
        await message.answer(f"Кампания #{camp['campaign_order_number']} создана. Запускаем раунд...", reply_markup=vote_keyboard)
        await message.answer("Сколько победителей выбрать в новом раунде? (по умолчанию 3)", reply_markup=vote_keyboard)
        await state.set_state(StartRoundStates.enter_winners_count)
    except Exception as e:
        await message.answer(f"Ошибка: {e}", reply_markup=vote_keyboard)
        await state.clear()

@dp.callback_query(lambda c: c.data == "trans_skip")
async def process_transfer_skip(callback: CallbackQuery, state: FSMContext):
    await callback.message.edit_text("Перенос пропущен.")
    await state.clear()
    await callback.answer()

@dp.message(Command("set_current_round"))
async def cmd_set_current_round(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        await message.answer("Только для админов", reply_markup=vote_keyboard)
        return
    campaigns = await get_active_campaigns()
    if not campaigns:
        await message.answer("Нет активных кампаний.", reply_markup=vote_keyboard)
        return
    kb = InlineKeyboardMarkup(inline_keyboard=[])
    for c in campaigns:
        kb.inline_keyboard.append([
            InlineKeyboardButton(text=c['name'], callback_data=f"setcr_camp_{c['id']}")
        ])
    kb.inline_keyboard.append([InlineKeyboardButton(text="Отмена", callback_data="cancel")])
    await message.answer("Выберите кампанию:", reply_markup=kb)
    await state.set_state("set_current_round_camp")

@dp.callback_query(lambda c: c.data.startswith("setcr_camp_") or c.data == "cancel")
async def process_setcr_camp(callback: CallbackQuery, state: FSMContext):
    if callback.data == "cancel":
        await callback.message.edit_text("Отменено.")
        await state.clear()
        await callback.answer()
        return
    try:
        camp_id = int(callback.data.split("_")[-1])
    except:
        await callback.answer("Ошибка", show_alert=True)
        return
    rounds = await get_rounds_for_campaign(camp_id)
    active = [r for r in rounds if r["status"] == "active"]
    if not active:
        await callback.message.edit_text("Нет активных раундов.")
        await state.clear()
        return
    kb = InlineKeyboardMarkup(inline_keyboard=[])
    for rd in active:
        kb.inline_keyboard.append([
            InlineKeyboardButton(text=f"Раунд #{rd['number']}", callback_data=f"setcr_round_{rd['id']}")
        ])
    await callback.message.edit_text("Выберите раунд как текущий:", reply_markup=kb)
    await state.set_state("set_current_round_round")
    await callback.answer()

@dp.callback_query(lambda c: c.data.startswith("setcr_round_"))
async def process_setcr_round(callback: CallbackQuery, state: FSMContext):
    try:
        round_id = int(callback.data.split("_")[-1])
    except:
        await callback.answer("Ошибка", show_alert=True)
        return
    payload = {"round_id": round_id}
    try:
        resp = await api_post(API_SET_CURRENT_ROUND, payload)
        await callback.message.edit_text(resp.get("message", "Готово!"))
    except Exception as e:
        await callback.message.edit_text(f"Ошибка: {e}")
    await state.clear()
    await callback.answer()

# ──────────────────────────────────────────────
# ЗАПУСК
# ──────────────────────────────────────────────
async def main():
    logger.info("Бот запущен")
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())