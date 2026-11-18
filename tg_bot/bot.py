import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import asyncio
import base64
import logging
import logging.config
from html import escape
from core.observability import logging_config, event
from typing import Union
from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command
from aiogram.types import Message, InlineKeyboardMarkup, InlineKeyboardButton, CallbackQuery, ReplyKeyboardMarkup, \
    KeyboardButton
from aiogram.types.input_file import BufferedInputFile
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from decouple import config

import api
import lexicon
from middlewares import AntiFloodMiddleware, RoleLoggingMiddleware, AdminFilter
from campaign_access import register_campaign_access, CampaignAccessStates
from voting.access import VOTER_NOT_ALLOWED
from event_menu import register_event_menu, EventStates, show_campaign, show_round

BOT_TOKEN = config("TELEGRAM_TOKEN")

# --- РОЛИ АДМИНОВ ---
def parse_admin_ids(value: str) -> set[int]:
    return {
        int(item.strip())
        for item in value.split(",")
        if item.strip()
    }

FULL_ADMINS = parse_admin_ids(config("FULL_ADMIN_IDS", default=""))
LIMITED_ADMINS = parse_admin_ids(config("LIMITED_ADMIN_IDS", default=""))

def is_full_admin(user_id: int) -> bool:
    return user_id in FULL_ADMINS

def is_any_admin(user_id: int) -> bool:
    return user_id in FULL_ADMINS or user_id in LIMITED_ADMINS


full_access = AdminFilter(FULL_ADMINS)
admin_access = AdminFilter(FULL_ADMINS | LIMITED_ADMINS)
logging.config.dictConfig(logging_config('bot'))

def log_handler_error(exc):
    if not isinstance(exc, api.APIError) or exc.status >= 500 or exc.status == 0:
        logging.getLogger(__name__).exception("bot.operation_failed")


bot = Bot(token=BOT_TOKEN)
dp = Dispatcher(storage=MemoryStorage())

dp.message.outer_middleware(RoleLoggingMiddleware(FULL_ADMINS, LIMITED_ADMINS))
dp.callback_query.outer_middleware(RoleLoggingMiddleware(FULL_ADMINS, LIMITED_ADMINS))

# 2. А потом защищаем от спама
dp.message.middleware(AntiFloodMiddleware(limit_seconds=1.0))
dp.callback_query.middleware(AntiFloodMiddleware(limit_seconds=1.0))

dp.startup.register(api.on_startup)
dp.shutdown.register(api.on_shutdown)
register_campaign_access(dp, full_access)
register_event_menu(dp, full_access, admin_access, is_full_admin)

vote_keyboard = ReplyKeyboardMarkup(keyboard=[[KeyboardButton(text=lexicon.BTN_VOTE)]], resize_keyboard=True)


class StartRoundStates(StatesGroup):
    choose_campaign = State()
    enter_new_campaign = State()
    choose_type = State()
    enter_number = State()
    enter_winners_count = State()


class CreateCampaignStates(StatesGroup):
    enter_name = State()


class AddParticipantStates(StatesGroup):
    choose_round = State()
    waiting_for_name = State()


class TransferStandardStates(StatesGroup):
    choose_source_round = State()
    choose_target = State()
    choose_keep_votes = State()


class SmartSetCurrentStates(StatesGroup):
    choose_action = State()


class DeleteParticipantStates(StatesGroup):
    choose_round = State()


class ResurrectRoundStates(StatesGroup):
    choose_round = State()


class SetWinnersCountStates(StatesGroup):
    choose_round = State()
    enter_count = State()


