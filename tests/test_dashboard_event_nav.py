"""Ссылки страницы события не сбрасывают код. Одиночный стек остаётся без параметра event."""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import re
import time
from html import unescape
from types import SimpleNamespace

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from starlette.testclient import TestClient

from config import config as bot_config
from database import db as bot_db

from dashboard import cf_access
from dashboard.cf_access import ACCESS_HEADER
from dashboard.config import DashboardConfig
from dashboard.main import create_app
from dashboard.registry import EventSource
from tests._dbtpl import fast_init_db

TEAM_DOMAIN = "aiesec"
AUD = "app-aud-tag"
ISSUER = f"https://{TEAM_DOMAIN}.cloudflareaccess.com"
SUPERADMIN_EMAIL = "admin@aiesec.ru"
BOT_TOKEN = "123456:dummy-test-token"
ADMIN_ID = 1
STATS_MANAGER_ID = 900700

_HREF = re.compile(r'href="([^"]*)"')
_SKIP_PREFIXES = ("#", "/static/", "/theme.css", "/api/file/", "data:")


def _use_tmp_db(tmp_path, name: str) -> str:
    path = str(tmp_path / name)
    bot_config.DB_PATH = path
    fast_init_db()
    return path


async def _seed_async(*, settings=None, users=None, cities=None, checkins=None, staff=None):
    async with bot_db._connect() as conn:
        for code, label, enabled, sort_order in cities or []:
            await conn.execute(
                "INSERT INTO cities (code, label, enabled, sort_order, created_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (code, label, enabled, sort_order, "2026-01-01 00:00:00"),
            )
        for key, value in (settings or {}).items():
            await conn.execute(
                "INSERT INTO bot_settings (key, value) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (key, value),
            )
        for row in users or []:
            cols = ", ".join(row.keys())
            placeholders = ", ".join("?" for _ in row)
            await conn.execute(
                f"INSERT INTO users ({cols}) VALUES ({placeholders})", tuple(row.values()),
            )
        for row in checkins or []:
            await conn.execute(
                "INSERT INTO checkins (telegram_id, point, scanned_at, source, created_at, day) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                row,
            )
        for telegram_id, role, city in staff or []:
            await conn.execute(
                "INSERT INTO staff (telegram_id, role, added_by, added_at, city) "
                "VALUES (?, ?, ?, ?, ?)",
                (telegram_id, role, ADMIN_ID, "2026-01-01 00:00:00", city),
            )
        await conn.commit()


def _seed(**kwargs):
    asyncio.run(_seed_async(**kwargs))


def _cfg(events=(), **overrides) -> DashboardConfig:
    base = dict(
        db_path="data/forum.db",
        public_url="https://stats.example.com",
        session_secret="test-session-secret",
        bot_username="YouLead_test_bot",
        bot_token=BOT_TOKEN,
        admin_ids=(ADMIN_ID,),
        proxy_url=None,
        event_city_default="msk",
        trusted_proxies="172.31.0.0/16",
        events=events,
        access_team_domain=TEAM_DOMAIN,
        access_aud=AUD,
        superadmin_emails=(SUPERADMIN_EMAIL,),
        access_dev_bypass=False,
    )
    base.update(overrides)
    return DashboardConfig(**base)


def _client(cfg: DashboardConfig) -> TestClient:
    return TestClient(create_app(cfg=cfg), base_url="https://testserver")


@pytest.fixture(autouse=True)
def _reset_jwks():
    cf_access.reset_jwks_cache()
    yield
    cf_access.reset_jwks_cache()


def _auth(monkeypatch, *, email=SUPERADMIN_EMAIL):
    cf_access.reset_jwks_cache()
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)

    class _FixtureJWKClient:
        def __init__(self, *args, **kwargs):
            pass

        def get_signing_key_from_jwt(self, token):
            return SimpleNamespace(key=private_key.public_key())

    monkeypatch.setattr(cf_access.jwt, "PyJWKClient", _FixtureJWKClient)
    now = int(time.time())
    payload = {"email": email, "aud": AUD, "iss": ISSUER, "iat": now - 10, "exp": now + 3600}
    token = jwt.encode(payload, private_key, algorithm="RS256", headers={"kid": "test-kid"})
    return {ACCESS_HEADER: token}


def _event_db(tmp_path) -> DashboardConfig:
    path_a = _use_tmp_db(tmp_path, "a.db")
    _seed(
        settings={
            "event_name": "Альфа",
            "event_city_enabled": "on",
            "event_season": "2026",
            "delegate_chat_id": "-1001",
        },
        cities=[("msk", "Москва", 1, 1), ("spb", "Петербург", 1, 2)],
        users=[
            {"telegram_id": 1, "registration_date": "2026-08-01 10:00:00", "status": "approved"},
            {
                "telegram_id": 2,
                "registration_date": "2026-08-02 10:00:00",
                "status": "pending",
                "season": "2025",
            },
        ],
        checkins=[
            (1, "entry", "2026-10-03 10:00:00", "scan", "2026-10-03 10:00:00", "2026-10-03"),
            (1, "entry", "2026-10-04 11:00:00", "scan", "2026-10-04 11:00:00", "2026-10-04"),
        ],
    )
    path_b = _use_tmp_db(tmp_path, "b.db")
    _seed(settings={"event_name": "Бета"})
    events = (EventSource(code="a", db_path=path_a), EventSource(code="b", db_path=path_b))
    return _cfg(events)


