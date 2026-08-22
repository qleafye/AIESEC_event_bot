"""Phase 15 Plan 05 (STAT-01/STAT-02, D-03/D-09/D-11/D-16/D-17): FastAPI-приложение
дашборда — маршруты, работа за Cloudflare Tunnel, модель доступа на КАЖДЫЙ запрос.

Работа за Cloudflare Tunnel (D-03, пересмотр 22.08): порт наружу не публикуется вовсе
(план 15-06) — единственный источник запроса к origin — контейнер cloudflared из
docker-сети `edge`. Самый внешний слой приложения — `ProxyHeadersMiddleware` с доверенной
подсетью `cfg.trusted_proxies`: он подменяет `scope["scheme"]`/`scope["client"]` по
`X-Forwarded-Proto`/`X-Forwarded-For`, но ТОЛЬКО если запрос пришёл с доверенного адреса —
именно поэтому список доверия задаётся подсетью, а не флагом uvicorn `--proxy-headers`
(его дефолт доверия — `127.0.0.1`, адрес cloudflared другой).

Все абсолютные ссылки наружу (redirect-цель Login Widget) строятся от `cfg.public_url`,
НИКОГДА от `request.url` — так они остаются `https://`, даже если middleware по какой-то
причине не сработал.

Маршрут `/app` НЕ занят (D-02, зарезервирован под Mini App Phase 19). Маршрутов
экспорта/CSV нет вовсе (D-17). `/docs`/`/openapi.json` отключены — дашборд смотрит на базу
с ПД, автодокументация FastAPI не должна быть публичной.
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware
from uvicorn.middleware.proxy_headers import ProxyHeadersMiddleware

from dashboard import queries
from dashboard.access import has_stats, viewer_scope
from dashboard.auth import session_middleware_kwargs, verify_login_payload
from dashboard.config import DashboardConfig, load_config
from dashboard.db import read_conn
from dashboard.notify import notify_access_request

logger = logging.getLogger(__name__)

BASE_DIR = Path(__file__).resolve().parent
TEMPLATES_DIR = BASE_DIR / "templates"
STATIC_DIR = BASE_DIR / "static"

templates = Jinja2Templates(directory=str(TEMPLATES_DIR))


def build_page_context(conn, cfg: DashboardConfig, scope: queries.Scope, viewer: dict) -> dict:
    """Собирает весь контекст страницы одним вызовом, чтобы шаблон сам не звал БД (D-16:
    считаем на лету на каждый запрос, без кэша). Task 3 разворачивает это до полного набора
    блоков (воронка/динамика/разрезы/«где бросают»/гейма) — здесь уже готовы `event_name` и
    флаги тумблеров, которыми Task 3 управляет видимостью блоков."""
    flags = queries.dashboard_flags(conn)
    return {
        "event_name": flags.get("event_name"),
        "event_season": flags.get("event_season"),
        "flags": flags,
        "viewer": viewer,
        "scope": scope,
        "city_options": queries.city_options(conn),
        "season_options": queries.season_options(conn),
    }


def _build_asgi_app(cfg: DashboardConfig) -> FastAPI:
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None, redirect_slashes=False)
    app.state.cfg = cfg

    if STATIC_DIR.is_dir():
        app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

    app.add_middleware(SessionMiddleware, **session_middleware_kwargs(cfg))

    @app.middleware("http")
    async def _security_headers(request: Request, call_next):
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["X-Frame-Options"] = "DENY"
        return response

    @app.get("/health")
    def health() -> dict:
        # Открыт, без обращения к БД — только для HEALTHCHECK контейнера.
        return {"status": "ok"}

    @app.get("/login", response_class=HTMLResponse)
    def login_page(request: Request):
        return templates.TemplateResponse(
            request,
            "login.html",
            {
                "public_url": cfg.public_url,
                "bot_username": cfg.bot_username,
            },
        )

    @app.get("/auth/callback")
    def auth_callback(request: Request):
        telegram_id = verify_login_payload(dict(request.query_params), cfg.bot_token)
        if telegram_id is None:
            # Человеческая страница без деталей проверки подписи в теле ответа.
            return HTMLResponse(
                "<!doctype html><html lang=\"ru\"><body>"
                "<p>Не удалось подтвердить вход через Telegram. "
                "Попробуйте войти ещё раз со страницы входа.</p>"
                "</body></html>",
                status_code=400,
            )
        # D-09: в сессию — ТОЛЬКО telegram_id (плюс имя/username для приветствия). Набор прав
        # не кэшируется — каждый защищённый маршрут пересверяет his_stats заново.
        request.session["telegram_id"] = telegram_id
        request.session["username"] = request.query_params.get("username")
        request.session["first_name"] = request.query_params.get("first_name")
        return RedirectResponse(url="/", status_code=302)

    @app.get("/logout")
    def logout(request: Request):
        request.session.clear()
        return RedirectResponse(url="/login", status_code=302)

    @app.get("/", response_class=HTMLResponse)
    def dashboard_page(
        request: Request,
        city: Optional[str] = None,
        season: Optional[str] = None,
    ):
        telegram_id = request.session.get("telegram_id")
        if telegram_id is None:
            return RedirectResponse(url="/login", status_code=302)

        with read_conn(cfg.db_path) as conn:
            # D-09: пересверка права на КАЖДЫЙ запрос, без кэша — снятое в боте право
            # закрывает доступ уже на следующем открытии страницы, без перелогина.
            if not has_stats(conn, telegram_id, cfg.admin_ids):
                notify_access_request(
                    cfg,
                    telegram_id=telegram_id,
                    username=request.session.get("username"),
                    first_name=request.session.get("first_name"),
                )
                return templates.TemplateResponse(
                    request, "no_access.html", {}, status_code=403
                )

            scope = viewer_scope(conn, telegram_id, cfg.admin_ids, city)
            viewer = {
                "telegram_id": telegram_id,
                "bound_city": scope.city if scope.city else None,
            }
            context = build_page_context(conn, cfg, scope, viewer)

        return templates.TemplateResponse(request, "dashboard.html", context)

    return app


def create_app(cfg: Optional[DashboardConfig] = None) -> FastAPI:
    """Фабрика для тестов (своя конфигурация/временная БД). Возвращает ASGI-приложение,
    самым внешним слоем которого является `ProxyHeadersMiddleware` (D-03) — без него
    заголовки `X-Forwarded-*` не могли бы прийти вообще ни от кого, а с флагом «доверять
    всем» дашборд слепо доверял бы подделанной схеме/адресу от кого угодно, кто дотянется
    до origin в обход туннеля.

    `fastapi_app` — ссылка на само FastAPI-приложение ПОД middleware, для тестов/интроспекции
    маршрутов (список путей и т.п.) — сам возврат этой функции больше не FastAPI-инстанс.
    """
    cfg = cfg or load_config()
    inner = _build_asgi_app(cfg)
    wrapped = ProxyHeadersMiddleware(inner, trusted_hosts=cfg.trusted_proxies)
    wrapped.fastapi_app = inner
    return wrapped  # type: ignore[return-value]


try:
    app = create_app()
except RuntimeError as exc:
    # Импорт модуля (напр. тестами, которые зовут create_app(cfg=...) явно со своей
    # временной конфигурацией) не должен падать из-за отсутствующего DASHBOARD_SESSION_SECRET
    # в окружении процесса — падать обязан только реальный запуск `uvicorn dashboard.main:app`
    # без .env, и тогда сообщение будет точно тем же (см. dashboard.config.load_config).
    logger.warning("dashboard.main: app не создан при импорте модуля (%s)", exc)
    app = None  # type: ignore[assignment]