# ==========================================
# ГЕНЕРАТОР МЕНЮ ГОЛОСОВАНИЯ (УМНЫЙ)
# ==========================================
async def get_vote_menu(user_id: int):
    try:
        data = await api.request("GET", f"rounds/active_info/?user_id={user_id}")
    except api.APIError as exc:
        if exc.status == 404 and exc.code == "no_active_round":
            return lexicon.MSG_NO_ROUND, None
        return "Сервис временно недоступен. Попробуйте позже.", None

    status = data.get("status")
    round_type = data.get("round_type", "standard")
    participants = data.get("participants", [])
    user_votes = data.get("user_votes", [])

    if status == "published":
        text = f"🏁 <b>Голосование завершено!</b>\nРезультаты ({escape(data.get('round_name', ''))}):\n\n"
        if data.get('event_stage') == 'qualification':
            text = f"<b>{escape(data.get('round_name', 'Итоги тура'))}</b>\nОжидайте начала следующего раунда.\n\n"
        for p in participants:
            text += f"🏆 {escape(p['full_name'])} — {p['votes']} голосов\n"
        return text, None

    if data.get("can_vote") is False:
        return VOTER_NOT_ALLOWED, None

    text = ""
    kb = InlineKeyboardMarkup(inline_keyboard=[])

    if round_type == "individual":
        if not participants:
            text += "Участников пока нет\n"
        else:
            p = participants[0]

            # Проверяем, как проголосовал этот конкретный юзер
            user_vote = next((v for v in user_votes if v["participant_id"] == p["id"]), None)

            if user_vote:
                # ЕСЛИ ЮЗЕР УЖЕ ПРОГОЛОСОВАЛ:
                # Меняем текст сообщения на утвердительный и НЕ добавляем кнопки
                choice_text = "ЗА" if user_vote['choice'] == 'yes' else "ПРОТИВ"
                emoji = "❤️" if user_vote['choice'] == 'yes' else "💔"
                text = f"Ваш голос <b>{choice_text}</b> {emoji}  успешно учтён!"
                # kb останется пустой (без кнопок)
            else:
                # ЕСЛИ ЕЩЕ НЕ ГОЛОСОВАЛ:
                # Показываем стандартный вопрос и добавляем кнопки Да/Нет
                text += f"Готовы ли вы пригласить на свое мероприятие такого ведущего, как\n<b>{escape(p['full_name'])}</b>?"
                row = [
                    InlineKeyboardButton(text="Да", callback_data=f"vote_{data['round_id']}_{p['id']}_yes"),
                    InlineKeyboardButton(text="Нет", callback_data=f"vote_{data['round_id']}_{p['id']}_no")
                ]
                kb.inline_keyboard.append(row)

    else:
        voted_ids = [v["participant_id"] for v in user_votes]
        limit = data.get('max_choices', 3)
        remaining = max(0, limit - len(voted_ids))
        text += f"Можно выбрать не более {limit} участников. Осталось голосов: {remaining}.\n"
        if not remaining:
            text += 'Все ваши голоса учтены.\n'
        for p in participants:
            if not remaining or p['id'] in voted_ids:
                if p['id'] in voted_ids:
                    text += f"✓ {escape(p['full_name'])}\n"
                continue
            btn_text = f"#{p['order_number']} {p['full_name']}"
            if p["id"] in voted_ids:
                btn_text += " ❤️"
            kb.inline_keyboard.append(
                [InlineKeyboardButton(text=btn_text, callback_data=f"vote_{data['round_id']}_{p['id']}")])

    return text, kb if kb.inline_keyboard else None


# ==========================================
# ОБЩИЕ КОМАНДЫ ПОЛЬЗОВАТЕЛЕЙ
# ==========================================
@dp.message(Command("myid"))
async def cmd_myid(message: Message):
    await message.answer(f"Ваш Telegram ID: <code>{message.from_user.id}</code>", parse_mode="HTML")


@dp.message(Command("help"))
async def cmd_help(message: Message):
    user_id = message.from_user.id
    text = "📖 <b>Справка по командам бота:</b>\n\n"
    text += (
        "👤 <b>Для зрителей:</b>\n"
        "▪️ /start — Перезапуск бота\n"
        "▪️ /vote (или кнопка) — Открыть меню голосования\n"
        "▪️ /myid — Узнать свой ID\n"
        "▪️ /help — Это меню\n"
    )
    if is_any_admin(user_id):
        text += (
            "\n🛠 <b>Для модераторов:</b>\n"
            "▪️ /event — Туры → раунды по ФИО, топ-N, финал\n"
            "▪️ /set_current_round — Выбрать раунд для показа на главном экране\n"
            "▪️ /end_current_round — Завершить текущий/выбранный раунд и перенести победителей\n"
            "▪️ /hide_round — Скрыть раунд с сайта\n"
        )
    if is_full_admin(user_id):
        text += (
            "\n👑 <b>Для главных админов:</b>\n"
            "▪️ /create_campaign — Создать новую кампанию\n"
            "▪️ /campaign_access — Режим доступа и список Telegram ID кампании\n"
            "▪️ /jury_winner — Дополнительный победитель по выбору администратора\n"
            "▪️ /start_round — Запустить раунд\n"
            "▪️ /add_participant — Вписать участников (можно списком!)\n"
            "▪️ /set_winners_count — Настроить топ-N по уровням баллов\n"
            "▪️ /del_participant — ❌ Удалить участника\n"
            "▪️ /resurrect_round — 🧟‍♂️ Восстановить раунд\n"
            "▪️ /export — 📊 Отчёт по мероприятию: результаты и голоса\n"
        )
    await message.answer(text, parse_mode="HTML")


@dp.message(Command("start"))
async def cmd_start(message: Message, state: FSMContext):
    await state.clear()
    await message.answer(lexicon.MSG_START, reply_markup=vote_keyboard)


@dp.message(F.text == lexicon.BTN_VOTE)
@dp.message(Command("vote"))
async def cmd_vote(message: Message):
    text, kb = await get_vote_menu(message.from_user.id)
    if kb:
        await message.answer(text, reply_markup=kb, parse_mode="HTML")
    else:
        await message.answer(text, reply_markup=vote_keyboard, parse_mode="HTML")


@dp.callback_query(F.data.startswith("vote_"))
async def process_vote_callback(callback: CallbackQuery):
    parts = callback.data.split("_")
    payload = {"round": parts[1], "participant": parts[2], "user_telegram_id": callback.from_user.id}
    if len(parts) > 3: payload["choice"] = parts[3]

    try:
        result = await api.request("POST", "votes", data=payload)
    except api.APIError as exc:
        text = str(exc).lower()
        if "уже проголосовали" in text or exc.code == "duplicate":
            await callback.answer(lexicon.MSG_VOTED_ALREADY, show_alert=True)
        elif "не активен" in text or "закрыто" in text:
            await callback.answer("⏳ Голосование за этого участника уже завершено!", show_alert=True)
        elif exc.code == "voter_not_allowed":
            await callback.answer(VOTER_NOT_ALLOWED, show_alert=True)
        elif exc.code == 'vote_limit_reached':
            await callback.answer(str(exc), show_alert=True)
        else:
            await callback.answer(lexicon.MSG_ERROR, show_alert=True)
        return
    event("vote.confirmed_by_api", round_id=payload["round"], participant_id=payload["participant"], vote_id=result.get("vote_id"))
    try:
        await callback.answer("Голос учтён! Спасибо! ❤️", show_alert=True)
        text, kb = await get_vote_menu(callback.from_user.id)
        await callback.message.edit_text(text, reply_markup=kb, parse_mode="HTML")
    except Exception:
        logging.getLogger(__name__).exception("telegram.delivery_failed_after_vote_commit")


