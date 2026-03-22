import time
import logging
from typing import Any, Awaitable, Callable, Dict
from aiogram import BaseMiddleware
from aiogram.types import TelegramObject, Message, CallbackQuery

# Создаем отдельный логгер, чтобы он красиво выделялся
action_logger = logging.getLogger("USER_ACTION")


class RoleLoggingMiddleware(BaseMiddleware):
    """Мидлварь для красивого логирования ролей пользователей"""

    def __init__(self, full_admins: list, limited_admins: list):
        self.full_admins = full_admins
        self.limited_admins = limited_admins

    async def __call__(
            self,
            handler: Callable[[TelegramObject, Dict[str, Any]], Awaitable[Any]],
            event: TelegramObject,
            data: Dict[str, Any]
    ) -> Any:

        user = None
        action_text = ""

        # Проверяем: это текст или нажатие кнопки?
        if isinstance(event, Message):
            user = event.from_user
            action_text = f"Написал: {event.text}"
        elif isinstance(event, CallbackQuery):
            user = event.from_user
            action_text = f"Нажал кнопку: {event.data}"

        # Если это пользователь, определяем его роль и пишем в лог
        if user:
            if user.id in self.full_admins:
                role = "👑 ГЛ. АДМИН"
            elif user.id in self.limited_admins:
                role = "🛠 МОДЕРАТОР"
            else:
                role = "👤 ЗРИТЕЛЬ "

            username = f"@{user.username}" if user.username else "Скрыт"
            # Формируем красивую строчку в лог
            action_logger.info(f"[{role}] ID:{user.id} ({username}) -> {action_text}")

        # Пропускаем запрос дальше (к боту)
        return await handler(event, data)


class AntiFloodMiddleware(BaseMiddleware):
    """Защита от спама кнопками (как было раньше)"""

    def __init__(self, limit_seconds: float = 1.0):
        self.limit = limit_seconds
        self.user_timestamps = {}

    async def __call__(
            self,
            handler: Callable[[TelegramObject, Dict[str, Any]], Awaitable[Any]],
            event: TelegramObject,
            data: Dict[str, Any]
    ) -> Any:
        if isinstance(event, (Message, CallbackQuery)):
            user_id = event.from_user.id
            current_time = time.time()
            last_time = self.user_timestamps.get(user_id, 0)

            if current_time - last_time < self.limit:
                if isinstance(event, CallbackQuery):
                    await event.answer("⏳ Не нажимайте так быстро!", show_alert=False)
                return

            self.user_timestamps[user_id] = current_time

        return await handler(event, data)
