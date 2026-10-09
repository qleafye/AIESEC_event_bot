"""Phase 19 (D-01/D-05/D-09): FastAPI-приложение Mini App — фабрика `create_app(cfg)`.

Отдельный процесс рядом с дашбордом (`uvicorn miniapp.main:app --port 8001`); Cloudflare
Tunnel маршрутизирует `yl26.<домен>/app*` сюда, остальное — в дашборд. Слои как у дашборда:
самый внешний — `ProxyHeadersMiddleware` с доверенной подсетью `cfg.trusted_proxies`
(решения о доступе никогда не по IP — T-19-09), затем `SessionMiddleware` с теми же
параметрами, что у дашборда (`dashboard.auth.session_middleware_kwargs` — один secret =
cookie `yl_dash` читается здесь без изменений, D-05).

260824-8qw (HG-01): сразу после `SessionMiddleware` подключён `miniapp.body_limit.
BodyLimitMiddleware` — предел тела запроса на уровне ASGI, до `request.form()`. Порядок
критичен: `app.add_middleware` делает позже добавленный слой внешним, поэтому итоговый стек
— `_security_headers -> _enabled_gate -> BodyLimit -> Session -> роутер`. 413 остаётся ПОД
слоем заголовков (CSP на ответе во фрейме Telegram обязана быть и на 413) и ПОД гейтом
тумблера (выключенное приложение по-прежнему отвечает 503, а не 413 — гейт проверяется
раньше, до того как middleware успеет прочитать тело).

Security-заголовки (D-09, RESEARCH Pattern 5 / Pitfall 5): Telegram Web K/A встраивают
Mini App в `<iframe>` на `https://web.telegram.org`, поэтому ответы `/app*` несут
`Content-Security-Policy: frame-ancestors https://web.telegram.org https://*.telegram.org`
и НЕ несут `X-Frame-Options` — `DENY` остаётся только у дашборда (T-19-06).

Тумблер `miniapp_enabled` (D-06): при `off` приложение закрыто ДЕЛЕГАТАМ — их запросы
отвечают 503 `miniapp_off`, а персонал (хоть одно право приложения) входит как обычно
(`_passes_when_off`): сканер и мастер первой настройки есть только здесь. `/app/health`
живёт всегда, чтобы контейнер не перезапускался по кругу.

`/docs`/`/openapi.json` отключены (T-19-10) — приложение смотрит в базу с ПД.
Статика монтируется на `/app/static` только при наличии каталога — фабрика обязана работать
без него (тесты, урезанный образ). Оболочка `/app` при выключенном тумблере отдаётся как
обычно (кто открыл — станет ясно только из JS по initData), делегат получает 503 на первом же
API-запросе и видит `miniapp_disabled_text`. Человеческая страница 503 без ядра
(`routers.page.render_disabled_page`) остаётся для недоступной БД.
"""
from __future__ import annotations

import logging
import mimetypes
import re
import sqlite3
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.middleware.sessions import SessionMiddleware
from uvicorn.middleware.proxy_headers import ProxyHeadersMiddleware

from dashboard.auth import session_middleware_kwargs
from dashboard.db import read_conn

from miniapp.body_limit import BodyLimitMiddleware
from miniapp.config import (
    DEFAULT_MAX_BODY_BYTES,
    MAX_BODY_BYTES,
    MAX_UPLOAD_BYTES,
    DashboardConfig,
    load_miniapp_config,
)
from dashboard.access import resolve_capabilities
from miniapp.auth import verify_init_data
from miniapp.deps import BOT_ONLY_CAPS, read_setting
from miniapp.file_tokens import verify_file_token
from miniapp.logging_config import configure_logging
from secret_redact import register_secret
from miniapp.routers import ALL_ROUTERS
from miniapp.routers.page import STATIC_PREFIX
from miniapp.routers.page import render_disabled_page

logger = logging.getLogger(__name__)

HEALTH_PATH = "/app/health"
SHELL_PATHS = frozenset({"/app", "/app/"})
STATIC_DIR = Path(__file__).resolve().parent / "static"

