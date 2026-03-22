import asyncio
import logging
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
from middlewares import AntiFloodMiddleware, RoleLoggingMiddleware
from logging.handlers import RotatingFileHandler

BOT_TOKEN = config("TELEGRAM_TOKEN")

# --- РОЛИ АДМИНОВ ---
FULL_ADMINS = [1251634923]
LIMITED_ADMINS = [558525552]


def is_full_admin(uid: int) -> bool: return uid in FULL_ADMINS


def is_any_admin(uid: int) -> bool: return uid in FULL_ADMINS or uid in LIMITED_ADMINS


log_formatter = logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s')
file_handler = RotatingFileHandler('logs/bot.log', maxBytes=5 * 1024 * 1024, backupCount=3, encoding='utf-8')
file_handler.setFormatter(log_formatter)
console_handler = logging.StreamHandler()
console_handler.setFormatter(log_formatter)

logging.basicConfig(level=logging.INFO, handlers=[file_handler, console_handler])

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher(storage=MemoryStorage())

dp.message.middleware(RoleLoggingMiddleware(FULL_ADMINS, LIMITED_ADMINS))
dp.callback_query.middleware(RoleLoggingMiddleware(FULL_ADMINS, LIMITED_ADMINS))

# 2. А потом защищаем от спама
dp.message.middleware(AntiFloodMiddleware(limit_seconds=1.0))
dp.callback_query.middleware(AntiFloodMiddleware(limit_seconds=1.0))

dp.startup.register(api.on_startup)
dp.shutdown.register(api.on_shutdown)

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
    except Exception:
        return lexicon.MSG_NO_ROUND, None

    status = data.get("status")
    round_type = data.get("round_type", "standard")
    participants = data.get("participants", [])
    user_votes = data.get("user_votes", [])

    if status == "published":
        text = f"🏁 <b>Голосование завершено!</b>\nРезультаты ({data.get('round_name')}):\n\n"
        for p in participants:
            text += f"🏆 {p['full_name']} — {p['votes']} голосов\n"
        return text, None

    text = ""
    kb = InlineKeyboardMarkup(inline_keyboard=[])

    if round_type == "individual":
        if not participants:
            text += "Участников пока нет\n"
        else:
            p = participants[0]
            text += f"Готовы ли вы пригласить на свое мероприятие такого ведущего, как\n<b>{p['full_name']}</b>?"

            user_vote = next((v for v in user_votes if v["participant_id"] == p["id"]), None)
            row = []
            btn_yes_text, btn_no_text = "Да", "Нет"
            if user_vote:
                text += f"\n\nВы уже проголосовали {'за' if user_vote['choice'] == 'yes' else 'против'}."
                if user_vote["choice"] == "yes": btn_yes_text += " ❤️"
                if user_vote["choice"] == "no": btn_no_text += " 💔"

            row.append(InlineKeyboardButton(text=btn_yes_text, callback_data=f"vote_{data['round_id']}_{p['id']}_yes"))
            row.append(InlineKeyboardButton(text=btn_no_text, callback_data=f"vote_{data['round_id']}_{p['id']}_no"))
            kb.inline_keyboard.append(row)
    else:
        voted_ids = [v["participant_id"] for v in user_votes]
        text += "Выберите участников (можно нескольких):\n"
        for p in participants:
            btn_text = f"#{p['order_number']} {p['full_name']}"
            if p["id"] in voted_ids:
                btn_text += " ❤️"
            kb.inline_keyboard.append(
                [InlineKeyboardButton(text=btn_text, callback_data=f"vote_{data['round_id']}_{p['id']}")])

    return text, kb


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
            "▪️ /set_current_round — Выбрать раунд для показа на главном экране\n"
            "▪️ /end_current_round — Завершить текущий/выбранный раунд и перенести победителей\n"
            "▪️ /hide_round — Скрыть раунд с сайта\n"
        )
    if is_full_admin(user_id):
        text += (
            "\n👑 <b>Для главных админов:</b>\n"
            "▪️ /create_campaign — Создать новую кампанию\n"
            "▪️ /start_round — Запустить раунд\n"
            "▪️ /add_participant — Вписать участников (можно списком!)\n"
            "▪️ /set_winners_count — Изменить кол-во призовых мест\n"
            "▪️ /del_participant — ❌ Удалить участника\n"
            "▪️ /resurrect_round — 🧟‍♂️ Восстановить раунд\n"
            "▪️ /export — 📊 Скачать Excel-отчет\n"
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
        await api.request("POST", "votes", data=payload)
        await callback.answer("Голос учтён! Спасибо! ❤️", show_alert=True)
        text, kb = await get_vote_menu(callback.from_user.id)
        if kb:
            await callback.message.edit_text(text, reply_markup=kb, parse_mode="HTML")
        else:
            await callback.message.edit_text(text, parse_mode="HTML")
    except Exception as e:
        err = str(e).lower()
        if "уже проголосовали" in err or "unique" in err:
            await callback.answer(lexicon.MSG_VOTED_ALREADY, show_alert=True)
        elif "не активен" in err or "закрыто" in err:
            # Если юзер жмет кнопки на старых сообщениях
            await callback.answer("⏳ Голосование за этого участника уже завершено!", show_alert=True)
        elif "modified" in err:
            # Если Telegram ругается, что меню не изменилось (защита от двойных кликов)
            await callback.answer("Ваш голос уже учтён!", show_alert=False)
        else:
            # Если это реально какая-то другая серверная ошибка — запишем её в лог
            logging.error(f"VOTE ERROR: {e}")
            await callback.answer(lexicon.MSG_ERROR, show_alert=True)


# ==========================================
# МОДЕРАТОРЫ (ВЫВОД НА ЭКРАН И ЗАВЕРШЕНИЕ)
# ==========================================
@dp.message(Command("set_current_round"))
async def cmd_set_current(msg: Message):
    if not is_any_admin(msg.from_user.id): return
    try:
        rounds = await api.request("GET", "rounds", is_admin=True)
        active = [r for r in rounds if r["status"] in ["active", "published"]]
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
                [InlineKeyboardButton(text=btn_text, callback_data=f"setc_{r['id']}_{r['status']}")])

        await msg.answer("Выберите раунд для вывода на экраны:", reply_markup=kb)
    except Exception as e:
        await msg.answer(f"Ошибка: {e}")


