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
    _set("miniapp_accent", "#F48924")
    resp = _client(db_path).get("/app/theme.css")
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/css")
    assert "--accent: #F48924" in resp.text
    assert "--secondary: #F48924" in resp.text


def test_theme_css_garbage_accent_falls_back_to_default(tmp_path):
    db_path = _use_tmp_db(tmp_path, "miniapp_theme_bad.db")
    _standard_seed()
    client = _client(db_path)
    for bad in ("037EF3", "#GG0000", "", "#fff", "#037EF3; } body { background: url(x) }"):
        _set("miniapp_accent", bad)
        resp = client.get("/app/theme.css")
        assert resp.status_code == 200, bad
        assert resp.text == ":root { --accent: #037EF3; --secondary: #037EF3; }\n", bad


# ── фронт-ядро: таблица маршрутов (план 19-02, <route_table>) ───────────────────────────

EXPECTED_ROUTES = {
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
    "#/task-edit/new": "screens/task_edit.js",
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
    routes = _routes_from_app_js()
    assert routes == EXPECTED_ROUTES
    assert len(routes) == 13
    assert set(routes.values()) == set(EXPECTED_ROUTES.values())  # 12 модулей, task_edit — 2 маршрута


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


# ── экраны делегата (план 19-03): tasks / card / profile / coins / leaderboard ──────────

DELEGATE_SCREENS = ["tasks.js", "card.js", "profile.js", "coins.js", "leaderboard.js"]

# Пути, которые фронт зовёт через api("/...") / api(`/...`): литеральный префикс до `?`
# или `${` — сверяется с маршрутами приложения (ловит опечатку в пути).
_API_CALL = re.compile(r"""api\(\s*(["'`])/([^"'`?$]*)""")


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
            full = "/app/api/" + rel.rstrip("/")
            # `${...}` в середине пути — хвост после первого `${` уже отрезан регексом:
            # сверяем как префикс с одним параметром (например /tasks/ -> /tasks/{id}).
            candidates = [full, full + "/x", full + "x"]
            assert any(p.match(c) for p in patterns for c in candidates), (
                f"{path.name}: api('/{rel}') не соответствует ни одному маршруту приложения"
            )
    assert seen >= 6  # tasks, tasks/{id}, profile, coins/balance, coins/history, leaderboard


def test_screens_use_registry_empty_texts_not_literals():
    for name in ("tasks.js", "coins.js", "leaderboard.js"):
        text = _js_without_comments(SCREENS_DIR / name)
        assert "empty_text" in text, f"{name}: пустое состояние — текстом из реестра"


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
    # Счётчик частей, «Убрать последнее», «Готово» -> одиночный POST /submissions.
    assert "📸" in text and "📄" in text and "✍️" in text
    assert "Убрать последнее" in text
    assert 'setMainButton("Готово"' in text
    assert 'api("/submissions", {' in text and text.count('api("/submissions"') == 1
    assert "empty_hint" in text              # пустая отправка — подсказка, не сброс
    assert "err.status === 409" in text      # «Уже отправлено»
    assert 'navigate("#/tasks")' in text

