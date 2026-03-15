# bot/bot.py
import asyncio
import logging
from typing import Union
from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command
from aiogram.types import Message, InlineKeyboardMarkup, InlineKeyboardButton, CallbackQuery, ReplyKeyboardMarkup, \
    KeyboardButton
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from decouple import config

import api  # Подключаем нашу сеть
import lexicon  # Подключаем наши тексты

BOT_TOKEN = config("TELEGRAM_TOKEN")
ADMIN_IDS = [1251634923]

logging.basicConfig(level=logging.INFO)
bot = Bot(token=BOT_TOKEN)
dp = Dispatcher(storage=MemoryStorage())

# Привязываем запуск сессии из api.py
dp.startup.register(api.on_startup)
dp.shutdown.register(api.on_shutdown)

vote_keyboard = ReplyKeyboardMarkup(
    keyboard=[[KeyboardButton(text=lexicon.BTN_VOTE)]],
    resize_keyboard=True
)


def is_admin(user_id: int) -> bool:
    return user_id in ADMIN_IDS


# --- СОСТОЯНИЯ ---
class StartRoundStates(StatesGroup):
    choose_campaign = State()
    enter_new_campaign = State()
    choose_type = State()
    enter_number = State()
    enter_winners_count = State()


class EndRoundStates(StatesGroup):
    choose_round = State()
    choose_action = State()
    choose_existing_round = State()
    enter_new_campaign_name = State()
    enter_winners_count = State()


class AddParticipantStates(StatesGroup):
    choose_round = State()
    waiting_for_name = State()


# ==========================================
# ОБЩИЕ КОМАНДЫ ПОЛЬЗОВАТЕЛЕЙ
# ==========================================
@dp.message(Command("start"))
async def cmd_start(message: Message, state: FSMContext):
    await state.clear()
    await message.answer(lexicon.MSG_START, reply_markup=vote_keyboard)


@dp.message(F.text == lexicon.BTN_VOTE)
@dp.message(Command("vote"))
async def cmd_vote(message: Message):
    try:
        # Используем наш api.py!
        data = await api.request("GET", f"rounds/active_info/?user_id={message.from_user.id}")
    except Exception:
        await message.answer(lexicon.MSG_NO_ROUND, reply_markup=vote_keyboard)
        return

    status = data.get("status")
    round_type = data.get("round_type", "standard")
    participants = data.get("participants", [])
    user_votes = data.get("user_votes", [])

    if status == "published":
        text = f"🏁 <b>Голосование завершено!</b>\nРезультаты ({data.get('round_name')}):\n\n"
        for p in participants:
            text += f"🏆 {p['full_name']} — {p['votes']} голосов\n"
        await message.answer(text, parse_mode="HTML", reply_markup=vote_keyboard)
        return

    text = ""
    kb = InlineKeyboardMarkup(inline_keyboard=[])

    if round_type == "individual":
        text += "Готовы ли вы пригласить на свое мероприятие такого ведущего, как\n"
        if not participants: text += "Участников пока нет\n"

        for p in participants:
            text += f"<b>{p['full_name']}</b>?"
            user_vote = next((v for v in user_votes if v["participant_id"] == p["id"]), None)

            row = []
            btn_yes_text, btn_no_text = "Да", "Нет"

            if user_vote:
                text += f"\n\nВы уже проголосовали {'за' if user_vote['choice'] == 'yes' else 'против'} данного ведущего\n\n"
                if user_vote["choice"] == "yes": btn_yes_text += " ❤️"
                if user_vote["choice"] == "no": btn_no_text += " 💔"
            else:
                text += "\n\n"

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

    await message.answer(text, reply_markup=kb, parse_mode="HTML")