@dp.callback_query(F.data.startswith("setc_"))
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
            await call.message.edit_text(f"Ошибка: {e}")
    else:
        await state.update_data(target_round=round_id)
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="📊 Вывести таблицу (голосование закрыто)", callback_data="smart_show")],
            [InlineKeyboardButton(text="🔥 ОТКРЫТЬ ГОЛОСОВАНИЕ (Сделать Активным)", callback_data="smart_open")],
        ])
        await call.message.edit_text("Этот раунд закрыт для голосов. Что сделать?", reply_markup=kb)
        await state.set_state(SmartSetCurrentStates.choose_action)


@dp.callback_query(F.data.startswith("smart_"), SmartSetCurrentStates.choose_action)
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
        await call.message.edit_text(f"Ошибка: {e}")
    await state.clear()


@dp.message(Command("end_current_round"))
async def cmd_end_current(msg: Message, state: FSMContext):
    if not is_any_admin(msg.from_user.id): return
    round_id, round_type = None, None

    try:
        data = await api.request("GET", f"rounds/active_info/?user_id={msg.from_user.id}")
        round_id = data.get("round_id")
        round_type = data.get("round_type")
    except Exception:
        pass

    try:
        if not round_id:
            rounds = await api.request("GET", "rounds", is_admin=True)
            available = [r for r in rounds if r["status"] == "published" and r["type"] == "standard"]

            if not available:
                await msg.answer("Нет активного на экране или опубликованного в архиве раунда для завершения.")
                return

            kb = InlineKeyboardMarkup(inline_keyboard=[])
            for r in available:
                kb.inline_keyboard.append(
                    [InlineKeyboardButton(text=f"Р#{r['number']} - Опубликован", callback_data=f"endsrc_{r['id']}")])
            await msg.answer(
                "На экранах сейчас пусто. Какой из опубликованных раундов вы хотите завершить и перенести?",
                reply_markup=kb)
            await state.set_state(TransferStandardStates.choose_source_round)
            return

        await process_end_round_logic(msg, state, round_id, round_type)

    except Exception as e:
        logging.exception("cmd_end_current failed")
        await msg.answer(f"Ошибка при поиске раунда: {e}")


@dp.callback_query(F.data.startswith("endsrc_"), TransferStandardStates.choose_source_round)
async def process_choose_source_round(call: CallbackQuery, state: FSMContext):
    await call.answer()
    round_id = int(call.data.split('_')[1])
    await process_end_round_logic(call, state, round_id, "standard")


