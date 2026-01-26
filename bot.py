# bot.py
import asyncio
import logging
import requests
from typing import Optional, List, Dict

from aiogram import Bot, Dispatcher, types
from aiogram.filters import Command
from aiogram.types import (
    Message, InlineKeyboardMarkup, InlineKeyboardButton,
    CallbackQuery
)
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage

# ──────────────────────────────────────────────
# НАСТРОЙКИ — поменяй под себя
# ──────────────────────────────────────────────

BOT_TOKEN = "8221500401:AAEMcTQ1U1OD9VkCJ2EvqmIoh2jVmDlP-uA"
DJANGO_API_BASE = "http://127.0.0.1:8000"
API_VOTE_URL = f"{DJANGO_API_BASE}/api/vote/"
API_ACTIVE_PARTICIPANTS = f"{DJANGO_API_BASE}/api/active-participants/"
API_ACTIVE_ROUND_INFO = f"{DJANGO_API_BASE}/api/active-round-info/"
API_ACTIVE_ROUNDS = f"{DJANGO_API_BASE}/api/active-rounds/"
API_START_ROUND = f"{DJANGO_API_BASE}/api/start-round/"
API_END_ROUND = f"{DJANGO_API_BASE}/api/end-round/"
API_ADD_PARTICIPANT = f"{DJANGO_API_BASE}/api/add-participant/"
API_CREATE_CAMPAIGN = f"{DJANGO_API_BASE}/api/create-campaign/"
API_ACTIVE_CAMPAIGNS = f"{DJANGO_API_BASE}/api/active-campaigns/"

ADMIN_IDS = [1251634923]  # добавь сюда все админ-ID

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher(storage=MemoryStorage())


# ──────────────────────────────────────────────
# Состояния FSM
# ──────────────────────────────────────────────

class AddParticipantStates(StatesGroup):
    choose_round = State()
    waiting_for_name = State()


# class StartRoundStates(StatesGroup):
#     enter_data = State()
class StartRoundStates(StatesGroup):
    enter_data = State()
    choose_campaign = State()
    enter_round_number = State()          # если не авто
    enter_new_campaign_name = State()

class EndRoundStates(StatesGroup):
    choose_round = State()


class TransferWinnersStates(StatesGroup):
    choose_action = State()          # Выбор: существующий или новый раунд
    choose_existing_round = State()  # Выбор существующего
    enter_new_round_data = State()   # Ввод данных для нового
    confirm_transfer = State()       # Подтверждение
    enter_new_campaign_name = State()

# ──────────────────────────────────────────────
# Вспомогательные функции
# ──────────────────────────────────────────────

def is_admin(user_id: int) -> bool:
    return user_id in ADMIN_IDS


async def get_active_rounds() -> List[Dict]:
    try:
        r = requests.get(API_ACTIVE_ROUNDS, timeout=8)
        r.raise_for_status()
        return r.json()
    except Exception as e:
        logger.error(f"Ошибка получения активных раундов: {e}")
        return []


async def transfer_winners_to_round(winners: List[Dict], target_round_id: int) -> str:
    """Добавляет победителей в целевой раунд как новых участников"""
    success_count = 0
    errors = []
    for winner in winners:
        payload = {
            "round_id": target_round_id,
            "full_name": winner["full_name"],
            "description": f"Победитель предыдущего раунда (голосов: {winner['votes']})"
        }
        try:
            r = requests.post(API_ADD_PARTICIPANT, json=payload, timeout=10)
            r.raise_for_status()
            success_count += 1
        except Exception as e:
            errors.append(f"{winner['full_name']}: {str(e)}")

    if errors:
        return f"Добавлено {success_count}/{len(winners)}. Ошибки: {', '.join(errors)}"
    return f"Все {success_count} победителей добавлены успешно!"


# ──────────────────────────────────────────────
# Команды для всех пользователей (без изменений, для полноты)
# ──────────────────────────────────────────────

@dp.message(Command("start"))
async def cmd_start(message: Message):
    await message.answer(
        "Привет! 👋 Это бот для голосования.\n\n"
        "Что можно делать:\n"
        "• /vote /list /participants — посмотреть текущий раунд и проголосовать\n"
        "• /help — все доступные команды\n"
        "• /myid — узнать свой Telegram ID\n\n"

    )


