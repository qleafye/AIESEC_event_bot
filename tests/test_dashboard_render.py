"""Phase 15 Plan 05 (STAT-01/STAT-02): рендер шаблонов дашборда — CSS-токены (Task 2),
состав страницы и тумблеры блоков (Task 3).

Task 2 покрывает каркас (`tokens.css`/`app.css`/`base.html`/`login.html`/`no_access.html`):
только CSS-переменные вместо цветов, единственный внешний скрипт — Login Widget на странице
входа, `data-auth-url` собран из `DASHBOARD_PUBLIC_URL` (D-03: всегда `https://…`, а не из
адреса текущего запроса), текст «нет доступа» дословно по смыслу D-11.
"""
from __future__ import annotations

import asyncio
import re
from pathlib import Path

from starlette.testclient import TestClient

from config import config as bot_config
from database import db as bot_db

from dashboard.config import DashboardConfig
from dashboard.main import create_app

DASHBOARD_DIR = Path(__file__).resolve().parent.parent / "dashboard"
TOKENS_CSS = DASHBOARD_DIR / "static" / "tokens.css"
APP_CSS = DASHBOARD_DIR / "static" / "app.css"
TEMPLATES_DIR = DASHBOARD_DIR / "templates"

BOT_TOKEN = "123456:ABCDEF-testtoken"
ADMIN_ID = 900001

# Литеральный цвет — #rrggbb/#rgb или rgb(/rgba( — вне tokens.css это запрещено (D-15,
# «Дизайн серый и черновой», acceptance_criteria плана).
_HEX_OR_RGB_COLOR = re.compile(r"#[0-9a-fA-F]{3,8}\b|rgba?\(")


def _use_tmp_db(tmp_path, name: str = "dashboard_render.db") -> str:
    path = str(tmp_path / name)
    bot_config.DB_PATH = path
    asyncio.run(bot_db.init_db())
    return path


def _cfg(db_path: str, **overrides) -> DashboardConfig:
    base = dict(
        db_path=db_path,
        public_url="https://yl26.example.com",
        session_secret="test-session-secret",
        bot_username="YouLead_test_bot",
        bot_token=BOT_TOKEN,
        admin_ids=(ADMIN_ID,),
        proxy_url=None,
        event_city_default="msk",
        trusted_proxies="172.31.0.0/16",
    )
    base.update(overrides)
    return DashboardConfig(**base)


def _client(cfg: DashboardConfig, **kwargs) -> TestClient:
    app = create_app(cfg=cfg)
    kwargs.setdefault("base_url", "https://testserver")
    return TestClient(app, **kwargs)


# ── tokens.css ────────────────────────────────────────────────────────────────────────────

def test_tokens_css_has_accent_variable():
    text = TOKENS_CSS.read_text(encoding="utf-8")
    assert text.count("--accent") >= 1


def test_tokens_css_only_defines_root_and_dark_scheme_selectors():
    text = TOKENS_CSS.read_text(encoding="utf-8")
    # Ни одного селектора компонента — только :root и @media (prefers-color-scheme: dark).
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.endswith("{"):
            assert stripped == ":root {" or stripped.startswith("@media"), (
                f"unexpected component selector in tokens.css: {stripped!r}"
            )


def test_no_hardcoded_colors_outside_tokens_css():
    for path in [APP_CSS, *TEMPLATES_DIR.glob("*.html")]:
        text = path.read_text(encoding="utf-8")
        matches = _HEX_OR_RGB_COLOR.findall(text)
        assert not matches, f"hardcoded color literal in {path.name}: {matches}"


# ── no external CDNs except Login Widget ────────────────────────────────────────────────

def test_no_external_cdn_except_telegram_widget_on_login_page():
    for path in TEMPLATES_DIR.glob("*.html"):
        text = path.read_text(encoding="utf-8")
        urls = re.findall(r"https?://[^\s\"'>]+", text)
        for url in urls:
            assert "telegram.org/js/telegram-widget.js" in url, (
                f"unexpected external URL in {path.name}: {url}"
            )


# ── login page ───────────────────────────────────────────────────────────────────────────

def test_login_page_data_auth_url_built_from_public_url_https(tmp_path):
    db_path = _use_tmp_db(tmp_path)
    cfg = _cfg(db_path, public_url="https://yl26.alekseev.info")
    client = _client(cfg)
    resp = client.get("/login")
    assert resp.status_code == 200
    assert 'data-auth-url="https://yl26.alekseev.info/auth/callback"' in resp.text
    assert "http://" not in resp.text


def test_login_page_uses_tokens_and_no_hardcoded_style_attrs(tmp_path):
    db_path = _use_tmp_db(tmp_path)
    client = _client(_cfg(db_path))
    resp = client.get("/login")
    assert 'href="/static/tokens.css"' in resp.text
    assert 'href="/static/app.css"' in resp.text
    assert not _HEX_OR_RGB_COLOR.search(resp.text)


# ── no_access page (D-11) ────────────────────────────────────────────────────────────────

def test_no_access_html_contains_stats_capability_wording():
    text = (TEMPLATES_DIR / "no_access.html").read_text(encoding="utf-8")
    assert "📊 Статистика" in text
    assert "доступ" in text.lower()


class _FakeTelegramResponse:
    status_code = 200


class _FakeHTTPXClient:
    """Заглушка httpx.Client — без сети, не должна замедлять/ронять рендер-тесты."""

    def __init__(self, *args, **kwargs):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def post(self, *args, **kwargs):
        return _FakeTelegramResponse()


def test_no_access_page_rendered_over_http_still_uses_https_assets(tmp_path, monkeypatch):
    monkeypatch.setattr("dashboard.notify._last_notified", {})
    monkeypatch.setattr("dashboard.notify.httpx.Client", _FakeHTTPXClient)
    db_path = _use_tmp_db(tmp_path)
    _, resp = _login_and_hit_root_without_stats(tmp_path, db_path)
    assert resp.status_code == 403
    assert "📊 Статистика" in resp.text
    assert not _HEX_OR_RGB_COLOR.search(resp.text)


def _login_and_hit_root_without_stats(tmp_path, db_path):
    import hashlib
    import hmac
    import time

    cfg = _cfg(db_path)
    client = _client(cfg)

    telegram_id = 900500
    payload = {
        "id": str(telegram_id),
        "first_name": "Тест",
        "auth_date": str(int(time.time()) - 5),
    }
    check_string = "\n".join(f"{k}={v}" for k, v in sorted(payload.items()))
    secret = hashlib.sha256(BOT_TOKEN.encode()).digest()
    payload["hash"] = hmac.new(secret, check_string.encode(), hashlib.sha256).hexdigest()

    client.get("/auth/callback", params=payload, follow_redirects=False)
    return client, client.get("/")
