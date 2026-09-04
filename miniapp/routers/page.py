"""Phase 19 (расширено 19.1-02, D-03/D-04/D-07): оболочка и служебные маршруты — `/app`,
`/app/theme.css`, `/app/health`, `/app/api/me`.

`/app` — единственная Jinja-страница приложения (`templates/app.html`): шапка с названием
мероприятия и лого, контейнеры `#screen`/`#nav`, тексты экранов состояний из реестра в
data-атрибутах `<body>` (фронт-ядро читает их без отдельного запроса — экран «Откройте через
бота» нужен именно тогда, когда API недоступно без initData). При `miniapp_enabled = off`
тот же шаблон отдаёт человеку страницу-объяснение 503, а не JSON (гасящий middleware в
`miniapp.main` делает исключение для HTML-маршрута).

`/app/theme.css` — полный набор ручек активного пресета (акцент/вторичный/фон/шрифт
заголовков + осветлённая тёмная пара) из `web_theme.resolve_theme`/`theme_css_text` (D-03).
Отдельный маршрут вместо inline-`style` — чтобы CSP осталась без `'unsafe-inline'`. Значения
проверяются в `web_theme` дважды (при разрешении ручки и при сборке CSS), иначе молча
дефолт активного пресета (T-19-73/T-19.1-05: значение никогда не склеивается с разметкой;
fail-soft: опечатка менеджера не роняет приложение).

`/app/health` открыт и не трогает БД (HEALTHCHECK контейнера) — единственный маршрут,
живущий и при `miniapp_enabled = off`.

`/app/api/me` — первый запрос фронта: кто я, какие разделы включены, оформление (пресет,
тон, ассеты).
"""
from __future__ import annotations

import json
from pathlib import Path

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates

from dashboard.db import read_conn
from reg_labels import STATUS_LABELS
from settings_schema import SETTINGS_SCHEMA

import web_theme
from miniapp.deps import (
    SECTIONS, Principal, delegate_denial, form_access_denial, form_status, principal, read_setting,
)

router = APIRouter()

BASE_DIR = Path(__file__).resolve().parent.parent
TEMPLATES_DIR = BASE_DIR / "templates"
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))

# Тексты экранов состояний: имя в контексте шаблона -> ключ реестра.
STATE_TEXT_KEYS = {
    "open_in_bot": "miniapp_open_in_bot_text",
    "login_button": "miniapp_login_button",
    "login_hint": "miniapp_login_hint",
    "session_expired": "miniapp_session_expired_text",
    "no_access": "miniapp_no_access_text",
    "disabled": "miniapp_disabled_text",
}

# Phase 23 план 05 (D-25, зеркало STATE_TEXT_KEYS/section_labels): подписи экрана «📋 Заявки»,
# которых НЕТ в ответе `GET /app/api/applications/next` (тот отдаёт только то, что зависит от
# карточки/фильтра — `filters.chips`/`reject_templates`/`empty_text`). Читаются один раз при
# рендере оболочки — тот же приём, что `section_labels`/`STATE_TEXT_KEYS`. Task 2 — подписи
# карточки; Task 3 — шторка отказа/тост отмены/«Принять всех».
APPLICATIONS_TEXT_KEYS = {
    "show_all": "miniapp_applications_show_all",
    "approve_button": "miniapp_applications_approve_button",
    "reject_button": "miniapp_applications_reject_button",
    "resume_open": "miniapp_applications_resume_open",
    "resume_none": "miniapp_applications_resume_none",
    "history_label": "miniapp_applications_history_label",
    "undo_button": "miniapp_applications_undo_button",
    "approved_toast": "miniapp_applications_approved_toast",
    "rejected_toast": "miniapp_applications_rejected_toast",
    "undone_toast": "miniapp_applications_undone_toast",
    "undo_too_late": "miniapp_applications_undo_too_late",
    "approve_all_confirm": "miniapp_applications_approve_all_confirm",
    "approve_all_button": "miniapp_applications_approve_all_button",
    "reject_no_reason": "miniapp_applications_reject_no_reason",
    "reject_own_reason": "miniapp_applications_reject_own_reason",
    "reject_cancel": "miniapp_applications_reject_cancel",
}


