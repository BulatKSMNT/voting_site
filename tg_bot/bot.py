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
from middlewares import AntiFloodMiddleware

BOT_TOKEN = config("TELEGRAM_TOKEN")

# --- РОЛИ АДМИНОВ ---
FULL_ADMINS = [1251634923]  # Имеют доступ ко всему (ТВОЙ ID)
LIMITED_ADMINS = []  # Помощники (только выставляют и завершают раунд)


def is_full_admin(uid: int) -> bool: return uid in FULL_ADMINS


def is_any_admin(uid: int) -> bool: return uid in FULL_ADMINS or uid in LIMITED_ADMINS

import logging
from logging.handlers import RotatingFileHandler


# Настраиваем логирование:
log_formatter = logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s')


file_handler = RotatingFileHandler('logs/bot.log', maxBytes=5*1024*1024, backupCount=3, encoding='utf-8')
file_handler.setFormatter(log_formatter)

console_handler = logging.StreamHandler()
console_handler.setFormatter(log_formatter)

logging.basicConfig(level=logging.INFO, handlers=[file_handler, console_handler])


bot = Bot(token=BOT_TOKEN)
dp = Dispatcher(storage=MemoryStorage())

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
    choose_target = State()
    choose_keep_votes = State()


# ==========================================
# ГЕНЕРАТОР МЕНЮ ГОЛОСОВАНИЯ (УМНЫЙ)
# ==========================================
async def get_vote_menu(user_id: int):
    """Функция запрашивает данные у Django и собирает красивое меню с сердечками"""
    try:
        data = await api.request("GET", f"rounds/active_info/?user_id={user_id}")
    except Exception:
        return lexicon.MSG_NO_ROUND, None

    status = data.get("status")
    round_type = data.get("round_type", "standard")
    participants = data.get("participants", [])
    user_votes = data.get("user_votes", [])

    # Если раунд завершен (Опубликован)
    if status == "published":
        text = f"🏁 <b>Голосование завершено!</b>\nРезультаты ({data.get('round_name')}):\n\n"
        for p in participants:
            text += f"🏆 {p['full_name']} — {p['votes']} голосов\n"
        return text, None

    text = ""
    kb = InlineKeyboardMarkup(inline_keyboard=[])

    # Если Индивидуальный раунд
    if round_type == "individual":
        if not participants:
            text += "Участников пока нет\n"
        else:
            p = participants[0]
            text += f"Готовы ли вы пригласить на свое мероприятие такого ведущего, как\n<b>{p['full_name']}</b>?"

            # Проверяем, как проголосовал этот конкретный юзер
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

    # Если Стандартный раунд
    else:
        voted_ids = [v["participant_id"] for v in user_votes]
        text += "Выберите участников (можно нескольких):\n"
        for p in participants:
            btn_text = f"#{p['order_number']} {p['full_name']}"
            if p["id"] in voted_ids:
                btn_text += " ❤️"  # Ставим сердечко, если ID участника есть в голосах юзера
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
        "▪️ /myid — Узнать свой ID (нужно для выдачи админки)\n"
        "▪️ /help — Это меню\n"
    )
    if is_any_admin(user_id):
        text += (
            "\n🛠 <b>Для модераторов:</b>\n"
            "▪️ /set_current_round — Выбрать раунд для показа на главном экране\n"
            "▪️ /end_current_round — Завершить текущий раунд и перенести победителей\n"
            "▪️ /hide_round — Скрыть раунд с сайта\n"
        )
    if is_full_admin(user_id):
        text += (
            "\n👑 <b>Для главных админов:</b>\n"
            "▪️ /create_campaign — Создать новую кампанию\n"
            "▪️ /start_round — Запустить раунд (настроить тип и места)\n"
            "▪️ /add_participant — Вписать участников (можно списком!)\n"
            "▪️ /export — 📊 Скачать Excel-отчет с результатами\n"
        )
    await message.answer(text, parse_mode="HTML")


@dp.message(Command("start"))
async def cmd_start(message: Message, state: FSMContext):
    await state.clear()
    await message.answer(lexicon.MSG_START, reply_markup=vote_keyboard)


@dp.message(F.text == lexicon.BTN_VOTE)
@dp.message(Command("vote"))
async def cmd_vote(message: Message):
    # Получаем готовое меню из нашего умного генератора
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

        # Плавное обновление меню (МЕНЯЕМ КНОПКИ БЕЗ ПЕРЕСЫЛКИ СООБЩЕНИЯ)
        text, kb = await get_vote_menu(callback.from_user.id)
        if kb:
            await callback.message.edit_text(text, reply_markup=kb, parse_mode="HTML")
        else:
            await callback.message.edit_text(text, parse_mode="HTML")

    except Exception as e:
        err = str(e).lower()
        if "уже проголосовали" in err or "unique" in err:
            await callback.answer(lexicon.MSG_VOTED_ALREADY, show_alert=True)
        else:
            await callback.answer(lexicon.MSG_ERROR, show_alert=True)