@dp.message(Command("help"))
async def cmd_help(message: Message):
    user_id = message.from_user.id

    # Базовые кнопки, которые видны всем
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="Как проголосовать", callback_data="help_vote"),
            InlineKeyboardButton(text="Мой Telegram ID", callback_data="help_myid"),
        ],
        [
            InlineKeyboardButton(text="Статус раунда", callback_data="help_status"),
            InlineKeyboardButton(text="Общие команды", callback_data="help_common"),
        ],
    ])

    # Дополнительные кнопки только для админа
    if is_admin(user_id):
        kb.inline_keyboard.extend([
            [
                InlineKeyboardButton(text="Запуск нового раунда", callback_data="help_start_round"),
                InlineKeyboardButton(text="Завершение раунда", callback_data="help_end_round"),
            ],
            [
                InlineKeyboardButton(text="Добавление участников", callback_data="help_add_participant"),
            ],
        ])

    text = (
        "📖 Помощь по командам\n\n"
        "Выберите тему ниже, чтобы получить подробное объяснение:"
    )

    await message.answer(text, reply_markup=kb)


@dp.callback_query(lambda c: c.data.startswith("help_"))
async def process_help_callback(callback: CallbackQuery):
    topic = callback.data.split("_")[1]
    user_id = callback.from_user.id
    print(topic)
    print(is_admin(user_id))
    if topic == "vote":
        text = (
            "Как проголосовать:\n\n"
            "Напишите одну из команд:\n"
            "• /vote\n"
            "• /list\n"
            "• /participants\n\n"
            "Бот покажет текущий активный раунд и список участников с количеством голосов.\n"
            "Нажмите на кнопку с именем участника — ваш голос будет учтён мгновенно.\n\n"
            "Голосовать можно только один раз за раунд."
        )

    elif topic == "myid":
        text = (
            "/myid — показывает ваш Telegram ID.\n\n"
            "Это полезно, если вы админ и хотите добавить себя в список администраторов бота\n"
            "(в файле bot.py в переменной ADMIN_IDS)."
        )

    elif topic == "status":
        text = (
            "/status — показывает базовую информацию о текущем раунде:\n"
            "• название раунда\n"
            "• статус (активен / завершён)\n\n"
            "Если активного раунда нет — бот сообщит об этом."
        )

    elif topic == "common":
        text = (
            "Общие команды, доступные всем:\n\n"
            "• /start — приветствие и начало работы\n"
            "• /help — это меню помощи\n"
            "• /vote, /list, /participants — голосование\n"
            "• /myid — ваш Telegram ID\n"
            "• /status — статус раунда"
        )

    # Админские темы — показываем только если пользователь админ
    elif topic == "start" and is_admin(user_id):
        text = (
            "Запуск нового раунда (/start_round):\n\n"
            "Формат: /start_round <campaign_id> <номер_раунда>\n\n"
            "Примеры:\n"
            "• /start_round 1 3     → раунд №3 в кампании 1\n"
            "• /start_round 2 1     → первый раунд в кампании 2\n\n"
            "Номер раунда можно указать любой, но рекомендуется последовательный.\n"
            "После запуска раунд становится активным — участники могут голосовать."
        )

    elif topic == "end" and is_admin(user_id):
        text = (
            "Завершение раунда (/end_round):\n\n"
            "1. Напишите /end_round\n"
            "2. Выберите раунд из списка активных\n"
            "3. Подтвердите завершение\n\n"
            "После завершения:\n"
            "• раунд переходит в статус «Завершён»\n"
            "• бот покажет топ-победителей\n"
            "• вы сможете перенести победителей в другой раунд (та же кампания или новая)"
        )

    elif topic == "add" and is_admin(user_id):
        text = (
            "Добавление участников (/add_participant):\n\n"
            "1. Напишите /add_participant\n"
            "2. Выберите активный раунд\n"
            "3. По одному вводите участников:\n"
            "   - Иванов Иван Иванович\n"
            "   - Сидорова Анна (отличный оратор)\n"
            "4. Когда закончите — напишите «готово» или «отмена»\n\n"
            "Участники сразу появятся в раунде и будут доступны для голосования."
        )

    else:
        text = "Извините, по этой теме пока нет подробной информации."

    await callback.message.answer(text)
    await callback.answer()  # убираем "часики" на кнопке