# ==========================================
# МОДЕРАТОРЫ (ВЫВОД НА ЭКРАН И ЗАВЕРШЕНИЕ)
# ==========================================
@dp.message(Command("set_current_round"), admin_access)
async def cmd_set_current(msg: Message):
    if not is_any_admin(msg.from_user.id): return
    try:
        rounds = await api.request("GET", "rounds", is_admin=True)
        active = [r for r in rounds if r["status"] in ["active", "published", "pending"]]
        if not active:
            await msg.answer("Нет раундов, открытых для показа.")
            return

        kb = InlineKeyboardMarkup(inline_keyboard=[])
        for r in active:
            mark = " ✅" if r.get("is_current") else ""
            status_text = "Для голос." if r["status"] == "active" else " (Таблица)"
            name = f"Индив: {r.get('participant_name', 'Пусто')}" if r['type'] == 'individual' else "Стандарт"
            btn_text = f"Р#{r['number']} ({name}){status_text}{mark}"
            kb.inline_keyboard.append(
                [InlineKeyboardButton(text=btn_text, callback_data=f"ev_round_{r['id']}" if r.get("event_stage") else f"setc_{r['id']}_{r['status']}")])

        await msg.answer("Выберите раунд для вывода на экраны:", reply_markup=kb)
    except Exception as e:
        log_handler_error(e)
        await msg.answer(f"Ошибка: {e}")


@dp.callback_query(F.data.startswith("setc_"), admin_access)
async def process_set_current(call: CallbackQuery, state: FSMContext):
    await call.answer()
    parts = call.data.split('_')
    round_id = parts[1]
    status = parts[2]

    if status == "active":
        try:
            await api.request("PATCH", f"rounds/{round_id}", {"is_current": True}, is_admin=True)
            await call.message.edit_text("✅ Раунд выставлен на главный экран! Идет голосование.")
        except Exception as e:
            log_handler_error(e)
            await call.message.edit_text(f"Ошибка: {e}")
    else:
        await state.update_data(target_round=round_id)
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="📊 Вывести таблицу (голосование закрыто)", callback_data="smart_show")],
            [InlineKeyboardButton(text="🔥 ОТКРЫТЬ ГОЛОСОВАНИЕ (Сделать Активным)", callback_data="smart_open")],
        ])
        await call.message.edit_text("Этот раунд закрыт для голосов. Что сделать?", reply_markup=kb)
        await state.set_state(SmartSetCurrentStates.choose_action)


@dp.callback_query(F.data.startswith("smart_"), SmartSetCurrentStates.choose_action, admin_access)
async def process_smart_set(call: CallbackQuery, state: FSMContext):
    await call.answer()
    action = call.data.split('_')[1]
    data = await state.get_data()
    round_id = data["target_round"]

    try:
        if action == "show":
            await api.request("PATCH", f"rounds/{round_id}", {"is_current": True}, is_admin=True)
            await call.message.edit_text("✅ Таблица выведена на экраны. Голосование пока закрыто.")
        elif action == "open":
            await api.request("PATCH", f"rounds/{round_id}", {"is_current": True, "status": "active"}, is_admin=True)
            await call.message.edit_text("✅ Голосование ОТКРЫТО! Зрители могут голосовать.")
    except Exception as e:
        log_handler_error(e)
        await call.message.edit_text(f"Ошибка: {e}")
    await state.clear()


# ==========================================
# УМНОЕ ЗАВЕРШЕНИЕ РАУНДА
# ==========================================
@dp.message(Command("end_current_round"), admin_access)
async def cmd_end_current(msg: Message, state: FSMContext):
    if not is_any_admin(msg.from_user.id): return

    round_id = None
    round_type = None

    # ШАГ 1: Тихо проверяем, есть ли что-то на главном экране
    try:
        data = await api.request("GET", f"rounds/active_info/?user_id={msg.from_user.id}")
        round_id = data.get("round_id")
        round_type = data.get("round_type")
    except api.APIError as exc:
        if exc.status != 404 or exc.code != "no_active_round":
            await msg.answer("Сервис временно недоступен. Попробуйте позже.")
            return

    try:
        # ШАГ 2: Если на экране ПУСТО — предлагаем выбрать из ОПУБЛИКОВАННЫХ стандартных
        if not round_id:
            rounds = await api.request("GET", "rounds", is_admin=True)

            # Ищем только стандартные раунды со статусом published
            available = [r for r in rounds if r["status"] == "published" and r["type"] == "standard"]

            if not available:
                await msg.answer("На экранах пусто, а в архиве нет ни одного опубликованного раунда для переноса.")
                return

            kb = InlineKeyboardMarkup(inline_keyboard=[])
            for r in available:
                kb.inline_keyboard.append([
                    InlineKeyboardButton(
                        text=f"Р#{r['number']} (Камп. {r['campaign_order_number']}) - Опубликован",
                        callback_data=f"endsrc_{r['id']}"
                    )
                ])
            await msg.answer(
                "На экранах сейчас пусто. Какой из опубликованных раундов вы хотите завершить и перенести победителей?",
                reply_markup=kb)
            await state.set_state(TransferStandardStates.choose_source_round)
            return

        # ШАГ 3: Если раунд ЕСТЬ на экране — сразу завершаем его
        await process_end_round_logic(msg, state, round_id, round_type)

    except Exception as e:
        logging.exception("cmd_end_current fallback failed")
        await msg.answer(f"Ошибка при поиске раунда: {e}")


