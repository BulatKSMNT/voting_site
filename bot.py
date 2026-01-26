# bot.py (исправленный полный код)
import asyncio
import logging
import requests
import time
from typing import List, Dict

from aiogram import Bot, Dispatcher, types
from aiogram.filters import Command
from aiogram.types import (
    Message, InlineKeyboardMarkup, InlineKeyboardButton,
    CallbackQuery
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
API_ACTIVE_PARTICIPANTS = f"{DJANGO_API_BASE}/api/active-participants/"
API_ACTIVE_ROUND_INFO = f"{DJANGO_API_BASE}/api/active-round-info/"
API_ACTIVE_ROUNDS = f"{DJANGO_API_BASE}/api/active-rounds/"
API_START_ROUND = f"{DJANGO_API_BASE}/api/start-round/"
API_END_ROUND = f"{DJANGO_API_BASE}/api/end-round/"
API_ADD_PARTICIPANT = f"{DJANGO_API_BASE}/api/add-participant/"
API_CREATE_CAMPAIGN = f"{DJANGO_API_BASE}/api/create-campaign/"
API_ACTIVE_CAMPAIGNS = f"{DJANGO_API_BASE}/api/active-campaigns/"

ADMIN_IDS = [1251634923, 1401411234]

# Заголовки
PUBLIC_HEADERS = {
    "Content-Type": "application/json"
}

ADMIN_HEADERS = {
    "Authorization": f"Token {DJANGO_API_TOKEN}",
    "Content-Type": "application/json"
}

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
    enter_round_number = State()
    enter_winners_count = State()  # Для выбора количества победителей
    enter_new_campaign_name = State()


class EndRoundStates(StatesGroup):
    choose_campaign = State()
    choose_round = State()


class TransferWinnersStates(StatesGroup):
    choose_action = State()          # та же / новая / существующий раунд
    choose_existing_campaign = State()  # Выбор существующей кампании
    choose_existing_round = State()     # Выбор существующего раунда
    enter_new_campaign_name = State()


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


async def get_active_rounds() -> List[Dict]:
    try:
        r = requests.get(API_ACTIVE_ROUNDS, headers=PUBLIC_HEADERS, timeout=8)
        r.raise_for_status()
        return r.json().get("rounds", [])
    except Exception as e:
        logger.error(f"Ошибка получения раундов: {e}")
        return []


async def get_rounds_for_campaign(campaign_id: int) -> List[Dict]:
    rounds = await get_active_rounds()
    print(rounds)
    return [rd for rd in rounds if rd.get("campaign_order_number") == campaign_id]


async def transfer_winners_to_round(winners: List[Dict], target_round_id: int) -> str:
    success_count = 0
    errors = []
    for winner in winners:
        payload = {
            "round_id": target_round_id,
            "full_name": winner["full_name"],
            "description": f"Победитель предыдущего раунда (голосов: {winner['votes']})"
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

@dp.message(Command("start"))
async def cmd_start(message: Message):
    await message.answer(
        "Привет! 👋 Это бот для голосования.\n\n"
        "Что можно делать:\n"
        "• /vote /list /participants — посмотреть текущий раунд и проголосовать\n"
        "• /help — все доступные команды\n"
        "• /myid — узнать свой Telegram ID\n\n"
        "Админам доступны специальные команды"
    )


@dp.message(Command("help"))
async def cmd_help(message: Message):
    user_id = message.from_user.id
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Как проголосовать", callback_data="help_vote")],
        [InlineKeyboardButton(text="Мой Telegram ID", callback_data="help_myid")],
        [InlineKeyboardButton(text="Статус раунда", callback_data="help_status")],
    ])
    if is_admin(user_id):
        kb.inline_keyboard.extend([
            [InlineKeyboardButton(text="Запуск раунда", callback_data="help_start_round")],
            [InlineKeyboardButton(text="Завершение раунда", callback_data="help_end_round")],
            [InlineKeyboardButton(text="Добавление участников", callback_data="help_add_participant")],
        ])
    await message.answer("📖 Помощь — выберите тему:", reply_markup=kb)


