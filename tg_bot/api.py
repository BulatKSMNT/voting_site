# tg_bot/api.py
import aiohttp
from typing import Union
from decouple import config

DJANGO_API_TOKEN = config("DJANGO_API_TOKEN", default="")
DJANGO_API_BASE = config("DJANGO_API_BASE", default="http://web:8000/voting/api").rstrip("/")

session: aiohttp.ClientSession = None


async def on_startup():
    global session
    timeout = aiohttp.ClientTimeout(total=15)
    session = aiohttp.ClientSession(timeout=timeout)


async def on_shutdown():
    global session
    if session and not session.closed:
        await session.close()


async def request(method: str, endpoint: str, data: dict = None, is_admin: bool = False) -> Union[dict, list]:
    """
    is_admin оставлен для совместимости со старым кодом,
    но токен теперь отправляется ВСЕГДА, если он есть.
    """
    endpoint = endpoint.strip('/')

    if "?" in endpoint:
        path, query = endpoint.split("?", 1)
        path = path.rstrip('/')
        url = f"{DJANGO_API_BASE}/{path}/?{query}"
    else:
        url = f"{DJANGO_API_BASE}/{endpoint}/"

    headers = {"Content-Type": "application/json"}
    if DJANGO_API_TOKEN:
        headers["Authorization"] = f"Token {DJANGO_API_TOKEN}"

    async with session.request(method, url, json=data, headers=headers) as resp:
        if resp.status >= 400:
            try:
                error_data = await resp.json()
                error_msg = (
                    error_data.get("error")
                    or error_data.get("detail")
                    or error_data.get("non_field_errors", [str(error_data)])[0]
                )
            except Exception:
                error_msg = await resp.text()
                if "<html" in error_msg.lower() or "<body" in error_msg.lower():
                    error_msg = f"Ошибка сервера {resp.status}. Проверьте терминал Django."

            raise Exception(error_msg)

        if resp.status == 204:
            return {}

        response_data = await resp.json()
        if isinstance(response_data, dict) and "results" in response_data and "count" in response_data:
            return response_data["results"]
        return response_data