async def process_end_round_logic(event: Union[Message, CallbackQuery], state: FSMContext, round_id: int,
                                  round_type: str):
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

        if round_type == "individual":
            active_standards = [r for r in rounds if
                                r["status"] in ["active", "published"] and r["type"] == "standard" and r[
                                    "campaign"] == campaign_id]
            if len(active_standards) <= 1:
                payload = {"action_type": "auto_individual"}
                if len(active_standards) == 1: payload["target_round_id"] = active_standards[0]["id"]
                res = await api.request("POST", f"rounds/{round_id}/end_and_transfer", payload, is_admin=True)

                text = f"{res.get('message', 'Завершено.')}\n\n"
                winners = res.get("winners", [])
                if winners:
                    text += "<b>Результат:</b>\n"
                    for w in winners: text += f"🏆 {w['name']} — {w['votes']} голосов\n"

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
                kb.inline_keyboard.append(
                    [InlineKeyboardButton(text=f"В раунд #{r['number']} (Камп. {r['campaign_order_number']})",
                                          callback_data=f"trans_{r['id']}")])
            kb.inline_keyboard.append(
                [InlineKeyboardButton(text="Создать новый стандартный раунд", callback_data="trans_new")])
            await state.set_state(TransferStandardStates.choose_target)

            if isinstance(event, CallbackQuery):
                await event.message.edit_text("Куда перенести участника?", reply_markup=kb)
            else:
                await event.answer("Куда перенести участника?", reply_markup=kb)
            return

        # ЕСЛИ СТАНДАРТНЫЙ РАУНД
        res = await api.request("POST", f"rounds/{round_id}/end_and_transfer", {"action_type": "end_standard"},
                                is_admin=True)
        winners = res.get("winners", [])
        text = f"🏁 {res.get('message')}\n\n<b>Победители:</b>\n"
        for w in winners: text += f"🏆 {w['name']} — {w['votes']} голосов\n"

        await state.clear()
        await state.update_data(source_round=round_id, winners_ids=[w['id'] for w in winners], source_type="standard",
                                campaign_id=campaign_id)
        rounds_after = await api.request("GET", "rounds", is_admin=True)
        active_standards = [r for r in rounds_after if
                            r["status"] in ["active", "published"] and r["type"] == "standard" and r["id"] != round_id]

        kb = InlineKeyboardMarkup(inline_keyboard=[])
        for r in active_standards:
            kb.inline_keyboard.append(
                [InlineKeyboardButton(text=f"В раунд #{r['number']} (Камп. {r['campaign_order_number']})",
                                      callback_data=f"trans_{r['id']}")])
        kb.inline_keyboard.append(
            [InlineKeyboardButton(text="Создать новый стандартный раунд", callback_data="trans_new")])
        kb.inline_keyboard.append([InlineKeyboardButton(text="Не переносить (Завершить)", callback_data="trans_none")])

        await state.set_state(TransferStandardStates.choose_target)
        if isinstance(event, CallbackQuery):
            await event.message.edit_text(text + "\nКуда перенести участников?", reply_markup=kb, parse_mode="HTML")
        else:
            await event.answer(text + "\nКуда перенести участников?", reply_markup=kb, parse_mode="HTML")

    except Exception as e:
        logging.exception("process_end_round_logic failed")
        err_msg = f"Ошибка при завершении: {e}"
        if isinstance(event, CallbackQuery):
            await event.message.edit_text(err_msg)
        else:
            await event.answer(err_msg)


@dp.callback_query(F.data.startswith("trans_"), TransferStandardStates.choose_target)
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
                for w in winners: text += f"🏆 {w['name']} — {w['votes']} голосов\n"
            await call.message.edit_text(text, parse_mode="HTML")
        except Exception as e:
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


@dp.callback_query(F.data.startswith("keep_"), TransferStandardStates.choose_keep_votes)
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
        await call.message.edit_text(f"Ошибка: {e}")
    await state.clear()


@dp.message(Command("hide_round"))
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
        await msg.answer(f"Ошибка: {e}")


@dp.callback_query(F.data.startswith("hide_"))
async def process_hide_round(call: CallbackQuery):
    await call.answer()
    try:
        await api.request("PATCH", f"rounds/{call.data.split('_')[1]}", {"status": "ended", "is_current": False},
                          is_admin=True)
        await call.message.edit_text("✅ Раунд переведен в статус «Завершен» и убран с экранов!")
    except Exception as e:
        await call.message.edit_text(f"Ошибка: {e}")