@dp.callback_query(F.data.startswith("endsrc_"), TransferStandardStates.choose_source_round, admin_access)
async def process_choose_source_round(call: CallbackQuery, state: FSMContext):
    await call.answer()
    round_id = int(call.data.split('_')[1])
    # Передаем "standard", так как вручную разрешаем выбирать только их
    await process_end_round_logic(call, state, round_id, "standard")


async def process_end_round_logic(event: Union[Message, CallbackQuery], state: FSMContext, round_id: int,
                                  round_type: str):
    """Общая логика завершения раунда (вызывается и автоматически, и после ручного выбора)"""
    try:
        rounds = await api.request("GET", "rounds", is_admin=True)
        source_round_obj = next((r for r in rounds if r["id"] == round_id), None)

        if not source_round_obj:
            text = "Не удалось найти выбранный раунд в базе."
            if isinstance(event, CallbackQuery):
                await event.message.edit_text(text)
            else:
                await event.answer(text)
            return

        campaign_id = source_round_obj["campaign"]

        if source_round_obj.get('event_stage'):
            res = await api.request('POST', f'rounds/{round_id}/event_finish', {})
            msg = event.message if isinstance(event, CallbackQuery) else event
            await msg.answer(res['message'] + '\nОткройте /event для следующего шага.')
            await state.clear()
            return

        # =========================
        # ИНДИВИДУАЛЬНЫЙ РАУНД (Авто-перенос)
        # =========================
        if round_type == "individual":
            active_standards = [
                r for r in rounds
                if r["status"] in ["active", "published"]
                   and r["type"] == "standard"
                   and r["campaign"] == campaign_id
            ]

            if len(active_standards) <= 1:
                payload = {"action_type": "auto_individual"}
                if len(active_standards) == 1:
                    payload["target_round_id"] = active_standards[0]["id"]

                res = await api.request("POST", f"rounds/{round_id}/end_and_transfer", payload, is_admin=True)

                text = f"{res.get('message', 'Завершено.')}\n\n"
                winners = res.get("winners", [])
                if winners:
                    text += "<b>Результат:</b>\n"
                    for w in winners:
                        text += f"🏆 {escape(w['name'])} — {w['votes']} голосов\n"

                if isinstance(event, CallbackQuery):
                    await event.message.edit_text(text, parse_mode="HTML")
                else:
                    await event.answer(text, parse_mode="HTML")
                await state.clear()
                return

            await state.clear()
            await state.update_data(source_round=round_id, source_type="individual", campaign_id=campaign_id)

            kb = InlineKeyboardMarkup(inline_keyboard=[])
            for r in active_standards:
                kb.inline_keyboard.append([
                    InlineKeyboardButton(text=f"В раунд #{r['number']} (Камп. {r['campaign_order_number']})",
                                         callback_data=f"trans_{r['id']}")
                ])
            kb.inline_keyboard.append(
                [InlineKeyboardButton(text="Создать новый стандартный раунд", callback_data="trans_new")])

            await state.set_state(TransferStandardStates.choose_target)

            if isinstance(event, CallbackQuery):
                await event.message.edit_text("Куда перенести участника из индивидуального раунда?", reply_markup=kb)
            else:
                await event.answer("Куда перенести участника из индивидуального раунда?", reply_markup=kb)
            return

        # =========================
        # СТАНДАРТНЫЙ РАУНД (Ручной перенос)
        # =========================
        res = await api.request("POST", f"rounds/{round_id}/end_and_transfer", {"action_type": "end_standard"},
                                is_admin=True)
        winners = res.get("winners", [])

        text = f"🏁 {res.get('message')}\n\n<b>Победители:</b>\n"
        for w in winners:
            text += f"🏆 {escape(w['name'])} — {w['votes']} голосов\n"

        await state.clear()
        await state.update_data(source_round=round_id, winners_ids=[w['id'] for w in winners], source_type="standard",
                                campaign_id=campaign_id)

        rounds_after = await api.request("GET", "rounds", is_admin=True)
        active_standards = [
            r for r in rounds_after
            if r["status"] in ["active", "published"]
               and r["type"] == "standard"
               and r["campaign"] == campaign_id
               and r["id"] != round_id
        ]

        kb = InlineKeyboardMarkup(inline_keyboard=[])
        for r in active_standards:
            kb.inline_keyboard.append([
                InlineKeyboardButton(text=f"В раунд #{r['number']} (Камп. {r['campaign_order_number']})",
                                     callback_data=f"trans_{r['id']}")
            ])
        kb.inline_keyboard.append(
            [InlineKeyboardButton(text="Создать новый стандартный раунд", callback_data="trans_new")])
        kb.inline_keyboard.append([InlineKeyboardButton(text="Не переносить (Завершить)", callback_data="trans_none")])

        await state.set_state(TransferStandardStates.choose_target)

        if isinstance(event, CallbackQuery):
            await event.message.edit_text(text + "\nКуда перенести этих участников?", reply_markup=kb,
                                          parse_mode="HTML")
        else:
            await event.answer(text + "\nКуда перенести этих участников?", reply_markup=kb, parse_mode="HTML")

    except Exception as e:
        logging.exception("process_end_round_logic failed")
        err_msg = f"Ошибка при завершении: {e}"
        if isinstance(event, CallbackQuery):
            await event.message.edit_text(err_msg)
        else:
            await event.answer(err_msg)