def _hrefs(html: str) -> list[str]:
    return [unescape(match.group(1)) for match in _HREF.finditer(html)]


def _exception(href: str) -> bool:
    if href == "/compare" or href.startswith("http"):
        return True
    return any(href.startswith(prefix) for prefix in _SKIP_PREFIXES)


def _assert_keeps_event(html: str, code: str = "a") -> list[str]:
    hrefs = _hrefs(html)
    missing = [href for href in hrefs if not _exception(href) and f"event={code}" not in href]
    assert missing == [], missing
    return hrefs


def _has_path(hrefs: list[str], path: str, code: str = "a") -> bool:
    return any(
        href.split("?", 1)[0].split("#", 1)[0] == path and f"event={code}" in href
        for href in hrefs
    )


def _login_as(client: TestClient, telegram_id: int):
    payload = {
        "id": str(telegram_id),
        "first_name": "Тест",
        "auth_date": str(int(time.time()) - 5),
    }
    check_string = "\n".join(f"{k}={v}" for k, v in sorted(payload.items()))
    secret = hashlib.sha256(BOT_TOKEN.encode()).digest()
    payload["hash"] = hmac.new(secret, check_string.encode(), hashlib.sha256).hexdigest()
    return client.get("/auth/callback", params=payload, follow_redirects=False)


def test_event_dashboard_hrefs_keep_event(tmp_path, monkeypatch):
    client = _client(_event_db(tmp_path))
    resp = client.get("/?event=a", headers=_auth(monkeypatch))
    assert resp.status_code == 200, resp.text[:400]
    hrefs = _assert_keeps_event(resp.text)
    assert "/compare" in hrefs
    assert resp.text.count("К сравнению событий") == 1
    assert 'class="brand" href="/?event=a"' in resp.text
    assert "/" not in hrefs
    for path in ("/chat", "/forum", "/feedback", "/sources"):
        assert _has_path(hrefs, path), path
    city = [href for href in hrefs if "city=" in href]
    season = [href for href in hrefs if "season=" in href]
    assert city and all("event=a" in href for href in city)
    assert season and all("event=a" in href for href in season)


def test_event_subpages_keep_event(tmp_path, monkeypatch):
    client = _client(_event_db(tmp_path))
    headers = _auth(monkeypatch)
    pages = {}
    for path in ("/chat", "/forum", "/sources", "/feedback"):
        resp = client.get(f"{path}?event=a", headers=headers)
        assert resp.status_code == 200, (path, resp.text[:400])
        pages[path] = resp.text
        hrefs = _assert_keeps_event(resp.text)
        assert "/compare" in hrefs
        assert resp.text.count("К сравнению событий") == 1
        assert re.search(r'href="[^"]*event=a[^"]*">← К статистике</a>', resp.text)

    forum = _hrefs(pages["/forum"])
    assert _has_path(forum, "/feedback")
    arrival = [href for href in forum if "arrival_day=" in href]
    assert arrival and all("event=a" in href and href.endswith("#arrival") for href in arrival)
    assert any("city=" in href and "event=a" in href for href in forum)
    assert any("season=" in href and "event=a" in href for href in forum)

    sources = pages["/sources"]
    source_hrefs = _hrefs(sources)
    assert any(href.startswith("?") and "event=a" in href for href in source_hrefs)
    assert '<input type="hidden" name="event" value="a">' in sources
    assert 'action="/sources"' in sources
    assert 'action="/sources?' not in sources

    feedback = pages["/feedback"]
    assert 'fetch("/feedback.json" + window.location.search, { credentials: "same-origin" })' in feedback
    assert "/feedback.json?" not in feedback
    feedback_hrefs = _hrefs(feedback)
    assert _has_path(feedback_hrefs, "/forum")
    assert any("city=" in href and "event=a" in href for href in feedback_hrefs)
    assert any("season=" in href and "event=a" in href for href in feedback_hrefs)

    chat = _hrefs(pages["/chat"])
    assert any(href.startswith("/chat?") and "event=a" in href for href in chat)


def test_single_mode_hrefs_have_no_event(tmp_path):
    db_path = _use_tmp_db(tmp_path, "single.db")
    _seed(
        settings={
            "role_caps_reg_manager": "moderate_reg;stats",
            "delegate_chat_id": "-1001",
            "event_name": "Один стек",
        },
        staff=[(STATS_MANAGER_ID, "reg_manager", None)],
    )
    client = _client(_cfg(db_path=db_path, events=()))
    login = _login_as(client, STATS_MANAGER_ID)
    assert login.status_code in (302, 303)
    home = client.get("/")
    assert home.status_code == 200, home.text[:400]
    assert 'href="/chat"' in home.text
    for path in ("/", "/chat", "/forum", "/sources", "/feedback"):
        resp = home if path == "/" else client.get(path)
        assert resp.status_code == 200, (path, resp.text[:400])
        assert "event=" not in resp.text, path
        assert "К сравнению событий" not in resp.text