# ==========================================
# ИНСТРУМЕНТЫ ГЛАВНОГО АДМИНА
# ==========================================
@dp.message(Command("set_winners_count"))
async def cmd_set_winners_count(msg: Message, state: FSMContext):
    if not is_full_admin(msg.from_user.id): return
    try:
        rounds = await api.request("GET", "rounds", is_admin=True)
        available = [r for r in rounds if r["status"] in ["active", "published"] and r["type"] == "standard"]
        if not available:
            await msg.answer("Нет доступных стандартных раундов для изменения мест.")
            return

        kb = InlineKeyboardMarkup(inline_keyboard=[])
        for r in available:
            kb.inline_keyboard.append(
                [InlineKeyboardButton(text=f"Раунд #{r['number']} (Сейчас мест: {r['winners_count']})",
                                      callback_data=f"setw_{r['id']}")])
        await msg.answer("В каком раунде изменить количество призовых мест?", reply_markup=kb)
        await state.set_state(SetWinnersCountStates.choose_round)
    except Exception as e:
        await msg.answer(f"Ошибка: {e}")


@dp.callback_query(F.data.startswith("setw_"), SetWinnersCountStates.choose_round)
async def process_set_winners_round(call: CallbackQuery, state: FSMContext):
    await call.answer()
    await state.update_data(round_id=call.data.split("_")[1])
    await call.message.edit_text("Введите НОВОЕ количество призовых мест (цифрой):")
    await state.set_state(SetWinnersCountStates.enter_count)


@dp.message(SetWinnersCountStates.enter_count)
async def process_set_winners_count(msg: Message, state: FSMContext):
    if not msg.text.isdigit():
        await msg.answer("Пожалуйста, введите цифру.")
        return
    try:
        res = await api.request("PATCH", f"rounds/{(await state.get_data())['round_id']}/update_winners_count",
                                {"winners_count": int(msg.text)}, is_admin=True)
        await msg.answer(res.get("message", "Количество мест обновлено!"))
    except Exception as e:
        await msg.answer(f"Ошибка: {e}")
    await state.clear()


@dp.message(Command("del_participant"))
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
        await msg.answer(f"Ошибка: {e}")


@dp.callback_query(F.data.startswith("delp_"), DeleteParticipantStates.choose_round)
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
        await call.message.edit_text(f"Ошибка: {e}")
        await state.clear()


@dp.callback_query(F.data.startswith("rmp_"), DeleteParticipantStates.choose_round)
async def process_rmp_execute(call: CallbackQuery, state: FSMContext):
    await call.answer()
    try:
        await api.request("DELETE", f"participants/{call.data.split('_')[1]}", is_admin=True)
        await call.message.edit_text("✅ Участник успешно удален!")
    except Exception as e:
        await call.message.edit_text(f"Ошибка удаления: {e}")
    await state.clear()


@dp.message(Command("resurrect_round"))
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
        await msg.answer(f"Ошибка: {e}")


@dp.callback_query(F.data.startswith("resu_"))
async def process_resurrect(call: CallbackQuery):
    await call.answer()
    try:
        await api.request("PATCH", f"rounds/{call.data.split('_')[1]}", {"status": "active", "is_current": False},
                          is_admin=True)
        await call.message.edit_text(
            "🧟‍♂️ Раунд успешно воскрешен! Теперь он Активен. Вы можете вывести его на экраны через /set_current_round.")
    except Exception as e:
        await call.message.edit_text(f"Ошибка: {e}")


@dp.message(Command("export"))
async def cmd_export(msg: Message):
    if not is_full_admin(msg.from_user.id): return
    wait_msg = await msg.answer("⏳ Собираю данные из базы, формирую Excel-файл...")
    try:
        data = await api.request("GET", "rounds/export_csv", is_admin=True)
        document = BufferedInputFile(data.get("csv_content", "").encode('utf-8-sig'),
                                     filename="Отчет_Битва_Ведущих.csv")
        await msg.answer_document(document, caption="📊 Полный отчет по всем кампаниям и раундам.")
        await wait_msg.delete()
    except Exception as e:
        await msg.answer(f"❌ Ошибка при выгрузке отчета: {e}")
        await wait_msg.delete()