@dp.callback_query(F.data.regexp(r'^ev_legacyend_[0-9]+$'), admin_access)
async def event_legacy_end(call: CallbackQuery, state: FSMContext):
    await call.answer()
    rid = int(call.data.split('_')[2])
    rd = await api.request('GET', f'rounds/{rid}')
    await process_end_round_logic(call, state, rid, rd['type'])


@dp.callback_query(F.data.startswith("trans_"), TransferStandardStates.choose_target, admin_access)
async def process_transfer_target(call: CallbackQuery, state: FSMContext):
    await call.answer()
    target = call.data.split("_", 1)[1]
    data = await state.get_data()

    if target == "none":
        await call.message.edit_text("✅ Раунд завершен. Перенос не выполнен.")
        await state.clear()
        return

    if target == "new":
        try:
            rd = await api.request("POST", "rounds",
                                   {"campaign": data["campaign_id"], "type": "standard", "winners_count": 3,
                                    "status": "published"}, is_admin=True)
            target = rd["id"]
        except Exception as e:
            log_handler_error(e)
            await call.message.edit_text(f"Ошибка создания раунда: {e}")
            return

    if data.get("source_type") == "individual":
        try:
            res = await api.request("POST", f"rounds/{data['source_round']}/end_and_transfer",
                                    {"action_type": "auto_individual", "target_round_id": target}, is_admin=True)
            text = f"{res.get('message', 'Перенос завершен!')}\n\n"
            winners = res.get("winners", [])
            if winners:
                text += "<b>Результат:</b>\n"
                for w in winners: text += f"🏆 {escape(w['name'])} — {w['votes']} голосов\n"
            await call.message.edit_text(text, parse_mode="HTML")
        except Exception as e:
            log_handler_error(e)
            await call.message.edit_text(f"Ошибка: {e}")
        await state.clear()
        return

    await state.update_data(target_round=target)
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Сохранить голоса", callback_data="keep_yes")],
        [InlineKeyboardButton(text="Начать с 0", callback_data="keep_no")]
    ])
    await call.message.edit_text("Сохранить накопленные голоса у переносимых участников?", reply_markup=kb)
    await state.set_state(TransferStandardStates.choose_keep_votes)


@dp.callback_query(F.data.startswith("keep_"), TransferStandardStates.choose_keep_votes, admin_access)
async def process_transfer_keep(call: CallbackQuery, state: FSMContext):
    await call.answer()
    data = await state.get_data()
    payload = {
        "action_type": "transfer_standard",
        "target_round_id": data["target_round"],
        "winners_ids": data["winners_ids"],
        "keep_votes": (call.data == "keep_yes")
    }
    try:
        res = await api.request("POST", f"rounds/{data['source_round']}/end_and_transfer", payload, is_admin=True)
        await call.message.edit_text(res.get("message", "Перенос завершен!"))
    except Exception as e:
        log_handler_error(e)
        await call.message.edit_text(f"Ошибка: {e}")
    await state.clear()


@dp.message(Command("hide_round"), admin_access)
async def cmd_hide_round(msg: Message):
    if not is_any_admin(msg.from_user.id): return
    try:
        rounds = await api.request("GET", "rounds", is_admin=True)
        published = [r for r in rounds if r["status"] == "published"]
        if not published:
            await msg.answer("Нет опубликованных раундов, которые можно скрыть.")
            return

        kb = InlineKeyboardMarkup(inline_keyboard=[])
        for r in published:
            mark = " (На экране)" if r.get("is_current") else ""
            btn_text = f"Р#{r['number']} (Камп. {r['campaign_order_number']}){mark}"
            kb.inline_keyboard.append([InlineKeyboardButton(text=btn_text, callback_data=f"hide_{r['id']}")])
        await msg.answer("Какой раунд убрать с экранов в архив?", reply_markup=kb)
    except Exception as e:
        log_handler_error(e)
        await msg.answer(f"Ошибка: {e}")


@dp.callback_query(F.data.startswith("hide_"), admin_access)
async def process_hide_round(call: CallbackQuery):
    await call.answer()
    try:
        await api.request("PATCH", f"rounds/{call.data.split('_')[1]}", {"status": "ended", "is_current": False},
                          is_admin=True)
        await call.message.edit_text("✅ Раунд переведен в статус «Завершен» и убран с экранов!")
    except Exception as e:
        log_handler_error(e)
        await call.message.edit_text(f"Ошибка: {e}")


