import time
from typing import Any, Awaitable, Callable, Dict
from aiogram import BaseMiddleware
from aiogram.types import TelegramObject, Message, CallbackQuery

class AntiFloodMiddleware(BaseMiddleware):
    def __init__(self, limit_seconds: float = 1.0):
        # limit_seconds - сколько секунд нельзя нажимать снова
        self.limit = limit_seconds
        self.user_timestamps = {}

    async def __call__(
        self,
        handler: Callable[[TelegramObject, Dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: Dict[str, Any]
    ) -> Any:
        # Проверяем только сообщения и нажатия кнопок
        if isinstance(event, (Message, CallbackQuery)):
            user_id = event.from_user.id
            current_time = time.time()
            last_time = self.user_timestamps.get(user_id, 0)

            # Если прошло меньше времени, чем разрешено (спам!)
            if current_time - last_time < self.limit:
                if isinstance(event, CallbackQuery):
                    # Показываем маленькое уведомление, чтобы кружок загрузки исчез
                    await event.answer("⏳ Не нажимайте так быстро!", show_alert=False)
                return # ПРЕРЫВАЕМ ОБРАБОТКУ, ДЖАНГО ЭТОГО НЕ УВИДИТ

            # Запоминаем время последнего успешного нажатия
            self.user_timestamps[user_id] = current_time

        # Пропускаем запрос дальше к боту
        return await handler(event, data)
