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
    assert text.index(TELEGRAM_SDK_URL) < text.index("/app/static/js/app.js")
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
    assert "/app/static/js/app.js" not in resp.text  # ядро не грузится на заглушке
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
    assert len(routes) == 13
    assert set(routes.values()) == set(EXPECTED_ROUTES.values())  # 13 модулей, у task_edit один маршрут
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
    pattern = re.compile(r"innerHTML\s*\+?=\s*(`|[^;]*\+)")
    for path in (API_JS, APP_JS, *(SCREENS_DIR.glob("*.js") if SCREENS_DIR.is_dir() else ())):
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


def test_profile_screen_opens_deeplink_via_telegram():
    text = _js_without_comments(SCREENS_DIR / "profile.js")
    assert "openTelegramLink" in text and "edit_deeplink" in text
    assert "Изменить — в боте" in text


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
    # Ошибки сервера — человеческим текстом из payload.text.
    assert "err.payload.text" in text
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


def test_settings_screen_confirms_dangerous_direction_and_warns_on_self_disable():
    """260824-8qw (MD-03): «в один тап» больше не правда для отнимающего направления двух
    самых дорогих тумблеров — второй тап с текстом последствий из реестра."""
    text = _js_without_comments(SCREENS_DIR / "settings.js")
    assert 'api("/admin/settings")' in text
    assert 'api("/admin/settings", { method: "POST"' in text
    # Тумблеры, способные выключить приложение у всех, — отдельным блоком с предупреждением.
    assert "miniapp_enabled" in text and "miniapp_staff_only" in text
    assert "спрячет приложение" in text
    # Опасное направление — второй тап через confirm-box (паттерн task_edit.js); текст
    # последствий приходит с сервера -- в JS нет ни одного литерала предупреждения.
    assert "confirm-box" in text
    assert "item.confirm" in text
    assert "Выключить Mini App" not in text
    assert "Скрыть приложение от делегатов" not in text


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
