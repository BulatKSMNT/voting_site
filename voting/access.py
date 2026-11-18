"""Telegram ID parsing shared by the admin, API and bot (no Django imports)."""
import re


def normalize_telegram_ids(values):
    if not isinstance(values, list):
        raise ValueError("Передайте список числовых Telegram ID.")
    result = set()
    for value in values:
        if isinstance(value, str):
            value = value.strip()
            if not re.fullmatch(r"[0-9]{1,19}", value):
                raise ValueError("Каждый Telegram ID должен быть положительным целым числом, без @username.")
            value = int(value)
        if type(value) is not int or not 0 < value < 2**63:
            raise ValueError("Каждый Telegram ID должен быть положительным целым числом до 9223372036854775807.")
        result.add(value)
    return sorted(result)


def parse_telegram_ids(text):
    return normalize_telegram_ids(re.split(r"[\s,;]+", text.strip()) if text.strip() else [])


VOTER_NOT_ALLOWED = "Напишите @khat911, проверим вас с утвержденным списком"
