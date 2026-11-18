"""Keep Telegram's command picker and /help aligned for each operator role."""
from aiogram.types import BotCommand, BotCommandScopeDefault, BotCommandScopeAllPrivateChats, BotCommandScopeChat

COMMON_COMMANDS = [
    ('start', 'Главное меню'), ('vote', 'Проголосовать'),
    ('check_access', 'Проверить доступ'), ('myid', 'Узнать свой Telegram ID'), ('help', 'Помощь'),
]
MODERATOR_COMMANDS = [
    ('event', 'Управление турами и раундами'),
    ('set_current_round', 'Выбрать результаты для экрана'),
    ('hide_round', 'Скрыть результаты с сайта'),
]
FULL_ADMIN_COMMANDS = [
    ('create_campaign', 'Создать мероприятие'),
    ('campaign_access', 'Настроить доступ к мероприятию'),
    ('export', 'Скачать результаты и голоса'),
]


def command_list(role):
    items = COMMON_COMMANDS + (MODERATOR_COMMANDS if role in ('limited', 'full') else [])
    if role == 'full':
        items += FULL_ADMIN_COMMANDS
    return [BotCommand(command=command, description=description) for command, description in items]


async def sync_commands(bot, full_admins, limited_admins):
    scopes = [(BotCommandScopeDefault(), 'viewer'), (BotCommandScopeAllPrivateChats(), 'viewer')]
    scopes += [(BotCommandScopeChat(chat_id=uid), 'full' if uid in full_admins else 'limited')
               for uid in sorted(full_admins | limited_admins)]
    for scope, role in scopes:
        for language_code in ('', 'ru', 'en'):
            await bot.set_my_commands(command_list(role), scope=scope, language_code=language_code)