@dp.message(Command("myid"))
async def cmd_myid(message: Message):
    user_id = message.from_user.id
    await message.answer(f"Твой Telegram ID: **{user_id}**\n\n"
                         "Если ты админ — добавь его в ADMIN_IDS в коде бота.")


@dp.message(Command("status"))
async def cmd_status(message: Message):
    try:
        r = requests.get(API_ACTIVE_ROUND_INFO, timeout=8)
        r.raise_for_status()
        data = r.json()
        if "error" in data:
            await message.answer(data["error"])
        else:
            await message.answer(f"Текущий раунд: {data.get('round_name', 'нет активного')}\n"
                                 f"Статус: {data.get('status', 'неизвестно')}")
    except Exception as e:
        await message.answer(f"Не удалось получить статус: {str(e)}")


# ──────────────────────────────────────────────
# Голосование — список участников + кнопки (без изменений)
# ──────────────────────────────────────────────

@dp.message(Command("vote", "list", "participants"))
async def cmd_show_participants(message: Message):
    user_id = message.from_user.id
    url = f"{API_ACTIVE_ROUND_INFO}?user_id={user_id}"

    try:
        r = requests.get(url, timeout=8)
        r.raise_for_status()
        data = r.json()

        if "error" in data:
            await message.answer(data["error"])
            return

        round_id = data["round_id"]
        round_name = data["round_name"]
        participants = data["participants"]
        user_vote = data.get("user_vote")

        lines = [f"Раунд: <b>{round_name}</b> (ID: {round_id})"]

        if user_vote:
            lines.append("")
            lines.append("Вы уже проголосовали:")
            lines.append(f"👉 <b>{user_vote['participant_name']}</b>")
            lines.append("Изменить голос пока нельзя (скоро добавим)")
        else:
            lines.append("\nВыберите участника для голосования:")

        keyboard = InlineKeyboardMarkup(inline_keyboard=[])
        for p in participants:
            btn_text = f"{p['full_name']} ({p['votes']} голосов)"
            if user_vote and user_vote["participant_id"] == p["id"]:
                btn_text += " ✓"
            btn = InlineKeyboardButton(
                text=btn_text,
                callback_data=f"vote_{round_id}_{p['id']}"
            )
            keyboard.inline_keyboard.append([btn])

        await message.answer("\n".join(lines), reply_markup=keyboard, parse_mode="HTML")

    except Exception as e:
        logger.error(f"Ошибка при /vote: {e}")
        await message.answer("Не удалось загрузить участников. Попробуйте позже.")


@dp.callback_query(lambda c: c.data.startswith("vote_"))
async def process_vote_callback(callback: CallbackQuery):
    try:
        _, round_id_str, participant_id_str = callback.data.split("_")
        round_id = int(round_id_str)
        participant_id = int(participant_id_str)
    except:
        await callback.answer("Ошибка кнопки", show_alert=True)
        return

    user_id = callback.from_user.id
    payload = {
        "round": round_id,
        "participant": participant_id,
        "user_telegram_id": user_id
    }

    try:
        r = requests.post(API_VOTE_URL, json=payload, timeout=8)
        r.raise_for_status()

        if r.status_code in (200, 201):
            await callback.message.edit_text(
                f"Голос учтён за участника #{participant_id}!\nСпасибо!",
                parse_mode="HTML"
            )
            await callback.answer("Голос принят!")
        else:
            error_msg = r.json().get("detail", r.text)
            await callback.answer(error_msg, show_alert=True)

    except Exception as e:
        logger.error(f"Ошибка голосования: {e}")
        await callback.answer("Не удалось отправить голос", show_alert=True)


# ──────────────────────────────────────────────
# Админ-команды
# ──────────────────────────────────────────────

