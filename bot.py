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

ADMIN_IDS = [1251634923, 1401411234]

# Заголовки
PUBLIC_HEADERS = {"Content-Type": "application/json"}
ADMIN_HEADERS = {"Authorization": f"Token {DJANGO_API_TOKEN}", "Content-Type": "application/json"}

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher(storage=MemoryStorage())

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
        r = requests.get(API_ACTIVE_CAMPAIGNS, headers=PUBLIC_HEADERS, timeout=8)
        r.raise_for_status()
        return r.json().get("campaigns", [])
    except Exception as e:
        logger.error(f"Ошибка получения кампаний: {e}")
        return []

async def get_active_rounds(round_type: str = None) -> List[Dict]:
    try:
        r = requests.get(API_ACTIVE_ROUNDS, headers=PUBLIC_HEADERS, timeout=8)
        r.raise_for_status()
        rounds = r.json().get("rounds", [])
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
            r = requests.post(API_ADD_PARTICIPANT, json=payload, headers=ADMIN_HEADERS, timeout=10)
            r.raise_for_status()
            success_count += 1
        except Exception as e:
            errors.append(f"{winner['full_name']}: {str(e)}")
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
        r = requests.get(url, headers=PUBLIC_HEADERS, timeout=8)
        data = r.json()
        if r.status_code != 200 or not data.get("round_id"):
            msg = data.get("message") or "Активного раунда сейчас нет."
            await message.answer(
                f"{msg}\n\nПриходи, как только запустят новый раунд!",
                reply_markup=vote_keyboard
            )
            return

        round_name = data["round_name"]
        round_type = data.get("round_type", "standard")
        participants = data["participants"]
        user_vote = data.get("user_vote")
        print(round_name)
        text = f"<b>{round_name}</b>\n\n"
        kb = InlineKeyboardMarkup(inline_keyboard=[])

        if round_type == "individual":
            text += "<b>Оцените ведущего</b>\n\n"

            if len(participants) == 0:
                text += "Участников пока нет\n"
            else:
                for p in participants:
                    full_name = p.get('full_name', '???')
                    description = p.get('description', '').strip()

                    # Основное сообщение — имя и описание крупно
                    text += f"<b>{full_name}</b>\n"
                    if description:
                        text += f"{description}\n"
                    text += f"Голосов «Да»: {p['votes']}\n\n"

            # Если пользователь уже голосовал — добавляем информацию
            if user_vote:
                choice_upper = user_vote.get('choice', '').upper()
                participant_name = user_vote.get('participant_name', '???')
                text += f"Вы уже оставили голос "
                if choice_upper == 'YES':
                    text += f"за данного ведущего\n\n"
                else:
                    text += f"против данного ведущего\n\n"

            # Кнопки — только Да / Нет, с отметкой если голосовал
            for p in participants:
                row = []

                # Кнопка "Да"
                da_text = "Да"
                if user_vote and user_vote.get("participant_id") == p["id"] and user_vote.get("choice") == "yes":
                    da_text += " ✓"
                row.append(
                    InlineKeyboardButton(
                        text=da_text,
                        callback_data=f"vote_{data['round_id']}_{p['id']}_yes"
                    )
                )

                # Кнопка "Нет"
                net_text = "Нет"
                if user_vote and user_vote.get("participant_id") == p["id"] and user_vote.get("choice") == "no":
                    net_text += " ✓"
                row.append(
                    InlineKeyboardButton(
                        text=net_text,
                        callback_data=f"vote_{data['round_id']}_{p['id']}_no"
                    )
                )

                kb.inline_keyboard.append(row)
        else:
            # Standard: список кнопок для множественного выбора
            voted_participants = []
            if user_vote:  # Теперь user_vote может быть списком, но API возвращает первый — адаптировать если нужно
                text += "Вы можете голосовать за нескольких (по 1 на каждого).\n"
                # Для множественного: нужно fetch все votes пользователя
                # Но для простоты: в API ActiveRoundInfo можно адаптировать return list user_votes
                # Пока предполагаем single для display, but allow multiple in backend
            else:
                text += "Выберите участников (можно нескольких):\n"
            for p in participants:
                btn_text = f"#{p['order_number']} {p.get('full_name', '?')} ({p['votes']} голосов)"
                if user_vote and user_vote["participant_id"] == p["id"]:
                    btn_text += " ✓"
                kb.inline_keyboard.append([InlineKeyboardButton(text=btn_text, callback_data=f"vote_{data['round_id']}_{p['id']}")])

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
    except:
        await callback.answer("Ошибка кнопки", show_alert=True)
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
        r = requests.post(API_VOTE_URL, json=payload, headers=PUBLIC_HEADERS, timeout=8)
        r.raise_for_status()
        await callback.message.edit_text("Голос учтён! Спасибо!")
        await callback.answer("Голос принят!")
    except Exception as e:
        await callback.answer("Не удалось проголосовать", show_alert=True)

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
        await message.answer("Только для админов")
        return
    campaigns = await get_active_campaigns()
    if not campaigns:
        await message.answer("Нет активных кампаний.")
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
        await message.answer("Добавление завершено.")
        await state.clear()
        return
    full_name = txt.title()
    description = ""
    if "(" in txt and ")" in txt:
        parts = txt.split("(", 1)
        full_name = parts[0].strip().title()
        description = parts[1].rstrip(")").strip()
    if not full_name:
        await message.answer("ФИО пустое. Попробуйте снова.")
        return
    data = await state.get_data()
    round_id = data.get("round_id")
    payload = {
        "round_id": round_id,
        "full_name": full_name,
        "description": description
    }
    try:
        r = requests.post(API_ADD_PARTICIPANT, json=payload, headers=ADMIN_HEADERS, timeout=10)
        r.raise_for_status()
        await message.answer(f"Добавлен: {full_name}. Чтобы завершить добавление, напишите 'стоп')")
    except Exception as e:
        await message.answer(f"Ошибка: {str(e)}")