@dp.callback_query(lambda c: c.data.startswith("help_"))
async def process_help_callback(callback: CallbackQuery):
    topic = callback.data.split("_")[1]

    texts = {
        "vote": "Напишите /vote — бот покажет участников с номерами и ФИО. Нажмите на кнопку — проголосуете.",
        "myid": "/myid покажет ваш Telegram ID.",
        "status": "/status покажет текущий раунд и его статус.",
        "start": "Админ-команда /start_round — выбираете кампанию из списка, номер раунда (или авто).",
        "end": "Админ-команда /end_round — выбираете раунд, завершаете, решаете куда перенести победителей.",
        "add": "Админ-команда /add_participant — выбираете кампанию → раунд → добавляете участников по одному.",
    }
    text = texts.get(topic, "Подробностей пока нет.")
    await callback.message.answer(text)
    await callback.answer()


@dp.message(Command("myid"))
async def cmd_myid(message: Message):
    await message.answer(f"Твой Telegram ID: **{message.from_user.id}**")


@dp.message(Command("status"))
async def cmd_status(message: Message):
    try:
        r = requests.get(API_ACTIVE_ROUND_INFO, headers=PUBLIC_HEADERS, timeout=8)
        r.raise_for_status()
        data = r.json()
        if "error" in data:
            await message.answer(data["error"])
        else:
            await message.answer(f"Раунд: {data.get('round_name', 'нет активного')}\nСтатус: {data.get('status', 'неизвестно')}")
    except Exception as e:
        await message.answer(f"Ошибка: {str(e)}")


# ──────────────────────────────────────────────
# ГОЛОСОВАНИЕ
# ──────────────────────────────────────────────

@dp.message(Command("vote", "list", "participants"))
async def cmd_show_participants(message: Message):
    user_id = message.from_user.id
    url = f"{API_ACTIVE_ROUND_INFO}?user_id={user_id}"
    try:
        r = requests.get(url, headers=PUBLIC_HEADERS, timeout=8)
        r.raise_for_status()
        data = r.json()
        if "error" in data:
            await message.answer(data["error"])
            return

        round_name = data["round_name"]
        participants = data["participants"]
        user_vote = data.get("user_vote")

        text = f"<b>{round_name}</b>\n\n"
        if user_vote:
            text += f"Вы уже проголосовали за {user_vote['participant_name']}\n"
        else:
            text += "Выберите участника:\n"

        kb = InlineKeyboardMarkup(inline_keyboard=[])
        for p in participants:
            btn_text = f"#{p['order_number']} {p.get('full_name', '?')} ({p['votes']} голосов)"
            if user_vote and user_vote["participant_id"] == p["id"]:
                btn_text += " ✓"
            kb.inline_keyboard.append([InlineKeyboardButton(text=btn_text, callback_data=f"vote_{data['round_id']}_{p['id']}")])


        await message.answer(text, reply_markup=kb, parse_mode="HTML")
    except Exception as e:
        logger.error(f"Ошибка /vote: {e}")
        await message.answer("Не удалось загрузить участников.")


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
        r = requests.post(API_VOTE_URL, json=payload, headers=PUBLIC_HEADERS, timeout=8)
        r.raise_for_status()
        await callback.message.edit_text("Голос учтён! Спасибо!")
        await callback.answer("Голос принят!")
    except Exception as e:
        await callback.answer("Не удалось проголосовать", show_alert=True)


