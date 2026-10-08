"""Клиент API Яндекс Форм и OAuth Яндекса. Без UI и без записи в БД токенов (это делает вызывающий).

Весь сетевой выход — через `_make_client` (точка подмены в тестах): без прокси Telegram и без
переменных окружения HTTP(S)_PROXY (`trust_env=False`) — Яндекс из РФ доступен напрямую.

Токены и секрет приложения регистрируются в `secret_redact`; в лог идут только метод, путь без
query и код ответа. Текст исключений httpx не логируем и в `YandexApiError` не кладём — в нём URL.
"""
from __future__ import annotations

import logging
import re
from datetime import timedelta
from urllib.parse import urlencode, urlsplit

import httpx

from config import config
from database import ext_forms_db as ef_db
from secret_redact import register_secret
from services.timeutil import msk_now

logger = logging.getLogger(__name__)

API_HOST = "api.forms.yandex.net"
BASE = f"https://{API_HOST}/v1"
OAUTH_TOKEN_URL = "https://oauth.yandex.ru/token"
OAUTH_AUTHORIZE_URL = "https://oauth.yandex.ru/authorize"
CALL_TIMEOUT = 15.0
PAGE_SIZE = 100
_FMT = "%Y-%m-%d %H:%M:%S"
_FORM_ID_RE = re.compile(r"\b([0-9a-f]{24})\b", re.I)


class YandexApiError(Exception):
    """`reason`: unauthorized | forbidden | not_found | rate_limited | upstream_unavailable |
    bad_response | no_app_keys | bad_code | org_required. Без URL, токена и тела ответа."""

    def __init__(self, reason: str, status: int | None = None):
        super().__init__(reason)
        self.reason = reason
        self.status = status


def parse_form_id(text: str) -> str | None:
    m = _FORM_ID_RE.search(text or "")
    return m.group(1).lower() if m else None


def _make_client(timeout: float) -> httpx.AsyncClient:
    """ЕДИНСТВЕННАЯ точка создания клиента (подмена на MockTransport в тестах)."""
    return httpx.AsyncClient(timeout=timeout, trust_env=False)


async def get_yandex_app_creds() -> tuple[str, str] | None:
    """Ключи приложения Яндекса: сначала external_form_secrets (кнопка в боте), иначе .env."""
    cid = await ef_db.get_app_secret("yandex_client_id")
    secret = await ef_db.get_app_secret("yandex_client_secret")
    if not (cid and secret):
        cid = (config.YANDEX_OAUTH_CLIENT_ID or "").strip()
        sec = config.YANDEX_OAUTH_CLIENT_SECRET
        secret = sec.get_secret_value().strip() if sec is not None else ""
    if not (cid and secret):
        return None
    register_secret(secret)
    return cid, secret


def authorize_url(client_id: str) -> str:
    return OAUTH_AUTHORIZE_URL + "?" + urlencode(
        {"response_type": "code", "client_id": client_id, "force_confirm": "yes"})


def _headers(conn: dict) -> dict:
    h = {"Authorization": f"OAuth {conn['access_token']}"}
    if conn.get("org_id"):
        h[conn.get("org_header") or "X-Org-Id"] = str(conn["org_id"])
    return h


def _reason_for(status: int) -> str:
    if status == 401:
        return "unauthorized"
    if status == 403:
        return "forbidden"
    if status == 404:
        return "not_found"
    if status == 429:
        return "rate_limited"
    if status >= 500:
        return "upstream_unavailable"
    return "bad_response"


def _org_required(response: httpx.Response) -> bool:
    """Яндекс шлёт detail с \\uXXXX-экранированием — в сыром тексте кириллицы нет, читаем JSON."""
    try:
        detail = str((response.json() or {}).get("detail") or "")
    except (ValueError, AttributeError):
        detail = response.text
    return "организац" in detail.lower()