def deep_link(bot_username: str | None) -> str:
    return f"https://t.me/{bot_username}?start=app" if bot_username else ""


def section_labels() -> dict[str, str]:
    """Подписи вкладок навигации — из label чекбоксов реестра (0 хардкода в JS)."""
    return {s: SETTINGS_SCHEMA[f"miniapp_section_{s}"]["label"] for s in SECTIONS}


# Кэш-бастинг статики (находка живой приёмки 19-10): вебвью Telegram хранит JS-модули по URL
# и не ревалидирует их эвристический срок — деплой не доезжал до открытых клиентов. Версия в
# самом ПУТИ меняет URL всех модулей разом (относительные import'ы внутри JS резолвятся под
# тем же префиксом), поэтому свежая сборка качается сразу. Версия = max(mtime) файлов статики,
# считается один раз на старте процесса; легаси-путь /app/static/ остаётся смонтированным.
_STATIC_ASSETS_DIR = Path(__file__).resolve().parent.parent / "static"


def _static_version() -> str:
    try:
        return str(max(int(f.stat().st_mtime) for f in _STATIC_ASSETS_DIR.rglob("*") if f.is_file()))
    except (ValueError, OSError):
        return "0"


STATIC_PREFIX = f"/app/static/v{_static_version()}"


def _shell_context(request: Request, conn) -> dict:
    cfg = request.app.state.cfg
    return {
        "static_prefix": STATIC_PREFIX,
        "section_labels": json.dumps(section_labels(), ensure_ascii=False),
        "event_name": read_setting(conn, "event_name"),
        "bot_username": cfg.bot_username,
        "deep_link": deep_link(cfg.bot_username),
        "logo_file_id": read_setting(conn, "miniapp_logo"),
        "sections": [s for s in SECTIONS if read_setting(conn, f"miniapp_section_{s}") == "on"],
        "texts": {name: read_setting(conn, key) or "" for name, key in STATE_TEXT_KEYS.items()},
        "applications_texts": json.dumps(
            {name: read_setting(conn, key) or "" for name, key in APPLICATIONS_TEXT_KEYS.items()},
            ensure_ascii=False,
        ),
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
            "static_prefix": STATIC_PREFIX,
            "event_name": None, "bot_username": cfg.bot_username,
            "deep_link": deep_link(cfg.bot_username), "logo_file_id": None, "sections": [],
            "section_labels": "{}",
            "texts": {name: "" for name in STATE_TEXT_KEYS},
            "applications_texts": "{}",
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


@router.get("/app/", include_in_schema=False)
def shell_trailing_slash(request: Request) -> RedirectResponse:
    """`/app/` (со слэшем) -> `/app`. Приложение живёт с `redirect_slashes=False` (иначе
    Starlette редиректил бы и API), поэтому слэш-вариант оболочки нужно объявить руками —
    без него человек, набравший адрес сам, получал JSON 404 (находка 3 приёмки 19-10).
    308 — постоянный редирект с сохранением метода; query переносится сервером, фрагмент
    `#tgWebAppData=…` браузер переносит на редиректе сам (сервер его не видит)."""
    target = "/app" + (f"?{request.url.query}" if request.url.query else "")
    return RedirectResponse(url=target, status_code=308)


@router.get("/app/theme.css")
def theme_css(request: Request) -> Response:
    cfg = request.app.state.cfg
    with read_conn(cfg.db_path) as conn:
        settings = {key: read_setting(conn, key) for key in web_theme.THEME_KEYS.values()}
    resolved = web_theme.resolve_theme(settings)
    body = web_theme.theme_css_text(resolved)
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
        # D-24/D-08: сервер решает, «открывать ли приложение на анкете» — JS только исполняет.
        # form_access считается той же функцией, что и form_gate (T-21-34).
        status = form_status(conn, p.telegram_id)
        form_access = form_access_denial(conn, p) is None
        # Quick 260904-aup (UAT D11 + Q4): сервер решает, кому вообще показывать привет-экран
        # (тот же принцип, что form_first строкой выше) — сотрудник и делегат с уже поданной
        # анкетой видели его снова на новом устройстве/после очистки localStorage, потому что
        # клиент решал это сам (только по localStorage). Здесь ТОЛЬКО «кому положено» —
        # «показывали ли уже на этом устройстве» по-прежнему решает localStorage в hub.js.
        show_onboarding = not p.is_staff and status in ("none", "draft") and form_access
        theme_settings = {key: read_setting(conn, key) for key in web_theme.THEME_KEYS.values()}
        assets = {name: read_setting(conn, key) for name, key in web_theme.ASSET_KEYS.items()}
        onboarding_text = read_setting(conn, "miniapp_onboarding_text") or ""
        onboarding_cta = read_setting(conn, "miniapp_onboarding_cta") or ""
        # Phase 23.1-03 (UI-REDESIGN-03): герой и шаги «как это работает» привет-экрана.
        onboarding_hero = read_setting(conn, "miniapp_onboarding_hero") or ""
        onboarding_steps_title = read_setting(conn, "miniapp_onboarding_steps_title") or ""
        onboarding_steps = read_setting(conn, "miniapp_onboarding_steps") or ""
        # Quick 260903: подпись плитки «Дашборд» — из реестра, адрес — из деплойного cfg
        # (см. dashboard_url ниже), не из bot_settings (D-05/D-19).
        dashboard_tile_label = read_setting(conn, "miniapp_tile_dashboard_label") or ""
    resolved = web_theme.resolve_theme(theme_settings)
    # Плитка «Дашборд» хаба менеджера: адрес виден только staff (is_staff — любое право),
    # и только когда деплой задал `DASHBOARD_PUBLIC_URL` (cfg.public_url) — тот же источник,
    # что у кнопки «🌐 Открыть дашборд» в боте (handlers/admin.py::_stats_keyboard_for).
    dashboard_url = cfg.public_url if (p.is_staff and cfg.public_url) else None
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
        # Анкета (gap closure фазы 21, D-24): статус заявки/черновика, доступ к экрану анкеты
        # и признак «дом приложения — анкета» (нет заявки или незаконченный черновик новой).
        # `not p.caps` (находка 21-13): менеджер без своей заявки (`caps` не пустой, строки
        # `users` нет — статус тоже "none") всегда попадает в хаб, а не на чужую для него
        # анкету; плитка «Анкета» остаётся видна через `form_access`.
        "form_status": status,
        "form_status_label": STATUS_LABELS.get(status, ""),
        "form_access": form_access,
        "form_first": form_access and sections["form"] and status in ("none", "draft") and not p.caps,
        "show_onboarding": show_onboarding,
        # Оформление (D-03/D-04/D-08/D-15/D-16) — новые поля, старые выше НЕ переименованы.
        "theme_preset": resolved["preset"],
        "playful_tone": resolved["playful_tone"] == "on",
        # Хаб (план 19.1-04, D-09): бренд-паттерн hero (только motion "full" + разрешение
        # пресета/кастома) и тексты приветственного экрана — 0 хардкода в hub.js.
        "pattern_enabled": resolved["pattern_enabled"] == "on",
        "onboarding_text": onboarding_text,
        "onboarding_cta": onboarding_cta,
        "onboarding_hero": onboarding_hero,
        "onboarding_steps_title": onboarding_steps_title,
        "onboarding_steps": onboarding_steps,
        "dashboard_url": dashboard_url,
        "dashboard_tile_label": dashboard_tile_label,
        **assets,
    }