# ==========================================
# ИНСТРУМЕНТЫ ГЛАВНОГО АДМИНА
# ==========================================
@dp.message(Command("set_winners_count"), full_access)
async def cmd_set_winners_count(msg: Message, state: FSMContext):
    if not is_full_admin(msg.from_user.id): return
    try:
        rounds = await api.request("GET", "rounds", is_admin=True)
        available = [r for r in rounds if r["status"] in ["pending", "active", "published"] and r["type"] == "standard" and not r.get("ended_at") and r.get("event_stage") != "final"]
        if not available:
            await msg.answer("Нет доступных стандартных раундов для изменения мест.")
            return

        kb = InlineKeyboardMarkup(inline_keyboard=[])
        for r in available:
            kb.inline_keyboard.append(
                [InlineKeyboardButton(text=f"Раунд #{r['number']} (топ-{r['winners_count']} по баллам)",
                                      callback_data=f"setw_{r['id']}")])
        await msg.answer("В каком раунде изменить число лучших уровней баллов?", reply_markup=kb)
        await state.set_state(SetWinnersCountStates.choose_round)
    except Exception as e:
        log_handler_error(e)
        await msg.answer(f"Ошибка: {e}")


@dp.callback_query(F.data.startswith("setw_"), SetWinnersCountStates.choose_round, full_access)
async def process_set_winners_round(call: CallbackQuery, state: FSMContext):
    await call.answer()
    await state.update_data(round_id=call.data.split("_")[1])
    await call.message.edit_text("Введите число лучших уровней баллов N (равные результаты проходят вместе):")
    await state.set_state(SetWinnersCountStates.enter_count)


@dp.message(SetWinnersCountStates.enter_count, full_access)
async def process_set_winners_count(msg: Message, state: FSMContext):
    if not msg.text.isdigit():
        await msg.answer("Пожалуйста, введите цифру.")
        return
    try:
        res = await api.request("PATCH", f"rounds/{(await state.get_data())['round_id']}/update_winners_count",
                                {"winners_count": int(msg.text)}, is_admin=True)
        await msg.answer(res.get("message", "Количество мест обновлено!"))
    except Exception as e:
        log_handler_error(e)
        await msg.answer(f"Ошибка: {e}")
    await state.clear()


@dp.message(Command("del_participant"), full_access)
async def cmd_del_participant(msg: Message, state: FSMContext):
    if not is_full_admin(msg.from_user.id): return
    try:
        rounds = await api.request("GET", "rounds", is_admin=True)
        available = [r for r in rounds if r["status"] in ["active", "published"]]
        kb = InlineKeyboardMarkup(inline_keyboard=[])
        for r in available:
            kb.inline_keyboard.append(
                [InlineKeyboardButton(text=f"Раунд #{r['number']} ({r['type']})", callback_data=f"delp_{r['id']}")])
        await msg.answer("Из какого раунда удалить участника?", reply_markup=kb)
        await state.set_state(DeleteParticipantStates.choose_round)
    except Exception as e:
        log_handler_error(e)
        await msg.answer(f"Ошибка: {e}")


@dp.callback_query(F.data.startswith("delp_"), DeleteParticipantStates.choose_round, full_access)
async def process_delp_round(call: CallbackQuery, state: FSMContext):
    await call.answer()
    round_id = call.data.split("_")[1]
    try:
        parts = await api.request("GET", "participants", is_admin=True)
        parts_filtered = [p for p in parts if str(p.get("round")) == round_id]

        if not parts_filtered:
            await call.message.edit_text("В этом раунде нет участников.")
            await state.clear()
            return

        kb = InlineKeyboardMarkup(inline_keyboard=[])
        for p in parts_filtered:
            kb.inline_keyboard.append(
                [InlineKeyboardButton(text=f"❌ {p['full_name']}", callback_data=f"rmp_{p['id']}")])
        await call.message.edit_text("Кого удалить навсегда?", reply_markup=kb)
    except Exception as e:
        log_handler_error(e)
        await call.message.edit_text(f"Ошибка: {e}")
        await state.clear()


@dp.callback_query(F.data.startswith("rmp_"), DeleteParticipantStates.choose_round, full_access)
async def process_rmp_execute(call: CallbackQuery, state: FSMContext):
    await call.answer()
    try:
        await api.request("DELETE", f"participants/{call.data.split('_')[1]}", is_admin=True)
        await call.message.edit_text("✅ Участник успешно удален!")
    except Exception as e:
        log_handler_error(e)
        await call.message.edit_text(f"Ошибка удаления: {e}")
    await state.clear()


@dp.message(Command("resurrect_round"), full_access)
async def cmd_resurrect_round(msg: Message):
    if not is_full_admin(msg.from_user.id): return
    try:
        rounds = await api.request("GET", "rounds", is_admin=True)
        ended = [r for r in rounds if r["status"] == "ended"]
        if not ended:
            await msg.answer("Нет завершенных раундов для восстановления.")
            return

        kb = InlineKeyboardMarkup(inline_keyboard=[])
        for r in ended[:10]:
            name = f"Индив: {r.get('participant_name', 'Пусто')}" if r['type'] == 'individual' else "Стандарт"
            kb.inline_keyboard.append(
                [InlineKeyboardButton(text=f"Р#{r['number']} ({name})", callback_data=f"resu_{r['id']}")])
        await msg.answer("Какой раунд вернуть к жизни (сделать Активным)?", reply_markup=kb)
    except Exception as e:
        log_handler_error(e)
        await msg.answer(f"Ошибка: {e}")


