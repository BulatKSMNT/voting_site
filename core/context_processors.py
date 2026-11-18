from django.conf import settings


def brand(request):
    return {'telegram_bot_url': f'https://t.me/{settings.TELEGRAM_BOT_USERNAME}'}
