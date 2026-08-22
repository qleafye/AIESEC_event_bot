"""Phase 15 Plan 05 Task 1 (STAT-01/STAT-02): маршруты `dashboard/main.py`, работа за
Cloudflare Tunnel и модель доступа на каждый запрос — `fastapi.testclient.TestClient`.

Cookie D-09 (`https_only=True` константой, dashboard/auth.py) реально «прилипает» у httpx
только если `base_url` теста сам по себе `https://…` — иначе TestClient честно не пришлёт
Secure-cookie назад на следующий запрос (подтверждено вручную: тот же тест с
`base_url="http://testserver"` теряет сессию между запросами). Тесты про прокси-заголовки
намеренно используют `base_url="http://testserver"` — так видно, что схему меняет именно
`ProxyHeadersMiddleware`, а не сам транспорт.
"""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import logging
import time
from typing import Optional

from fastapi import Request
from starlette.testclient import TestClient

from config import config as bot_config
from database import db as bot_db

from dashboard.config import DashboardConfig, load_config
from dashboard.main import create_app

BOT_TOKEN = "123456:ABCDEF-testtoken"
ADMIN_ID = 900001
NO_STATS_ID = 900500
STATS_MANAGER_ID = 900600


def _use_tmp_db(tmp_path, name: str = "dashboard_routes.db") -> str:
    path = str(tmp_path / name)
    bot_config.DB_PATH = path
    asyncio.run(bot_db.init_db())
    return path


async def _seed_async(*, staff=None, settings=None):
    async with bot_db._connect() as conn:
        for telegram_id, role, city in staff or []:
            await conn.execute(
                "INSERT INTO staff (telegram_id, role, added_by, added_at, city) "
                "VALUES (?, ?, ?, ?, ?)",
                (telegram_id, role, ADMIN_ID, "2026-01-01 00:00:00", city),
            )
        for key, value in (settings or {}).items():
            await conn.execute(
                "INSERT INTO bot_settings (key, value) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (key, value),
            )
        await conn.commit()


def _seed(**kwargs):
    asyncio.run(_seed_async(**kwargs))


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


def _sign(payload: dict, bot_token: str = BOT_TOKEN) -> dict:
    data = {k: v for k, v in payload.items() if k != "hash"}
    check_string = "\n".join(f"{k}={v}" for k, v in sorted(data.items()))
    secret = hashlib.sha256(bot_token.encode()).digest()
    signature = hmac.new(secret, check_string.encode(), hashlib.sha256).hexdigest()
    return {**data, "hash": signature}


def _login_payload(telegram_id: int, **extra) -> dict:
    base = {
        "id": str(telegram_id),
        "first_name": "Тест",
        "auth_date": str(int(time.time()) - 5),
    }
    base.update(extra)
    return _sign(base)


def _client(cfg: DashboardConfig, **kwargs) -> TestClient:
    app = create_app(cfg=cfg)
    kwargs.setdefault("base_url", "https://testserver")
    return TestClient(app, **kwargs)


def _login(client: TestClient, telegram_id: int, **extra):
    payload = _login_payload(telegram_id, **extra)
    return client.get("/auth/callback", params=payload, follow_redirects=False)


class _FakeTelegramResponse:
    status_code = 200


class _FakeHTTPXClient:
    """Заглушка httpx.Client — без сети, считает вызовы sendMessage."""

    sent: list = []

    def __init__(self, *args, **kwargs):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def post(self, url, json=None, **kwargs):
        _FakeHTTPXClient.sent.append(json)
        return _FakeTelegramResponse()


# ── маршруты открытые/закрытые по умолчанию ─────────────────────────────────────────────

def test_root_without_session_redirects_to_login(tmp_path):
    db_path = _use_tmp_db(tmp_path)
    client = _client(_cfg(db_path))
    resp = client.get("/", follow_redirects=False)
    assert resp.status_code in (302, 307)
    assert resp.headers["location"] == "/login"


def test_login_and_health_open(tmp_path):
    db_path = _use_tmp_db(tmp_path)
    client = _client(_cfg(db_path))
    assert client.get("/login").status_code == 200
    assert client.get("/health").json() == {"status": "ok"}


def test_openapi_and_docs_disabled(tmp_path):
    db_path = _use_tmp_db(tmp_path)
    client = _client(_cfg(db_path))
    assert client.get("/openapi.json").status_code == 404
    assert client.get("/docs").status_code == 404
    assert client.get("/redoc").status_code == 404


# ── /auth/callback ───────────────────────────────────────────────────────────────────────

def test_auth_callback_bad_signature_rejected_no_session(tmp_path):
    db_path = _use_tmp_db(tmp_path)
    client = _client(_cfg(db_path))
    payload = _login_payload(NO_STATS_ID)
    payload["hash"] = "0" * 64
    resp = client.get("/auth/callback", params=payload, follow_redirects=False)
    assert resp.status_code == 400
    assert "set-cookie" not in {k.lower() for k in resp.headers.keys()}


def test_auth_callback_valid_signature_sets_secure_cookie_and_redirects(tmp_path):
    db_path = _use_tmp_db(tmp_path)
    client = _client(_cfg(db_path))
    resp = _login(client, NO_STATS_ID)
    assert resp.status_code == 302
    location = resp.headers["location"]
    assert location == "/" or location.startswith("https://")
    assert not location.startswith("http://")

    set_cookie = resp.headers.get("set-cookie", "")
    assert "Secure" in set_cookie or "secure" in set_cookie
    assert "HttpOnly" in set_cookie or "httponly" in set_cookie
    assert "samesite=lax" in set_cookie.lower()


