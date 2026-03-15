# bot/api.py
import aiohttp
from typing import Union
from decouple import config

DJANGO_API_TOKEN = config("DJANGO_API_TOKEN")
# УЧЛИ ПРАВИЛЬНЫЙ АДРЕС С voting/
DJANGO_API_BASE = "http://127.0.0.1:8000/voting/api"

session: aiohttp.ClientSession = None


async def on_startup():
    global session
    session = aiohttp.ClientSession()


async def on_shutdown():
    global session
    if session and not session.closed:
        await session.close()


async def request(method: str, endpoint: str, data: dict = None, is_admin: bool = False) -> Union[dict, list]:
    url = f"{DJANGO_API_BASE}/{endpoint}/"
    headers = {"Content-Type": "application/json"}
    if is_admin:
        headers["Authorization"] = f"Token {DJANGO_API_TOKEN}"

    async with session.request(method, url, json=data, headers=headers, timeout=10) as resp:
        if resp.status >= 400:
            try:
                error_data = await resp.json()
                error_msg = error_data.get("error") or error_data.get("detail") or \
                            error_data.get("non_field_errors", [str(error_data)])[0]
            except Exception:
                error_msg = await resp.text()
            raise Exception(error_msg)

        if resp.status == 204:
            return {}

        data = await resp.json()
        if isinstance(data, dict) and "results" in data and "count" in data:
            return data["results"]
        return data
