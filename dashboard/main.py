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
from dataclasses import replace
from datetime import datetime
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware
from uvicorn.middleware.proxy_headers import ProxyHeadersMiddleware

import web_theme
from dashboard import queries
from dashboard.access import has_stats, staff_city, viewer_scope
from dashboard.auth import session_middleware_kwargs, verify_login_payload
from dashboard.config import DashboardConfig, load_config
from dashboard.db import read_conn
from dashboard.notify import notify_access_request

# D-14: разрезы (сверх городов/тарифа, у которых своя логика показа) — фиксированный порядок
# страницы (источник, вуз, курс, направление, трек), связь колонки/тумблера/подписи. `None`
# в toggle_key означает «разрез не гасится тумблером» (трек в D-19 не заведён).
_BREAKDOWN_CUTS: tuple[tuple[str, str, "str | None"], ...] = (
    ("source", "Источник", "dashboard_block_sources"),
    ("university", "ВУЗ", "dashboard_block_universities"),
    ("course", "Курс", "dashboard_block_courses"),
    ("study_field", "Направление обучения", "dashboard_block_study_fields"),
    ("participant_type", "Трек", None),
    ("payment_option", "Тариф", None),  # гасится payment_enabled внутри queries.breakdown
)

# Квик-задача 260905-iyw: подпись процента воронки должна называть ступень, от которой он
# посчитан (родительный падеж), а не всегда «зашедших» — база сместилась на первую ненулевую
# ступень (см. _funnel_display). Ключи — те же подписи, что и stages в queries.funnel().
_FUNNEL_BASELINE_LABELS: dict[str, str] = {
    "Зашли": "зашедших",
    "Начали анкету": "начавших анкету",
    "Дошли до конца": "дошедших до конца",
    "На модерации": "отправленных на модерацию",
    "Одобрено": "одобренных",
    "Оплатили": "оплативших",
}

logger = logging.getLogger(__name__)

BASE_DIR = Path(__file__).resolve().parent
TEMPLATES_DIR = BASE_DIR / "templates"
STATIC_DIR = BASE_DIR / "static"

templates = Jinja2Templates(directory=str(TEMPLATES_DIR))


def _read_setting(conn, key: str):
    """Сырое чтение ключа реестра по read-only подключению. Намеренно БЕЗ `settings_schema`:
    тот модуль импортирует `database.db` → aiosqlite, которого в slim-образе дашборда нет
    (образ падал `ModuleNotFoundError` на старте, 31.08). Типизация здесь не нужна: единственный
    потребитель — `web_theme.resolve_theme`, он сам проверяет hex/enum и подставляет пресет."""
    row = conn.execute("SELECT value FROM bot_settings WHERE key = ?", (key,)).fetchone()
    return row["value"] if row is not None else None


def _thousands(value) -> str:
    """`1284` → `1 284` (ru-RU: разряды через пробел, без «12.9K» — менеджеру нужна точная
    цифра). Не-числа (`None`, `—`, строки) возвращаются как есть."""
    if isinstance(value, bool) or not isinstance(value, int):
        return value
    return "{:,}".format(value).replace(",", " ")


templates.env.filters["thousands"] = _thousands


def _bar_rows(rows: list[tuple[str, int]]) -> list[dict]:
    """Пары (подпись, число) → готовые для рендера строки с процентом ширины бара
    относительно максимума в наборе (T-15-05-02: значения экранируются самим Jinja2 при
    рендере, здесь только числа)."""
    if not rows:
        return []
    top = max(count for _, count in rows) or 1
    return [
        {"label": label, "count": count, "pct": round(count / top * 100, 1)}
        for label, count in rows
    ]


def _funnel_display(rows: "list[tuple[str, int]] | None", tracking_since: "str | None" = None) -> "dict | None":
    """`None` — блок выключен тумблером (D-19: блока нет вовсе). Иначе — ширина бара каждой
    ступени относительно ПЕРВОЙ НЕНУЛЕВОЙ ступени (не обязательно rows[0] — в городе, где
    «Зашли» пусто, но «Начали анкету» уже есть данные, база — вторая ступень; иначе процент
    был бы неопределён/0% там, где данные реально есть), плюс `has_data`, чтобы шаблон мог
    показать заглушку «пока нет данных» вместо нулевой графики.

    `baseline_label` — родительный падеж подписи ступени, ставшей базой (для строки
    «N% от {baseline_label}» в шаблоне) — без него подпись всегда врала бы «от зашедших»,
    даже когда база сместилась. `since` — дата начала трекинга событий в формате ДД.ММ для
    подписи «с ДД.ММ — по событиям бота»; `None`, если трекинга ещё нет или `ts` не парсится
    (сбой формата не должен ронять страницу)."""
    if rows is None:
        return None
    baseline_label = "зашедших"
    baseline = 0
    for label, count in rows:
        if count:
            baseline = count
            baseline_label = _FUNNEL_BASELINE_LABELS.get(label, "зашедших")
            break
    has_data = any(count for _, count in rows)
    steps = [
        {
            "label": label,
            "count": count,
            "pct": round(count / baseline * 100, 1) if baseline else 0,
        }
        for label, count in rows
    ]
    since = None
    if tracking_since:
        try:
            since = datetime.strptime(tracking_since, "%Y-%m-%d %H:%M:%S").strftime("%d.%m")
        except ValueError:
            since = None
    return {"steps": steps, "has_data": has_data, "baseline_label": baseline_label, "since": since}