# ==========================================
# МОДЕРАТОРЫ (ВЫВОД НА ЭКРАН И ЗАВЕРШЕНИЕ)
# ==========================================
@dp.message(Command("set_current_round"))
async def cmd_set_current(msg: Message):
    if not is_any_admin(msg.from_user.id): return
    try:
        rounds = await api.request("GET", "rounds", is_admin=True)
        active = [r for r in rounds if r["status"] == "active"]
        if not active:
            await msg.answer("Нет активных раундов.")
            return

        kb = InlineKeyboardMarkup(inline_keyboard=[])
        for r in active:
            mark = " ✅" if r.get("is_current") else ""
            name = f"Индив: {r.get('participant_name', 'Пусто')}" if r['type'] == 'individual' else "Стандарт"
            btn_text = f"Р#{r['number']} ({name}){mark}"
            kb.inline_keyboard.append([InlineKeyboardButton(text=btn_text, callback_data=f"setc_{r['id']}")])

        await msg.answer("Выберите раунд для вывода на экраны:", reply_markup=kb)
    except Exception as e:
        await msg.answer(f"Ошибка: {e}")


@dp.callback_query(F.data.startswith("setc_"))
async def process_set_current(call: CallbackQuery):
    try:
        await api.request("PATCH", f"rounds/{call.data.split('_')[1]}", {"is_current": True}, is_admin=True)
        await call.message.edit_text("✅ Раунд выставлен на голосование! Зрители могут голосовать.")
    except Exception as e:
        await call.message.edit_text(f"Ошибка: {e}")


@dp.message(Command("end_current_round"))
async def cmd_end_current(msg: Message, state: FSMContext):
    if not is_any_admin(msg.from_user.id): return
    try:
        data = await api.request("GET", f"rounds/active_info/?user_id={msg.from_user.id}")
        round_id = data.get("round_id")
        round_type = data.get("round_type")

        if round_type == "individual":
            res = await api.request("POST", f"rounds/{round_id}/end_and_transfer", {"action_type": "auto_individual"},
                                    is_admin=True)
            await msg.answer(res.get('message', 'Завершено.'), parse_mode="HTML")
        else:
            res = await api.request("POST", f"rounds/{round_id}/end_and_transfer", {"action_type": "end_standard"},
                                    is_admin=True)
            winners = res.get("winners", [])

            text = f"🏁 {res.get('message')}\n\n<b>Победители:</b>\n"
            for w in winners:
                text += f"🏆 {w['name']} — {w['votes']} голосов\n"

            await state.update_data(source_round=round_id, winners_ids=[w['id'] for w in winners])

            rounds = await api.request("GET", "rounds", is_admin=True)
            active_standards = [r for r in rounds if r["status"] == "active" and r["type"] == "standard"]

            kb = InlineKeyboardMarkup(inline_keyboard=[])
            for r in active_standards:
                kb.inline_keyboard.append(
                    [InlineKeyboardButton(text=f"В раунд #{r['number']} (Камп. {r['campaign_order_number']})",
                                          callback_data=f"trans_{r['id']}")])
            kb.inline_keyboard.append(
                [InlineKeyboardButton(text="Создать новый стандартный раунд", callback_data="trans_new")])
            kb.inline_keyboard.append(
                [InlineKeyboardButton(text="Не переносить (Завершить)", callback_data="trans_none")])

            await msg.answer(text + "\nКуда перенести этих участников?", reply_markup=kb, parse_mode="HTML")
            await state.set_state(TransferStandardStates.choose_target)

    except Exception as e:
        await msg.answer(f"Ошибка при завершении: {e}")


@dp.callback_query(F.data.startswith("trans_"), TransferStandardStates.choose_target)
async def process_transfer_target(call: CallbackQuery, state: FSMContext):
    target = call.data.split("_")[1]
    if target == "none":
        await call.message.edit_text("✅ Раунд завершен. Перенос не выполнен.")
        await state.clear()
        return

    if target == "new":
        try:
            rd = await api.request("POST", "rounds",
                                   {"campaign": 1, "type": "standard", "winners_count": 3, "status": "active"},
                                   is_admin=True)
            target = rd["id"]
        except Exception as e:
            await call.message.edit_text(f"Ошибка создания раунда: {e}")
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
    keep_votes = (call.data == "keep_yes")
    data = await state.get_data()

    payload = {
        "action_type": "transfer_standard",
        "target_round_id": data["target_round"],
        "winners_ids": data["winners_ids"],
        "keep_votes": keep_votes
    }

    try:
        res = await api.request("POST", f"rounds/{data['source_round']}/end_and_transfer", payload, is_admin=True)
        await call.message.edit_text(res.get("message", "Перенос завершен!"))
    except Exception as e:
        await call.message.edit_text(f"Ошибка: {e}")
    await state.clear()


