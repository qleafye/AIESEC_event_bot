"""Phase 26.1 Plan 02 Task 2 (SD-03/SD-04/SD-08): маршрут `/compare` за гейтом Cloudflare
Access и поведение `/` в мульти-режиме. Свой файл — не конфликтует с существующими правками
`tests/test_dashboard_routes.py`.

Токен Access — фикстурная RSA-пара (тот же приём, что `tests/test_dashboard_cf_access.py`),
подставляется через monkeypatch `dashboard.cf_access.jwt.PyJWKClient`: маршрут вызывает
`require_superadmin_email(request, cfg)` БЕЗ явного `jwks_client`, поэтому шов — на уровне
класса, из которого модуль строит свой ленивый синглтон (тот же шов, что в задаче 1).
"""
from __future__ import annotations

import asyncio
import re
import time
from types import SimpleNamespace

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from starlette.testclient import TestClient

from config import config as bot_config
from database import db as bot_db

from dashboard import cf_access, compare
from dashboard.cf_access import ACCESS_HEADER
from dashboard.config import DashboardConfig
from dashboard.main import create_app
from dashboard.registry import EventSource

TEAM_DOMAIN = "aiesec"
AUD = "app-aud-tag"
ISSUER = f"https://{TEAM_DOMAIN}.cloudflareaccess.com"
SUPERADMIN_EMAIL = "admin@aiesec.ru"


# ── фикстуры баз событий (тот же приём, что tests/test_dashboard_compare.py) ─────────────

def _use_tmp_db(tmp_path, name: str) -> str:
    path = str(tmp_path / name)
    bot_config.DB_PATH = path
    asyncio.run(bot_db.init_db())
    return path


async def _seed_async(*, settings=None, users=None):
    async with bot_db._connect() as conn:
        for key, value in (settings or {}).items():
            await conn.execute(
                "INSERT INTO bot_settings (key, value) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (key, value),
            )
        for row in users or []:
            cols = ", ".join(row.keys())
            placeholders = ", ".join("?" for _ in row)
            await conn.execute(f"INSERT INTO users ({cols}) VALUES ({placeholders})", tuple(row.values()))
        await conn.commit()


def _seed(**kwargs):
    asyncio.run(_seed_async(**kwargs))


def _make_event_db(tmp_path, name, **seed_kwargs) -> str:
    path = _use_tmp_db(tmp_path, name)
    if seed_kwargs:
        _seed(**seed_kwargs)
    return path


def _users_row(tid, *, registration_date, status="approved", **overrides):
    row = {"telegram_id": tid, "registration_date": registration_date, "status": status}
    row.update(overrides)
    return row