def _city_label(conn, code: "str | None") -> "str | None":
    if not code:
        return None
    row = conn.execute("SELECT label FROM cities WHERE code = ?", (code,)).fetchone()
    return row["label"] if row is not None else code


def build_page_context(conn, cfg: DashboardConfig, scope: queries.Scope, viewer: dict) -> dict:
    """Собирает ВЕСЬ контекст страницы одним вызовом — шаблон сам не зовёт БД (D-16: на лету
    на каждый запрос, без кэша). Каждый блок гасится своим тумблером `dashboard_block_*`
    (D-19: выключен — блока нет вовсе, а не пустая карточка) — сама заглушка «пока нет
    данных» для включённого, но пустого блока рисуется в шаблоне по `has_data`/пустому списку.
    """
    flags = queries.dashboard_flags(conn)

    funnel_rows = queries.funnel(conn, scope) if flags.get("dashboard_block_funnel") == "on" else None
    funnel_since = queries.funnel_tracking_since(conn) if funnel_rows is not None else None
    daily_rows = (
        queries.daily_registrations(conn, scope)
        if flags.get("dashboard_block_dynamics") == "on"
        else None
    )
    dropout_rows = (
        queries.dropout_steps(conn, scope) if flags.get("dashboard_block_dropout") == "on" else None
    )

    city_options = queries.city_options(conn)
    bound_city_code = viewer.get("bound_city")
    # D-15: свитчер городов — только когда модуль городов включён И зритель не привязан к
    # своему городу (у привязанного менеджера — статичная подпись, без выбора, D-10).
    show_city_switcher = bool(city_options) and not bound_city_code
    city_cut = None
    if city_options and scope.city is None:
        # «Все города» — сравнение городов рядом (семантика render_stats_text), а не разрез
        # одного из них.
        city_cut = queries.city_comparison(conn, scope)

    cuts: list[dict] = []
    for column, title, toggle_key in _BREAKDOWN_CUTS:
        if toggle_key is not None and flags.get(toggle_key) != "on":
            continue
        if column == "payment_option" and flags.get("payment_enabled") != "on":
            continue  # тариф — только при оплате (D-14), тумблера у него нет
        rows = queries.breakdown(conn, column, scope=scope, limit=10)
        cuts.append({"title": title, "rows": _bar_rows(rows), "has_data": bool(rows)})

    game_stats = queries.game_block(conn, scope) if flags.get("dashboard_block_game") == "on" else None

    daily_chart = None
    if daily_rows is not None and daily_rows:
        # NB: ключ НЕ "values" — у dict есть одноимённый встроенный метод, и Jinja2 сначала
        # пробует getattr (тогда `daily_chart.values` вернёт bound-метод, а не список).
        daily_chart = {
            "labels": [day for day, _ in daily_rows],
            "counts": [count for _, count in daily_rows],
        }

    return {
        "event_name": flags.get("event_name"),
        "event_season": scope.season or flags.get("event_season"),
        "viewer": viewer,
        "scope": scope,
        "city_options": city_options,
        "show_city_switcher": show_city_switcher,
        "bound_city_label": _city_label(conn, bound_city_code),
        "season_options": queries.season_options(conn),
        "kpi": queries.kpi_row(conn, scope),
        "funnel": _funnel_display(funnel_rows, funnel_since),
        "dynamics_enabled": daily_rows is not None,
        "daily_chart": daily_chart,
        "city_cut": (
            {"rows": city_cut} if city_cut is not None else None
        ),
        "cuts": cuts,
        "dropout": (
            {"rows": _bar_rows(dropout_rows), "has_data": bool(dropout_rows)}
            if dropout_rows is not None
            else None
        ),
        "game": game_stats,
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

    @app.get("/theme.css")
    def theme_css() -> Response:
        # Phase 19.1-02 (D-03): тот же движок, что `/app/theme.css` Mini App — resolve_theme
        # читает ручки пресета из bot_settings, theme_css_text собирает CSS. Дашборд саму
        # атрибут-ветку `[data-theme="dark"]` никогда не активирует (D-06: остаётся светлым
        # при любой системной теме) — блок в CSS присутствует, но мёртв на этой поверхности.
        with read_conn(cfg.db_path) as conn:
            settings = {key: _read_setting(conn, key) for key in web_theme.THEME_KEYS.values()}
        resolved = web_theme.resolve_theme(settings)
        body = web_theme.theme_css_text(resolved)
        return Response(body, media_type="text/css", headers={"Cache-Control": "no-cache"})

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
        # не кэшируется — каждый защищённый маршрут пересверяет has_stats заново.
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
                    request,
                    "no_access.html",
                    {"bot_username": cfg.bot_username},
                    status_code=403,
                )

            # D-13: сезон резолвится отдельно от города — viewer_scope закрывает только
            # городскую ось (D-10), season из query подставляется поверх её результата.
            scope = replace(viewer_scope(conn, telegram_id, cfg.admin_ids, city), season=season)
            # D-10: привязка к городу — источник правды для «свитчер показывать или нет»,
            # а НЕ просто scope.city (тот совпадает со своим городом и у привязанного, и у
            # свободного зрителя, выбравшего конкретный город из списка).
            viewer = {
                "telegram_id": telegram_id,
                "bound_city": staff_city(conn, telegram_id),
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
