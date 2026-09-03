"""Phase 19 (D-03): единственная точка выхода Mini App в `api.telegram.org`.

Все вызовы — `httpx.AsyncClient(proxy=cfg.proxy_url)` (прецедент `dashboard/notify.py`):
процесс живёт в том же окружении, что бот, и без `PROXY_URL` до Telegram из RU не достучится.

Токен бота живёт ТОЛЬКО в URL исходящего запроса (T-19-19): он не попадает ни в один
возвращаемый объект и ни в одну строку лога. Логируем метод и код ответа; текст исключений
httpx в лог не пишем вовсе — `str(exc)` у него содержит URL запроса вместе с токеном.
Ошибка любого рода наружу — `TelegramApiError(reason)` с безопасным коротким текстом.

Потребители: `routers/submissions.py` (sendPhoto/sendDocument — часть сдачи в чат
загрузившего, получаем `file_id`), `routers/files.py` (getFile + скачивание — прокси для
`<img>`/ссылок в приложении), планы 19-05..19-07 (sendMessage делегату).
"""
from __future__ import annotations

import logging
from typing import AsyncIterator

import httpx

logger = logging.getLogger(__name__)

API_BASE = "https://api.telegram.org/bot"
FILE_BASE = "https://api.telegram.org/file/bot"

UPLOAD_TIMEOUT = 60.0
CALL_TIMEOUT = 15.0
DOWNLOAD_TIMEOUT = 60.0
DOWNLOAD_CHUNK = 64 * 1024


class TelegramApiError(Exception):
    """`reason` — короткий безопасный код: upstream_unavailable | bad_response | api_error |
    not_found. Никогда не содержит URL, токена или тела ответа Bot API."""

    def __init__(self, reason: str, status: int | None = None):
        super().__init__(reason)
        self.reason = reason
        self.status = status


def _make_client(cfg, timeout: float) -> httpx.AsyncClient:
    """Точка подмены в тестах (MockTransport) — весь модуль открывает клиент только здесь."""
    return httpx.AsyncClient(proxy=cfg.proxy_url, timeout=timeout)


def _method_url(cfg, method: str) -> str:
    return f"{API_BASE}{cfg.bot_token}/{method}"


async def _call(cfg, method: str, *, data: dict | None = None, files: dict | None = None,
                json_body: dict | None = None, timeout: float = CALL_TIMEOUT) -> dict:
    """POST `method`, вернуть `result` ответа Bot API. Любая ошибка -> TelegramApiError."""
    try:
        async with _make_client(cfg, timeout) as client:
            response = await client.post(
                _method_url(cfg, method), data=data, files=files, json=json_body,
            )
    except httpx.HTTPError as exc:
        # Ни str(exc), ни repr — там URL с токеном. Только класс.
        logger.warning("telegram_api: %s недоступен (%s)", method, type(exc).__name__)
        raise TelegramApiError("upstream_unavailable") from None
    try:
        payload = response.json()
    except ValueError:
        logger.warning("telegram_api: %s вернул не-JSON, код %s", method, response.status_code)
        raise TelegramApiError("bad_response", response.status_code) from None
    if response.status_code != 200 or not isinstance(payload, dict) or not payload.get("ok"):
        logger.warning("telegram_api: %s вернул код %s", method, response.status_code)
        raise TelegramApiError("api_error", response.status_code)
    result = payload.get("result")
    return result if isinstance(result, dict) else {}


async def send_photo(cfg, chat_id: int, content: bytes, filename: str, content_type: str,
                     caption: str | None = None) -> dict:
    data = {"chat_id": str(chat_id)}
    if caption:
        data["caption"] = caption
    return await _call(
        cfg, "sendPhoto", data=data,
        files={"photo": (filename, content, content_type)}, timeout=UPLOAD_TIMEOUT,
    )


async def send_document(cfg, chat_id: int, content: bytes, filename: str, content_type: str,
                        caption: str | None = None) -> dict:
    data = {"chat_id": str(chat_id)}
    if caption:
        data["caption"] = caption
    return await _call(
        cfg, "sendDocument", data=data,
        files={"document": (filename, content, content_type)}, timeout=UPLOAD_TIMEOUT,
    )


async def send_message(cfg, chat_id: int, text: str, reply_markup: dict | None = None) -> dict:
    body: dict = {"chat_id": chat_id, "text": text}
    if reply_markup:
        body["reply_markup"] = reply_markup
    return await _call(cfg, "sendMessage", json_body=body)


async def get_file(cfg, file_id: str) -> dict:
    """`result` getFile: `file_path` ЗДЕСЬ И ОСТАЁТСЯ — наружу (в ответ клиенту, в лог) его
    не отдавать: вместе с токеном он и есть прямая ссылка на файл (RESEARCH Pattern 4)."""
    return await _call(cfg, "getFile", json_body={"file_id": file_id})


async def get_user_profile_photos(cfg, user_id: int, limit: int = 1) -> dict:
    """`result`: `{total_count, photos: [[PhotoSize, ...], ...]}` — внутренний список каждого
    фото отсортирован от меньшего размера к большему (D-02, план 23-03, аватар делегата на
    карточке заявки). Кеширует и разбирает ответ `miniapp/avatars.py`."""
    return await _call(cfg, "getUserProfilePhotos", json_body={"user_id": user_id, "limit": limit})


class FileStream:
    """Открытый поток файла Telegram: `content_type`, `content_length` (может быть None) и
    `chunks()` — асинхронный итератор байтов, закрывающий соединение по завершении."""

    def __init__(self, client: httpx.AsyncClient, response: httpx.Response):
        self._client = client
        self._response = response
        self.content_type = response.headers.get("content-type") or "application/octet-stream"
        length = response.headers.get("content-length")
        self.content_length = int(length) if length and length.isdigit() else None

    async def chunks(self) -> AsyncIterator[bytes]:
        try:
            async for chunk in self._response.aiter_bytes(DOWNLOAD_CHUNK):
                yield chunk
        finally:
            await self.aclose()

    async def aclose(self) -> None:
        await self._response.aclose()
        await self._client.aclose()


async def download_file(cfg, file_path: str) -> FileStream:
    """Начать скачивание по `file_path` из getFile. Ошибка/не-200 -> TelegramApiError."""
    client = _make_client(cfg, DOWNLOAD_TIMEOUT)
    request = client.build_request("GET", f"{FILE_BASE}{cfg.bot_token}/{file_path}")
    try:
        response = await client.send(request, stream=True)
    except httpx.HTTPError as exc:
        await client.aclose()
        logger.warning("telegram_api: скачивание файла не удалось (%s)", type(exc).__name__)
        raise TelegramApiError("upstream_unavailable") from None
    if response.status_code != 200:
        status = response.status_code
        await response.aclose()
        await client.aclose()
        logger.warning("telegram_api: скачивание файла вернуло код %s", status)
        raise TelegramApiError("not_found", status)
    return FileStream(client, response)


__all__ = [
    "FileStream",
    "TelegramApiError",
    "download_file",
    "get_file",
    "send_document",
    "send_message",
    "send_photo",
]