@dp.message(Command("create_campaign"))
async def cmd_create_camp(msg: Message, state: FSMContext):
    if not is_full_admin(msg.from_user.id): return
    await msg.answer("Введите название новой кампании:")
    await state.set_state(CreateCampaignStates.enter_name)


@dp.message(CreateCampaignStates.enter_name)
async def process_create_camp(msg: Message, state: FSMContext):
    try:
        camp = await api.request("POST", "campaigns", {"name": msg.text, "admin_telegram_id": msg.from_user.id},
                                 is_admin=True)
        await msg.answer(f"✅ Кампания «{camp['name']}» успешно создана!")
    except Exception as e:
        await msg.answer(f"Ошибка: {e}")
    await state.clear()


@dp.message(Command("start_round"))
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
        await message.answer(f"Ошибка: {e}")


@dp.callback_query(F.data.startswith("sr_c_"), StartRoundStates.choose_campaign)
async def process_sr_camp(call: CallbackQuery, state: FSMContext):
    await call.answer()
    await state.update_data(campaign_id=call.data.split("_")[2])
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Стандартный (Таблица)", callback_data="sr_type_standard")],
        [InlineKeyboardButton(text="Индивидуальный (1 чел)", callback_data="sr_type_individual")]
    ])
    await call.message.edit_text("Выберите тип раунда:", reply_markup=kb)
    await state.set_state(StartRoundStates.choose_type)


@dp.callback_query(F.data.startswith("sr_type_"), StartRoundStates.choose_type)
async def process_sr_type(call: CallbackQuery, state: FSMContext):
    await call.answer()
    await state.update_data(type=call.data.split("_")[2])
    kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="Авто-номер", callback_data="sr_auto")]])
    await call.message.edit_text("Номер раунда (выберите Авто или введите цифру):", reply_markup=kb)
    await state.set_state(StartRoundStates.enter_number)


@dp.callback_query(F.data == "sr_auto")
@dp.message(StartRoundStates.enter_number)
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
            await msg.answer(f"Ошибка запуска: {e}")
        await state.clear()
    else:
        await msg.answer("Сколько будет победителей? (введите число)")
        await state.set_state(StartRoundStates.enter_winners_count)


@dp.message(StartRoundStates.enter_winners_count)
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
        await msg.answer(f"Ошибка запуска: {e}")
    await state.clear()


@dp.message(Command("add_participant"))
async def cmd_add_participant(message: Message, state: FSMContext):
    if not is_full_admin(message.from_user.id): return
    try:
        rounds = await api.request("GET", "rounds", is_admin=True)
        active = [r for r in rounds if r["status"] in ["active", "published"]]
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text=f"Раунд #{r['number']} ({r['type']})", callback_data=f"addp_{r['id']}")] for r in
            active])
        await message.answer("В какой раунд добавляем?", reply_markup=kb)
        await state.set_state(AddParticipantStates.choose_round)
    except Exception as e:
        await message.answer(f"Ошибка: {e}")


@dp.callback_query(F.data.startswith("addp_"), AddParticipantStates.choose_round)
async def process_addp_round(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    await state.update_data(round_id=callback.data.split("_")[1])
    await callback.message.edit_text(
        "Отправляй ФИО по одному или <b>списком (каждый с новой строки)</b>.\nДля завершения напиши 'стоп'.",
        parse_mode="HTML")
    await state.set_state(AddParticipantStates.waiting_for_name)


@dp.message(AddParticipantStates.waiting_for_name)
async def process_add_participant_name(message: Message, state: FSMContext):
    if message.text.lower() in ("готово", "стоп"):
        await message.answer("Добавление завершено.")
        await state.clear()
        return

    data = await state.get_data()
    names = message.text.strip().split("\n")
    added, errors = [], []

    for name in names:
        clean_name = name.strip().title()
        if not clean_name: continue
        try:
            await api.request("POST", "participants", data={"round": data["round_id"], "full_name": clean_name},
                              is_admin=True)
            added.append(clean_name)
        except Exception as e:
            errors.append(f"{clean_name}: {e}")

    result_msg = ""
    if added: result_msg += "✅ <b>Добавлены:</b>\n" + "\n".join(f"• {n}" for n in added) + "\n\n"
    if errors: result_msg += "❌ <b>Ошибки:</b>\n" + "\n".join(f"• {e}" for e in errors) + "\n\n"
    result_msg += "Присылай еще или напиши «стоп»."
    await message.answer(result_msg, parse_mode="HTML")


async def main():
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