@dp.callback_query(F.data.startswith("resu_"), full_access)
async def process_resurrect(call: CallbackQuery):
    await call.answer()
    try:
        await api.request("PATCH", f"rounds/{call.data.split('_')[1]}", {"status": "active", "is_current": False},
                          is_admin=True)
        await call.message.edit_text(
            "🧟‍♂️ Раунд успешно воскрешен! Теперь он Активен. Вы можете вывести его на экраны через /set_current_round.")
    except Exception as e:
        log_handler_error(e)
        await call.message.edit_text(f"Ошибка: {e}")


@dp.message(Command("export"), full_access)
async def cmd_export(msg: Message):
    if not is_full_admin(msg.from_user.id): return
    if msg.chat.type != 'private':
        await msg.answer('Отчёт с Telegram ID доступен в личном чате с ботом. Отправьте там /export.')
        return
    try:
        data = await api.request('GET', 'campaigns/report_choices', is_admin=True)
        rows = [[InlineKeyboardButton(text=c['name'] + (' (архив)' if not c['is_active'] else ''),
                                      callback_data=f"report_campaign_{c['id']}")] for c in data['campaigns']]
        if not rows:
            await msg.answer('Мероприятий пока нет.')
        for start in range(0, len(rows), 50):
            await msg.answer('Выберите мероприятие для отчёта:', reply_markup=InlineKeyboardMarkup(inline_keyboard=rows[start:start+50]))
    except Exception as e:
        log_handler_error(e)
        await msg.answer('Не удалось открыть список отчётов. Попробуйте /export ещё раз.')


@dp.callback_query(F.data.regexp(r'^report_campaign_[0-9]+$'), full_access)
async def export_campaign(call: CallbackQuery):
    if call.message.chat.type != 'private':
        await call.answer('Откройте личный чат с ботом и отправьте /export.', show_alert=True)
        return
    await call.answer('Собираю отчёт…')
    try:
        data = await api.request('GET', f"campaigns/{int(call.data.split('_')[-1])}/report", is_admin=True)
        document = BufferedInputFile(base64.b64decode(data['content_base64']), filename=data['filename'])
        await call.message.answer_document(document, caption=(
            '📊 Отчёт по выбранному мероприятию.\n'
            'Результаты.csv — итоги по турам.\n'
            'Голоса.csv — Telegram ID, участник, выбор и время.\n'
            'Данные.json — данные для пересчёта и снимки завершений.'))
    except Exception as e:
        log_handler_error(e)
        await call.message.answer('Не удалось выгрузить отчёт. Попробуйте /export ещё раз.')


@dp.message(Command("create_campaign"), full_access)
async def cmd_create_camp(msg: Message, state: FSMContext):
    if not is_full_admin(msg.from_user.id): return
    await msg.answer("Введите название новой кампании:")
    await state.set_state(CreateCampaignStates.enter_name)


@dp.message(CreateCampaignStates.enter_name, full_access)
async def process_create_camp(msg: Message, state: FSMContext):
    try:
        camp = await api.request("POST", "campaigns", {"name": msg.text, "admin_telegram_id": msg.from_user.id},
                                 is_admin=True)
        await msg.answer(f"✅ Кампания «{camp['name']}» успешно создана!")
    except Exception as e:
        log_handler_error(e)
        await msg.answer(f"Ошибка: {e}")
    await state.clear()


@dp.message(Command("start_round"), full_access)
async def cmd_start_round(message: Message, state: FSMContext):
    if not is_full_admin(message.from_user.id): return
    try:
        camps = await api.request("GET", "campaigns/active_list")
        kb = InlineKeyboardMarkup(inline_keyboard=[])
        for c in camps.get("campaigns", []):
            kb.inline_keyboard.append(
                [InlineKeyboardButton(text=f"#{c['order_number']} {c['name']}", callback_data=f"sr_c_{c['id']}")])
        await message.answer("Выберите кампанию для запуска раунда:", reply_markup=kb)
        await state.set_state(StartRoundStates.choose_campaign)
    except Exception as e:
        log_handler_error(e)
        await message.answer(f"Ошибка: {e}")


@dp.callback_query(F.data.startswith("sr_c_"), StartRoundStates.choose_campaign, full_access)
async def process_sr_camp(call: CallbackQuery, state: FSMContext):
    await call.answer()
    cid = int(call.data.split("_")[2])
    data = await api.request('GET', f'campaigns/{cid}/event')
    if any(r['stage'] for r in data['rounds']):
        await state.clear()
        await show_campaign(call.message, cid, True)
        return
    await state.update_data(campaign_id=cid)
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Стандартный (Таблица)", callback_data="sr_type_standard")],
        [InlineKeyboardButton(text="Индивидуальный (1 чел)", callback_data="sr_type_individual")]
    ])
    await call.message.edit_text("Выберите тип раунда:", reply_markup=kb)
    await state.set_state(StartRoundStates.choose_type)


