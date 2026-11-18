import logging
import time
from aiogram import BaseMiddleware
from aiogram.filters import Filter
from aiogram.types import Message, CallbackQuery
from core.observability import context, event, request_id


class AdminFilter(Filter):
    def __init__(self, allowed):
        self.allowed = allowed

    async def __call__(self, obj):
        if obj.from_user.id in self.allowed:
            return True
        event('access.denied', outcome='rejected', reason_code='insufficient_role')
        if isinstance(obj, CallbackQuery):
            await obj.answer('Недостаточно прав.', show_alert=True)
        return False


class RoleLoggingMiddleware(BaseMiddleware):
    def __init__(self, full_admins, limited_admins):
        self.full_admins, self.limited_admins = full_admins, limited_admins

    async def __call__(self, handler, obj, data):
        user = getattr(obj, 'from_user', None)
        actor_id = user.id if user else None
        role = 'full' if actor_id in self.full_admins else 'limited' if actor_id in self.limited_admins else 'viewer'
        token = context.set({'request_id': request_id(), 'source': 'bot', 'actor_id': actor_id,
                             'actor_role': role, 'telegram_update_id': getattr(data.get('event_update'), 'update_id', None)})
        try:
            # No raw text/username/callback payload. Background requests stay out of round audit.
            event('telegram.received', traffic=True, kind=type(obj).__name__)
            return await handler(obj, data)
        except Exception:
            logging.getLogger(__name__).exception('telegram.handler_failed')
            raise
        finally:
            context.reset(token)


class AntiFloodMiddleware(BaseMiddleware):
    def __init__(self, limit_seconds=1.0):
        self.limit = limit_seconds
        self.user_timestamps = {}
        self.last_cleanup = time.monotonic()

    async def __call__(self, handler, obj, data):
        now = time.monotonic()
        if now - self.last_cleanup > 60:
            self.user_timestamps = {uid: ts for uid, ts in self.user_timestamps.items() if now-ts < 60}
            self.last_cleanup = now
        if isinstance(obj, (Message, CallbackQuery)) and obj.from_user:
            uid = obj.from_user.id
            if now - self.user_timestamps.get(uid, -float('inf')) < self.limit:
                event('telegram.throttled', traffic=True)
                if isinstance(obj, CallbackQuery):
                    await obj.answer('⏳ Не нажимайте так быстро!', show_alert=False)
                return
            self.user_timestamps[uid] = now
        return await handler(obj, data)