# ──────────────────────────────────────────────
# АДМИН: ЗАПУСК РАУНДА
# ──────────────────────────────────────────────
@dp.message(Command("start_round"))
async def cmd_start_round(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        await message.answer("Только для админов")
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
        await message.answer("Ошибка: не найдена кампания. Начните заново с /start_round")
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
        await message.answer("Не удалось распознать число → используем 3 по умолчанию.")
    payload = {
        "campaign_id": campaign_id,
        "winners_count": winners_count,
        "type": round_type
    }
    if round_number is not None:
        payload["number"] = round_number

    print("Отправляем в /api/start-round/: ", payload)

    try:
        r = requests.post(API_START_ROUND, json=payload, headers=ADMIN_HEADERS, timeout=10)
        r.raise_for_status()
        resp = r.json()
        round_id = resp.get("round_id")
        msg = f"✅ Раунд создан (победителей: {winners_count}, тип: {round_type})"
        if is_auto_transfer and winners:
            result = await transfer_winners_to_round(winners, round_id)
            msg += f"\n{result}"
        elif is_auto_transfer:
            msg += "\n(победители не найдены — перенос пропущен)"
        await message.answer(msg)
    except Exception as e:
        await message.answer(f"Ошибка создания раунда: {str(e)}")
    await state.clear()

@dp.message(StartRoundStates.enter_new_campaign_name)
async def process_sr_new_campaign(message: Message, state: FSMContext):
    name = message.text.strip()
    if not name:
        await message.answer("Название пустое.")
        return
    payload = {"name": name, "admin_telegram_id": message.from_user.id}
    try:
        r = requests.post(API_CREATE_CAMPAIGN, json=payload, headers=ADMIN_HEADERS, timeout=10)
        r.raise_for_status()
        data = r.json()
        await message.answer(f"Кампания #{data['campaign_order_number']} создана. Запускаем раунд...")
        round_payload = {"campaign_id": data["campaign_id"]}
        await state.update_data(payload=round_payload)
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="Стандартный", callback_data="sr_type_standard")],
            [InlineKeyboardButton(text="Индивидуальный", callback_data="sr_type_individual")]
        ])
        await message.answer("Выберите тип раунда:", reply_markup=kb)
        await state.set_state(StartRoundStates.choose_type)
    except Exception as e:
        await message.answer(f"Ошибка: {e}")
        await state.clear()