def _cfg(events=(), **overrides) -> DashboardConfig:
    base = dict(
        db_path="data/forum.db",
        public_url="https://stats.example.com",
        session_secret="test-session-secret",
        bot_username="YouLead_test_bot",
        bot_token="123:abc",
        admin_ids=(1,),
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


def _client(cfg: DashboardConfig, **kwargs) -> TestClient:
    app = create_app(cfg=cfg)
    kwargs.setdefault("base_url", "https://testserver")
    return TestClient(app, **kwargs)


# ── фикстурный токен Access: monkeypatch на уровне класса PyJWKClient ───────────────────

@pytest.fixture(autouse=True)
def _reset_cache_and_jwks(monkeypatch):
    compare.reset_cache()
    cf_access.reset_jwks_cache()
    yield
    compare.reset_cache()
    cf_access.reset_jwks_cache()


def _keypair():
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    return private_key, private_key.public_key()


def _install_fixture_jwks(monkeypatch, public_key):
    """Route вызывает `require_superadmin_email(request, cfg)` без `jwks_client` — синглтон
    строится модулем сам. Monkeypatch `jwt.PyJWKClient` классом, чей инстанс всегда отдаёт наш
    фикстурный публичный ключ, независимо от URL/kid."""

    class _FixtureJWKClient:
        def __init__(self, *args, **kwargs):
            pass

        def get_signing_key_from_jwt(self, token):
            return SimpleNamespace(key=public_key)

    monkeypatch.setattr(cf_access.jwt, "PyJWKClient", _FixtureJWKClient)


def _access_token(private_key, *, email=SUPERADMIN_EMAIL, aud=AUD, iss=ISSUER) -> str:
    now = int(time.time())
    payload = {"email": email, "aud": aud, "iss": iss, "iat": now - 10, "exp": now + 3600}
    return jwt.encode(payload, private_key, algorithm="RS256", headers={"kid": "test-kid"})


def _access_headers(token: str) -> dict:
    return {ACCESS_HEADER: token}


def _cell_value(html: str, name_marker: str, data_label: str) -> str:
    """Значение ячейки KPI-таблицы для строки события `name_marker` под колонкой
    `data_label`. Ищем ТОЛЬКО внутри секции «Событие рядом» — имя события до неё уже
    встречается в ряду «фишек» (event-chips), где нет `data-label`, но ЕСТЬ имя другого
    события впереди по документу, и наивный поиск «от первого вхождения имени» уводил бы
    на чужую ячейку."""
    table_start = html.find("Событие рядом")
    assert table_start != -1, "секция «Событие рядом» не найдена в разметке"
    scoped = html[table_start:]
    pattern = re.compile(
        re.escape(name_marker) + r".*?data-label=\"" + re.escape(data_label) + r"\">\s*([0-9]+)",
        re.S,
    )
    match = pattern.search(scoped)
    assert match, f"cell not found: событие={name_marker!r}, колонка={data_label!r}"
    return match.group(1)


# ── тесты ─────────────────────────────────────────────────────────────────────────────

def test_no_header_returns_403_super_only_text(tmp_path):
    path_a = _make_event_db(tmp_path, "a.db", users=[_users_row(1, registration_date="2026-08-01 10:00:00")])
    path_b = _make_event_db(tmp_path, "b.db", users=[_users_row(1, registration_date="2026-08-01 10:00:00")])
    cfg = _cfg((EventSource(code="a", db_path=path_a), EventSource(code="b", db_path=path_b)))
    client = _client(cfg)

    resp = client.get("/compare")
    assert resp.status_code == 403
    assert "только суперадминам" in resp.text


def test_valid_token_email_outside_list_returns_403(tmp_path, monkeypatch):
    private_key, public_key = _keypair()
    _install_fixture_jwks(monkeypatch, public_key)
    path_a = _make_event_db(tmp_path, "a.db", users=[_users_row(1, registration_date="2026-08-01 10:00:00")])
    path_b = _make_event_db(tmp_path, "b.db", users=[_users_row(1, registration_date="2026-08-01 10:00:00")])
    cfg = _cfg((EventSource(code="a", db_path=path_a), EventSource(code="b", db_path=path_b)))
    client = _client(cfg)

    token = _access_token(private_key, email="stranger@aiesec.ru")
    resp = client.get("/compare", headers=_access_headers(token))
    assert resp.status_code == 403
    assert "только суперадминам" in resp.text


def test_valid_token_from_list_returns_200_with_both_event_names(tmp_path, monkeypatch):
    private_key, public_key = _keypair()
    _install_fixture_jwks(monkeypatch, public_key)
    path_a = _make_event_db(
        tmp_path, "a.db",
        settings={"event_name": "Юлид'26"},
        users=[_users_row(1, registration_date="2026-08-01 10:00:00")],
    )
    path_b = _make_event_db(
        tmp_path, "b.db",
        settings={"event_name": "РилТолк Форум"},
        users=[_users_row(1, registration_date="2026-08-01 10:00:00")],
    )
    cfg = _cfg((EventSource(code="a", db_path=path_a), EventSource(code="b", db_path=path_b)))
    client = _client(cfg)

    token = _access_token(private_key)
    resp = client.get("/compare", headers=_access_headers(token))
    assert resp.status_code == 200
    assert "Юлид" in resp.text
    assert "РилТолк" in resp.text


def test_notify_access_request_never_called_on_compare(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr("dashboard.main.notify_access_request", lambda *a, **kw: calls.append((a, kw)))
    private_key, public_key = _keypair()
    _install_fixture_jwks(monkeypatch, public_key)
    path_a = _make_event_db(tmp_path, "a.db", users=[_users_row(1, registration_date="2026-08-01 10:00:00")])
    path_b = _make_event_db(tmp_path, "b.db", users=[_users_row(1, registration_date="2026-08-01 10:00:00")])
    cfg = _cfg((EventSource(code="a", db_path=path_a), EventSource(code="b", db_path=path_b)))
    client = _client(cfg)

    # Без заголовка, с плохим токеном, с валидным токеном вне списка, с валидным токеном из списка.
    client.get("/compare")
    client.get("/compare", headers=_access_headers("not-a-real-jwt"))
    client.get("/compare", headers=_access_headers(_access_token(private_key, email="stranger@aiesec.ru")))
    client.get("/compare", headers=_access_headers(_access_token(private_key)))

    assert calls == []


def test_single_registry_compare_returns_404(tmp_path):
    db_path = _use_tmp_db(tmp_path, "solo.db")
    cfg = _cfg(events=(), db_path=db_path)  # пустой реестр -> multi_mode ложно
    client = _client(cfg)
    resp = client.get("/compare")
    assert resp.status_code == 404


def test_events_query_one_known_one_garbage_returns_200(tmp_path, monkeypatch):
    private_key, public_key = _keypair()
    _install_fixture_jwks(monkeypatch, public_key)
    path_a = _make_event_db(tmp_path, "a.db", users=[_users_row(1, registration_date="2026-08-01 10:00:00")])
    path_b = _make_event_db(tmp_path, "b.db", users=[_users_row(1, registration_date="2026-08-01 10:00:00")])
    cfg = _cfg((EventSource(code="a", db_path=path_a), EventSource(code="b", db_path=path_b)))
    client = _client(cfg)

    token = _access_token(private_key)
    resp = client.get("/compare", params={"events": "a,mustery-garbage-code"}, headers=_access_headers(token))
    assert resp.status_code == 200


def test_axis_nonsense_falls_back_to_day_n_returns_200(tmp_path, monkeypatch):
    private_key, public_key = _keypair()
    _install_fixture_jwks(monkeypatch, public_key)
    path_a = _make_event_db(tmp_path, "a.db", users=[_users_row(1, registration_date="2026-08-01 10:00:00")])
    path_b = _make_event_db(tmp_path, "b.db", users=[_users_row(1, registration_date="2026-08-01 10:00:00")])
    cfg = _cfg((EventSource(code="a", db_path=path_a), EventSource(code="b", db_path=path_b)))
    client = _client(cfg)

    token = _access_token(private_key)
    resp = client.get("/compare", params={"axis": "nonsense"}, headers=_access_headers(token))
    assert resp.status_code == 200


def test_seasons_switch_changes_numbers_only_for_that_event(tmp_path, monkeypatch):
    private_key, public_key = _keypair()
    _install_fixture_jwks(monkeypatch, public_key)
    path_a = _make_event_db(
        tmp_path, "a.db",
        settings={"event_name": "Юлид'26", "event_season": "YL26"},
        users=[
            _users_row(1, registration_date="2026-08-01 10:00:00", season="YL26"),
            _users_row(2, registration_date="2025-08-01 10:00:00", season="YL25"),
            _users_row(3, registration_date="2025-08-02 10:00:00", season="YL25"),
        ],
    )
    path_b = _make_event_db(
        tmp_path, "b.db",
        settings={"event_name": "РилТолк Форум", "event_season": "RT26"},
        users=[_users_row(1, registration_date="2026-08-01 10:00:00", season="RT26")],
    )
    cfg = _cfg((EventSource(code="a", db_path=path_a), EventSource(code="b", db_path=path_b)))
    client = _client(cfg)
    token = _access_token(private_key)

    resp_current = client.get("/compare", headers=_access_headers(token))
    assert resp_current.status_code == 200

    resp_switched = client.get(
        "/compare", params={"seasons": "a:YL25"}, headers=_access_headers(token),
    )
    assert resp_switched.status_code == 200
    # Сезон a переключён на YL25 (2 заявки), b не задет (1 заявка, RT26 без изменений).
    # Маркер без апострофа — Jinja2 экранирует «'» в «&#39;» при автоэскейпе.
    assert _cell_value(resp_switched.text, "Юлид", "Заявок") == "2"
    assert _cell_value(resp_switched.text, "РилТолк", "Заявок") == "1"
    assert _cell_value(resp_current.text, "Юлид", "Заявок") == "1"


def test_seasons_garbage_value_returns_200_without_crash(tmp_path, monkeypatch):
    private_key, public_key = _keypair()
    _install_fixture_jwks(monkeypatch, public_key)
    path_a = _make_event_db(tmp_path, "a.db", users=[_users_row(1, registration_date="2026-08-01 10:00:00")])
    path_b = _make_event_db(tmp_path, "b.db", users=[_users_row(1, registration_date="2026-08-01 10:00:00")])
    cfg = _cfg((EventSource(code="a", db_path=path_a), EventSource(code="b", db_path=path_b)))
    client = _client(cfg)

    token = _access_token(private_key)
    resp = client.get("/compare", params={"seasons": "совсем не то, чего ждали"}, headers=_access_headers(token))
    assert resp.status_code == 200


def test_root_redirects_to_compare_in_multi_mode(tmp_path):
    path_a = _make_event_db(tmp_path, "a.db")
    path_b = _make_event_db(tmp_path, "b.db")
    cfg = _cfg((EventSource(code="a", db_path=path_a), EventSource(code="b", db_path=path_b)))
    client = _client(cfg)

    resp = client.get("/", follow_redirects=False)
    assert resp.status_code == 302
    assert resp.headers["location"] == "/compare"


def test_root_single_mode_unchanged_redirects_to_login(tmp_path):
    db_path = _use_tmp_db(tmp_path, "solo.db")
    cfg = DashboardConfig(
        db_path=db_path,
        public_url="https://yl26.example.com",
        session_secret="test-session-secret",
        bot_username="YouLead_test_bot",
        bot_token="123:abc",
        admin_ids=(1,),
        proxy_url=None,
        event_city_default="msk",
        trusted_proxies="172.31.0.0/16",
    )
    client = _client(cfg)

    resp = client.get("/", follow_redirects=False)
    assert resp.status_code == 302
    assert resp.headers["location"] == "/login"