@dp.callback_query(F.data.startswith("vote_"))
async def process_vote_callback(callback: CallbackQuery):
    parts = callback.data.split("_")
    payload = {"round": parts[1], "participant": parts[2], "user_telegram_id": callback.from_user.id}
    if len(parts) > 3: payload["choice"] = parts[3]

    try:
        await api.request("POST", "votes", data=payload)
        await callback.answer("Голос учтён! Спасибо! ❤️", show_alert=True)

        fake_msg = callback.message
        fake_msg.from_user = callback.from_user
        await bot.delete_message(callback.message.chat.id, callback.message.message_id)
        await cmd_vote(fake_msg)

    except Exception as e:
        error_text = str(e).lower()
        if "уже проголосовали" in error_text or "unique" in error_text:
            await callback.answer(lexicon.MSG_VOTED_ALREADY, show_alert=True)
        else:
            await callback.answer(lexicon.MSG_ERROR, show_alert=True)


# ==========================================
# АДМИН: ДОБАВЛЕНИЕ УЧАСТНИКОВ
# ==========================================
@dp.message(Command("add_participant"))
async def cmd_add_participant(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id): return
    try:
        rounds = await api.request("GET", "rounds")
        active = [r for r in rounds if r["status"] == "active"]
        if not active:
            await message.answer("Нет активных раундов.")
            return

        kb = InlineKeyboardMarkup(inline_keyboard=[])
        for r in active:
            kb.inline_keyboard.append([
                InlineKeyboardButton(text=f"Раунд #{r['number']} (Камп. #{r['campaign_order_number']})",
                                     callback_data=f"addp_{r['id']}")
            ])
        await message.answer("Выберите раунд:", reply_markup=kb)
        await state.set_state(AddParticipantStates.choose_round)
    except Exception as e:
        await message.answer(f"Ошибка API: {e}")


@dp.callback_query(F.data.startswith("addp_"), AddParticipantStates.choose_round)
async def process_addp_round(callback: CallbackQuery, state: FSMContext):
    await state.update_data(round_id=callback.data.split("_")[1])
    await callback.message.edit_text("Отправляйте участников по одному (ФИО).\nГотово / Отмена — завершить.")
    await state.set_state(AddParticipantStates.waiting_for_name)


@dp.message(AddParticipantStates.waiting_for_name)
async def process_add_participant_name(message: Message, state: FSMContext):
    txt = message.text.strip().lower()
    if txt in ("готово", "всё", "стоп", "отмена"):
        await message.answer("Добавление завершено.", reply_markup=vote_keyboard)
        await state.clear()
        return

    data = await state.get_data()
    try:
        await api.request("POST", "participants", data={"round": data["round_id"], "full_name": message.text.title()},
                          is_admin=True)
        await message.answer(f"Добавлен: {message.text.title()} 👍\nПиши следующего или «стоп».")
    except Exception as e:
        await message.answer(f"Ошибка: {e}")


# ==========================================
# АДМИН: ЗАПУСК РАУНДА
# ==========================================
@dp.message(Command("start_round"))
async def cmd_start_round(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id): return
    try:
        camps = await api.request("GET", "campaigns/active_list")
        camps = camps.get("campaigns", [])

        kb = InlineKeyboardMarkup(inline_keyboard=[])
        for c in camps:
            kb.inline_keyboard.append(
                [InlineKeyboardButton(text=f"#{c['order_number']} {c['name']}", callback_data=f"sr_c_{c['id']}")])
        kb.inline_keyboard.append([InlineKeyboardButton(text="Новая кампания", callback_data="sr_new_camp")])

        await message.answer("Выберите кампанию:", reply_markup=kb)
        await state.set_state(StartRoundStates.choose_campaign)
    except Exception as e:
        await message.answer(f"Ошибка: {e}")


@dp.callback_query(F.data.startswith("sr_c_") | (F.data == "sr_new_camp"))
async def process_sr_camp(call: CallbackQuery, state: FSMContext):
    if call.data == "sr_new_camp":
        await call.message.edit_text("Введите название новой кампании:")
        await state.set_state(StartRoundStates.enter_new_campaign)
        return

    await state.update_data(campaign_id=call.data.split("_")[2])
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Стандартный", callback_data="sr_type_standard")],
        [InlineKeyboardButton(text="Индивидуальный", callback_data="sr_type_individual")]
    ])
    await call.message.edit_text("Выберите тип раунда:", reply_markup=kb)
    await state.set_state(StartRoundStates.choose_type)