# ──────────────────────────────────────────────
# ЗАВЕРШЕНИЕ РАУНДА + ПЕРЕНОС
# ──────────────────────────────────────────────
@dp.message(Command("end_round"))
async def cmd_end_round(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        await message.answer("Только для админов")
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
        r = requests.post(API_END_ROUND, json=payload, headers=ADMIN_HEADERS, timeout=10)
        r.raise_for_status()
        resp = r.json()

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

            kb.inline_keyboard.append([
                InlineKeyboardButton(text="Не переносить", callback_data="trans_skip")
            ])
        else:
            # старая логика для стандартного
            active_rounds = await get_rounds_for_campaign(campaign_id)
            active = [r for r in active_rounds if r["status"] == "active" and r["id"] != round_id]

            if active:
                kb.inline_keyboard.append([
                    InlineKeyboardButton(text="В существующий раунд", callback_data="trans_existing")
                ])

            kb.inline_keyboard.extend([
                [InlineKeyboardButton(text="В ту же кампанию (новый раунд)", callback_data="trans_same")],
                [InlineKeyboardButton(text="В новую кампанию", callback_data="trans_new")],
                [InlineKeyboardButton(text="Не переносить", callback_data="trans_skip")],
            ])

        # Обновляем сообщение и убираем старую клавиатуру
        await callback.message.edit_text(text, reply_markup=kb)
        await callback.answer("Раунд завершён!")

    except requests.HTTPError as e:
        if e.response and e.response.status_code == 400:
            try:
                error_msg = e.response.json().get("error", "Ошибка сервера")
                if "уже завершён" in error_msg.lower():
                    await state.update_data(round_ended=True, processing_round=None)
                    await callback.message.edit_text(
                        "Раунд уже был завершён ранее.",
                        reply_markup=InlineKeyboardMarkup(inline_keyboard=[])
                    )
                    await callback.answer("Уже завершено")
                else:
                    await callback.message.edit_text(f"Ошибка: {error_msg}")
            except:
                await callback.message.edit_text("Не удалось разобрать ошибку сервера")
        else:
            await callback.message.edit_text(f"Ошибка связи: {str(e)}")

        await state.clear()
        await callback.answer()

    except Exception as e:
        logger.error(f"Ошибка в process_er_round: {e}", exc_info=True)
        await callback.message.edit_text(f"Критическая ошибка: {str(e)}")
        await state.clear()
        await callback.answer()

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
        r = requests.post(
            f"{DJANGO_API_BASE}/api/transfer-winners/",  # новый эндпоинт
            json=payload,
            headers=ADMIN_HEADERS,
            timeout=10
        )
        r.raise_for_status()
        resp = r.json()
        await callback.message.edit_text(resp.get("message", "Перенос выполнен."))
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
    await callback.message.edit_text(f"Победители перенесены в выбранный раунд. {result}")
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
        is_auto_transfer=True # метка, что это перенос
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
        await message.answer("Название пустое.")
        return
    data = await state.get_data()
    winners = data.get("winners", [])
    payload = {"name": name, "admin_telegram_id": message.from_user.id}
    try:
        r = requests.post(API_CREATE_CAMPAIGN, json=payload, headers=ADMIN_HEADERS, timeout=10)
        r.raise_for_status()
        camp = r.json()
        campaign_id = camp["campaign_id"]
        await state.update_data(
            campaign_id=campaign_id,
            winners=winners,
            is_auto_transfer=True
        )
        await message.answer(f"Кампания #{camp['campaign_order_number']} создана. Запускаем раунд...")
        await message.answer("Сколько победителей выбрать в новом раунде? (по умолчанию 3)")
        await state.set_state(StartRoundStates.enter_winners_count)
    except Exception as e:
        await message.answer(f"Ошибка: {e}")
        await state.clear()

@dp.callback_query(lambda c: c.data == "trans_skip")
async def process_transfer_skip(callback: CallbackQuery, state: FSMContext):
    await callback.message.edit_text("Перенос пропущен.")
    await state.clear()
    await callback.answer()

@dp.message(Command("set_current_round"))
async def cmd_set_current_round(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        await message.answer("Только для админов")
        return
    campaigns = await get_active_campaigns()
    if not campaigns:
        await message.answer("Нет активных кампаний.")
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
        r = requests.post(API_SET_CURRENT_ROUND, json=payload, headers=ADMIN_HEADERS, timeout=10)
        r.raise_for_status()
        resp = r.json()
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