# Ассеты оболочки (CSS/шрифты/иконки), которым разрешено грузиться ДАЖЕ при miniapp_enabled=off
# -- план 19.1-08 (визуальная сверка): `render_disabled_page` рисует ту же `app.html`, что и
# обычный shell (см. её докстринг «оболочка без ядра С ТЕКСТОМ»), но до этой правки гасящий
# middleware блокировал `/app/static/*`/`/app/theme.css` наравне с API -- страница-объяснение
# грузилась вообще без стилей и шрифтов (голый HTML, в этой среде браузер рисовал его тёмным
# автоинвертированием). `/app/static/js/*` НАРОЧНО остаётся заблокирован -- «без ядра» означает
# именно это: JS-модуль не должен исполниться и заново задёргать `/app/api/me`.
_SHELL_ASSET_PREFIXES = ("/app/static/tokens.css", "/app/static/app.css", "/app/static/fonts/",
                         "/app/static/icons/")


def _is_shell_asset(path: str) -> bool:
    # Версионированный префикс (/app/static/v123/…) приводится к легаси-виду — список
    # префиксов оболочки один, независимо от того, каким путём пришёл клиент.
    path = re.sub(r"^/app/static/v\d+/", "/app/static/", path)
    return path == "/app/theme.css" or path.startswith(_SHELL_ASSET_PREFIXES)

CONTENT_SECURITY_POLICY = (
    "frame-ancestors https://web.telegram.org https://*.telegram.org; "
    "default-src 'self'; "
    "script-src 'self' https://telegram.org; "
    "style-src 'self'; "
    "img-src 'self' data: blob:; "
    "connect-src 'self'; "
    "font-src 'self'"
)


# Квик 260919-u7e (P5, находка 02-miniapp, пункт 2): одна точка лога на каждый 4xx/5xx ответ
# API — `_json_errors` уже единственное место, форматирующее ВСЕ такие ответы (см. его
# докстринг), поэтому лог ставится сюда, а не в каждый роутер по отдельности. `telegram_id`
# берёт `request.state.telegram_id`, который `miniapp.deps.principal`/`file_principal`
# проставляют СРАЗУ по извлечении id — раньше любой отказ гейта, включая тот, что случился
# уже ПОСЛЕ того, как личность делегата установлена (403 no_cap/section_off/edit_closed и
# т.п.), поэтому телеграм-id известен почти всегда, кроме исходно анонимных 401
# (no_auth/bad_initdata) — там "anon" и есть честный ответ, личность ещё не проверена.
# T-21-08/T-30-06: в лог идут ТОЛЬКО ключи полей/причина, никогда значение ответа делегата —
# `_error_log_extra` читает `errors`/`field`/`cap`/`section`/`kind` тела ответа, а не body как
# целое, и не касается `text` (человеческий текст могут собрать из ответа делегата в будущем —
# сюда его подмешивать нельзя).
_FILE_ROUTE_PREFIX = "/app/api/file/"


def _error_log_extra(body: dict) -> str:
    errors = body.get("errors")
    if isinstance(errors, dict):
        return f" fields={sorted(errors.keys())}"
    for key in ("field", "cap", "section", "kind"):
        value = body.get(key)
        if value:
            return f" {key}={value}"
    return ""


def _log_api_error(request: Request, status_code: int, body: dict) -> None:
    if status_code < 400:
        return
    telegram_id = getattr(request.state, "telegram_id", "anon")
    reason = body.get("reason", "error") if isinstance(body, dict) else "error"
    extra = _error_log_extra(body) if isinstance(body, dict) else ""
    line = "api error: %s %s -> %s reason=%s telegram_id=%s%s"
    args = (request.method, request.url.path, status_code, reason, telegram_id, extra)
    if request.url.path.startswith(_FILE_ROUTE_PREFIX) and status_code in (401, 403):
        # Тег <img> не шлёт initData -- 401/403 на публичной/приватной картинке без токена
        # штатны на каждый показ карточки, WARNING здесь топит лог шумом, а не сигналом.
        logger.debug(line, *args)
    elif status_code >= 500:
        logger.error(line, *args)
    else:
        logger.warning(line, *args)