# ==========================================
# ГЛАВНЫЙ АДМИН (ЗАПУСК И ДОБАВЛЕНИЕ)
# ==========================================
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
    await state.update_data(campaign_id=call.data.split("_")[2])
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Стандартный", callback_data="sr_type_standard")],
        [InlineKeyboardButton(text="Индивидуальный (1 чел)", callback_data="sr_type_individual")]
    ])
    await call.message.edit_text("Выберите тип раунда:", reply_markup=kb)
    await state.set_state(StartRoundStates.choose_type)


@dp.callback_query(F.data.startswith("sr_type_"), StartRoundStates.choose_type)
async def process_sr_type(call: CallbackQuery, state: FSMContext):
    round_type = call.data.split("_")[2]
    await state.update_data(type=round_type)

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
               "winners_count": int(msg.text) if msg.text.isdigit() else 3, "status": "active"}
    if data.get("number"): payload["number"] = data["number"]

    try:
        rd = await api.request("POST", "rounds", data=payload, is_admin=True)
        await msg.answer(f"✅ Стандартный раунд #{rd['number']} запущен!\nНе забудьте /add_participant")
    except Exception as e:
        await msg.answer(f"Ошибка запуска: {e}")
    await state.clear()


@dp.message(Command("add_participant"))
async def cmd_add_participant(message: Message, state: FSMContext):
    if not is_full_admin(message.from_user.id): return
    try:
        rounds = await api.request("GET", "rounds", is_admin=True)
        active = [r for r in rounds if r["status"] == "active"]
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text=f"Раунд #{r['number']} ({r['type']})", callback_data=f"addp_{r['id']}")] for r in
            active])
        await message.answer("В какой раунд добавляем?", reply_markup=kb)
        await state.set_state(AddParticipantStates.choose_round)
    except Exception:
        pass


@dp.callback_query(F.data.startswith("addp_"), AddParticipantStates.choose_round)
async def process_addp_round(callback: CallbackQuery, state: FSMContext):
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

    added = []
    errors = []

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
    if added:
        result_msg += "✅ <b>Добавлены:</b>\n" + "\n".join(f"• {n}" for n in added) + "\n\n"
    if errors:
        result_msg += "❌ <b>Ошибки:</b>\n" + "\n".join(f"• {e}" for e in errors) + "\n\n"

    result_msg += "Присылай еще или напиши «стоп»."
    await message.answer(result_msg, parse_mode="HTML")


@dp.message(Command("hide_round"))
async def cmd_hide_round(msg: Message):
    if not is_any_admin(msg.from_user.id): return
    try:
        # Запрашиваем все раунды
        rounds = await api.request("GET", "rounds", is_admin=True)
        # Ищем только те, которые сейчас висят как "Опубликованные"
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
    round_id = call.data.split('_')[1]
    try:
        # Магия Django REST: отправляем PATCH-запрос, меняя только статус и убирая с экрана
        payload = {
            "status": "ended",
            "is_current": False
        }
        await api.request("PATCH", f"rounds/{round_id}", payload, is_admin=True)
        await call.message.edit_text("✅ Раунд переведен в статус «Завершен» и убран с экранов!")
    except Exception as e:
        await call.message.edit_text(f"Ошибка: {e}")


@dp.message(Command("export"))
async def cmd_export(msg: Message):
    if not is_full_admin(msg.from_user.id): return

    wait_msg = await msg.answer("⏳ Собираю данные из базы, формирую Excel-файл...")

    try:
        data = await api.request("GET", "rounds/export_csv", is_admin=True)
        csv_content = data.get("csv_content", "")

        # Кодировка utf-8-sig (с BOM) гарантирует, что русский Excel откроет файл
        # с правильными русскими буквами без иероглифов!
        file_bytes = csv_content.encode('utf-8-sig')

        # Создаем виртуальный файл в оперативной памяти бота
        document = BufferedInputFile(file_bytes, filename="Отчет_Битва_Ведущих.csv")

        await msg.answer_document(document, caption="📊 Полный отчет по всем кампаниям и раундам.")
        await wait_msg.delete()

    except Exception as e:
        await msg.answer(f"❌ Ошибка при выгрузке отчета: {e}")
        await wait_msg.delete()


async def main():
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