@dp.callback_query(lambda c: c.data == "refresh_participants")
async def refresh_participants(callback: CallbackQuery):
    # Создаём фейковое сообщение с обязательным полем date
    fake_message = types.Message(
        message_id=callback.message.message_id,
        from_user=callback.from_user,
        chat=callback.message.chat,
        date=int(time.time()),  # Текущее время в Unix timestamp (обязательное поле!)
        text="/vote"  # Симулируем команду
    )

    await cmd_show_participants(fake_message)
    await callback.message.delete()  # Удаляем старое сообщение после вызова
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
    print(campaigns)
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
        await message.answer(f"Добавлен: {full_name}")
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
async def process_sr_round_number(event: types.CallbackQuery | Message, state: FSMContext):
    data = await state.get_data()
    campaign_id = data.get("campaign_id")
    payload = {"campaign_id": campaign_id}

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
    payload = data.get("payload", {})
    try:
        winners_count = int(message.text.strip())
    except ValueError:
        winners_count = 3  # Default
        await message.answer("Число не распознано → используем 3 по умолчанию.")

    payload["winners_count"] = winners_count

    try:
        r = requests.post(API_START_ROUND, json=payload, headers=ADMIN_HEADERS, timeout=10)
        r.raise_for_status()
        resp = r.json()
        await message.answer(
            f"✅ {resp.get('message')} (победителей: {winners_count})"
        )
    except Exception as e:
        await message.answer(f"Ошибка: {e}")

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
        await state.update_data(payload=round_payload)  # Сохраняем для следующего шага
        await message.answer("Сколько победителей выбрать? (по умолчанию 3)")
        await state.set_state(StartRoundStates.enter_winners_count)  # Переходим к выбору победителей
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
        await callback.message.edit_text("Нет активных раундов.")
        await state.clear()
        return

    kb = InlineKeyboardMarkup(inline_keyboard=[])
    for rd in active:
        kb.inline_keyboard.append([
            InlineKeyboardButton(text=f"Раунд #{rd['number']}", callback_data=f"er_round_{rd['id']}")
        ])

    await callback.message.edit_text("Выберите раунд:", reply_markup=kb)
    await state.set_state(EndRoundStates.choose_round)
    await callback.answer()


@dp.callback_query(lambda c: c.data.startswith("er_round_"))
async def process_er_round(callback: CallbackQuery, state: FSMContext):
    try:
        round_id = int(callback.data.split("_")[-1])
    except:
        await callback.answer("Ошибка", show_alert=True)
        return

    payload = {"round_id": round_id}
    try:
        r = requests.post(API_END_ROUND, json=payload, headers=ADMIN_HEADERS, timeout=10)
        r.raise_for_status()
        data = r.json()
        await callback.message.edit_text(f"Раунд завершён.")

        winners = data.get("winners", [])
        if winners:
            text = "Победители:\n" + "\n".join([f"{w['full_name']} ({w['votes']})" for w in winners])
            kb = InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="В ту же кампанию", callback_data="trans_same")],
                [InlineKeyboardButton(text="В новую кампанию", callback_data="trans_new")],
                [InlineKeyboardButton(text="В существующий раунд", callback_data="trans_existing")],
                [InlineKeyboardButton(text="Не переносить", callback_data="trans_skip")],
            ])
            await callback.message.answer(text, reply_markup=kb)
            await state.update_data(winners=winners, current_round_id=round_id,
                                    campaign_id=data.get("ended_round_campaign_id"))
            await state.set_state(TransferWinnersStates.choose_action)
        else:
            await callback.message.answer("Нет победителей.")
            await state.clear()
    except Exception as e:
        await callback.message.edit_text(f"Ошибка: {e}")
        await state.clear()

    await callback.answer()