def _miniapp_state(db_path: str) -> str:
    """`"on"` | `"off"` | `"error"`. Тумблер читается на каждый запрос (без кэша — как и
    права). Недоступная БД — `"error"`: лучше честный 503, чем трассировка с путём к базе."""
    try:
        with read_conn(db_path) as conn:
            return "on" if read_setting(conn, "miniapp_enabled") == "on" else "off"
    except sqlite3.Error as exc:
        logger.warning("miniapp: не удалось прочитать miniapp_enabled (%s)", exc)
        return "error"


def _passes_when_off(request: Request, cfg: DashboardConfig) -> bool:
    """Выключенное приложение закрыто ДЕЛЕГАТАМ, не персоналу: сканер, мастер первой настройки
    и поиск настроек живут только здесь. Личность — подписанный initData или токен файла
    (`<img>`); у кого есть хоть одно право приложения (те же права, что в админке бота, без
    `BOT_ONLY_CAPS`) — проходит, остальным 503 `miniapp_off` (клиент рисует
    `miniapp_disabled_text`).

    Запрос без личности пропускается дальше как есть: оболочка `/app` (личность в Телеграме
    узнаётся только из JS — сервер на этом шаге её не видит), публичные картинки, вход по cookie
    дашборда (он и так только для персонала, `principal` -> 403 `staff_only`). Закрытые ручки
    без личности сами ответят 401 — клиент покажет «Откройте через бота» с кнопкой входа."""
    init_data = request.headers.get("x-telegram-init-data")
    token = request.query_params.get("t") if request.url.path.startswith(_FILE_ROUTE_PREFIX) else None
    if init_data:
        data = verify_init_data(init_data, cfg.bot_token)
        telegram_id = int(data["user"]["id"]) if data else None
    elif token:
        telegram_id = verify_file_token(token, cfg.bot_token)
    else:
        return True
    if telegram_id is None:
        return True  # подпись не сошлась — маршрут сам ответит 401
    try:
        with read_conn(cfg.db_path) as conn:
            caps = set(resolve_capabilities(conn, telegram_id, cfg.admin_ids)) - BOT_ONLY_CAPS
    except sqlite3.Error as exc:
        logger.warning("miniapp: не удалось прочитать права при выключенном приложении (%s)", exc)
        return False
    return bool(caps)


