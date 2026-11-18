import asyncio
import logging
from time import monotonic
import aiohttp
from decouple import config
from core.observability import context, event, request_id

DJANGO_API_TOKEN = config('DJANGO_API_TOKEN', default='')
DJANGO_API_BASE = config('DJANGO_API_BASE', default='http://web:8000/voting/api').rstrip('/')
session = None


class APIError(Exception):
    def __init__(self, message, *, status=0, code='network_error', request_id='', retry_after=None):
        super().__init__(message)
        self.status, self.code, self.request_id, self.retry_after = status, code, request_id, retry_after


async def on_startup():
    global session
    session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=15))


async def on_shutdown():
    if session and not session.closed:
        await session.close()


def error_message(value):
    if isinstance(value, dict):
        for key in ('error', 'detail', 'non_field_errors'):
            if key in value:
                return error_message(value[key])
        return '; '.join(f'{key}: {error_message(item)}' for key, item in value.items() if key not in {'code', 'request_id'})
    if isinstance(value, list):
        return '; '.join(error_message(item) for item in value)
    return str(value)


async def request(method, endpoint, data=None, is_admin=False):
    endpoint = endpoint.strip('/')
    path, sep, query = endpoint.partition('?')
    url = f'{DJANGO_API_BASE}/{path.rstrip("/")}/' + (f'?{query}' if sep else '')
    actor = context.get()
    rid = request_id(actor.get('request_id'))
    headers = {'Content-Type': 'application/json', 'X-Request-ID': rid}
    if actor.get('actor_id'):
        headers['X-Telegram-User-ID'] = str(actor['actor_id'])
    if DJANGO_API_TOKEN:
        headers['Authorization'] = f'Token {DJANGO_API_TOKEN}'
    started = monotonic()
    try:
        async with session.request(method, url, json=data, headers=headers) as resp:
            try:
                body = await resp.json() if resp.status != 204 else {}
            except (ValueError, aiohttp.ContentTypeError):
                body = {'error': f'Некорректный ответ сервера ({resp.status}).', 'code': 'invalid_response'}
                if resp.status < 400:
                    event('api.invalid_response', level=logging.ERROR, request_id=rid, path=path)
                    raise APIError(body['error'], status=502, code=body['code'], request_id=rid)
            code = body.get('code', 'request_failed') if isinstance(body, dict) else 'request_failed'
            quiet = method == 'GET' and resp.status < 500 and resp.status not in {401, 403}
            event('api.response', traffic=quiet, level=logging.ERROR if resp.status >= 500 else logging.WARNING if resp.status in {401, 403} else logging.INFO,
                  request_id=rid, method=method, path=path, status=resp.status,
                  reason_code=code if resp.status >= 400 else '', duration_ms=round((monotonic()-started)*1000, 2))
            if resp.status >= 400:
                raise APIError(error_message(body), status=resp.status, code=code,
                               request_id=resp.headers.get('X-Request-ID', rid), retry_after=resp.headers.get('Retry-After'))
            if isinstance(body, dict) and 'results' in body and 'count' in body:
                return body['results']
            return body
    except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
        event('api.network_failed', level=logging.ERROR, request_id=rid, method=method, path=path,
              error_type=type(exc).__name__)
        raise APIError('Сервер временно недоступен. Повторите позже.', request_id=rid) from exc
