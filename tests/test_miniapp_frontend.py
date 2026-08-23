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