def _build_asgi_app(cfg: DashboardConfig) -> FastAPI:
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None, redirect_slashes=False)
    app.state.cfg = cfg

    if STATIC_DIR.is_dir():
        # Quick 260903 (BACKLOG-0309-PATTERN): на этой машине `mimetypes.guess_type("a.webp")`
        # отдаёт (None, None) — без явной регистрации StaticFiles отдало бы паттерн-растр
        # `application/octet-stream`, и поведение зависело бы от mime-таблиц конкретного
        # образа. Регистрация — идемпотентна, безопасна на модуле, который уже настроил webp
        # (Python сам не перезатирает существующее сопоставление другим типом).
        mimetypes.add_type("image/webp", ".webp")
        # Порядок обязателен: версионированный mount ДО легаси, иначе /app/static перехватит
        # и /app/static/v…/ (Starlette матчит mounts в порядке добавления).
        app.mount(STATIC_PREFIX, StaticFiles(directory=str(STATIC_DIR)), name="static_versioned")
        app.mount("/app/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

    app.add_middleware(SessionMiddleware, **session_middleware_kwargs(cfg))

    # HG-01: см. докстринг модуля -- порядок (сразу после Session, до обоих @app.middleware)
    # критичен. `report_limits` держит в ответе 413 потолок ФАЙЛА (MAX_UPLOAD_BYTES), а не
    # потолок тела с multipart-обвязкой (MAX_BODY_BYTES) -- контракт фронта не меняется.
    app.add_middleware(
        BodyLimitMiddleware,
        limits={"/app/api/uploads": MAX_BODY_BYTES},
        default=DEFAULT_MAX_BODY_BYTES,
        report_limits={"/app/api/uploads": MAX_UPLOAD_BYTES},
    )

    @app.exception_handler(StarletteHTTPException)
    async def _json_errors(request: Request, exc: StarletteHTTPException):
        # Контракт: тело ошибки — JSON с полем `reason` на верхнем уровне (не `detail`).
        # Базовый класс Starlette — иначе 404 неизвестного маршрута (его бросает роутер
        # Starlette, не FastAPI) ушёл бы стандартным {"detail": "Not Found"}.
        detail = exc.detail
        if isinstance(detail, dict) and "reason" in detail:
            body = detail
        elif exc.status_code == 404:
            body = {"reason": "not_found"}
        else:
            body = {"reason": "error", "detail": detail}
        _log_api_error(request, exc.status_code, body)
        return JSONResponse(body, status_code=exc.status_code, headers=exc.headers)

    # Порядок важен: `@app.middleware` добавляет слои «последний — самый внешний».
    # Заголовки ставятся ВНЕШНИМ слоем, чтобы попасть и на 503 тумблера, и на 4xx
    # зависимостей — иначе ответ с ошибкой ушёл бы во фрейм Telegram без CSP.
    @app.middleware("http")
    async def _enabled_gate(request: Request, call_next):
        path = request.url.path
        # вебхуки внешних форм должны доходить и при выключенном Mini App — иначе Яндекс
        # получает 503 и бросает интеграцию
        if path == HEALTH_PATH or path.startswith("/app/hooks/"):
            return await call_next(request)
        state = _miniapp_state(cfg.db_path)
        if state == "on":
            return await call_next(request)
        if state == "error" and path in SHELL_PATHS:
            return render_disabled_page(request)
        if _is_shell_asset(path):
            return await call_next(request)  # см. _is_shell_asset -- стили/шрифты оболочки
        if state == "off" and _passes_when_off(request, cfg):
            return await call_next(request)  # выключено только для делегатов
        return JSONResponse({"reason": "miniapp_off"}, status_code=503)

    @app.middleware("http")
    async def _security_headers(request: Request, call_next):
        response = await call_next(request)
        response.headers["Content-Security-Policy"] = CONTENT_SECURITY_POLICY
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        # X-Frame-Options НЕ ставим вовсе — DENY остаётся у дашборда (D-09).
        # JS/CSS оболочки — только с ревалидацией (ETag -> 304): вебвью Telegram без
        # Cache-Control кэшировал модули эвристикой, и после деплоя клиенты неделями
        # исполняли старый app.js (живая приёмка 19-10: фиксы не доезжали до клиента).
        if request.url.path.startswith("/app/static/") or request.url.path in SHELL_PATHS:
            response.headers["Cache-Control"] = "no-cache"
        return response

    for router in ALL_ROUTERS:
        app.include_router(router)

    return app


def create_app(cfg: Optional[DashboardConfig] = None) -> FastAPI:
    """Фабрика для тестов и запуска. Возвращает ASGI-приложение под
    `ProxyHeadersMiddleware`; `fastapi_app` — ссылка на FastAPI-инстанс под middleware
    (для интроспекции маршрутов), как у `dashboard.main.create_app`.

    Квик 260919-u7e (P5): `configure_logging()` — здесь, а не только под
    `uvicorn miniapp.main:app`, чтобы у логгеров модулей был хендлер и в тестах
    (`create_app(cfg=...)` зовётся из `tests/test_miniapp_*`), и при реальном запуске.
    Идемпотентна — повторные вызовы (второй `create_app` в том же процессе) не плодят хендлеры."""
    configure_logging()
    cfg = cfg or load_miniapp_config()
    register_secret(cfg.bot_token)
    inner = _build_asgi_app(cfg)
    wrapped = ProxyHeadersMiddleware(inner, trusted_hosts=cfg.trusted_proxies)
    wrapped.fastapi_app = inner
    return wrapped  # type: ignore[return-value]


try:
    app = create_app()
except RuntimeError as exc:
    # Импорт модуля тестами (они зовут create_app(cfg=...) со своей конфигурацией) не должен
    # падать без DASHBOARD_SESSION_SECRET — падает только реальный `uvicorn miniapp.main:app`.
    logger.warning("miniapp.main: app не создан при импорте модуля (%s)", exc)
    app = None  # type: ignore[assignment]