@dp.message(StartRoundStates.enter_new_campaign)
async def process_sr_new_camp_name(msg: Message, state: FSMContext):
    try:
        camp = await api.request("POST", "campaigns", {"name": msg.text, "admin_telegram_id": msg.from_user.id},
                                 is_admin=True)
        await state.update_data(campaign_id=camp["id"])

        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="Стандартный", callback_data="sr_type_standard")],
            [InlineKeyboardButton(text="Индивидуальный", callback_data="sr_type_individual")]
        ])
        await msg.answer("Кампания создана. Выберите тип раунда:", reply_markup=kb)
        await state.set_state(StartRoundStates.choose_type)
    except Exception as e:
        await msg.answer(f"Ошибка: {e}")


@dp.callback_query(F.data.startswith("sr_type_"))
async def process_sr_type(call: CallbackQuery, state: FSMContext):
    await state.update_data(type=call.data.split("_")[2])
    kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="Авто-номер", callback_data="sr_auto")]])
    await call.message.edit_text("Запустить раунд с авто-номером или введите число вручную:", reply_markup=kb)
    await state.set_state(StartRoundStates.enter_number)


@dp.callback_query(F.data == "sr_auto")
@dp.message(StartRoundStates.enter_number)
async def process_sr_number(event: Union[CallbackQuery, Message], state: FSMContext):
    num = None
    if isinstance(event, Message):
        try:
            num = int(event.text)
        except:
            pass

    await state.update_data(number=num)
    msg = event.message if isinstance(event, CallbackQuery) else event
    await msg.answer("Сколько победителей выбрать? (по умолчанию 3)")
    await state.set_state(StartRoundStates.enter_winners_count)


@dp.message(StartRoundStates.enter_winners_count)
async def process_sr_winners(msg: Message, state: FSMContext):
    try:
        w_count = int(msg.text)
    except:
        w_count = 3

    data = await state.get_data()
    payload = {"campaign": data["campaign_id"], "type": data["type"], "winners_count": w_count, "status": "active"}
    if data.get("number"): payload["number"] = data["number"]

    try:
        rd = await api.request("POST", "rounds", data=payload, is_admin=True)
        await msg.answer(
            f"✅ Раунд №{rd['number']} запущен!\n• /add_participant — добавить участников\n• /vote — посмотреть голосование")
    except Exception as e:
        await msg.answer(f"Ошибка запуска: {e}")
    await state.clear()


# ==========================================
# АДМИН: ЗАВЕРШЕНИЕ РАУНДА
# ==========================================
@dp.message(Command("end_round"))
async def cmd_end_round(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id): return
    try:
        rounds = await api.request("GET", "rounds")
        active = [r for r in rounds if r["status"] == "active"]
        if not active:
            await message.answer("Нет активных раундов.")
            return

        kb = InlineKeyboardMarkup(inline_keyboard=[])
        for r in active:
            kb.inline_keyboard.append([
                InlineKeyboardButton(text=f"Раунд #{r['number']} (Камп. {r['campaign_order_number']})",
                                     callback_data=f"er_{r['id']}")
            ])
        await message.answer("Выберите раунд для завершения:", reply_markup=kb)
        await state.set_state(EndRoundStates.choose_round)
    except Exception as e:
        await message.answer(f"Ошибка: {e}")


@dp.callback_query(F.data.startswith("er_"))
async def process_er_round(call: CallbackQuery, state: FSMContext):
    round_id = call.data.split("_")[1]
    try:
        rd = await api.request("GET", f"rounds/{round_id}")
        await state.update_data(ending_round=round_id, campaign_id=rd["campaign"])
    except Exception as e:
        await call.answer(f"Ошибка API: {e}", show_alert=True)
        return

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Опубликовать результаты", callback_data="ea_pub")],
        [InlineKeyboardButton(text="В существующий раунд", callback_data="ea_exist")],
        [InlineKeyboardButton(text="В новый раунд (эта кампания)", callback_data="ea_new_same")],
        [InlineKeyboardButton(text="В новую кампанию", callback_data="ea_new_camp")],
        [InlineKeyboardButton(text="Отмена", callback_data="ea_cancel")]
    ])
    await call.message.edit_text("Что делать с победителями?", reply_markup=kb)
    await state.set_state(EndRoundStates.choose_action)