# ── доступ по праву stats (D-09/D-11) ────────────────────────────────────────────────────

def test_viewer_without_stats_gets_403_no_access_page_and_one_notification(tmp_path, monkeypatch):
    monkeypatch.setattr("dashboard.notify._last_notified", {})
    _FakeHTTPXClient.sent = []
    monkeypatch.setattr("dashboard.notify.httpx.Client", _FakeHTTPXClient)

    db_path = _use_tmp_db(tmp_path)
    client = _client(_cfg(db_path))
    _login(client, NO_STATS_ID)

    r1 = client.get("/")
    r2 = client.get("/")
    assert r1.status_code == 403
    assert r2.status_code == 403
    assert "Статистика" in r1.text
    # антиспам (D-11): один человек в сутки, второй заход в тот же день не шлёт письмо снова
    assert len(_FakeHTTPXClient.sent) == 1


def test_spoofed_cf_connecting_ip_does_not_bypass_antispam_key(tmp_path, monkeypatch):
    monkeypatch.setattr("dashboard.notify._last_notified", {})
    _FakeHTTPXClient.sent = []
    monkeypatch.setattr("dashboard.notify.httpx.Client", _FakeHTTPXClient)

    db_path = _use_tmp_db(tmp_path)
    client = _client(_cfg(db_path))
    _login(client, NO_STATS_ID)

    client.get("/", headers={"CF-Connecting-IP": "1.1.1.1"})
    client.get("/", headers={"CF-Connecting-IP": "9.9.9.9"})
    # антиспам ключуется по telegram_id из сессии, а не по адресу — подделка адреса
    # не даёт второго уведомления.
    assert len(_FakeHTTPXClient.sent) == 1


def test_revoked_capability_closes_access_on_next_request_without_relogin(tmp_path):
    db_path = _use_tmp_db(tmp_path)
    _seed(
        staff=[(STATS_MANAGER_ID, "reg_manager", None)],
        settings={"role_caps_reg_manager": "moderate_reg;stats"},
    )
    client = _client(_cfg(db_path))
    _login(client, STATS_MANAGER_ID)

    r1 = client.get("/")
    assert r1.status_code == 200

    # Право снимают в БД бота между двумя запросами — тот же клиент, без перелогина.
    _seed(settings={"role_caps_reg_manager": "moderate_reg"})

    r2 = client.get("/")
    assert r2.status_code == 403


def test_unknown_city_or_season_query_does_not_crash_page(tmp_path):
    db_path = _use_tmp_db(tmp_path)
    _seed(
        staff=[(STATS_MANAGER_ID, "reg_manager", None)],
        settings={"role_caps_reg_manager": "moderate_reg;stats"},
    )
    client = _client(_cfg(db_path))
    _login(client, STATS_MANAGER_ID)
    resp = client.get("/", params={"city": "not-a-real-city", "season": "not-a-real-season"})
    assert resp.status_code == 200


# ── поверхность маршрутов (D-02/D-17/T-15-05-06) ─────────────────────────────────────────

def test_no_app_route_and_no_export_or_csv_route(tmp_path):
    db_path = _use_tmp_db(tmp_path)
    app = create_app(cfg=_cfg(db_path))
    paths = [getattr(route, "path", "") for route in app.fastapi_app.routes]
    assert not any(path.startswith("/app") for path in paths)
    assert not any("export" in path.lower() or "csv" in path.lower() for path in paths)


def test_security_headers_present_on_open_route(tmp_path):
    db_path = _use_tmp_db(tmp_path)
    client = _client(_cfg(db_path))
    resp = client.get("/health")
    assert resp.headers.get("x-content-type-options") == "nosniff"
    assert resp.headers.get("referrer-policy") == "no-referrer"
    assert resp.headers.get("x-frame-options") == "DENY"


# ── работа за Cloudflare Tunnel (D-03/T-15-05-08) ────────────────────────────────────────

def _mount_debug_scope_route(app):
    @app.fastapi_app.get("/_debug/scope")
    def _debug_scope(request: Request):
        return {
            "scheme": request.url.scheme,
            "client": request.client.host if request.client else None,
        }
    return app


def test_trusted_proxy_headers_upgrade_scheme_and_client(tmp_path):
    db_path = _use_tmp_db(tmp_path)
    app = create_app(cfg=_cfg(db_path))
    _mount_debug_scope_route(app)
    client = TestClient(app, base_url="http://testserver", client=("172.31.0.5", 0))

    resp = client.get(
        "/_debug/scope",
        headers={"X-Forwarded-Proto": "https", "X-Forwarded-For": "203.0.113.9"},
    )
    assert resp.json() == {"scheme": "https", "client": "203.0.113.9"}


def test_untrusted_address_proxy_headers_ignored(tmp_path):
    db_path = _use_tmp_db(tmp_path)
    app = create_app(cfg=_cfg(db_path))
    _mount_debug_scope_route(app)
    client = TestClient(app, base_url="http://testserver", client=("1.2.3.4", 0))

    resp = client.get(
        "/_debug/scope",
        headers={"X-Forwarded-Proto": "https", "X-Forwarded-For": "203.0.113.9"},
    )
    body = resp.json()
    assert body["scheme"] == "http"
    assert body["client"] != "203.0.113.9"


def test_wildcard_trusted_proxies_logs_warning(caplog):
    with caplog.at_level(logging.WARNING, logger="dashboard.config"):
        cfg = load_config(env={"DASHBOARD_SESSION_SECRET": "x", "DASHBOARD_TRUSTED_PROXIES": "*"})
    assert cfg.trusted_proxies == "*"
    assert any("доверяем заголовкам" in record.message for record in caplog.records)