@dp.callback_query(lambda c: c.data.startswith("trans_"))
async def process_transfer(callback: CallbackQuery, state: FSMContext):
    if callback.data == "trans_skip":
        await callback.message.edit_text("Перенос отменён.")
        await state.clear()
        await callback.answer()
        return

    data = await state.get_data()
    winners = data.get("winners", [])

    if callback.data == "trans_same":
        campaign_id = data.get("campaign_id")
        if not campaign_id:
            await callback.message.answer("Не удалось определить кампанию.")
            await state.clear()
            await callback.answer()
            return

        rounds = await get_rounds_for_campaign(campaign_id)
        if not rounds:
            # Автоматически создаём раунд, если нет активных
            round_payload = {"campaign_id": campaign_id}
            try:
                rr = requests.post(API_START_ROUND, json=round_payload, headers=ADMIN_HEADERS, timeout=10)
                rr.raise_for_status()
                round_data = rr.json()
                target_round_id = round_data["round_id"]
                await callback.message.answer("Создан новый раунд, поскольку активных не найдено.")
            except Exception as e:
                await callback.message.edit_text(f"Ошибка создания раунда: {e}")
                await state.clear()
                await callback.answer()
                return
        else:
            # Если есть активные, выбираем первый (по умолчанию)
            target_round_id = rounds[0]["id"]
            await callback.message.answer("Переносим в первый активный раунд в той же кампании.")

        result = await transfer_winners_to_round(winners, target_round_id)
        await callback.message.edit_text(f"Победители перенесены в раунд в той же кампании. {result}")
        await state.clear()
        await callback.answer()
        return

    if callback.data == "trans_new":
        await callback.message.edit_text("Введите название новой кампании:")
        await state.set_state(TransferWinnersStates.enter_new_campaign_name)
        await callback.answer()
        return

    if callback.data == "trans_existing":
        campaigns = await get_active_campaigns()
        kb = InlineKeyboardMarkup(inline_keyboard=[])
        for c in campaigns:
            kb.inline_keyboard.append([
                InlineKeyboardButton(text=f"#{c['order_number']} {c['name']}", callback_data=f"trans_exist_camp_{c['id']}")
            ])
        kb.inline_keyboard.append([InlineKeyboardButton(text="Отмена", callback_data="trans_skip")])

        await callback.message.answer("Выберите существующую кампанию для переноса:", reply_markup=kb)
        await state.set_state(TransferWinnersStates.choose_existing_campaign)
        await callback.answer()
        return


@dp.callback_query(lambda c: c.data.startswith("trans_exist_camp_"))
async def process_transfer_existing_campaign(callback: CallbackQuery, state: FSMContext):
    if callback.data == "trans_skip":
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
        # Автоматически создаём раунд, если нет активных
        round_payload = {"campaign_id": camp_id}
        try:
            rr = requests.post(API_START_ROUND, json=round_payload, headers=ADMIN_HEADERS, timeout=10)
            rr.raise_for_status()
            round_data = rr.json()
            target_round_id = round_data["round_id"]
            await callback.message.answer("Создан новый раунд, поскольку активных не найдено.")
        except Exception as e:
            await callback.message.edit_text(f"Ошибка создания раунда: {e}")
            await state.clear()
            await callback.answer()
            return
        data = await state.get_data()
        winners = data.get("winners", [])
        result = await transfer_winners_to_round(winners, target_round_id)
        await callback.message.edit_text(f"Победители перенесены. {result}")
        await state.clear()
        await callback.answer()
        return

    kb = InlineKeyboardMarkup(inline_keyboard=[])
    for rd in rounds:
        kb.inline_keyboard.append([
            InlineKeyboardButton(text=f"Раунд #{rd['number']}", callback_data=f"trans_exist_round_{rd['id']}")
        ])
    kb.inline_keyboard.append([InlineKeyboardButton(text="Отмена", callback_data="trans_skip")])

    await callback.message.edit_text("Выберите существующий раунд:", reply_markup=kb)
    await state.update_data(existing_campaign_id=camp_id)
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
    await callback.message.edit_text(f"Победители перенесены в существующий раунд. {result}")
    await state.clear()
    await callback.answer()


@dp.message(TransferWinnersStates.enter_new_campaign_name)
async def process_transfer_new_campaign(message: Message, state: FSMContext):
    name = message.text.strip()
    if not name:
        await message.answer("Название пустое.")
        return

    payload = {"name": name, "admin_telegram_id": message.from_user.id}
    try:
        r = requests.post(API_CREATE_CAMPAIGN, json=payload, headers=ADMIN_HEADERS, timeout=10)
        r.raise_for_status()
        camp = r.json()
        round_payload = {"campaign_id": camp["campaign_id"]}
        rr = requests.post(API_START_ROUND, json=round_payload, headers=ADMIN_HEADERS, timeout=10)
        rr.raise_for_status()
        round = rr.json()
        data = await state.get_data()
        winners = data.get("winners", [])
        result = await transfer_winners_to_round(winners, round["round_id"])
        await message.answer(f"Кампания и раунд созданы. {result}")
    except Exception as e:
        await message.answer(f"Ошибка: {e}")

    await state.clear()


# ──────────────────────────────────────────────
# ЗАПУСК
# ──────────────────────────────────────────────

async def main():
    logger.info("Бот запущен")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())