# /add_participant — пошагово (без изменений, кроме .title() для ФИО)
@dp.message(Command("add_participant"))
async def cmd_add_participant_start(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        await message.answer("Только для админов")
        return

    rounds = await get_active_rounds()
    if not rounds:
        await message.answer("Нет активных раундов. Сначала запустите раунд через /start_round")
        return

    kb = InlineKeyboardMarkup(inline_keyboard=[])
    for rd in rounds:
        text = f"{rd['round_name']} ({rd.get('campaign_name', 'Кампания')}) — {rd.get('participants_count', 0)} уч."
        kb.inline_keyboard.append([
            InlineKeyboardButton(text=text, callback_data=f"addp_round_{rd['round_id']}")
        ])
    kb.inline_keyboard.append([InlineKeyboardButton(text="Отмена", callback_data="cancel")])

    await message.answer("Выберите раунд для добавления участников:", reply_markup=kb)
    await state.set_state(AddParticipantStates.choose_round)


@dp.callback_query(lambda c: c.data.startswith("addp_round_") or c.data == "cancel")
async def process_add_participant_round(callback: CallbackQuery, state: FSMContext):
    if callback.data == "cancel":
        await callback.message.edit_text("Действие отменено.")
        await state.clear()
        await callback.answer()
        return

    try:
        round_id = int(callback.data.split("_")[-1])
    except:
        await callback.answer("Ошибка выбора раунда", show_alert=True)
        return

    await state.update_data(round_id=round_id)
    await callback.message.edit_text(
        f"Выбран раунд ID {round_id}.\n\n"
        "Отправляйте участников по одному:\n"
        "ФИО [описание в скобках — опционально]\n\n"
        "Примеры:\n"
        "Иванов Иван Иванович\n"
        "Сидорова Анна (отличный оратор)\n\n"
        "Когда закончите — напишите «готово» или «отмена»"
    )
    await callback.answer()
    await state.set_state(AddParticipantStates.waiting_for_name)


@dp.message(AddParticipantStates.waiting_for_name)
async def process_add_participant_name(message: Message, state: FSMContext):
    txt = message.text.strip().lower()
    if txt in ("готово", "всё", "стоп", "отмена", "finish", "done", "cancel"):
        await message.answer("Добавление участников завершено.")
        await state.clear()
        return

    if "(" in txt and ")" in txt:
        name_part, desc_part = txt.split("(", 1)
        full_name = name_part.strip().title()  # ← ФИКС: .title() для нормализации регистра
        description = desc_part.rstrip(")").strip()
    else:
        full_name = txt.title()  # ← ФИКС: .title()
        description = ""

    if not full_name:
        await message.answer("ФИО не может быть пустым. Попробуйте снова.")
        return

    data = await state.get_data()
    round_id = data.get("round_id")

    payload = {
        "round_id": round_id,
        "full_name": full_name,
        "description": description
    }

    try:
        r = requests.post(API_ADD_PARTICIPANT, json=payload, timeout=10)
        r.raise_for_status()
        await message.answer(
            f"✅ Добавлен участник:\n"
            f"ФИО: {full_name}\n"
            f"Описание: {description or '—'}\n\n"
            "Добавить ещё? (или напишите «готово»)"
        )
    except Exception as e:
        await message.answer(f"❌ Ошибка: {str(e)}\nПопробуйте снова или отмените.")

@dp.message(Command("start_round"))
async def cmd_start_round(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        await message.answer("Только для админов")
        return

    try:
        r = requests.get(API_ACTIVE_CAMPAIGNS, timeout=8)
        r.raise_for_status()
        data = r.json()
        campaigns = data.get("campaigns", [])
    except Exception as e:
        await message.answer(f"Не удалось загрузить список кампаний: {str(e)}")
        return

    if not campaigns:
        await message.answer(
            "Активных кампаний пока нет.\n"
            "Хотите создать новую прямо сейчас?",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
                InlineKeyboardButton(text="Создать новую кампанию", callback_data="new_campaign_start_round")
            ]])
        )
        await state.set_state(StartRoundStates.enter_new_campaign_name)
        return

    kb = InlineKeyboardMarkup(inline_keyboard=[])
    for camp in campaigns:
        text = f"{camp['name']} (раундов: {camp['rounds_count']})"
        kb.inline_keyboard.append([
            InlineKeyboardButton(text=text, callback_data=f"camp_{camp['id']}")
        ])
    kb.inline_keyboard.append([
        InlineKeyboardButton(text="Создать новую кампанию", callback_data="new_campaign_start_round")
    ])

    await message.answer(
        "Выберите кампанию для запуска раунда:",
        reply_markup=kb
    )
    await state.set_state(StartRoundStates.choose_campaign)