@dp.callback_query(EndRoundStates.choose_action)
async def process_er_action(call: CallbackQuery, state: FSMContext):
    action = call.data
    data = await state.get_data()

    if action == "ea_cancel":
        await call.message.edit_text("Отменено.")
        await state.clear()
    elif action == "ea_pub":
        try:
            res = await api.request("POST", f"rounds/{data['ending_round']}/end_and_transfer", {"publish": True},
                                    is_admin=True)
            await call.message.edit_text(res.get("message", "Успешно опубликовано!"))
        except Exception as e:
            await call.message.edit_text(f"Ошибка: {e}")
        await state.clear()
    elif action == "ea_exist":
        rounds = await api.request("GET", "rounds")
        active = [r for r in rounds if r["status"] == "active" and r["id"] != int(data["ending_round"])]
        if not active:
            await call.answer("Нет других активных раундов!", show_alert=True)
            return
        kb = InlineKeyboardMarkup(
            inline_keyboard=[[InlineKeyboardButton(text=f"Раунд #{r['number']}", callback_data=f"eret_{r['id']}")] for r
                             in active])
        await call.message.edit_text("Куда переносим?", reply_markup=kb)
        await state.set_state(EndRoundStates.choose_existing_round)
    elif action == "ea_new_same":
        await call.message.edit_text("Сколько победителей будет в НОВОМ раунде?")
        await state.set_state(EndRoundStates.enter_winners_count)
    elif action == "ea_new_camp":
        await call.message.edit_text("Введите название НОВОЙ кампании:")
        await state.set_state(EndRoundStates.enter_new_campaign_name)


@dp.callback_query(F.data.startswith("eret_"), EndRoundStates.choose_existing_round)
async def process_er_existing_target(call: CallbackQuery, state: FSMContext):
    try:
        res = await api.request("POST", f"rounds/{await state.get_data()['ending_round']}/end_and_transfer",
                                {"target_round_id": call.data.split("_")[1]}, is_admin=True)
        await call.message.edit_text(res.get("message", "Перенос выполнен! 🎉"))
    except Exception as e:
        await call.message.edit_text(f"Ошибка: {e}")
    await state.clear()


@dp.message(EndRoundStates.enter_new_campaign_name)
async def process_er_new_camp(msg: Message, state: FSMContext):
    try:
        camp = await api.request("POST", "campaigns", {"name": msg.text, "admin_telegram_id": msg.from_user.id},
                                 is_admin=True)
        await state.update_data(campaign_id=camp["id"])
        await msg.answer("Кампания создана. Сколько победителей будет в новом раунде?")
        await state.set_state(EndRoundStates.enter_winners_count)
    except Exception as e:
        await msg.answer(f"Ошибка: {e}")


@dp.message(EndRoundStates.enter_winners_count)
async def process_er_create_and_transfer(msg: Message, state: FSMContext):
    try:
        w_count = int(msg.text)
    except:
        w_count = 3

    data = await state.get_data()
    try:
        rd = await api.request("POST", "rounds",
                               {"campaign": data["campaign_id"], "type": "standard", "winners_count": w_count,
                                "status": "active"}, is_admin=True)
        res = await api.request("POST", f"rounds/{data['ending_round']}/end_and_transfer",
                                {"target_round_id": rd["id"]}, is_admin=True)
        await msg.answer(f"✅ Новый раунд #{rd['number']} запущен!\n{res.get('message', 'Перенос завершен!')}")
    except Exception as e:
        await msg.answer(f"Ошибка: {e}")
    await state.clear()


# ==========================================
# ЗАПУСК
# ==========================================
async def main():
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
