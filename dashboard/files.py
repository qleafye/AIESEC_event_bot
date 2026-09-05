"""Фаза 26, план 01 (RT-01/RT-02/RT-03, T-26-01-01..06): прокси ассетов оформления
дашборда — `GET /api/file/{file_id}` в `dashboard/main.py`.

Правила скопированы из `miniapp/routers/files.py` (прямая ссылка Telegram содержит токен
бота): наружу отдаётся только этот маршрут, `file_path` и URL с токеном не возвращаются и
не логируются; ни `str(exc)`, ни `repr(exc)` httpx в лог не пишутся — только класс
исключения (`type(exc).__name__`).

ЧЕМ этот прокси уже мини-аппового: у него есть ровно одна ветка allow-list — значения
ключей ОФОРМЛЕНИЯ мероприятия (`ASSET_SETTING_KEYS`). Ветки «сдача геймификации» / «аватар
делегата» / «своя обложка программы» из Mini App здесь НЕТ и не будет — дашборд читает
`bot_settings` read-only и не знает ни про сдачи, ни про делегатов отдельно от KPI.
"""
from __future__ import annotations

import logging
import re

import httpx

from dashboard.db import read_conn

import web_theme

logger = logging.getLogger(__name__)

# Единственное перечисление ключей оформления в этом модуле — список берётся из
# `web_theme.ASSET_KEYS`, второй раз ключи здесь не заводятся (`miniapp_logo` — не ключ
# `web_theme`, это ключ реестра лого приложения, тот же, что в `miniapp/routers/files.py`).
ASSET_SETTING_KEYS: tuple[str, ...] = tuple(dict.fromkeys(("miniapp_logo", *web_theme.ASSET_KEYS.values())))

FILE_ID_RE = re.compile(r"^[A-Za-z0-9_-]{20,200}$")

MAX_ASSET_BYTES = 5 * 1024 * 1024
_TIMEOUT_SECONDS = 15

API_BASE = "https://api.telegram.org/bot"
FILE_BASE = "https://api.telegram.org/file/bot"


def _make_client(cfg, timeout: float = _TIMEOUT_SECONDS) -> httpx.Client:
    """Единственная точка открытия клиента — и точка подмены в тестах (MockTransport),
    тот же приём, что `miniapp/telegram_api.py::_make_client` и `dashboard/notify.py`."""
    return httpx.Client(proxy=cfg.proxy_url, timeout=timeout)


def is_theme_asset(db_path: str, file_id: str) -> bool:
    """`True`, если `file_id` — текущее значение ОДНОГО из ключей оформления в
    `bot_settings`. Любая ошибка sqlite -> WARNING и `False` (T-26-01-05: недоступная БД не
    должна ронять страницу с favicon)."""
    placeholders = ", ".join("?" for _ in ASSET_SETTING_KEYS)
    try:
        with read_conn(db_path) as conn:
            rows = conn.execute(
                f"SELECT value FROM bot_settings WHERE key IN ({placeholders})",
                ASSET_SETTING_KEYS,
            ).fetchall()
    except Exception as exc:  # noqa: BLE001 — сырое sqlite3.Error и его подклассы
        logger.warning("dashboard.files: is_theme_asset не смог прочитать bot_settings (%s)", type(exc).__name__)
        return False
    return any(row["value"] == file_id for row in rows)


def fetch_theme_asset(cfg, file_id: str) -> "tuple[bytes, str] | None":
    """`getFile` + скачивание через `_make_client`. `None` (без исключения наружу) при любой
    ошибке сети, не-200 ответе, ответе без `file_path`, `content_type` не `image/*`, теле
    длиннее `MAX_ASSET_BYTES` (по `Content-Length` И по фактической длине тела). `file_path`
    и URL с токеном никогда не логируются и не возвращаются (T-26-01-03)."""
    try:
        with _make_client(cfg) as client:
            get_file_resp = client.post(
                f"{API_BASE}{cfg.bot_token}/getFile",
                json={"file_id": file_id},
            )
    except httpx.HTTPError as exc:
        logger.warning("dashboard.files: getFile недоступен (%s)", type(exc).__name__)
        return None

    try:
        payload = get_file_resp.json()
    except ValueError:
        logger.warning("dashboard.files: getFile вернул не-JSON, код %s", get_file_resp.status_code)
        return None
    if get_file_resp.status_code != 200 or not isinstance(payload, dict) or not payload.get("ok"):
        logger.warning("dashboard.files: getFile вернул код %s", get_file_resp.status_code)
        return None
    result = payload.get("result")
    file_path = result.get("file_path") if isinstance(result, dict) else None
    if not file_path:
        return None

    try:
        with _make_client(cfg) as client:
            download_resp = client.get(f"{FILE_BASE}{cfg.bot_token}/{file_path}")
    except httpx.HTTPError as exc:
        logger.warning("dashboard.files: скачивание файла упало (%s)", type(exc).__name__)
        return None

    if download_resp.status_code != 200:
        logger.warning("dashboard.files: скачивание файла вернуло код %s", download_resp.status_code)
        return None

    content_type = download_resp.headers.get("content-type", "")
    if not content_type.startswith("image/"):
        return None

    content_length = download_resp.headers.get("content-length")
    if content_length is not None:
        try:
            if int(content_length) > MAX_ASSET_BYTES:
                return None
        except ValueError:
            pass

    content = download_resp.content
    if len(content) > MAX_ASSET_BYTES:
        return None

    return content, content_type