@dp.callback_query(lambda c: c.data.startswith("camp_") or c.data == "new_campaign_start_round")
async def process_campaign_choice(callback: CallbackQuery, state: FSMContext):
    if callback.data == "new_campaign_start_round":
        await callback.message.edit_text(
            "Введите название новой кампании:"
        )
        await state.set_state(StartRoundStates.enter_new_campaign_name)
        await callback.answer()
        return

    try:
        campaign_id = int(callback.data.split("_")[1])
    except:
        await callback.answer("Ошибка выбора кампании", show_alert=True)
        return

    await state.update_data(campaign_id=campaign_id)

    # Можно сразу запустить с авто-номером
    await callback.message.edit_text(
        f"Кампания выбрана. Запускаем раунд с автоматическим номером?\n\n"
        "Или введите желаемый номер раунда (например: 4)"
    )
    await state.set_state(StartRoundStates.enter_round_number)
    await callback.answer()


@dp.message(StartRoundStates.enter_round_number)
async def process_round_number(message: Message, state: FSMContext):
    text = message.text.strip()
    data = await state.get_data()
    campaign_id = data.get("campaign_id")

    payload = {"campaign_id": campaign_id}

    if text.isdigit():
        payload["number"] = int(text)

    # иначе — сервер сам поставит следующий номер

    try:
        r = requests.post(API_START_ROUND, json=payload, timeout=10)
        r.raise_for_status()
        resp = r.json()
        await message.answer(f"✅ {resp.get('message', 'Раунд успешно запущен!')}")
    except Exception as e:
        await message.answer(f"Ошибка запуска раунда: {str(e)}")

    await state.clear()


@dp.message(StartRoundStates.enter_new_campaign_name)
async def process_new_campaign_name(message: Message, state: FSMContext):
    name = message.text.strip()
    if not name:
        await message.answer("Название не может быть пустым. Попробуйте снова.")
        return

    payload = {
        "name": name,
        "admin_telegram_id": message.from_user.id
    }

    try:
        r = requests.post(API_CREATE_CAMPAIGN, json=payload, timeout=10)
        r.raise_for_status()
        data = r.json()
        campaign_id = data["campaign_id"]

        await message.answer(f"Кампания «{name}» создана. Запускаем первый раунд...")

        # Запускаем раунд №1 автоматически
        round_payload = {"campaign_id": campaign_id}
        rr = requests.post(API_START_ROUND, json=round_payload, timeout=10)
        rr.raise_for_status()
        round_data = rr.json()

        await message.answer(f"✅ {round_data.get('message', 'Первый раунд запущен!')}")
    except Exception as e:
        await message.answer(f"Ошибка: {str(e)}")

    await state.clear()

