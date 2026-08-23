"""Phase 19: оболочка и служебные маршруты — `/app`, `/app/theme.css`, `/app/health`,
`/app/api/me`.

`/app` — единственная Jinja-страница приложения (`templates/app.html`): шапка с названием
мероприятия и лого, контейнеры `#screen`/`#nav`, тексты экранов состояний из реестра в
data-атрибутах `<body>` (фронт-ядро читает их без отдельного запроса — экран «Откройте через
бота» нужен именно тогда, когда API недоступно без initData). При `miniapp_enabled = off`
тот же шаблон отдаёт человеку страницу-объяснение 503, а не JSON (гасящий middleware в
`miniapp.main` делает исключение для HTML-маршрута).

`/app/theme.css` — одна строка `:root { --accent; --secondary }` из `miniapp_accent` (D-06).
Отдельный маршрут вместо inline-`style` — чтобы CSP осталась без `'unsafe-inline'`. Значение
проверяется regex `^#[0-9A-Fa-f]{6}$`, иначе молча дефолт (T-19-73: значение никогда не
склеивается с разметкой; fail-soft: опечатка менеджера не роняет приложение).

`/app/health` открыт и не трогает БД (HEALTHCHECK контейнера) — единственный маршрут,
живущий и при `miniapp_enabled = off`.

`/app/api/me` — первый запрос фронта: кто я, какие разделы включены, оформление.
"""
from __future__ import annotations

import re
from pathlib import Path

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, Response
from fastapi.templating import Jinja2Templates

from dashboard.db import read_conn

from miniapp.deps import SECTIONS, Principal, delegate_denial, principal, read_setting

router = APIRouter()

BASE_DIR = Path(__file__).resolve().parent.parent
TEMPLATES_DIR = BASE_DIR / "templates"
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))

DEFAULT_ACCENT = "#037EF3"
_HEX6 = re.compile(r"^#[0-9A-Fa-f]{6}$")

# Тексты экранов состояний: имя в контексте шаблона -> ключ реестра.
STATE_TEXT_KEYS = {
    "open_in_bot": "miniapp_open_in_bot_text",
    "login_button": "miniapp_login_button",
    "login_hint": "miniapp_login_hint",
    "session_expired": "miniapp_session_expired_text",
    "no_access": "miniapp_no_access_text",
    "disabled": "miniapp_disabled_text",
}


def safe_accent(value) -> str:
    """`miniapp_accent` из реестра -> гарантированно валидный `#rrggbb`."""
    text = (value or "").strip() if isinstance(value, str) else ""
    return text if _HEX6.match(text) else DEFAULT_ACCENT


def deep_link(bot_username: str | None) -> str:
    return f"https://t.me/{bot_username}?start=app" if bot_username else ""


def _shell_context(request: Request, conn) -> dict:
    cfg = request.app.state.cfg
    return {
        "event_name": read_setting(conn, "event_name"),
        "bot_username": cfg.bot_username,
        "deep_link": deep_link(cfg.bot_username),
        "logo_file_id": read_setting(conn, "miniapp_logo"),
        "sections": [s for s in SECTIONS if read_setting(conn, f"miniapp_section_{s}") == "on"],
        "texts": {name: read_setting(conn, key) or "" for name, key in STATE_TEXT_KEYS.items()},
    }


def render_disabled_page(request: Request) -> HTMLResponse:
    """503 для человека: оболочка без ядра с текстом `miniapp_disabled_text`.
    Зовётся гасящим middleware `miniapp.main` на HTML-маршруте."""
    cfg = request.app.state.cfg
    try:
        with read_conn(cfg.db_path) as conn:
            context = _shell_context(request, conn)
    except Exception:  # noqa: BLE001 — БД недоступна: дефолт реестра и пустая шапка
        context = {
            "event_name": None, "bot_username": cfg.bot_username,
            "deep_link": deep_link(cfg.bot_username), "logo_file_id": None, "sections": [],
            "texts": {name: "" for name in STATE_TEXT_KEYS},
        }
    context["disabled_text"] = (
        context["texts"].get("disabled") or "Приложение временно недоступно."
    )
    return templates.TemplateResponse(request, "app.html", context, status_code=503)


@router.get("/app", response_class=HTMLResponse)
def shell(request: Request):
    cfg = request.app.state.cfg
    with read_conn(cfg.db_path) as conn:
        context = _shell_context(request, conn)
    return templates.TemplateResponse(request, "app.html", context)


@router.get("/app/theme.css")
def theme_css(request: Request) -> Response:
    cfg = request.app.state.cfg
    with read_conn(cfg.db_path) as conn:
        accent = safe_accent(read_setting(conn, "miniapp_accent"))
    body = f":root {{ --accent: {accent}; --secondary: {accent}; }}\n"
    return Response(body, media_type="text/css", headers={"Cache-Control": "no-cache"})


@router.get("/app/health")
def health() -> dict:
    return {"status": "ok"}


@router.get("/app/api/me")
def me(request: Request, p: Principal = Depends(principal)) -> dict:
    cfg = request.app.state.cfg
    with read_conn(cfg.db_path) as conn:
        sections = {s: read_setting(conn, f"miniapp_section_{s}") == "on" for s in SECTIONS}
        accent = read_setting(conn, "miniapp_accent")
        event_name = read_setting(conn, "event_name")
        logo_file_id = read_setting(conn, "miniapp_logo")
        is_delegate = delegate_denial(conn, p) is None
    return {
        "telegram_id": p.telegram_id,
        "via": p.via,
        "caps": sorted(p.caps),
        "city": p.city,
        "is_delegate": is_delegate,
        "is_staff": p.is_staff,
        "sections": sections,
        "accent": accent,
        "event_name": event_name,
        "logo_file_id": logo_file_id,
        "bot_username": cfg.bot_username,
    }