async def _request(method: str, url: str, *, headers: dict | None = None,
                   data: dict | None = None) -> dict:
    path = urlsplit(url).path
    try:
        async with _make_client(CALL_TIMEOUT) as client:
            response = await client.request(method, url, headers=headers, data=data)
    except (httpx.HTTPError, OSError):
        logger.warning("yandex %s %s: сеть недоступна", method, path)
        raise YandexApiError("upstream_unavailable") from None
    logger.info("yandex %s %s -> %s", method, path, response.status_code)
    if response.status_code == 400 and _org_required(response):
        # «Требуется организация»: API Форм отдаёт только формы организации (Яндекс 360 или
        # Yandex Cloud); форма личного аккаунта и вход без ID организации так не читаются.
        raise YandexApiError("org_required", 400)
    if response.status_code >= 400:
        raise YandexApiError(_reason_for(response.status_code), response.status_code)
    try:
        payload = response.json()
    except ValueError:
        raise YandexApiError("bad_response", response.status_code) from None
    if not isinstance(payload, dict):
        raise YandexApiError("bad_response", response.status_code)
    return payload


async def _token_call(form: dict) -> dict:
    creds = await get_yandex_app_creds()
    if creds is None:
        raise YandexApiError("no_app_keys")
    cid, secret = creds
    try:
        payload = await _request("POST", OAUTH_TOKEN_URL,
                                 data={**form, "client_id": cid, "client_secret": secret})
    except YandexApiError as e:
        if e.status == 400:
            raise YandexApiError("bad_code", 400) from None
        raise
    access = payload.get("access_token")
    if not isinstance(access, str) or not access:
        raise YandexApiError("bad_response")
    refresh = payload.get("refresh_token")
    register_secret(access)
    register_secret(refresh)
    try:
        ttl = int(payload.get("expires_in") or 0)
    except (TypeError, ValueError):
        ttl = 0
    expires_at = (msk_now() + timedelta(seconds=ttl)).strftime(_FMT) if ttl else None
    return {"access_token": access, "refresh_token": refresh, "expires_at": expires_at}


async def exchange_code(code: str) -> dict:
    return await _token_call({"grant_type": "authorization_code", "code": (code or "").strip()})


async def refresh_token(conn: dict) -> dict:
    result = await _token_call({"grant_type": "refresh_token",
                                "refresh_token": conn.get("refresh_token") or ""})
    if not result.get("refresh_token"):
        result["refresh_token"] = conn.get("refresh_token")
    return result


async def _get(conn: dict, url: str) -> dict:
    register_secret(conn.get("access_token"))
    return await _request("GET", url, headers=_headers(conn))


ORG_HEADERS = ("X-Org-Id", "X-Cloud-Org-Id")  # Яндекс 360 для бизнеса | Yandex Cloud (Identity Hub)


async def detect_org_header(conn: dict, org_id: str) -> str | None:
    """Какой из двух заголовков принимает организация: менеджер знает ID, но не её вид.
    None — ни один не подошёл (чужая организация или опечатка в ID)."""
    for header in ORG_HEADERS:
        try:
            await _get({**conn, "org_id": org_id, "org_header": header}, f"{BASE}/surveys")
        except YandexApiError as e:
            if e.reason in ("unauthorized", "upstream_unavailable"):
                raise
            continue
        return header
    return None


async def get_survey(conn: dict, survey_id: str) -> dict:
    return await _get(conn, f"{BASE}/surveys/{survey_id}")


async def get_questions(conn: dict, survey_id: str) -> dict:
    return await _get(conn, f"{BASE}/surveys/{survey_id}/questions")


async def get_answer(conn: dict, answer_id: str) -> dict:
    return await _get(conn, f"{BASE}/answers?answer_id={answer_id}")


async def list_answer_ids(conn: dict, survey_id: str, *, max_pages: int = 200) -> list[str]:
    ids: list[str] = []
    url: str | None = f"{BASE}/surveys/{survey_id}/answers?ordering=asc&page_size={PAGE_SIZE}"
    pages = 0
    while url and pages < max_pages:
        page = await _get(conn, url)
        pages += 1
        for a in page.get("answers") or []:
            if isinstance(a, dict) and a.get("id") is not None:
                ids.append(str(a["id"]))
        nxt = page.get("next")
        url = nxt.get("next_url") if isinstance(nxt, dict) else None
        if url:
            parts = urlsplit(url)
            if parts.scheme != "https" or parts.hostname != API_HOST:
                break
    return ids