# /end_round — с выбором раунда + перенос победителей
@dp.message(Command("end_round"))
async def cmd_end_round_start(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        await message.answer("Только для админов")
        return

    rounds = await get_active_rounds()
    if not rounds:
        await message.answer("Нет активных раундов для завершения.")
        return

    kb = InlineKeyboardMarkup(inline_keyboard=[])
    for rd in rounds:
        text = f"{rd['round_name']} (ID {rd['round_id']})"
        kb.inline_keyboard.append([
            InlineKeyboardButton(text=text, callback_data=f"end_r_{rd['round_id']}")
        ])
    kb.inline_keyboard.append([InlineKeyboardButton(text="Отмена", callback_data="cancel")])

    await message.answer("Выберите раунд для завершения:", reply_markup=kb)
    await state.set_state(EndRoundStates.choose_round)


@dp.callback_query(lambda c: c.data.startswith("end_r_") or c.data == "cancel")
async def process_end_round_selection(callback: CallbackQuery, state: FSMContext):
    if callback.data == "cancel":
        await callback.message.edit_text("Завершение отменено.")
        await state.clear()
        await callback.answer()
        return

    try:
        round_id = int(callback.data.split("_")[-1])
    except:
        await callback.answer("Ошибка выбора", show_alert=True)
        return

    payload = {"round_id": round_id}

    try:
        r = requests.post(API_END_ROUND, json=payload, timeout=10)
        r.raise_for_status()
        data = r.json()
        await callback.message.edit_text(f"✅ {data.get('message', 'Раунд завершён!')}")

        # Теперь стартуем перенос победителей
        winners = data.get("winners", [])
        winners_count = data.get("winners_count", 0)
        ended_round_campaign_id = data.get("ended_round_campaign_id")  # Нужно добавить в EndRoundAPIView: "ended_round_campaign_id": round_obj.campaign.id

        if not winners:
            await callback.message.answer("Нет победителей для переноса (возможно, 0 голосов).")
            await state.clear()
            await callback.answer()
            return

        # Сохраняем в state
        await state.update_data(winners=winners, ended_campaign_id=ended_round_campaign_id, ended_round_id=round_id)

        # Пояснение и вопрос о кампании
        winners_list = "\n".join([f"- {w['full_name']} ({w['votes']} голосов)" for w in winners])
        text = (
            f"Раунд завершён успешно.\n\n"
            f"Топ-{winners_count} победителей:\n{winners_list}\n\n"
            "Сохранить победителей в ту же кампанию? (Если да — создадим новый раунд автоматически. Если нет — создадим новую кампанию с названием от вас.)"
        )
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="Да, в ту же кампанию", callback_data="transfer_same_campaign")],
            [InlineKeyboardButton(text="Нет, в новую кампанию", callback_data="transfer_new_campaign")],
            [InlineKeyboardButton(text="Пропустить перенос", callback_data="transfer_skip")],
        ])

        await callback.message.answer(text, reply_markup=kb)
        await state.set_state(TransferWinnersStates.choose_action)

    except Exception as e:
        await callback.message.edit_text(f"❌ Ошибка: {str(e)}")
        await state.clear()
        await callback.answer()

# ──────────────────────────────────────────────
# Логика переноса победителей (новый FSM)
# ──────────────────────────────────────────────

@dp.callback_query(lambda c: c.data.startswith("transfer_"))
async def process_transfer_choice(callback: CallbackQuery, state: FSMContext):
    action = callback.data

    if action == "transfer_skip":
        await callback.message.edit_text("Перенос победителей пропущен.")
        await state.clear()
        await callback.answer()
        return

    data = await state.get_data()
    winners = data.get("winners", [])
    ended_campaign_id = data.get("ended_campaign_id")

    if action == "transfer_same_campaign":
        # Создаём новый раунд в той же кампании (number auto)
        payload = {"campaign_id": ended_campaign_id}  # number не передаём — API возьмёт auto
        try:
            r = requests.post(API_START_ROUND, json=payload, timeout=10)
            r.raise_for_status()
            new_data = r.json()
            target_round_id = new_data["round_id"]
            await callback.message.answer(f"✅ Новый раунд создан автоматически в той же кампании (ID скрыт). Добавляем победителей...")

            result = await transfer_winners_to_round(winners, target_round_id)
            await callback.message.answer(result)
            await state.clear()
        except Exception as e:
            await callback.message.answer(f"❌ Ошибка: {str(e)}")
            await state.clear()
        await callback.answer()
        return

    elif action == "transfer_new_campaign":
        await callback.message.edit_text(
            "Создаём новую кампанию.\n\n"
            "Отправьте название кампании (например: 'Новая кампания 2026').\n"
            "После — бот создаст кампанию и первый раунд в ней, добавит победителей."
        )
        await state.set_state(TransferWinnersStates.enter_new_campaign_name)
        await callback.answer()
        return

