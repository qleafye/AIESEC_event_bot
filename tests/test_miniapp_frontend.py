"""Phase 19 Plan 02 (WEBAPP-01, D-02/D-05/D-06, T-19-07/T-19-73/T-19-74/T-19-75/T-19-76):
оболочка Mini App и фронт-ядро — сторожа по файлам + `TestClient`.

Файл создан планом 19-02 и дополняется всеми последующими планами фазы (экраны).

- `miniapp/static/tokens.css` — побайтная копия `dashboard/static/tokens.css` (дрейф токенов
  ловится здесь, пока в образе нет симлинка).
- Литеральные цвета — только в `tokens.css`; шаблон и остальные css — через var(--…).
- Единственный внешний URL во всём `miniapp/templates` и `miniapp/static` —
  `https://telegram.org/js/telegram-web-app.js?63`. Google Fonts и CDN запрещены
  (CSP `font-src 'self'`, T-19-74).
- `/app/theme.css` подставляет `miniapp_accent`, мусор -> дефолт `#037EF3` (T-19-73).
- Выключенный тумблер на `/app` -> 503 с человеческим текстом из реестра, не JSON.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest
from starlette.testclient import TestClient

from miniapp.main import create_app

from tests.test_miniapp_routes import (
    ADMIN_ID,
    _cfg,
    _hdr,
    _seed,
    _set,
    _standard_seed,
    _use_tmp_db,
)

ROOT = Path(__file__).resolve().parent.parent
DASHBOARD_TOKENS = ROOT / "dashboard" / "static" / "tokens.css"
MINIAPP_DIR = ROOT / "miniapp"
MINIAPP_STATIC = MINIAPP_DIR / "static"
MINIAPP_TEMPLATES = MINIAPP_DIR / "templates"
MINIAPP_TOKENS = MINIAPP_STATIC / "tokens.css"
APP_HTML = MINIAPP_TEMPLATES / "app.html"
APP_JS = MINIAPP_STATIC / "js" / "app.js"
API_JS = MINIAPP_STATIC / "js" / "api.js"
SCREENS_DIR = MINIAPP_STATIC / "js" / "screens"

TELEGRAM_SDK_URL = "https://telegram.org/js/telegram-web-app.js?63"

# Как в tests/test_dashboard_render.py: #rrggbb/#rgb или rgb(/rgba( — вне tokens.css запрещено.
_HEX_OR_RGB_COLOR = re.compile(r"#[0-9a-fA-F]{3,8}\b|rgba?\(")
_URL = re.compile(r"https?://[^\s\"'`>)]+")
# XML-неймспейс SVG (D-13, icons.js/document.createElementNS) — константа спецификации DOM,
# никогда не фетчится сетью; не считается «внешним URL» для этого сторожа.
_ALLOWED_NON_FETCH_URLS = {"http://www.w3.org/2000/svg"}


def _frontend_files():
    files = [p for p in MINIAPP_TEMPLATES.rglob("*") if p.is_file()]
    files += [p for p in MINIAPP_STATIC.rglob("*") if p.is_file() and p.suffix in {".css", ".js", ".html"}]
    return files


def _client(db_path: str) -> TestClient:
    return TestClient(create_app(cfg=_cfg(db_path)), base_url="https://testserver")


# ── токены и цвета ───────────────────────────────────────────────────────────────────────

def test_tokens_css_is_byte_for_byte_copy_of_dashboard_tokens():
    assert MINIAPP_TOKENS.read_bytes() == DASHBOARD_TOKENS.read_bytes(), (
        "miniapp/static/tokens.css разошёлся с dashboard/static/tokens.css — скопируйте заново"
    )


def test_no_hardcoded_colors_outside_tokens_css():
    for path in _frontend_files():
        if path == MINIAPP_TOKENS:
            continue
        matches = _HEX_OR_RGB_COLOR.findall(path.read_text(encoding="utf-8"))
        assert not matches, f"hardcoded color literal in {path.relative_to(ROOT)}: {matches}"


def test_only_external_url_is_telegram_web_app_sdk():
    found = set()
    for path in _frontend_files():
        for url in _URL.findall(path.read_text(encoding="utf-8")):
            if url in _ALLOWED_NON_FETCH_URLS:
                continue
            found.add(url)
            assert url == TELEGRAM_SDK_URL, f"unexpected external URL in {path.relative_to(ROOT)}: {url}"
    assert found == {TELEGRAM_SDK_URL}


def test_shell_links_sdk_before_module_and_no_inline_style():
    text = APP_HTML.read_text(encoding="utf-8")
    assert text.index(TELEGRAM_SDK_URL) < text.index("/js/app.js")
    assert 'type="module"' in text
    assert "/app/theme.css" in text
    assert "style=" not in text and "<style" not in text, "CSP без 'unsafe-inline' — стили только файлами"
    assert "fonts.googleapis.com" not in text


# ── GET /app ─────────────────────────────────────────────────────────────────────────────

def test_shell_renders_with_event_name_sdk_and_deep_link(tmp_path):
    db_path = _use_tmp_db(tmp_path, "miniapp_front.db")
    _standard_seed()
    resp = _client(db_path).get("/app")
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/html")
    assert "форума YouLead" in resp.text
    assert TELEGRAM_SDK_URL in resp.text
    assert 'data-deep-link="https://t.me/YouLead_test_bot?start=app"' in resp.text
    assert "/app/api/file/" not in resp.text  # лого не задано — блок не рисуется
    assert "Content-Security-Policy" in resp.headers


def test_shell_renders_logo_only_when_set(tmp_path):
    db_path = _use_tmp_db(tmp_path, "miniapp_front_logo.db")
    _standard_seed()
    _set("miniapp_logo", "AgACAgIAAxkBAAI")
    resp = _client(db_path).get("/app")
    assert resp.status_code == 200
    assert '/app/api/file/AgACAgIAAxkBAAI' in resp.text


def test_shell_escapes_registry_texts(tmp_path):
    db_path = _use_tmp_db(tmp_path, "miniapp_front_xss.db")
    _standard_seed()
    _set("miniapp_open_in_bot_text", '<script>alert(1)</script>')
    resp = _client(db_path).get("/app")
    assert "<script>alert(1)</script>" not in resp.text
    assert "&lt;script&gt;" in resp.text


def test_shell_disabled_returns_human_page_503(tmp_path):
    db_path = _use_tmp_db(tmp_path, "miniapp_front_off.db")
    _seed(settings={"miniapp_enabled": "off", "miniapp_disabled_text": "Ушли на обед, всё в боте."})
    resp = _client(db_path).get("/app")
    assert resp.status_code == 503
    assert resp.headers["content-type"].startswith("text/html")
    assert "Ушли на обед, всё в боте." in resp.text
    assert '"reason"' not in resp.text
    assert "/js/app.js" not in resp.text  # ядро не грузится на заглушке
    # API при этом по-прежнему JSON.
    assert _client(db_path).get("/app/api/me", headers=_hdr(ADMIN_ID)).json() == {"reason": "miniapp_off"}


def test_static_served_under_app_prefix(tmp_path):
    db_path = _use_tmp_db(tmp_path, "miniapp_front_static.db")
    _standard_seed()
    resp = _client(db_path).get("/app/static/tokens.css")
    assert resp.status_code == 200
    assert "--accent" in resp.text


# ── /app/theme.css ───────────────────────────────────────────────────────────────────────

def test_theme_css_uses_registry_accent(tmp_path):
    db_path = _use_tmp_db(tmp_path, "miniapp_theme.db")
    _standard_seed()
    _set("miniapp_accent", "#112233")
    _set("miniapp_theme_secondary", "#445566")
    _set("miniapp_theme_bg", "#778899")
    _set("miniapp_theme_heading_font", "lato")
    resp = _client(db_path).get("/app/theme.css")
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/css")
    assert "--accent: #112233;" in resp.text
    assert "--secondary: #445566;" in resp.text
    assert "--bg: #778899;" in resp.text
    assert '--font-heading: "Lato"' in resp.text
    assert "--font-heading-style: normal;" in resp.text


def test_theme_css_garbage_accent_falls_back_to_default(tmp_path):
    db_path = _use_tmp_db(tmp_path, "miniapp_theme_bad.db")
    _standard_seed()
    client = _client(db_path)
    for bad in ("037EF3", "#GG0000", "", "#fff", "#037EF3; } body { background: url(x) }"):
        _set("miniapp_accent", bad)
        _set("miniapp_theme_secondary", bad)
        _set("miniapp_theme_bg", bad)
        resp = client.get("/app/theme.css")
        assert resp.status_code == 200, bad
        # Мусор в ЛЮБОЙ из трёх цветовых ручек -> значение активного пресета (bluebook),
        # не литерал и не пустота (T-19.1-05: не проверяем строку целиком, только объявления).
        assert "--accent: #037EF3;" in resp.text, bad
        assert "--secondary: #F48924;" in resp.text, bad
        assert "--bg: #F3F4F7;" in resp.text, bad


def test_theme_css_dark_block_has_lightened_accent(tmp_path):
    db_path = _use_tmp_db(tmp_path, "miniapp_theme_dark.db")
    _standard_seed()
    resp = _client(db_path).get("/app/theme.css")
    assert resp.status_code == 200
    assert ':root[data-theme="dark"] {' in resp.text
    dark_block = resp.text.split(':root[data-theme="dark"] {', 1)[1]
    assert "--accent:" in dark_block
    # Тёмный акцент — не тот же литерал, что светлый (осветлён под контраст, D-07).
    assert "--accent: #037EF3;" not in dark_block


# ── фронт-ядро: таблица маршрутов (план 19-02, <route_table>) ───────────────────────────

EXPECTED_ROUTES = {
    "#/hub": "screens/hub.js",
    "#/tasks": "screens/tasks.js",
    "#/task/{id}": "screens/card.js",
    "#/profile": "screens/profile.js",
    "#/coins": "screens/coins.js",
    "#/leaderboard": "screens/leaderboard.js",
    "#/submit/{id}": "screens/submit.js",
    "#/review": "screens/review.js",
    "#/stats": "screens/stats.js",
    "#/admin-tasks": "screens/admin_tasks.js",
    "#/task-edit/{id}": "screens/task_edit.js",
    "#/admin-coins": "screens/admin_coins.js",
    "#/settings": "screens/settings.js",
    "#/settings/{code}": "screens/settings.js",
    "#/form": "screens/form.js",
    "#/applications": "screens/applications.js",
    "#/questions": "screens/questions.js",
}
_ROUTE_ROW = re.compile(r'\[\s*"(#/[^"]+)"\s*,\s*"(screens/[^"]+\.js)"\s*\]')


def _routes_from_app_js() -> dict[str, str]:
    text = APP_JS.read_text(encoding="utf-8")
    block = text[text.index("export const ROUTES"):]
    block = block[:block.index("];")]
    return dict(_ROUTE_ROW.findall(block))


def test_route_table_matches_phase_plan_exactly():
    # Найдено планом 19.1-08 (визуальная сверка): отдельная литеральная запись
    # "#/task-edit/new" стояла ПЕРЕД "#/task-edit/{id}" и перехватывала маршрут первой — у
    # литерального паттерна нет {id}, params.id оставался undefined, task_edit.js::isNew
    # (сравнение с "new") никогда не срабатывало -- мастер создания падал на
    # /admin/tasks/undefined (422). Запись убрана из app.js: "new" и так проходит [^/]+ у
    # {id}, второй маршрут не нужен -- проверяем явно ниже, чтобы регрессия не вернулась.
    routes = _routes_from_app_js()
    assert routes == EXPECTED_ROUTES
    # Phase 22 Plan 07 (D-16): "#/settings/{code}" — второй маршрут на тот же модуль
    # screens/settings.js (params.code решает старт/раздел), поэтому маршрутов на один
    # больше числа уникальных модулей (settings.js — единственный модуль с двумя записями).
    # Quick 260904-2cj: +1 маршрут "#/questions" (16 -> 17).
    assert len(routes) == 17
    assert set(routes.values()) == set(EXPECTED_ROUTES.values())
    assert "#/task-edit/new" not in routes


def test_every_screen_module_is_registered_in_routes():
    """Встречная проверка: недостижимых экранов быть не может. Каталог заполняют планы
    19-03..19-07 — каждый новый файл обязан появиться в ROUTES."""
    registered = {Path(m).name for m in _routes_from_app_js().values()}
    existing = {p.name for p in SCREENS_DIR.glob("*.js")} if SCREENS_DIR.is_dir() else set()
    unregistered = existing - registered
    assert not unregistered, f"экраны без маршрута в app.js: {sorted(unregistered)}"


def test_route_modules_are_lazily_imported():
    text = APP_JS.read_text(encoding="utf-8")
    assert "await import(" in text
    assert not re.search(r'^import .* from "\./screens/', text, re.M), "экраны только через import()"


# ── фронт-ядро: экраны состояний и D-05 ─────────────────────────────────────────────────

def test_open_in_bot_screen_has_deep_link_login_button_and_hint():
    text = APP_JS.read_text(encoding="utf-8")
    block = text[text.index('state === "open-in-bot"'):text.index('state === "expired"')]
    assert "ds.deepLink" in block          # deep-link на бота из data-атрибута шаблона
    assert 'href: "/login"' in block       # относительный адрес, не из данных (T-19-75)
    assert "ds.loginButton" in block and "ds.loginHint" in block  # подписи из реестра
    html = APP_HTML.read_text(encoding="utf-8")
    for attr in ("data-deep-link", "data-login-button", "data-login-hint", "data-open-in-bot-text",
                 "data-session-expired-text", "data-no-access-text", "data-disabled-text"):
        assert attr in html


def test_shell_passes_registry_texts_for_state_screens(tmp_path):
    db_path = _use_tmp_db(tmp_path, "miniapp_front_texts.db")
    _standard_seed()
    _set("miniapp_login_button", "Войти как организатор")
    _set("miniapp_login_hint", "После входа вернитесь на /app")
    resp = _client(db_path).get("/app")
    assert 'data-login-button="Войти как организатор"' in resp.text
    assert 'data-login-hint="После входа вернитесь на /app"' in resp.text
    assert "Это приложение открывается из бота" in resp.text  # дефолт miniapp_open_in_bot_text
    assert "Сессия истекла" in resp.text
    assert 'data-section-labels=' in resp.text and "Рейтинг" in resp.text


def test_expired_screen_closes_webapp():
    text = APP_JS.read_text(encoding="utf-8")
    block = text[text.index('state === "expired"'):text.index('state === "no-access"')]
    assert "ds.sessionExpiredText" in block
    assert "tg.close()" in block


def test_missing_screen_module_does_not_break_app():
    text = APP_JS.read_text(encoding="utf-8")
    block = text[text.index("await import("):]
    assert 'showState("missing")' in block[:400]


# ── api.js: ветки ошибок, без ретраев, без innerHTML ────────────────────────────────────

def test_api_js_handles_all_error_branches_without_retry():
    text = API_JS.read_text(encoding="utf-8")
    assert "X-Telegram-Init-Data" in text
    assert '"X-Requested-With": "fetch"' in text
    assert 'credentials: "same-origin"' in text
    assert 'reason === "bad_initdata"' in text
    assert '"expired"' in text and '"open-in-bot"' in text
    assert "response.status === 403" in text and '"no-access"' in text
    assert "response.status === 503" in text and 'reason === "miniapp_off"' in text
    assert "retry" not in text.lower()
    # 401 — терминально: бросаем, не зовём api() повторно.
    branch = text[text.index("response.status === 401"):]
    assert "api(" not in branch.split("throw new ApiError")[0]
    assert "export function esc" in text


# `[^\n]*`, а не `.*`: с re.S точка ела бы всё до конца файла после первого `//` — и
# сторож innerHTML проверял бы пустую строку (так и было в 19-02, поймано в 19-03).
_JS_COMMENT = re.compile(r"/\*.*?\*/|^\s*//[^\n]*$|(?<=[;{}])[ \t]*//[^\n]*$", re.S | re.M)


def _js_without_comments(path: Path) -> str:
    return _JS_COMMENT.sub("", path.read_text(encoding="utf-8"))


def test_no_innerhtml_with_interpolation_in_core():
    # 21-04: выборка расширена с (API_JS, APP_JS, screens/*.js) на ВЕСЬ miniapp/static/js/*.js —
    # корневые модули типа form.js раньше не покрывались этим сторожем.
    js_dir = MINIAPP_STATIC / "js"
    root_js = sorted(js_dir.glob("*.js")) if js_dir.is_dir() else []
    screens_js = sorted(SCREENS_DIR.glob("*.js")) if SCREENS_DIR.is_dir() else []
    pattern = re.compile(r"innerHTML\s*\+?=\s*(`|[^;]*\+)")
    for path in {*root_js, *screens_js}:
        text = _js_without_comments(path)
        assert not pattern.search(text), f"innerHTML с интерполяцией в {path.name}"
        assert "innerHTML" not in text, f"innerHTML в {path.name} — только textContent/esc()"


# ── навигация: три раскладки, переключаемые NAV_LAYOUT (план 19.1-04, D-10) ─────────────

def test_nav_layout_constant_defaults_to_hub_and_is_written_to_dataset():
    text = APP_JS.read_text(encoding="utf-8")
    m = re.search(r'const NAV_LAYOUT = "(tabbar|toptabs|hub)";', text)
    assert m, "NAV_LAYOUT — одна константа со значением из tabbar/toptabs/hub"
    assert m.group(1) == "hub"  # дефолт до голосования команды (D-10, UI-SPEC)
    assert "body.dataset.nav = NAV_LAYOUT;" in text


def test_three_nav_layouts_present_in_css():
    css = (MINIAPP_STATIC / "app.css").read_text(encoding="utf-8")
    for value in ("tabbar", "toptabs", "hub"):
        assert f'body[data-nav="{value}"]' in css, value


def test_old_pill_nav_removed_from_css():
    css = (MINIAPP_STATIC / "app.css").read_text(encoding="utf-8")
    assert 'class="nav"' not in css
    assert ".nav a" not in css
    assert ".nav {" not in css


# Phase 20 (ADMIN-IA-04): у менеджерских записей добавилось поле `group` — раздел хаба.
# Состав, права и порядок записей при этом не менялись ни на строку: сторож переснят ровно
# на одно новое поле, а не ослаблен (иначе он перестал бы ловить правку прав).
EXPECTED_NAV = [
    {"hash": "#/tasks", "section": "tasks", "delegate": True},
    {"hash": "#/coins", "section": "coins", "delegate": True},
    {"hash": "#/leaderboard", "section": "leaderboard", "delegate": True},
    {"hash": "#/profile", "section": "profile", "delegate": True},
    {"hash": "#/form", "section": "form", "delegate": True},
    {"hash": "#/applications", "section": "applications", "cap": "moderate_reg", "group": "apps"},
    {"hash": "#/questions", "section": "questions", "cap": "moderate_reg", "group": "apps"},
    {"hash": "#/review", "section": "review", "cap": "moderate_game", "group": "game"},
    {"hash": "#/admin-tasks", "section": "admin_tasks", "cap": "moderate_game", "group": "game"},
    {"hash": "#/admin-coins", "section": "coins", "cap": "moderate_game", "staffOnly": True, "group": "game"},
    {"hash": "#/stats", "section": "stats", "cap": "stats", "group": "data"},
    {"hash": "#/settings", "section": "settings", "cap": "settings", "group": "manage"},
]


def _nav_from_app_js() -> list[dict]:
    text = APP_JS.read_text(encoding="utf-8")
    block = text[text.index("export const NAV = ["):]
    block = block[:block.index("\n];") + 1]
    items = []
    for row in re.finditer(r"\{([^{}]*)\}", block):
        fields: dict = {}
        for pair in re.finditer(r'(\w+):\s*("([^"]*)"|true)', row.group(1)):
            key = pair.group(1)
            fields[key] = pair.group(3) if pair.group(3) is not None else True
        items.append(fields)
    return items


def test_nav_array_composition_unchanged_by_navigation_layout_plan():
    """D-10: смена NAV_LAYOUT/добавление раскладок не должно тихо тронуть состав или права
    NAV — сверка с составом, зафиксированным до плана 19.1-04 (те же hash/section/cap/
    delegate/staffOnly, что были в app.js планов 19-02..19-07, см. git)."""
    assert _nav_from_app_js() == EXPECTED_NAV


# ── motion.js: три уровня, конфетти, докрутка, хаптика (план 19.1-04, D-17) ─────────────
# Логика проверяется статически (в проекте нет запускалки JS-тестов) — осознанный предел
# плана, визуально проверяется в 19.1-08.

MOTION_JS = MINIAPP_STATIC / "js" / "motion.js"


def test_motion_js_exports_five_helpers():
    text = _js_without_comments(MOTION_JS)
    for name in ("resolveMotionTier", "applyMotionTier", "confetti", "countUp", "haptic"):
        assert re.search(rf"export\s+(async\s+)?function\s+{name}\s*\(", text), name


def test_motion_reduced_motion_checked_before_battery_and_cores():
    text = MOTION_JS.read_text(encoding="utf-8")
    reduced_pos = text.index("prefers-reduced-motion")
    assert reduced_pos < text.index("getBattery")
    assert reduced_pos < text.index("hardwareConcurrency")


def test_motion_cores_check_only_paired_with_android_platform():
    text = _js_without_comments(MOTION_JS)
    idx = text.index("hardwareConcurrency")
    window = text[max(0, idx - 200):idx]
    assert 'platform === "android"' in window


def test_motion_get_battery_checked_for_existence_and_wrapped_in_catch():
    text = _js_without_comments(MOTION_JS)
    assert "typeof navigator.getBattery" in text
    assert ".catch(" in text


def test_motion_haptic_independent_of_motion_tier():
    text = _js_without_comments(MOTION_JS)
    haptic_fn = text[text.index("export function haptic"):]
    assert "dataset.motion" not in haptic_fn  # хаптика не гейтится уровнем (D-17)


# ── экраны делегата (план 19-03): tasks / card / profile / coins / leaderboard ──────────

DELEGATE_SCREENS = ["tasks.js", "card.js", "profile.js", "coins.js", "leaderboard.js"]

# Пути, которые фронт зовёт через api("/...") / api(`/...`): весь литерал до закрывающей
# кавычки; `${…}` считается одним сегментом-параметром, query-строка отбрасывается.
# Сверяется с маршрутами приложения целиком (ловит опечатку в пути и `${action}` вместо
# литерального имени действия — план 19-05).
_API_CALL = re.compile(r"""api\(\s*(["'`])(/[^"'`]*)\1""")
_TEMPLATE_PARAM = re.compile(r"\$\{[^}]*\}")


@pytest.mark.parametrize("name", DELEGATE_SCREENS)
def test_delegate_screen_exports_render(name):
    path = SCREENS_DIR / name
    assert path.is_file(), f"экран {name} не создан"
    text = _js_without_comments(path)
    assert re.search(r"export\s+async\s+function\s+render\s*\(root,\s*params,\s*ctx\)", text), name
    assert "innerHTML" not in text
    assert "document.write" not in text
    assert not _HEX_OR_RGB_COLOR.findall(text), f"литеральный цвет в {name}"
    assert "https://" not in text and "http://" not in text, f"внешний URL в {name}"


def _app_api_route_patterns() -> list[re.Pattern]:
    """Маршруты /app/api/* из ALL_ROUTERS с `{param}` -> `[^/]+`."""
    from miniapp.routers import ALL_ROUTERS

    patterns = []
    for router in ALL_ROUTERS:
        for route in router.routes:
            path = getattr(route, "path", "")
            if path.startswith("/app/api/"):
                patterns.append(re.compile("^" + re.sub(r"\{[^}]+\}", "[^/]+", path) + "$"))
    return patterns


def test_every_fetch_path_in_screens_matches_an_app_route():
    patterns = _app_api_route_patterns()
    seen = 0
    for path in SCREENS_DIR.glob("*.js"):
        text = _js_without_comments(path)
        for _quote, rel in _API_CALL.findall(text):
            seen += 1
            literal = _TEMPLATE_PARAM.sub("x", rel.split("?")[0]).rstrip("/")
            full = "/app/api" + literal
            assert any(p.match(full) for p in patterns), (
                f"{path.name}: api('{rel}') не соответствует ни одному маршруту приложения"
            )
    assert seen >= 9  # 19-03: 6 делегатских; 19-04: uploads/limits, uploads, submissions; 19-05: review/*, stats


def test_screens_use_registry_empty_texts_not_literals():
    for name in ("tasks.js", "coins.js", "leaderboard.js", "review.js"):
        text = _js_without_comments(SCREENS_DIR / name)
        assert "empty_text" in text, f"{name}: пустое состояние — текстом из реестра"
    # 19.1-06: сторож против возврата двух литералов, снятых с review.js в план 19.1-02 не
    # завёл ключи реестра. Кнопка «Начать сначала» от текста не зависит — остаётся на месте.
    review_text = _js_without_comments(SCREENS_DIR / "review.js")
    assert "Сдач на проверке нет" not in review_text
    assert "Пропущено всё" not in review_text
    assert "card.empty_text" in review_text
    assert "Начать сначала" in review_text and "card.remaining" in review_text


def test_card_screen_main_button_submit_and_back():
    text = _js_without_comments(SCREENS_DIR / "card.js")
    assert 'setMainButton("Сдать"' in text
    assert "#/submit/" in text
    assert "/app/api/file/" in text and '"error"' in text  # обложка деградирует без ошибки


def test_task_card_uses_plate_and_proof_block():
    text = _js_without_comments(SCREENS_DIR / "card.js")
    for token in ("plate--task", "proof-drop", "flat-list flush", 'icon("camera")'):
        assert token in text, f"нет {token} в screens/card.js"
    assert 'class: "card task-card"' not in text


def test_profile_screen_opens_form_edit_inside_app():
    # План 21-11 (D-24): «Изменить анкету» ведёт на #/form внутри приложения, не по
    # deep-link в бота — deeplink/openTelegramLink здесь больше нет, подпись кнопки только
    # с сервера (me.edit_cta_text), литерала-подписи в файле нет.
    text = _js_without_comments(SCREENS_DIR / "profile.js")
    assert "openTelegramLink" not in text and "edit_deeplink" not in text
    assert 'navigate("#/form")' in text
    assert "me.can_edit" in text and "me.edit_cta_text" in text


def test_show_more_pagination_in_lists():
    for name in ("tasks.js", "coins.js"):
        text = _js_without_comments(SCREENS_DIR / name)
        assert "Показать ещё" in text and "offset" in text, name
    text = _js_without_comments(SCREENS_DIR / "leaderboard.js")
    assert "limit=50" in text and "board.me" in text


# ── экран сдачи (план 19-04): submit.js ─────────────────────────────────────────────────

def test_submit_screen_exports_render_and_checks_size_before_upload():
    path = SCREENS_DIR / "submit.js"
    assert path.is_file(), "экран submit.js не создан"
    text = _js_without_comments(path)
    assert re.search(r"export\s+async\s+function\s+render\s*\(root,\s*params,\s*ctx\)", text)
    assert "innerHTML" not in text and "document.write" not in text
    assert not _HEX_OR_RGB_COLOR.findall(text)
    # Префиксы ссылок в isLink() — не URL; всё остальное с протоколом запрещено.
    stripped = text.replace('"http://"', "").replace('"https://"', "")
    assert "https://" not in stripped and "http://" not in stripped
    # Проверка размера ДО отправки — раньше первого вызова /uploads, текст из реестра.
    size_check = text.index("file.size > limits.max_bytes")
    assert size_check < text.index('api("/uploads", {')
    assert "too_large_text" in text
    assert "1024 * 1024" not in text and "20971520" not in text  # числа лимитов не хардкодятся
    # Счётчик частей — иконками из icons.js (D-13, план 19.1-05), не эмодзи; «Убрать
    # последнее», «Готово» -> одиночный POST /submissions.
    assert "counter-group" in text and 'icon("image")' in text and 'icon("pen-line")' in text
    assert "Убрать последнее" in text
    assert 'setMainButton("Готово"' in text
    assert 'api("/submissions", {' in text and text.count('api("/submissions"') == 1
    assert "empty_hint" in text              # пустая отправка — подсказка, не сброс
    assert "err.status === 409" in text      # «Уже отправлено»
    assert 'navigate("#/tasks")' in text


def test_submit_screen_has_per_part_remove_and_accepted_moment():
    text = _js_without_comments(SCREENS_DIR / "submit.js")
    # Кнопка удаления части — иконка "x" с площадью тапа не меньше var(--tap-min) и aria-label.
    assert 'icon("x")' in text and '"aria-label": "Убрать часть"' in text
    css = (MINIAPP_STATIC / "app.css").read_text(encoding="utf-8")
    assert ".part-remove" in css and "var(--tap-min)" in css[css.index(".part-remove"):]
    # Принятая сдача — состояние успеха (стикер-слот "success" из ui.js::emptyState), не
    # мгновенный переход: хаптик и конфетти вызываются на этом состоянии (D-17/D-18).
    assert 'slot: "success"' in text
    accepted = text[text.index("function showAccepted"):]
    assert 'haptic("success")' in accepted and "confetti(view)" in accepted
    # Ошибка загрузки (превышение размера / отказ сервера) — errorState, не голый notice.
    assert "errorState(h, {" in text and "showUploadError" in text


# Узкий список эмодзи, использовавшихся как ФУНКЦИОНАЛЬНЫЕ иконки интерфейса (кнопки/счётчики/
# статусы частей submit.js) до перевода на icons.js (D-13, план 19.1-05) — не эмодзи из текстов
# реестра/мемного тона (те не в зоне ответственности JS-файлов вовсе, приходят с сервера).
_CHROME_ICON_EMOJI = ("📸", "📄", "✍️", "📎", "✅")


@pytest.mark.parametrize("name", DELEGATE_SCREENS + ["submit.js"])
def test_delegate_screens_have_no_emoji_icons_in_chrome(name):
    text = _js_without_comments(SCREENS_DIR / name)
    for e in _CHROME_ICON_EMOJI:
        assert e not in text, f"{name}: эмодзи {e} в роли иконки интерфейса — замените icons.js (D-13)"


# Найдено планом 19.1-08 (визуальная сверка): STATE_ICONS в app.js — экраны состояний
# (open-in-bot/expired/no-access/disabled/missing) — рисовались эмодзи (🤖⏳⛔🚧🧩), та же
# категория нарушения D-13, что и _CHROME_ICON_EMOJI выше, только в ядре, не в screens/*.js.
_STATE_ICON_EMOJI = ("🤖", "⏳", "⛔", "🚧", "🧩", "ℹ️")


def test_app_js_state_icons_have_no_emoji_and_use_icons_js():
    text = _js_without_comments(APP_JS)
    for e in _STATE_ICON_EMOJI:
        assert e not in text, f"app.js: эмодзи {e} в роли иконки экрана состояния — замените icons.js (D-13)"
    # showState() обязан собирать иконку через icon(...), не текстом.
    assert 'h("div", { class: "icon" }, icon(STATE_ICONS[state]' in text


# Сторож D-04 (нашла headless-съёмка 23.1: заголовки экранов «🗂 Отбор заявок»/«⚙️ Настройки-
# лайт» дублировали ведущий эмодзи подписи раздела реестром рядом с иконкой строки/плиты) —
# структурный, не поведенческий: любой text:/title:/label: и любое `.textContent =`, чьё
# значение читает подпись раздела реестра (`sectionLabel(...)`/`sectionLabelsFromDom()`/
# `labels[...]`), обязано быть обёрнуто в `labelText(` (ui.js) — иначе эмодзи из реестра
# просачивается в заголовок как второй глиф. Функция `sectionLabel`, объявление `const
# labels = …` и точечный доступ вида `labels.category` (свой локальный словарь task_edit.js,
# не подписи реестра) сюда не попадают — регэксп ищет только `labels[`/`sectionLabel(`.
_REGISTRY_LABEL_SOURCE = re.compile(r"\bsectionLabel\w*\s*\(|\blabels\[")
_TITLE_LIKE_PROP = re.compile(r"\b(?:text|title|label)\s*:\s*([^,}\n]+)")
_TEXTCONTENT_ASSIGN = re.compile(r"\.textContent\s*=\s*([^;]+);")


def test_screen_titles_from_registry_section_labels_use_labeltext():
    offenders = []
    for path in sorted(SCREENS_DIR.glob("*.js")):
        text = _js_without_comments(path)
        for m in _TITLE_LIKE_PROP.finditer(text):
            expr = m.group(1)
            if _REGISTRY_LABEL_SOURCE.search(expr) and "labelText(" not in expr:
                offenders.append(f"{path.name}: {expr.strip()}")
        for m in _TEXTCONTENT_ASSIGN.finditer(text):
            expr = m.group(1)
            if _REGISTRY_LABEL_SOURCE.search(expr) and "labelText(" not in expr:
                offenders.append(f"{path.name}: .textContent = {expr.strip()}")
    assert not offenders, (
        "заголовок из подписи раздела реестра без labelText() (D-04): " + "; ".join(offenders)
    )


# ── экраны менеджера (план 19-05): review.js / stats.js ────────────────────────────────

MANAGER_SCREENS = ["review.js", "stats.js"]


@pytest.mark.parametrize("name", MANAGER_SCREENS)
def test_manager_screen_exports_render_without_innerhtml_or_colors(name):
    path = SCREENS_DIR / name
    assert path.is_file(), f"экран {name} не создан"
    text = _js_without_comments(path)
    assert re.search(r"export\s+async\s+function\s+render\s*\(root,\s*params,\s*ctx\)", text), name
    assert "innerHTML" not in text and "document.write" not in text
    assert not _HEX_OR_RGB_COLOR.findall(text), f"литеральный цвет в {name}"
    assert "https://" not in text and "http://" not in text, f"внешний URL в {name}"


def test_review_screen_one_card_four_actions_and_already_is_calm():
    text = _js_without_comments(SCREENS_DIR / "review.js")
    # Одна карточка: GET /review/next с offset; «Пропустить» — offset+1 на клиенте.
    assert "/review/next?offset=" in text
    assert "offset += 1" in text
    # Четыре действия (editorial-минимал 19.1-06: главное действие — MainButton «Принять
    # · +N», без эмодзи — D-13; вторичные — обведённый ряд, «Отклонить» с иконкой x).
    assert "Принять · +${card.task.coins}" in text
    for label in ("Своя сумма", "Отклонить", "Пропустить"):
        assert label in text, label
    assert 'icon("x")' in text  # «Отклонить» — иконка вместо эмодзи
    # Решения — два литеральных пути (сторож путей их сверяет с маршрутами).
    assert "/approve`" in text and "/reject`" in text
    assert "${action}" not in text
    # «Уже обработано» — спокойный текст и переход дальше, не ошибка.
    assert "Уже обработано" in text
    already = text[text.index("Уже обработано"):]
    assert "load()" in already[:200]
    # Файлы частей — через прокси, текст — цитатой; счётчик «Осталось».
    assert "/app/api/file/" in text
    assert "blockquote" in text
    assert "Осталось:" in text
    # Причина отклонения обязательна, своя сумма — числовое поле с дефолтом из задания.
    assert "!body.reason" in text
    assert 'type: "number"' in text and "card.task.coins" in text


def test_stats_screen_tiles_and_bars_without_libraries():
    text = _js_without_comments(SCREENS_DIR / "stats.js")
    assert 'api("/stats/game")' in text
    assert "stat-value" in text                      # плитки чисел (классы app.css)
    assert "bar-track" in text and "bar-fill" in text  # полосы классами, не canvas
    assert "Chart" not in text and "canvas" not in text.lower()
    assert "by_category" in text and "r.label" in text  # подписи из API (реестр), не свои
    assert "Пока никто ничего не сдавал" in text      # тот же текст, что экран 9 бота


def test_review_and_bar_css_classes_exist_on_tokens():
    css = (MINIAPP_STATIC / "app.css").read_text(encoding="utf-8")
    for cls in (".review-card", ".review-photo", ".review-quote", ".review-actions", ".bar-track", ".bar-fill"):
        assert cls in css, cls
    # Dataviz-контракт (UI-SPEC §Dataviz, план 19.1-06): заливка полосы — цвет серии данных
    # --chart-1, трек — --chart-track, не акцент интерфейса --accent/--surface-alt.
    track_rule = css[css.index(".bar-track {"):css.index("}", css.index(".bar-track {"))]
    fill_rule = css[css.index(".bar-fill {"):css.index("}", css.index(".bar-fill {"))]
    assert "var(--chart-track)" in track_rule and "var(--accent)" not in track_rule and "var(--surface-alt)" not in track_rule
    assert "var(--chart-1)" in fill_rule and "var(--accent)" not in fill_rule


# ── экраны заданий менеджера (план 19-06): admin_tasks.js / task_edit.js ────────────────

ADMIN_TASK_SCREENS = ["admin_tasks.js", "task_edit.js"]


@pytest.mark.parametrize("name", ADMIN_TASK_SCREENS)
def test_admin_task_screen_exports_render_without_innerhtml_or_colors(name):
    path = SCREENS_DIR / name
    assert path.is_file(), f"экран {name} не создан"
    text = _js_without_comments(path)
    assert re.search(r"export\s+async\s+function\s+render\s*\(root,\s*params,\s*ctx\)", text), name
    assert "innerHTML" not in text and "document.write" not in text
    assert not _HEX_OR_RGB_COLOR.findall(text), f"литеральный цвет в {name}"
    assert "https://" not in text and "http://" not in text, f"внешний URL в {name}"


def test_admin_tasks_screen_toggle_rows_and_new_button():
    text = _js_without_comments(SCREENS_DIR / "admin_tasks.js")
    # Тумблер — два состояния, переключение без перезагрузки: флаг archived в query.
    assert "Активные" in text and "Архив" in text
    assert "archived=${archived ? 1 : 0}" in text
    assert "location.reload" not in text
    # Строки: номер, счётчики, переход в карточку; «➕ Новое задание» -> #/task-edit/new.
    assert "№${item.number}" in text
    assert "item.pending" in text and "item.approved" in text
    assert "#/task-edit/${item.id}" in text and '"#/task-edit/new"' in text
    assert "Показать ещё" in text and "offset" in text
    assert "empty_text" in text                       # пустое состояние — текстом с сервера
    assert "item.category_label" in text and '"Light"' not in text  # подписи готовые, кодов нет


def test_task_edit_screen_point_edits_confirmations_and_wizard():
    text = _js_without_comments(SCREENS_DIR / "task_edit.js")
    # Два режима на одном экране.
    assert 'params.id === "new"' in text
    # Режим «правка»: точечные правки — плоский список (D-11), «подпись — значение —
    # карандаш» (icon("pen-line")), без эмодзи-иконок (D-13, план 19.1-06).
    for label in ("Название", "Описание", "Монеты", "Дедлайн", "Обложка"):
        assert f'title: "{label}"' in text, label
    assert 'icon("pen-line")' in text and 'icon("image")' in text and 'icon("archive")' in text
    assert 'icon("rotate-ccw")' in text and 'icon("trash-2")' in text
    assert 'method: "PATCH"' in text and "card.text" in text and "/app/api/file/" in text
    # Превью «как видит делегат» — та же вёрстка, что у карточки делегата (card.js, план
    # 19.1-05): структурные поля, не card_text-блоб.
    assert "card.category_label" in text and "card.deadline_display" in text
    # Дедлайн — пресеты с сервера + своя дата с примером формата в подсказке.
    assert "deadline_presets" in text and "deadline_example" in text and "ДД.ММ.ГГГГ ЧЧ:ММ" in text
    # Фото: размер проверяется до отправки, текст из реестра; затем PATCH с part_token.
    assert text.index("file.size > limits.photo_max_bytes") < text.index('api("/uploads", {')
    assert "too_large_text" in text and "part_token: up.part_token" in text
    # Двухшаговое подтверждение архивации и удаления с описанием последствий (формулировки
    # последствий — не переписаны, только кнопки лишились эмодзи-префикса).
    assert "confirm-box" in text
    assert "Да, в архив" in text and "делегаты перестанут его видеть" in text
    assert "Да, удалить" in text and "удалено навсегда" in text
    # Удаление недоступно при сдачах — с объяснением, не молча.
    assert "disabled: !card.can_delete" in text and "cannot_delete_text" in text
    # Создание: категория/типы/город — кнопки и чекбоксы сеткой с подписями из /options; кодов нет.
    assert 'api("/admin/tasks/options")' in text
    assert 'type: "checkbox"' in text and "options.categories" in text and "options.cities" in text
    assert "city_choice" in text and "bound_city_label" in text
    # Человеческие подписи категорий/городов не хардкодятся (коды — не подписи).
    for code in ('"Light"', '"Medium"', '"Hard"', '"msk"', '"spb"'):
        assert code not in text, f"код {code} не должен быть в экране"
    # Исключение: код-имя иконки типа подтверждения ("pdf"/"link" — тот же приём, что и в
    # card.js, план 19.1-05) — техническая карта код -> имя иконки, не подпись человеку.
    assert 'pdf: "file-text"' in text and 'link: "link"' in text
    assert '"Опубликовать"' in text and 'api("/admin/tasks", {' in text
    # Ошибки сервера — человеческим текстом из payload.text. Сама логика перенесена в form.js
    # (план 21-04, был дословный дубль с settings.js) — экран только импортирует и зовёт.
    assert 'from "../form.js"' in text and "errorText(err," in text
    assert "err.payload.text" in _js_without_comments(FORM_JS)
    # Архив/возврат/удаление — литеральные пути (сторож путей их сверяет с маршрутами).
    assert "/archive`" in text and "/unarchive`" in text and 'method: "DELETE"' in text
    assert "${action}" not in text


def test_admin_tasks_css_classes_exist_on_tokens():
    css = (MINIAPP_STATIC / "app.css").read_text(encoding="utf-8")
    for cls in (".toggle", ".toggle-btn", ".admin-task-row", ".task-actions", ".choice-grid", ".confirm-box", ".wizard-step"):
        assert cls in css, cls
    toggles = css[css.index(".toggle"):]
    assert "var(--accent)" in toggles and "var(--border)" in toggles


# ── экраны монет вручную и настроек-лайт (план 19-07): admin_coins.js / settings.js ────

COINS_SETTINGS_SCREENS = ["admin_coins.js", "settings.js"]


@pytest.mark.parametrize("name", COINS_SETTINGS_SCREENS)
def test_coins_settings_screen_exports_render_without_innerhtml_or_colors(name):
    path = SCREENS_DIR / name
    assert path.is_file(), f"экран {name} не создан"
    text = _js_without_comments(path)
    assert re.search(r"export\s+async\s+function\s+render\s*\(root,\s*params,\s*ctx\)", text), name
    assert "innerHTML" not in text and "document.write" not in text
    assert not _HEX_OR_RGB_COLOR.findall(text), f"литеральный цвет в {name}"
    assert "https://" not in text and "http://" not in text, f"внешний URL в {name}"


def test_admin_coins_screen_has_confirm_step_before_charging():
    text = _js_without_comments(SCREENS_DIR / "admin_coins.js")
    # Поиск получателя, quick-pick сумм из реестра, обязательная причина.
    assert "/admin/users/search?q=" in text
    assert "/admin/coins/presets" in text
    assert "Причина обязательна" in text
    # Шаг подтверждения «кому · сколько · за что» — отдельный экран перед отправкой.
    assert "Кому:" in text and "Сколько:" in text and "За что:" in text
    assert 'api("/admin/coins", { method: "POST"' in text
    # Журнал — постранично, «Показать ещё».
    assert "/admin/coins?offset=" in text and "Показать ещё" in text


def test_settings_reads_whole_registry_not_a_whitelist():
    """Фаза 22 (план 22-05, D-01): экран больше не закрытый список `EDITABLE_KEYS` (десять
    тумблеров, план 19-07/MD-03) — весь правимый реестр одним запросом `settings/all`, шапка
    города тем же `set_admin_city`, что у бота. Список `miniapp_enabled`/`miniapp_staff_only`
    как отдельных "опасных" ключей в JS не хардкодится — опасность и текст последствий
    (`item.dangerous`/`item.confirm_text`) считает сервер (T-22-13, 22-04)."""
    text = _js_without_comments(SCREENS_DIR / "settings.js")
    assert 'api("/admin/settings/all")' in text
    assert 'api("/admin/settings/city"' in text
    assert "DANGER_KEYS" not in text
    assert '"miniapp_enabled"' not in text and '"miniapp_staff_only"' not in text
    # Поиск/свёртка/маркер состояния — из данных сервера, не из захардкоженных ключей.
    assert "searchFilter" in text and "settingSpec" in text


def test_coins_settings_css_classes_exist_on_tokens():
    css = (MINIAPP_STATIC / "app.css").read_text(encoding="utf-8")
    for cls in (".search-result", ".check-row", ".danger-settings"):
        assert cls in css, cls


# Узкий список модификаторов строки списка (D-11, план 19.1-06): каждый — надстройка поверх
# .flat-row (ui.js::flatRow), не отдельная карточка. Список специально узкий (не весь файл),
# чтобы не ловить легитимные карточки (.card, .recipient-card, .pinned — тот остаётся в паре
# с .card по решению плана 19.1-05) как ложное срабатывание.
_FLAT_ROW_MODIFIER_CLASSES = (".admin-task-row", ".search-result", ".check-row")


def test_manager_row_modifiers_do_not_redeclare_card_shell():
    css = (MINIAPP_STATIC / "app.css").read_text(encoding="utf-8")
    for cls in _FLAT_ROW_MODIFIER_CLASSES:
        for line in css.splitlines():
            stripped = line.strip()
            if not stripped.startswith(cls):
                continue
            # :active/.on — состояние по нажатию/включению, не форма строки — разрешено.
            if ":active" in stripped or ".on" in stripped:
                continue
            assert "border:" not in stripped and "background:" not in stripped, (
                f"{cls}: строка списка не должна рисовать свою рамку/фон — {stripped}"
            )


# Узкий список эмодзи, использовавшихся как ФУНКЦИОНАЛЬНЫЕ иконки интерфейса (кнопки/чипы/
# состояния) на шести менеджерских экранах до итоговой ревизии плана 19.1-06 (D-13) — не
# эмодзи из текстов реестра (те приходят с сервера, не в зоне ответственности этих файлов).
# Простые типографские стрелки (←/→ навигации визарда) сюда намеренно не входят — это не
# пиктограммы-иконки, тот же принцип, что и в _CHROME_ICON_EMOJI для делегатских экранов.
_MANAGER_CHROME_ICON_EMOJI = (
    "✅", "❌", "✏️", "💰", "📅", "🗄", "↩️", "🗑", "📷", "➕", "⏭", "🔁", "⏰",
    "👁", "👤", "◀️", "🌍", "☐", "🪙", "👥", "⏳",
)

MANAGER_SIX_SCREENS = ["review.js", "stats.js", "admin_tasks.js", "task_edit.js", "admin_coins.js", "settings.js"]


@pytest.mark.parametrize("name", MANAGER_SIX_SCREENS)
def test_manager_screens_have_no_emoji_icons_in_chrome(name):
    text = _js_without_comments(SCREENS_DIR / name)
    for e in _MANAGER_CHROME_ICON_EMOJI:
        assert e not in text, f"{name}: эмодзи {e} в роли иконки интерфейса — замените icons.js (D-13)"


# ── хаб делегата и менеджера + привет-экран (план 19.1-04, D-09/D-10) ───────────────────

def test_hub_screen_exports_render_without_innerhtml_or_colors():
    path = SCREENS_DIR / "hub.js"
    assert path.is_file(), "экран hub.js не создан"
    text = _js_without_comments(path)
    assert re.search(r"export\s+async\s+function\s+render\s*\(root,\s*params,\s*ctx\)", text)
    assert "innerHTML" not in text and "document.write" not in text
    assert not _HEX_OR_RGB_COLOR.findall(text), "литеральный цвет в hub.js"
    assert "https://" not in text and "http://" not in text, "внешний URL в hub.js"


def test_hub_route_registered_as_home():
    routes = _routes_from_app_js()
    assert routes["#/hub"] == "screens/hub.js"


def test_hub_onboarding_texts_come_from_registry_not_literals():
    text = _js_without_comments(SCREENS_DIR / "hub.js")
    assert "Погнали" not in text, "текст кнопки приветственного экрана — только из реестра"
    assert "onboarding_text" in text and "onboarding_cta" in text
    # Phase 23.1-03 (UI-REDESIGN-03): герой и шаги привет-экрана — тоже только из реестра.
    assert "onboarding_hero" in text
    assert "onboarding_steps_title" in text and "onboarding_steps" in text
    assert "Привет!" not in text, "заголовок привет-экрана — только из реестра"
    assert "Как это работает" not in text, "надзаголовок шагов — только из реестра"


def test_hub_tiles_built_from_visible_nav_and_nav_icons():
    text = _js_without_comments(SCREENS_DIR / "hub.js")
    assert 'from "../app.js"' in text and "visibleNav" in text and "NAV_ICONS" in text


def test_hub_manager_counters_are_fail_soft_per_tile():
    text = _js_without_comments(SCREENS_DIR / "hub.js")
    # Каждый счётчик — свой try/catch внутри общего Promise.all, отказ одной ручки не роняет хаб.
    assert "Promise.all(items.map(async" in text
    assert "try {" in text and "catch (_)" in text


# ── сторож: replaceChildren не принимает null/false/undefined верхним аргументом (quick
# 260823-rnc) — Element.replaceChildren() не фильтрует детей как это делает h() в app.js,
# любой null прямым аргументом рисуется пользователю как текстовый узел «null». ──────────

_STRING_LITERAL = re.compile(r"'(?:\\.|[^'\\])*'|\"(?:\\.|[^\"\\])*\"|`(?:\\.|[^`\\])*`", re.S)
_REPLACE_CHILDREN_CALL = re.compile(r"\breplaceChildren\s*\(")
_TOP_LEVEL_BAD_TOKEN = re.compile(r"\.filter\(Boolean\)|[()\[\]{}]|\b(?:null|false|undefined)\b")


def _mask_span(m: re.Match) -> str:
    """Заменяет найденный участок пробелами той же длины; переносы строк сохраняются, чтобы
    номера строк в маскированном тексте совпадали с исходным файлом."""
    return "".join(c if c == "\n" else " " for c in m.group(0))


def _js_masked(path: Path) -> str:
    """Маскирует (не удаляет, в отличие от `_js_without_comments`) комментарии и строковые
    литералы пробелами той же длины — длины и номера строк остаются как в исходнике, а
    скобки/слова внутри комментариев и текстовых подписей не ломают подсчёт вложенности."""
    text = path.read_text(encoding="utf-8")
    text = _JS_COMMENT.sub(_mask_span, text)
    text = _STRING_LITERAL.sub(_mask_span, text)
    return text


def _replace_children_calls(masked_text: str):
    """Для каждого вызова replaceChildren(...) в маскированном тексте возвращает
    (номер_строки, срез_аргументов). Падает, если скобки не сошлись до конца файла."""
    calls = []
    for m in _REPLACE_CHILDREN_CALL.finditer(masked_text):
        depth = 1
        i = m.end()
        n = len(masked_text)
        while i < n and depth > 0:
            if masked_text[i] in "([{":
                depth += 1
            elif masked_text[i] in ")]}":
                depth -= 1
            i += 1
        line_no = masked_text.count("\n", 0, m.start()) + 1
        if depth != 0:
            raise AssertionError(f"не удалось разобрать скобки replaceChildren на строке {line_no}")
        calls.append((line_no, masked_text[m.end():i - 1]))
    return calls


def _top_level_bad_token(args: str) -> bool:
    """True, если в аргументах на глубине 0 (не внутри вложенных h(...)/[...]/{...}) есть
    null/false/undefined. `.filter(Boolean)`, встреченный на глубине 0 (санкционированный
    способ передать список с пропусками — `...[…].filter(Boolean)`), делает весь вызов
    безопасным сразу; учитывается только на глубине 0, чтобы несвязанный `.filter(Boolean)`
    внутри вложенного h(...) (например, для join(" · ")) не маскировал реальный дефект."""
    depth = 0
    for m in _TOP_LEVEL_BAD_TOKEN.finditer(args):
        tok = m.group(0)
        if tok == ".filter(Boolean)":
            if depth == 0:
                return False
        elif tok in "([{":
            depth += 1
        elif tok in ")]}":
            depth -= 1
        elif depth == 0:
            return True
    return False


def test_replace_children_never_gets_null_false_undefined_as_top_level_argument():
    problems = []
    files = [APP_JS, *(SCREENS_DIR.glob("*.js") if SCREENS_DIR.is_dir() else ())]
    for path in files:
        masked = _js_masked(path)
        for line_no, args in _replace_children_calls(masked):
            if _top_level_bad_token(args):
                problems.append(f"{path.name}:{line_no}")
    assert not problems, (
        "null/false/undefined прямым аргументом replaceChildren: DOM-узел «null» увидит "
        "пользователь. Оберните список: replaceChildren(...[…].filter(Boolean)). Места: "
        + ", ".join(problems)
    )
# ── Phase 20 (ADMIN-IA-04, D-05): хаб Mini App = та же IA, что корень /admin ─────────────
# Менеджер держит в голове ОДНУ карту админки. Подписи разделов лежат в двух местах —
# `handlers/admin_sections.py::SECTIONS` (бот) и `SECTION_GROUPS` в app.js (веб) — поэтому
# нужен тот же сторож дрейфа, что у побайтной копии tokens.css выше
# (`test_tokens_css_is_byte_for_byte_copy_of_dashboard_tokens`): расхождение обязано ронять
# сборку, а не всплывать у менеджера двумя разными меню.

def _section_groups_from_app_js() -> list[tuple[str, str]]:
    text = APP_JS.read_text(encoding="utf-8")
    start = text.index("export const SECTION_GROUPS = [")
    block = text[start:text.index("];", start)]
    return [(m.group(1), m.group(2)) for m in re.finditer(r'\["([^"]+)",\s*"([^"]+)"\]', block)]


def _manager_hub_body() -> str:
    """Тело renderManagerHub из hub.js — от объявления до следующей функции верхнего уровня."""
    text = (SCREENS_DIR / "hub.js").read_text(encoding="utf-8")
    start = text.index("async function renderManagerHub")
    rest = text[start + 1:]
    ends = [m.start() for m in re.finditer(r"^(async )?function ", rest, re.M)]
    return rest[:ends[0]] if ends else rest


def test_section_groups_match_bot_sections():
    """Сверка идёт по ИСХОДНЫМ строкам `SECTION_GROUPS` и `SECTIONS`, а не по тому, что видно
    на экране: хаб рисует заголовок раздела капсом — `.sec` в `miniapp/static/app.css` ставит
    `text-transform: uppercase`. Поэтому «🎪 СОБЫТИЕ» на экране при «🎪 Событие» в коде — это
    оформление, а не расхождение карты админки, и чинить регистр в литералах не нужно."""
    from handlers.admin_sections import SECTIONS

    assert _section_groups_from_app_js() == [(token, label) for token, label, _ in SECTIONS], (
        "SECTION_GROUPS в miniapp/static/js/app.js разошёлся с handlers/admin_sections.py::"
        "SECTIONS — подписи и порядок разделов правятся в двух местах сразу, иначе бот и "
        "Mini App показывают менеджеру две разные админки"
    )


def test_every_manager_nav_item_has_a_group():
    tokens = {token for token, _ in _section_groups_from_app_js()}
    for item in _nav_from_app_js():
        if item.get("delegate"):
            assert "group" not in item, f"{item['hash']}: делегатский хаб не группируется (D-05)"
            continue
        assert item.get("group") in tokens, (
            f"{item['hash']}: менеджерская вкладка обязана лежать в одном из разделов "
            f"SECTION_GROUPS, сейчас group={item.get('group')!r}"
        )


def test_router_ignores_telegram_webapp_fragment():
    """Telegram Web (K/A) передаёт initData фрагментом URL (#tgWebAppData=…). Роутер обязан
    считать такой hash пустым и уводить на homeHash, а не в showState("missing") — иначе
    каждый первый вход из Telegram Web упирается в «Раздел пока недоступен» (находка 19-10)."""
    text = APP_JS.read_text(encoding="utf-8")
    start = text.index("async function route()")
    body = text[start:start + 800]
    assert 'startsWith("#tgWebApp")' in body, (
        "route() обязан сбрасывать служебный фрагмент #tgWebApp… в пустой hash до маршрутизации"
    )


def test_payment_row_hidden_when_payment_module_off():
    """Вопрос владельца 02.09: «почему висит Не оплатил, оплата же выключена». Контракт:
    сервер шлёт пустой payment_status_label при payment_enabled=off (profile.py), а профиль
    рисует строку «Оплата» только при непустой подписи; хаб уже падает на «профиль»."""
    server = (Path("miniapp/routers/profile.py")).read_text(encoding="utf-8")
    assert 'get_setting_typed("payment_enabled")' in server, (
        "/app/api/profile обязан сверяться с тумблером payment_enabled"
    )
    js = (SCREENS_DIR / "profile.js").read_text(encoding="utf-8")
    # Phase 23.1-05: чип оплаты собирается веткой `if (me.payment_status_label)` (плита с
    # массивом chips), не тернарником — тот же контракт (пустая подпись -> чипа нет вовсе).
    assert "if (me.payment_status_label)" in js, (
        "профиль обязан прятать чип «Оплата» при пустой подписи (модуль оплаты выключен)"
    )


def test_delegate_manager_sees_manager_groups_too():
    """Находка 19-10 + решение владельца 02.09: делегат-менеджер получает ОБА вида хаба через
    переключатель «Делегат | Менеджер» (localStorage), а не простыню и не только делегатский
    вид. Контракт: renderHub ветвится по hubMode и умеет вызвать оба рендера; чистые роли
    переключателя не видят."""
    text = (SCREENS_DIR / "hub.js").read_text(encoding="utf-8")
    start = text.index("async function renderHub")
    rest = text[start + 1:]
    ends = [m.start() for m in re.finditer(r"^(async )?function ", rest, re.M)]
    body = rest[:ends[0]] if ends else rest
    assert "renderDelegateHub" in body and "renderManagerHub" in body, (
        "renderHub обязан уметь оба вида"
    )
    assert "modeSwitch" in body and "hubMode()" in body, (
        "делегат-менеджер получает переключатель видов, а не один вид"
    )
    assert "HUB_MODE_KEY" in text and "localStorage" in text, "выбор вида запоминается на устройстве"
    assert ".hub-seg" in (Path("miniapp/static/app.css").read_text(encoding="utf-8")), (
        "стили переключателя living в app.css"
    )


def test_manager_hub_renders_group_headers():
    body = _manager_hub_body()
    assert "SECTION_GROUPS" in body, "плитки менеджера раскладываются по SECTION_GROUPS"
    assert "item.group" in body, "раздел плитки берётся из поля group записи NAV"
    assert "continue" in body, "раздел без видимых плиток пропускается целиком (T-20-06)"
    assert 'class: "sec"' in body, "у раздела есть заголовок — та же вёрстка, что в хабе делегата"
    # Подпись ПЛИТКИ — по-прежнему из реестра (miniapp_section_*), а не из SECTION_GROUPS:
    # иначе на плитках оказались бы подписи разделов и «Данные» вместо «Статистики».
    assert "labels[item.section]" in body


def test_manager_hub_dashboard_tile_driven_by_registry_not_literal():
    """Quick 260903: плитка «Дашборд» — адрес и подпись из ctx.me (/app/api/me), не литерал.
    hub.js уже проходит общий сторож «нет https:///http://» (test_hub_screen_exports_render_
    without_innerhtml_or_colors) — здесь проверяем, что сама плитка читает dashboard_url/
    dashboard_tile_label, а не собирает ссылку/текст сама."""
    text = _js_without_comments(SCREENS_DIR / "hub.js")
    assert "me.dashboard_url" in text
    assert "me.dashboard_tile_label" in text


def test_manager_hub_dashboard_tile_opens_via_telegram_or_new_tab():
    text = _js_without_comments(SCREENS_DIR / "hub.js")
    assert "tg.openLink" in text or "openLink(" in text
    assert 'window.open(url, "_blank", "noopener")' in text


def test_manager_hub_dashboard_tile_absent_when_url_empty():
    """dashboardTile() возвращает null при пустом dashboard_url — плитки нет вовсе, не
    пустая/сломанная кнопка."""
    text = _js_without_comments(SCREENS_DIR / "hub.js")
    start = text.index("function dashboardTile(")
    body = text[start:text.index("\n}\n", start)]
    assert "if (!me.dashboard_url) return null;" in body


def test_manager_hub_dashboard_tile_lives_in_data_group():
    """Плитка встаёт в ту же группу «data», что «#/stats» — и рисует заголовок раздела, даже
    если единственная видимая плитка в нём — сам дашборд (менеджер без права stats)."""
    body = _manager_hub_body()
    assert 'token === "data"' in body
    assert "dashboardTile(h, tg, me)" in body
    assert "!groupItems.length && !dashTile" in body


def test_manager_hub_countdown_hint_is_fail_soft_and_server_driven():
    """Quick 260903 (BACKLOG-0309-COUNTDOWN): подсказка про незаданную «Дату отсчёта до
    форума» — текст и решение «показывать/нет» считает сервер (D-06, `settings/hints`),
    hub.js не заводит человеко-видимых литералов и не роняет хаб при отказе ручки (тот же
    приём, что у MANAGER_FETCHERS ниже по файлу)."""
    body = _manager_hub_body()
    assert '"/admin/settings/hints"' in body
    assert "try {" in body and "catch (_)" in body
    assert "countdown.text" in body
    assert "countdown.hash" in body
    assert 'icon: "calendar"' in body


def test_manager_hero_only_with_review_tile():
    """Критерий успеха №4 ROADMAP: при выключенной гейме хаб остаётся осмысленным. Герой
    считает очередь проверки сдач, и без плитки «#/review» его нечем заполнить — он показывал
    бы вечный ноль «сдач на проверке». Проверяем, что создание героя не стоит на верхнем
    уровне функции безусловно: до него есть проверка наличия «#/review»."""
    body = _manager_hub_body()
    hero_at = body.index('"hero hero-flat"')
    assert '"#/review"' in body[:hero_at], (
        "герой рисуется до/без проверки наличия плитки «#/review» — при выключенной "
        "геймификации менеджер увидит вечный ноль «сдач на проверке»"
    )


# ── form.js: общие формо-компоненты анкеты (план 21-04, D-22, Reuse Contract фазы 22) ───

FORM_JS = MINIAPP_STATIC / "js" / "form.js"

# Тот же список типов, что reg_engine.step_spec()::_ui_type_for публикует на сегодня
# (21-UI-SPEC.md § «Form Components → По типу») — контракт с движком, литерал намеренный.
FORM_SPEC_TYPES = [
    "text", "textarea", "phone", "email", "int", "date", "choice-chips",
    "select", "multi", "yesno", "file", "consent",
]


def test_form_js_exports_shared_components():
    text = _js_without_comments(FORM_JS)
    for name in (
        "field", "setFieldState", "createFormState", "diffView", "confirmBox",
        "listChips", "searchFilter", "groupCollapse", "errorText", "isAuthError",
    ):
        assert re.search(rf"export\s+(async\s+)?function\s+{name}\s*\(", text), name


def test_form_js_covers_every_spec_type():
    text = _js_without_comments(FORM_JS)
    for t in FORM_SPEC_TYPES:
        assert f'case "{t}"' in text, t


def test_form_js_has_no_human_text_literals():
    """D-25: подписи в form.js — только из spec/payload, ни одного кириллического литерала
    в строках/шаблонах. Комментарии (по конвенции проекта — на русском) исключены заранее
    через _js_without_comments; здесь проверяются только строковые/шаблонные литералы."""
    text = _js_without_comments(FORM_JS)
    cyrillic = re.compile(r"[А-Яа-яЁё]")
    for m in _STRING_LITERAL.finditer(text):
        assert not cyrillic.search(m.group(0)), f"кириллический литерал в form.js: {m.group(0)}"


# ── screens/form.js: экран #/form (план 21-11, D-25) ─────────────────────────────────────

FORM_SCREEN_JS = SCREENS_DIR / "form.js"


def test_form_screen_has_no_human_text_literals():
    """D-25 — тот же сторож, что form.js (test_form_js_has_no_human_text_literals), теперь
    и на экране #/form: подписи только из ответа сервера (реестр reg_form_*)."""
    text = _js_without_comments(FORM_SCREEN_JS)
    cyrillic = re.compile(r"[А-Яа-яЁё]")
    for m in _STRING_LITERAL.finditer(text):
        assert not cyrillic.search(m.group(0)), f"кириллический литерал в screens/form.js: {m.group(0)}"


# ── form.js: типы реестра настроек + нестрогий поиск (фаза 22, D-05/D-13/D-15) ──────────
# Веб-настройки НЕ заводят второго набора компонентов: те же field()/setFieldState()
# расширены ветками toggle/photo/file/list, поиск — headless-функциями в том же файле.
# Поведение поиска проверяется в node отдельным файлом (tests/test_settings_search_js.py);
# здесь — статические сторожа, которые остаются гейтом и без node.

ICONS_JS = MINIAPP_STATIC / "js" / "icons.js"
APP_CSS = MINIAPP_STATIC / "app.css"
SETTINGS_CSS_CLASSES = (".settings-search", ".settings-group", ".settings-row", ".settings-batch-bar", ".settings-diff")
SETTINGS_CSS_MARKER = "/* ── экран настроек (фаза 22"


def test_form_js_covers_registry_types():
    text = _js_without_comments(FORM_JS)
    for t in ("toggle", "photo", "file", "list"):
        assert f'case "{t}"' in text, f"form.js: нет ветки типа реестра {t}"
    assert re.search(r"export\s+function\s+settingSpec\s*\(", text), "settingSpec не экспортирован"
    # Сеть остаётся у экрана: загрузка photo/file, batch и preview — не в компонентах.
    assert "fetch(" not in text and 'from "./api.js"' not in text
    # Тумблер — тот же визуал, что «настройки-лайт» (flatRow + check-row), не новый компонент.
    assert 'from "./ui.js"' in text and '"check-row"' in text


def test_toggle_control_paints_itself_before_calling_onchange():
    """Quick 260904-8o3 Task 1 (E1/E7) — статический сторож без node: регресс «контрол снова
    не красит себя» (тумблер зовёт onChange, а визуал ставит только вызывающий экран) ловится
    даже там, где node недоступен. Поведенческий тест — tests/test_settings_toggle_js.py."""
    text = _js_without_comments(FORM_JS)
    m = re.search(r"function toggleControl\([^)]*\)\s*\{.*?\n\}\n", text, re.S)
    assert m, "toggleControl не найден в form.js"
    body = m.group(0)
    paint_call = re.search(r"\bpaint\(", body)
    onchange_call = re.search(r"\bonChange\(", body)
    assert paint_call and onchange_call, "toggleControl должен вызывать и paint(...), и onChange(...)"
    assert paint_call.start() < onchange_call.start(), (
        "toggleControl должен красить себя (paint(next)) ДО onChange(next) — оптимистичная "
        "прокраска, откат делает вызывающий (screens/settings.js::saveToggle)"
    )


def test_form_js_search_exports_and_no_synonym_literals():
    text = _js_without_comments(FORM_JS)
    for name in ("searchFilter", "highlightMatch", "suggestTerms"):
        assert re.search(rf"export\s+function\s+{name}\s*\(", text), name
    # Синонимы приходят с сервера (D-13/D-15а) — во фронте словаря нет: ни одного
    # кириллического строкового литерала во всём файле (те же правила, что D-25 фазы 21).
    assert "search_terms" in text
    cyrillic = re.compile(r"[А-Яа-яЁё]")
    for m in _STRING_LITERAL.finditer(text):
        assert not cyrillic.search(m.group(0)), f"кириллический литерал в form.js: {m.group(0)}"
    # Подсветка — фрагментами h("mark"), не разметкой строкой (T-22-03).
    assert "innerHTML" not in text
    assert 'h("mark"' in text
    # Внешних библиотек расстояния нет — только относительные импорты соседних модулей.
    assert not re.search(r'import .* from "http', text)
    assert re.search(r'^import .* from "\./', text, re.M)


def _settings_css_block() -> str:
    css = APP_CSS.read_text(encoding="utf-8")
    assert SETTINGS_CSS_MARKER in css, "в app.css нет блока экрана настроек"
    return css[css.index(SETTINGS_CSS_MARKER):]


def test_settings_css_classes_present_and_tokenised():
    block = _settings_css_block()
    for cls in SETTINGS_CSS_CLASSES:
        assert cls in block, cls
    assert not _HEX_OR_RGB_COLOR.findall(block), "литеральный цвет в блоке экрана настроек"
    assert "position: sticky" in block[block.index(".settings-search"):]
    assert "position: fixed" in block[block.index(".settings-batch-bar"):]
    mark_rule = block[block.index(".settings-row mark"):]
    assert "var(--accent-soft)" in mark_rule[:mark_rule.index("}")]
    assert "var(--tap-min)" in block
    assert "var(--warn)" in block[block.index(".settings-diff"):]


def test_icons_js_has_eye_for_settings_preview():
    # icon("eye") без записи бросает и роняет экран настроек на первом ключе с плейсхолдером
    # (22-UI-SPEC § Icon Inventory) — ровно одна запись, тот же формат, что у соседей.
    text = ICONS_JS.read_text(encoding="utf-8")
    assert text.count('"eye": [') == 1
    assert 'viewBox", "0 0 24 24"' in text


# ── screens/settings.js: экран «⚙️ Настройки» (план 22-05, D-01…D-15) ────────────────────

SETTINGS_JS = SCREENS_DIR / "settings.js"


def test_settings_screen_uses_form_module():
    """Экран — ПОТРЕБИТЕЛЬ form.js (Reuse Contract 22-UI-SPEC): ровно один импорт, ни одного
    собственного ветвления по типу поля (свитч типов живёт в form.js::buildControl)."""
    text = _js_without_comments(SETTINGS_JS)
    assert len(re.findall(r'from "\.\./form\.js"', text)) == 1
    assert 'case "enum"' not in text and 'case "toggle"' not in text
    assert "settingSpec(item)" in text and "field(h, spec" in text


def test_settings_screen_has_no_human_text_literals():
    """D-25/22-UI-SPEC Copywriting Contract: ни одного кириллического литерала — каждая
    надпись экрана идёт из `texts` ответа `settings/all` (реестр `miniapp_settings_*`,
    план 22-02), помеченная человеку подпись поля/группы — из самого элемента реестра."""
    text = _js_without_comments(SETTINGS_JS)
    cyrillic = re.compile(r"[А-Яа-яЁё]")
    for m in _STRING_LITERAL.finditer(text):
        assert not cyrillic.search(m.group(0)), f"кириллический литерал в screens/settings.js: {m.group(0)}"


def test_settings_screen_covers_batch_response_branches():
    """Все четыре ветки ответа `POST settings/batch` разобраны (WEB-SET-03): errors — под
    поле, needs_confirm — авто-подтверждение на повторе, stale — «оставить как в боте»/
    «перезаписать» без потери остальных правок, saved — точечная перерисовка строк."""
    text = _js_without_comments(SETTINGS_JS)
    assert "resp.errors" in text
    assert "resp.needs_confirm" in text
    assert "resp.stale" in text
    assert "resp.saved" in text
    # stale — обе кнопки диалога (перезаписать / оставить как в боте), не только badge.
    assert "miniapp_settings_stale_overwrite_label_text" in text
    assert "miniapp_settings_stale_keep_label_text" in text


def test_settings_screen_reuses_existing_upload_route():
    """Фото/файл реестра идут тем же staff-путём, что резюме делегата (план 19-04/21-10) —
    экран не заводит своего маршрута загрузки."""
    text = _js_without_comments(SETTINGS_JS)
    assert 'api("/uploads"' in text
    assert 'api("/uploads/limits")' in text
    assert "/app/api/admin/uploads" not in text and "settings/upload" not in text


# ── swipe.js: распознавание жеста «принять/отклонить» (фаза 23 план 05, D-04) ───────────
# Поведение порогов/угла проверяется в node (tests/test_swipe_js.py); здесь — статические
# сторожа, которые остаются гейтом и без node.

SWIPE_JS = MINIAPP_STATIC / "js" / "swipe.js"


def _swipe_decision_source() -> str:
    """Текст ровно тела `swipeDecision` (без комментариев) — до начала `attachSwipe`."""
    text = _js_without_comments(SWIPE_JS)
    start = text.index("export function swipeDecision")
    end = text.index("export function attachSwipe")
    return text[start:end]


def test_swipe_js_exports_pure_and_no_dom_outside_attach_swipe():
    text = _js_without_comments(SWIPE_JS)
    assert re.search(r"export\s+function\s+swipeDecision\s*\(", text)
    assert re.search(r"export\s+function\s+attachSwipe\s*\(", text)
    decision_src = _swipe_decision_source()
    assert "document" not in decision_src and "window" not in decision_src, (
        "swipeDecision обязана быть чистой — без document/window"
    )
    before_decision = text[:text.index("export function swipeDecision")]
    assert "document" not in before_decision and "window" not in before_decision


def test_swipe_js_thresholds_are_named_constants():
    text = _js_without_comments(SWIPE_JS)
    for name in ("HORIZONTAL_MIN", "COMMIT_PX", "COMMIT_RATIO", "MAX_TILT", "EDGE_GUARD", "VERTICAL_SLOPE_MAX"):
        assert re.search(rf"export\s+const\s+{name}\s*=", text), name
    decision_src = _swipe_decision_source()
    # В теле swipeDecision числовые литералы допустимы только 0 и 1 (клэмп/дефолт) — каждый
    # реальный порог обязан быть именованной константой, объявленной выше по файлу.
    numbers = re.findall(r"(?<![\w.])\d+(?:\.\d+)?", decision_src)
    assert all(n in ("0", "1") for n in numbers), f"магическое число в swipeDecision: {numbers}"


def test_attach_swipe_sets_touch_action_pan_y_and_reads_motion_tier():
    text = _js_without_comments(SWIPE_JS)
    attach_src = text[text.index("export function attachSwipe"):]
    assert 'touchAction = "pan-y"' in attach_src
    assert "dataset.motion" in attach_src
    assert "setPointerCapture" in attach_src
    for ev in ("pointerdown", "pointermove", "pointerup", "pointercancel"):
        assert ev in attach_src, ev


def test_attach_swipe_skips_interactive_descendants_before_capture():
    """Владелец 03.09: pointerdown на кнопке/ссылке внутри карточки (например «Показать всё»)
    не должен начинать захват указателя — иначе браузер ретаргетит все последующие pointer- и
    производные click-события на саму карточку, и кнопка перестаёт отвечать на тап (см. node-
    поведенческий тест tests/test_swipe_js.py)."""
    text = _js_without_comments(SWIPE_JS)
    assert "button" in text and "summary" in text  # INTERACTIVE_SELECTOR перечисляет теги
    attach_src = text[text.index("export function attachSwipe"):]
    handle_down_src = attach_src[attach_src.index("function handleDown"):attach_src.index("function handleMove")]
    guard_idx = handle_down_src.find("isInteractiveTarget(")
    capture_idx = handle_down_src.find("setPointerCapture")
    assert guard_idx != -1, "handleDown не проверяет интерактивную цель"
    assert guard_idx < capture_idx, "проверка интерактивной цели должна идти раньше setPointerCapture"


# ── screens/applications.js: экран #/applications (фаза 23 план 05, D-01..D-09) ─────────

APPLICATIONS_JS = SCREENS_DIR / "applications.js"


def test_applications_screen_exports_render_without_innerhtml_or_colors():
    text = _js_without_comments(APPLICATIONS_JS)
    assert re.search(r"export\s+async\s+function\s+render\s*\(root,\s*params,\s*ctx\)", text)
    assert re.search(r"export\s+function\s+unmount\s*\(", text)
    assert "innerHTML" not in text
    assert not _HEX_OR_RGB_COLOR.findall(text), "литеральный цвет в applications.js"
    assert "https://" not in text and "http://" not in text


def test_applications_screen_has_no_human_text_literals():
    """D-25 (найдено планом 23-05): API 23-04 не отдавал часть подписей экрана вовсе —
    дописано в miniapp/routers/page.py::APPLICATIONS_TEXT_KEYS (body.dataset.applicationsTexts,
    тот же приём, что hub.js::sectionLabelsFromDom). Ни одного кириллического литерала
    в строковых/шаблонных литералах — тот же сторож, что form.js/settings.js."""
    text = _js_without_comments(APPLICATIONS_JS)
    cyrillic = re.compile(r"[А-Яа-яЁё]")
    for m in _STRING_LITERAL.finditer(text):
        assert not cyrillic.search(m.group(0)), f"кириллический литерал в screens/applications.js: {m.group(0)}"


def test_applications_screen_does_not_touch_review_screen_or_classes():
    text = _js_without_comments(APPLICATIONS_JS)
    assert "review-" not in text
    assert not re.search(r'from\s+"\.\./screens/review\.js"', text)


def test_applications_screen_uses_attach_swipe_exactly_once_via_swipe_js():
    text = _js_without_comments(APPLICATIONS_JS)
    assert re.search(r'import\s*\{[^}]*attachSwipe[^}]*\}\s*from\s*"\.\./swipe\.js"', text)
    assert len(re.findall(r"attachSwipe\(", text)) == 1


def test_applications_screen_touch_action_pan_y_present_in_css():
    css = APP_CSS.read_text(encoding="utf-8")
    assert "pan-y" in css


def test_appl_card_swipe_release_uses_named_duration_tokens_not_literals():
    """Владелец 03.09 («дёргано»): .appl-card раньше не имел ни одного transition вовсе —
    отпускание карточки было мгновенным скачком, а не анимацией. Токены — именованные
    (tokens.css), не литералы в app.css (тот же приём, что --dur/--ease везде в файле)."""
    css = APP_CSS.read_text(encoding="utf-8")
    appl_card_start = css.index(".appl-card {")
    appl_card_block = css[appl_card_start:css.index("}", appl_card_start)]
    assert "var(--dur-swipe-exit)" in appl_card_block
    assert "var(--ease-swipe-exit)" in appl_card_block
    assert "will-change: transform" in appl_card_block
    assert ".is-dragging" in css
    is_dragging_start = css.index(".appl-card.is-dragging")
    is_dragging_block = css[is_dragging_start:css.index("}", is_dragging_start)]
    assert "transition: none" in is_dragging_block
    assert "user-select: none" in is_dragging_block
    tokens = (MINIAPP_STATIC / "tokens.css").read_text(encoding="utf-8")
    assert "--dur-swipe-exit" in tokens and "--ease-swipe-exit" in tokens
    # Оба нулятся под motion off / prefers-reduced-motion — тот же приём, что --dur.
    assert tokens.count("--dur-swipe-exit: 0ms") == 2


def test_attach_swipe_coalesces_progress_via_request_animation_frame():
    """Владелец 03.09: сырые pointermove могут прилетать чаще кадра — handleMove обязан
    ТОЛЬКО запоминать событие, а сам onProgress звать через requestAnimationFrame (не более
    раза за кадр). Поведенческая проверка — tests/test_swipe_js.py (node, coalesce_result)."""
    text = _js_without_comments(SWIPE_JS)
    attach_src = text[text.index("export function attachSwipe"):]
    handle_move_src = attach_src[attach_src.index("function handleMove"):attach_src.index("function handleUp")]
    assert "requestAnimationFrame(" in handle_move_src
    assert "onProgress(" not in handle_move_src, "onProgress обязан звать flush по кадру, не сырое событие"


def test_applications_screen_api_paths_are_literal_not_action_interpolated():
    text = _js_without_comments(APPLICATIONS_JS)
    for path in ("/applications/next", "/applications/undo", "/applications/approve_all", "/approve", "/reject"):
        assert path in text, path
    assert "${action}" not in text


def test_applications_screen_undo_window_comes_from_server_not_hardcoded():
    text = _js_without_comments(APPLICATIONS_JS)
    assert "undo_seconds" in text
    assert "5000" not in text
    assert "5 * 1000" not in text


def test_applications_screen_reject_flow_has_template_own_and_no_reason_paths():
    text = _js_without_comments(APPLICATIONS_JS)
    assert "reject_templates" in text
    assert "reject_own_reason" in text or "rejectOwnBox" in text
    assert "reject_no_reason" in text


# ── D18 (квик 260904-7e7): шторка отказа — модальный низовой лист поверх стопки, а не блок
# под карточкой (владелец 04.09 переголосовал решение фазы 23 — жест закрытия вебвью уже
# выключен глобально `tg.disableVerticalSwipes()`, см. app.js). Переиспользует общий компонент
# `.sheet-backdrop`/`.sheet` (эталон — settings.js::diffBackdrop). ─────────────────────────────

def test_reject_sheet_built_on_shared_sheet_backdrop_component():
    text = _js_without_comments(APPLICATIONS_JS)
    assert "sheet-backdrop" in text
    assert re.search(r'class:\s*"[^"]*\bsheet\b[^"]*"', text), "класс листа не содержит sheet"
    assert 'role: "dialog"' in text
    assert '"aria-modal": "true"' in text
    assert 'tabindex: "-1"' in text


def test_reject_sheet_closes_via_backdrop_click_escape_and_cancel_button():
    text = _js_without_comments(APPLICATIONS_JS)
    assert re.search(r"e\.target\s*===\s*rejectBackdrop", text), "клик мимо листа не закрывает шторку"
    assert re.search(r'e\.key\s*===\s*"Escape"', text), "нет ветки Escape в keydown-обработчике"
    assert "texts.reject_cancel" in text, "кнопка «Отмена» не подписана из реестра"


def test_reject_sheet_keydown_listener_detached_on_close_and_unmount():
    text = _js_without_comments(APPLICATIONS_JS)
    removes = re.findall(r'removeEventListener\("keydown"', text)
    assert len(removes) >= 2, "снятие keydown-слушателя обязано быть и в closeRejectSheet(), и в unmount()"
    unmount_src = text[text.index("export function unmount"):]
    assert 'removeEventListener("keydown"' in unmount_src, "unmount() не снимает keydown-слушатель шторки"


def test_reject_sheet_backdrop_has_no_pointer_events_none_so_it_blocks_swipe():
    """Свайп карточки при открытой шторке блокируется не JS-гейтом, а физическим перехватом
    жеста затемнением (`.sheet-backdrop` без `pointer-events: none`, `position: fixed;
    inset: 0`) — см. интерфейс плана и общий компонент app.css:161-179."""
    css = APP_CSS.read_text(encoding="utf-8")
    backdrop_block = re.search(r"\.sheet-backdrop\s*\{([^}]*)\}", css)
    assert backdrop_block and "pointer-events: none" not in backdrop_block.group(1)
    assert "position: fixed" in backdrop_block.group(1) and "inset: 0" in backdrop_block.group(1)


def test_applications_screen_mass_approve_uses_shared_confirm_box():
    text = _js_without_comments(APPLICATIONS_JS)
    assert 'from "../form.js"' in text and "confirmBox" in text
    assert len(re.findall(r"confirmBox\(h,", text)) == 1


def test_applications_screen_history_renders_server_labels_not_raw_columns():
    """План 23-06 закрыл Known Stub 23-05: экран рисует `label`/`old`/`new`/`when`/
    `source_label`, которые уже перевёл сервер (`services.applications._history_entry`) —
    ни `row.column`, ни `row.source`, ни голого `row.changed_at` в JS больше нет."""
    text = _js_without_comments(APPLICATIONS_JS)
    assert "change.label" in text
    assert "entry.when" in text and "entry.source_label" in text
    assert "row.changed_at" not in text
    assert ".column" not in text and "row.source" not in text


def test_applications_screen_approve_all_confirm_shows_city_label():
    """D-07: подтверждение массового одобрения называет и число, и город — `city_label`
    приходит из ответа `/applications/next`, отдельной строкой под count-текстом (как
    appr_all_confirm бота), без литерала-подписи в JS."""
    text = _js_without_comments(APPLICATIONS_JS)
    assert "currentCard.city_label" in text
    assert "approveAllCityLine" in text


def test_applications_route_and_nav_registered_with_moderate_reg_cap():
    text = APP_JS.read_text(encoding="utf-8")
    assert text.count("#/applications") == 3  # ROUTES + NAV + NAV_ICONS
    nav_block = text[text.index("export const NAV ="):text.index("export const NAV_ICONS")]
    assert '"#/applications"' in nav_block and 'cap: "moderate_reg"' in nav_block and 'group: "apps"' in nav_block


# ── фикс «подложка закрывает кнопки» (квик 03.09): стопка карточек не должна геометрически
# или по каскаду перекрывать ряд кнопок решения -- см. applications.js::draw и .appl-stack/
# .appl-decide-row в app.css. ──────────────────────────────────────────────────────────────

def _find_call_args(text: str, marker: str) -> str:
    """Возвращает срез аргументов вызова `marker` (например `"cardHolder.replaceChildren("`)
    до закрывающей скобки на глубине 0 -- строковые литералы и комментарии не разбираются
    как код (скобки/запятые внутри них не сбивают подсчёт вложенности)."""
    start = text.index(marker) + len(marker)
    depth = 1
    i = start
    n = len(text)
    while i < n and depth > 0:
        if text[i] in "\"'`":
            quote = text[i]
            i += 1
            while i < n and text[i] != quote:
                i += 2 if text[i] == "\\" else 1
            i += 1
            continue
        if text[i:i + 2] == "/*":
            i = text.index("*/", i) + 2
            continue
        if text[i:i + 2] == "//":
            i = text.index("\n", i)
            continue
        if text[i] in "([{":
            depth += 1
        elif text[i] in ")]}":
            depth -= 1
        i += 1
    return text[start:i - 1]


def _split_top_level_args(s: str) -> list[str]:
    """Разбивает срез аргументов на верхнеуровневые (глубина 0) части по запятым."""
    parts, buf, depth, i, n = [], [], 0, 0, len(s)
    while i < n:
        c = s[i]
        if c in "\"'`":
            quote = c
            buf.append(c)
            i += 1
            while i < n and s[i] != quote:
                if s[i] == "\\" and i + 1 < n:
                    buf.append(s[i]); buf.append(s[i + 1]); i += 2
                    continue
                buf.append(s[i]); i += 1
            if i < n:
                buf.append(s[i]); i += 1
            continue
        if c in "([{":
            depth += 1
        elif c in ")]}":
            depth -= 1
        if c == "," and depth == 0:
            parts.append("".join(buf))
            buf = []
            i += 1
            continue
        buf.append(c)
        i += 1
    tail = "".join(buf).strip()
    if tail:
        parts.append(tail)
    return [p.strip() for p in parts if p.strip()]


def test_applications_decide_row_is_sibling_after_stack_not_nested_inside_it():
    """`.appl-decide-row` обязан быть ВЕРХНЕУРОВНЕВЫМ (не вложенным) ребёнком того же
    cardHolder, что и обёртка стопки `.appl-stack-wrap`, и идти ПОСЛЕ неё в разметке --
    иначе абсолютно спозиционированная тень стопки (z-index:0) красится поверх ряда кнопок
    независимо от того, что она визуально «позади» карточки (правило CSS2.1 stacking order:
    позиционированные потомки со stack level 0 красятся над статичным in-flow контентом
    независимо от порядка в DOM — разобрано в комментарии applications.js::draw)."""
    text = APPLICATIONS_JS.read_text(encoding="utf-8")
    args = _find_call_args(text, "cardHolder.replaceChildren(")
    top_level = _split_top_level_args(args)
    assert len(top_level) == 2, (
        f"ожидались ровно 2 верхнеуровневых ребёнка cardHolder.replaceChildren в draw(), "
        f"найдено {len(top_level)}"
    )
    stack_arg, decide_arg = top_level
    assert "appl-stack-wrap" in stack_arg, "первый ребёнок должен быть обёрткой стопки"
    assert "appl-decide-row" not in stack_arg, "ряд кнопок решения вложен внутрь обёртки стопки"
    assert "appl-decide-row" in decide_arg, "второй (следующий) ребёнок должен быть рядом кнопок решения"


def test_applications_stack_overlay_pointer_events_and_decide_row_stacking_in_css():
    """CSS-сторож того же фикса: тень стопки и оверлей свайпа не перехватывают тапы
    (pointer-events:none), ряд кнопок красится строго поверх тени (z-index выше), а скрытый
    тост (`.appl-toast.hidden`) не остаётся видимой пустой «таблеткой» из-за проигрыша
    каскаду `.appl-toast{display:inline-flex}` над общим `.hidden{display:none}` (найдено при
    разборе бага — оба правила с одинаковой специфичностью, `.appl-toast` объявлен ниже)."""
    css = APP_CSS.read_text(encoding="utf-8")
    stack_block = re.search(r"\.appl-stack\s*\{([^}]*)\}", css)
    assert stack_block and "pointer-events: none" in stack_block.group(1)
    overlay_block = re.search(r"\.appl-overlay\s*\{([^}]*)\}", css)
    assert overlay_block and "pointer-events: none" in overlay_block.group(1)
    decide_row_block = re.search(r"\.appl-decide-row\s*\{([^}]*)\}", css)
    assert decide_row_block and "z-index: 2" in decide_row_block.group(1)
    assert re.search(r"\.appl-toast\.hidden\s*\{\s*display:\s*none;\s*\}", css), (
        "нет явного .appl-toast.hidden{display:none} — скрытый тост снова рискует остаться "
        "видимой пустой таблеткой из-за каскада с .appl-toast{display:inline-flex}"
    )


# ── D17 (quick 260904-7e7): «.hidden» обязан побеждать в каскаде ВЕЗДЕ, не только у тоста ──
# Баг общего класса — переключение класса `hidden` не давало эффекта у «Показать всё» в
# отборе заявок, потому что одноклассовые правила `display` объявлены НИЖЕ `.hidden` в файле
# (равная специфичность 0,1,0 — побеждает порядок). Составные селекторы вида `.cls.hidden`
# (specificity 0,2,0, как `.appl-toast.hidden`) сильнее порядка и в проверку не входят.

_CSS_RULE_RE = re.compile(r"([^{}]+)\{([^{}]*)\}")
_SINGLE_CLASS_SELECTOR_RE = re.compile(r"^\.[a-z0-9_-]+$")
_JS_CLASS_ATTR_RE = re.compile(r'class:\s*"([^"]*)"')


def _strip_css_comments(css: str) -> str:
    """Комментарии `/* … */` заменяются пробелами той же длины (позиции остальных правил не
    сдвигаются) — иначе селектор перед правилом склеивается с текстом комментария и
    одноклассовый селектор перестаёт совпадать с `^\\.[a-z0-9_-]+$`."""
    return re.sub(r"/\*.*?\*/", lambda m: " " * len(m.group(0)), css, flags=re.DOTALL)


def _hidden_rule_position(css: str) -> int:
    """Позиция САМОГО правила `.hidden { ... }», не подстроки внутри составных вроде
    `.appl-toast.hidden` (у которых `.hidden {` тоже встречается как хвост селектора) и не
    упоминаний `.hidden{...}` в тексте комментариев (в файле есть пояснительные комментарии,
    которые цитируют само правило)."""
    m = re.search(r"(?<![.\w-])\.hidden\s*\{", _strip_css_comments(css))
    assert m, "нет правила .hidden в app.css"
    return m.start()


def _single_class_display_rules(css: str) -> list[tuple[int, str]]:
    """Список (позиция, селектор) одноклассовых правил, где объявлено свойство `display`."""
    css = _strip_css_comments(css)
    rules = []
    for m in _CSS_RULE_RE.finditer(css):
        body = m.group(2)
        if "display" not in body:
            continue
        for raw_sel in m.group(1).split(","):
            sel = raw_sel.strip()
            if _SINGLE_CLASS_SELECTOR_RE.match(sel):
                rules.append((m.start(), sel))
    return rules


def _composite_hidden_protected_classes(css: str) -> set[str]:
    """Классы, у которых уже есть составной селектор `.cls.hidden`/`.hidden.cls` с `display` —
    его специфичность (0,2,0) выше одноклассового, порядок в файле для них не важен."""
    css = _strip_css_comments(css)
    protected = set()
    for m in _CSS_RULE_RE.finditer(css):
        if "display" not in m.group(2):
            continue
        for raw_sel in m.group(1).split(","):
            sel = raw_sel.strip()
            if re.fullmatch(r"\.hidden\.[a-z0-9_-]+", sel):
                protected.add(f".{sel.split('.')[-1]}")
            elif re.fullmatch(r"\.[a-z0-9_-]+\.hidden", sel):
                protected.add(f".{sel.split('.')[1]}")
    return protected


def _js_class_strings_with_hidden(text: str) -> list[list[str]]:
    return [m.group(1).split() for m in _JS_CLASS_ATTR_RE.finditer(text) if "hidden" in m.group(1).split()]


def test_hidden_class_wins_the_cascade_for_every_toggled_class():
    """Test 1 (D17): для каждой строки `class: "… hidden …"` во всех screens/*.js и app.js ни
    один одноклассовый селектор из этой строки не задаёт `display` НИЖЕ `.hidden` в app.css.
    До фикса падает на `flat-list` (applications.js), `settings-search-clear` и
    `choice-chips` (settings.js) — после переноса `.hidden` в конец файла зелёный."""
    css = APP_CSS.read_text(encoding="utf-8")
    hidden_pos = _hidden_rule_position(css)
    single_class_display = _single_class_display_rules(css)
    protected = _composite_hidden_protected_classes(css)

    js_files = sorted((MINIAPP_STATIC / "js").rglob("*.js"))
    problems = []
    for path in js_files:
        text = path.read_text(encoding="utf-8")
        for classes in _js_class_strings_with_hidden(text):
            for cls in classes:
                if cls == "hidden":
                    continue
                sel = f".{cls}"
                if sel in protected:
                    continue
                if any(pos > hidden_pos for pos, s in single_class_display if s == sel):
                    problems.append(f"{path.name}: {sel}")
    assert not problems, f"классы теряют .hidden в каскаде (display объявлен ниже): {sorted(set(problems))}"


def test_hidden_rule_is_the_last_single_class_display_rule_in_app_css():
    """Test 2 (D17): `.hidden { display: none; }` — последнее одноклассовое правило файла,
    задающее `display`, чтобы никакой будущий блок CSS не отобрал у него каскад молча."""
    css = APP_CSS.read_text(encoding="utf-8")
    hidden_pos = _hidden_rule_position(css)
    single_class_display = _single_class_display_rules(css)
    after = [sel for pos, sel in single_class_display if pos > hidden_pos]
    assert not after, f"одноклассовые правила с display объявлены после .hidden: {after}"


# ── Phase 23.1: компоненты плиты (UI-REDESIGN-01) ────────────────────────────────────────────

def test_plate_components_exist_and_use_only_tokens():
    css = APP_CSS.read_text(encoding="utf-8")
    for selector in (".plate {", ".plate::before", ".next-action {", ".flat-list.flush {", ".screen-anchor {"):
        assert selector in css, f"нет селектора {selector} в app.css"
    assert "var(--font-heading-style)" in css
    assert "var(--plate-pattern" in css
    assert not _HEX_OR_RGB_COLOR.findall(css), "литеральный цвет в app.css"


def test_plate_alpha_tokens_declared_in_both_themes():
    text = MINIAPP_TOKENS.read_text(encoding="utf-8")
    for name in ("--on-accent-strong", "--on-accent-soft", "--on-accent-line", "--on-accent-rule", "--on-accent-wash"):
        assert text.count(name) == 2, f"{name}: ожидалось 2 вхождения (светлая+тёмная ветка), найдено {text.count(name)}"
    assert text == DASHBOARD_TOKENS.read_text(encoding="utf-8"), "tokens.css разошёлся с dashboard/static/tokens.css"


# ── Phase 23.1-04 (UI-REDESIGN-04): мастер анкеты — плита, живой прогресс, список вопросов ──

def test_no_inline_style_attribute_anywhere_in_frontend():
    """Живой дефект (план 23.1-04): `style-src 'self'` в `miniapp/main.py` без
    `'unsafe-inline'` — атрибут `style="…"` браузер молча отбрасывает, полоса прогресса
    мастера не заполнялась никогда. Писать в CSSOM (`el.style.x = …`) можно и нужно, эту
    политику она не ограничивает — сторож запрещён только атрибуту, не CSSOM-присвоению."""
    js_files = sorted((MINIAPP_STATIC / "js").glob("*.js")) + sorted(SCREENS_DIR.glob("*.js"))
    style_key_re = re.compile(r"\{[^}]*\bstyle\s*:")
    problems = []
    for path in js_files:
        text = _js_without_comments(path)
        if style_key_re.search(text):
            problems.append(path.name)
    assert not problems, f"ключ style: передан в h(...) в файлах {problems} — CSP его отбросит"

    template_problems = []
    for path in sorted(MINIAPP_TEMPLATES.rglob("*")):
        if path.is_file() and "style=" in path.read_text(encoding="utf-8"):
            template_problems.append(path.name)
    assert not template_problems, f"атрибут style= в шаблонах {template_problems}"


def test_form_screen_uses_plate_and_question_rows():
    text = _js_without_comments(SCREENS_DIR / "form.js")
    for token in ("plate--form", "wizard-field", "questionRow", "flat-list flush"):
        assert token in text, f"нет {token} в screens/form.js"
    assert 'setMainButton("→"' not in text
    assert not re.search(r"\{[^}]*\bstyle\s*:", text)