@dp.callback_query(F.data.startswith("sr_type_"), StartRoundStates.choose_type, full_access)
async def process_sr_type(call: CallbackQuery, state: FSMContext):
    await call.answer()
    await state.update_data(type=call.data.split("_")[2])
    kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="Авто-номер", callback_data="sr_auto")]])
    await call.message.edit_text("Номер раунда (выберите Авто или введите цифру):", reply_markup=kb)
    await state.set_state(StartRoundStates.enter_number)


@dp.callback_query(F.data == "sr_auto", StartRoundStates.enter_number, full_access)
@dp.message(StartRoundStates.enter_number, full_access)
async def process_sr_number(event: Union[CallbackQuery, Message], state: FSMContext):
    data = await state.get_data()
    num = int(event.text) if isinstance(event, Message) and event.text.isdigit() else None
    await state.update_data(number=num)
    msg = event.message if isinstance(event, CallbackQuery) else event

    if data["type"] == "individual":
        payload = {"campaign": data["campaign_id"], "type": "individual", "winners_count": 1, "status": "active"}
        if num: payload["number"] = num
        try:
            rd = await api.request("POST", "rounds", data=payload, is_admin=True)
            await msg.answer(
                f"✅ Индивидуальный раунд #{rd['number']} запущен!\nИспользуйте /add_participant чтобы добавить 1 человека.")
        except Exception as e:
            log_handler_error(e)
            await msg.answer(f"Ошибка запуска: {e}")
        await state.clear()
    else:
        await msg.answer("Сколько лучших уровней баллов отбирать? Введите N; одинаковые результаты проходят вместе.")
        await state.set_state(StartRoundStates.enter_winners_count)


@dp.message(StartRoundStates.enter_winners_count, full_access)
async def process_sr_winners(msg: Message, state: FSMContext):
    data = await state.get_data()
    payload = {"campaign": data["campaign_id"], "type": "standard",
               "winners_count": int(msg.text) if msg.text.isdigit() else 3, "status": "published"}
    if data.get("number"): payload["number"] = data["number"]
    try:
        rd = await api.request("POST", "rounds", data=payload, is_admin=True)
        await msg.answer(
            f"✅ Стандартный раунд #{rd['number']} создан!\nОн имеет статус <b>Опубликован</b>.\nНе забудьте /add_participant",
            parse_mode="HTML")
    except Exception as e:
        log_handler_error(e)
        await msg.answer(f"Ошибка запуска: {e}")
    await state.clear()


@dp.message(Command("add_participant"), full_access)
async def cmd_add_participant(message: Message, state: FSMContext):
    if not is_full_admin(message.from_user.id): return
    try:
        rounds = await api.request("GET", "rounds", is_admin=True)
        active = [r for r in rounds if (not r.get("event_stage") and r["status"] in ["active", "published"]) or (r.get("event_stage") == "qualification" and not r.get("ended_at")) or (r.get("event_stage") == "semifinal" and r["status"] == "pending")]
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text=r.get("display_name") or f"Раунд #{r['number']} ({r['type']})", callback_data=f"addp_{r['id']}")] for r in
            active])
        await message.answer("В какой раунд добавляем?", reply_markup=kb)
        await state.set_state(AddParticipantStates.choose_round)
    except Exception as e:
        log_handler_error(e)
        await message.answer(f"Ошибка: {e}")


@dp.callback_query(F.data.startswith("addp_"), AddParticipantStates.choose_round, full_access)
async def process_addp_round(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    rid = int(callback.data.split("_")[1])
    rd = await api.request('GET', f'rounds/{rid}')
    await state.update_data(round_id=rid, managed=bool(rd.get('event_stage')))
    await callback.message.edit_text(
        "Отправляй ФИО по одному или <b>списком (каждый с новой строки)</b>.\nДля завершения напиши 'стоп'.",
        parse_mode="HTML")
    await state.set_state(AddParticipantStates.waiting_for_name)


@dp.message(AddParticipantStates.waiting_for_name, full_access)
async def process_add_participant_name(message: Message, state: FSMContext):
    if message.text.lower() in ("готово", "стоп"):
        await message.answer("Добавление завершено.")
        await state.clear()
        return

    data = await state.get_data()
    names = message.text.strip().split("\n")
    if data.get('managed'):
        try:
            result = await api.request('POST', f"rounds/{data['round_id']}/add_entries", {'names': [n.strip() for n in names if n.strip()]})
            await message.answer(result['message'] + ' Присылайте ещё или напишите «стоп».')
        except api.APIError as exc:
            await message.answer(str(exc))
        return
    added, errors = [], []

    for name in names:
        clean_name = name.strip().title()
        if not clean_name: continue
        try:
            await api.request("POST", "participants", data={"round": data["round_id"], "full_name": clean_name},
                              is_admin=True)
            added.append(clean_name)
        except Exception as e:
            log_handler_error(e)
            errors.append(f"{clean_name}: {e}")

    result_msg = ""
    if added: result_msg += "✅ <b>Добавлены:</b>\n" + "\n".join(f"• {escape(n)}" for n in added) + "\n\n"
    if errors: result_msg += "❌ <b>Ошибки:</b>\n" + "\n".join(f"• {escape(e)}" for e in errors) + "\n\n"
    result_msg += "Присылай еще или напиши «стоп»."
    await message.answer(result_msg, parse_mode="HTML")


async def main():
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