@dp.callback_query(lambda c: c.data.startswith("transfer_to_") or c.data == "transfer_cancel")
async def process_existing_round_selection(callback: CallbackQuery, state: FSMContext):
    if callback.data == "transfer_cancel":
        await callback.message.edit_text("Перенос отменён.")
        await state.clear()
        await callback.answer()
        return

    try:
        target_round_id = int(callback.data.split("_")[-1])
    except:
        await callback.answer("Ошибка выбора раунда", show_alert=True)
        return

    data = await state.get_data()
    winners = data.get("winners", [])

    # Подтверждение
    winners_list = "\n".join([f"- {w['full_name']}" for w in winners])
    text = (
        f"Подтвердите добавление победителей в раунд ID {target_round_id}:\n"
        f"{winners_list}\n\n"
        "Это позволит объединить результаты раундов."
    )
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Подтвердить", callback_data=f"confirm_transfer_{target_round_id}")],
        [InlineKeyboardButton(text="Отмена", callback_data="transfer_cancel")],
    ])

    await callback.message.edit_text(text, reply_markup=kb)
    await state.set_state(TransferWinnersStates.confirm_transfer)
    await callback.answer()


@dp.message(TransferWinnersStates.enter_new_round_data)
async def process_new_round_data(message: Message, state: FSMContext):
    args = message.text.strip().split()
    if len(args) < 2:
        await message.answer("Недостаточно данных. Формат: <campaign_id> <номер_раунда> [длительность_мин]")
        return

    try:
        campaign_id = int(args[0])
        number = int(args[1])

    except ValueError:
        await message.answer("Все параметры должны быть числами.")
        return

    payload = {
        "campaign_id": campaign_id,
        "number": number,

    }

    try:
        r = requests.post(API_START_ROUND, json=payload, timeout=10)
        r.raise_for_status()
        data = r.json()
        new_round_id = data.get("round_id")

        await message.answer(f"✅ Новый раунд создан (ID: {new_round_id}). Теперь добавляем победителей...")

        # Автоматически добавляем
        data_state = await state.get_data()
        winners = data_state.get("winners", [])
        result = await transfer_winners_to_round(winners, new_round_id)
        await message.answer(result)

        await state.clear()
    except Exception as e:
        await message.answer(f"❌ Ошибка создания раунда: {str(e)}")


@dp.callback_query(lambda c: c.data.startswith("confirm_transfer_") or c.data == "transfer_cancel")
async def process_confirm_transfer(callback: CallbackQuery, state: FSMContext):
    if callback.data == "transfer_cancel":
        await callback.message.edit_text("Перенос отменён.")
        await state.clear()
        await callback.answer()
        return

    try:
        target_round_id = int(callback.data.split("_")[-1])
    except:
        await callback.answer("Ошибка подтверждения", show_alert=True)
        return

    data = await state.get_data()
    winners = data.get("winners", [])

    result = await transfer_winners_to_round(winners, target_round_id)

    await callback.message.edit_text(f"✅ {result}")
    await state.clear()
    await callback.answer()

@dp.message(TransferWinnersStates.enter_new_campaign_name)
async def process_new_campaign_name(message: Message, state: FSMContext):
    name = message.text.strip()
    if not name:
        await message.answer("Название не может быть пустым. Попробуйте снова.")
        return

    data = await state.get_data()
    admin_id = message.from_user.id  # или из модели, но для простоты

    payload = {
        "name": name,
        "admin_telegram_id": admin_id
    }

    try:
        r = requests.post(API_CREATE_CAMPAIGN, json=payload, timeout=10)
        r.raise_for_status()
        camp_data = r.json()
        new_campaign_id = camp_data["campaign_id"]

        await message.answer(f"✅ Кампания '{name}' создана (ID скрыт). Теперь создаём первый раунд...")

        # Создаём раунд в новой кампании (number=1 auto)
        round_payload = {"campaign_id": new_campaign_id}
        r_round = requests.post(API_START_ROUND, json=round_payload, timeout=10)
        r_round.raise_for_status()
        round_data = r_round.json()
        target_round_id = round_data["round_id"]

        winners = data.get("winners", [])
        result = await transfer_winners_to_round(winners, target_round_id)
        await message.answer(result)

        await state.clear()
    except Exception as e:
        await message.answer(f"❌ Ошибка: {str(e)}")

# ──────────────────────────────────────────────
# Запуск бота
# ──────────────────────────────────────────────

async def main():
    logger.info("Бот запускается...")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())