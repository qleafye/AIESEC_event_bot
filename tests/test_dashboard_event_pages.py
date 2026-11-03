"""Страница одного события на сводке: та же почта, что у сравнения, база из реестра."""
from __future__ import annotations

import asyncio
import time
from types import SimpleNamespace
from urllib.parse import parse_qs

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from starlette.testclient import TestClient

from config import config as bot_config
from database import db as bot_db

from dashboard import cf_access, main as dash_main, sources_daily
from dashboard.cf_access import ACCESS_HEADER
from dashboard.config import DashboardConfig
from dashboard.main import (
    build_chat_context,
    build_feedback_context,
    build_page_context,
    create_app,
)
from dashboard.queries import Scope
from dashboard.registry import EventSource
from tests._dbtpl import fast_init_db

TEAM_DOMAIN = "aiesec"
AUD = "app-aud-tag"
ISSUER = f"https://{TEAM_DOMAIN}.cloudflareaccess.com"
SUPERADMIN_EMAIL = "admin@aiesec.ru"
VIEWER = {"telegram_id": None, "bound_city": None}


def _use_tmp_db(tmp_path, name: str) -> str:
    path = str(tmp_path / name)
    bot_config.DB_PATH = path
    fast_init_db()
    return path


async def _seed_async(*, settings=None, users=None, delegation_answers=None):
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
            await conn.execute(
                f"INSERT INTO users ({cols}) VALUES ({placeholders})", tuple(row.values()),
            )
        for row in delegation_answers or []:
            cols = ", ".join(row.keys())
            placeholders = ", ".join("?" for _ in row)
            await conn.execute(
                f"INSERT INTO delegation_answers ({cols}) VALUES ({placeholders})",
                tuple(row.values()),
            )
        await conn.commit()


def _seed(**kwargs):
    asyncio.run(_seed_async(**kwargs))


def _make_event_db(tmp_path, name, **seed_kwargs) -> str:
    path = _use_tmp_db(tmp_path, name)
    if seed_kwargs:
        _seed(**seed_kwargs)
    return path


def _user(tid, **overrides):
    row = {"telegram_id": tid, "registration_date": "2026-08-01 10:00:00", "status": "approved"}
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


def _client(cfg: DashboardConfig) -> TestClient:
    app = create_app(cfg=cfg)
    return TestClient(app, base_url="https://testserver")


@pytest.fixture(autouse=True)
def _reset_jwks():
    cf_access.reset_jwks_cache()
    yield
    cf_access.reset_jwks_cache()


def _keypair():
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    return private_key, private_key.public_key()


def _install_fixture_jwks(monkeypatch, public_key):
    class _FixtureJWKClient:
        def __init__(self, *args, **kwargs):
            pass

        def get_signing_key_from_jwt(self, token):
            return SimpleNamespace(key=public_key)

    monkeypatch.setattr(cf_access.jwt, "PyJWKClient", _FixtureJWKClient)


def _token(private_key, *, email=SUPERADMIN_EMAIL) -> str:
    now = int(time.time())
    payload = {"email": email, "aud": AUD, "iss": ISSUER, "iat": now - 10, "exp": now + 3600}
    return jwt.encode(payload, private_key, algorithm="RS256", headers={"kid": "test-kid"})


def _auth(monkeypatch, *, email=SUPERADMIN_EMAIL):
    cf_access.reset_jwks_cache()
    private_key, public_key = _keypair()
    _install_fixture_jwks(monkeypatch, public_key)
    return {ACCESS_HEADER: _token(private_key, email=email)}


def _two(tmp_path):
    path_a = _make_event_db(tmp_path, "a.db", settings={"event_name": "Альфа"})
    path_b = _make_event_db(tmp_path, "b.db", settings={"event_name": "Бета"})
    events = (EventSource(code="a", db_path=path_a), EventSource(code="b", db_path=path_b))
    return _cfg(events), path_a, path_b


def _query(href: str) -> dict:
    return parse_qs(href.split("?", 1)[-1].split("#", 1)[0])


# ── ссылки, которые собирает Python ──────────────────────────────────────────────────────

def test_sources_pairs_keep_event():
    query = sources_daily.SourcesQuery()
    pairs = query.pairs(city="spb", season="S", event="a")
    assert pairs[0] == ("event", "a")
    assert ("city", "spb") in pairs
    assert ("season", "S") in pairs
    bare = query.pairs(city=None, season=None)
    assert all(key != "event" for key, _value in bare)


def test_sources_href_carries_event():
    query = sources_daily.SourcesQuery(answers=("ВК",))
    href = query.href(city=None, season=None, event="a")
    assert _query(href)["event"] == ["a"]
    toggled = query.href(city=None, season=None, event="a", toggle=("src", "ВК"))
    assert _query(toggled)["event"] == ["a"]
    assert "src" not in _query(toggled)


def test_feedback_csv_href_carries_event(tmp_path):
    path = _make_event_db(tmp_path, "a.db")
    cfg = _cfg()
    from dashboard.db import read_conn

    with read_conn(path) as conn:
        with_event = build_feedback_context(conn, cfg, Scope(), VIEWER, event_code="a")
        without = build_feedback_context(conn, cfg, Scope(), VIEWER)
    assert _query(with_event["csv_summary_href"])["event"] == ["a"]
    assert _query(with_event["csv_answers_href"])["event"] == ["a"]
    assert "event" not in _query(without["csv_summary_href"])
    assert "event" not in _query(without["csv_answers_href"])
    assert without["event_code"] is None


def test_chat_period_href_carries_event(tmp_path):
    path = _make_event_db(tmp_path, "a.db")
    cfg = _cfg()
    from dashboard.db import read_conn

    with read_conn(path) as conn:
        with_event = build_chat_context(conn, cfg, Scope(), VIEWER, event_code="a")
        without = build_chat_context(conn, cfg, Scope(), VIEWER)
    assert with_event["period_links"]
    for link in with_event["period_links"]:
        assert _query(link["href"])["event"] == ["a"]
    for link in without["period_links"]:
        assert "event" not in _query(link["href"])


def test_cuts_links_carry_event(tmp_path):
    path = _make_event_db(tmp_path, "a.db")
    cfg = _cfg()
    from dashboard.db import read_conn

    with read_conn(path) as conn:
        ctx = build_page_context(conn, cfg, Scope(), VIEWER, event_code="a")
    assert _query(ctx["cuts_links"]["all"])["event"] == ["a"]
    assert _query(ctx["cuts_links"]["approved"])["event"] == ["a"]
    assert ctx["event_code"] == "a"


# ── маршруты ─────────────────────────────────────────────────────────────────────────────

def test_root_without_event_still_redirects_to_compare(tmp_path):
    cfg, _path_a, _path_b = _two(tmp_path)
    resp = _client(cfg).get("/", follow_redirects=False)
    assert resp.status_code == 302
    assert resp.headers["location"] == "/compare"


def test_bare_section_routes_still_redirect(tmp_path, monkeypatch):
    cfg, _path_a, _path_b = _two(tmp_path)
    opened = []

    def _boom(path):
        opened.append(path)
        raise AssertionError(path)

    monkeypatch.setattr(dash_main, "read_conn", _boom)
    client = _client(cfg)
    for path in ("/chat", "/forum", "/sources", "/feedback"):
        resp = client.get(path, follow_redirects=False)
        assert resp.status_code == 302
        assert resp.headers["location"] == "/compare"
    for path in ("/feedback.json", "/feedback.csv"):
        resp = client.get(path)
        assert resp.status_code == 404
        assert resp.json() == {"error": "not_found"}
    assert opened == []


def test_event_page_requires_email_before_code(tmp_path, monkeypatch):
    cfg, _path_a, _path_b = _two(tmp_path)
    notices = []
    monkeypatch.setattr(
        dash_main, "notify_access_request", lambda *args, **kwargs: notices.append(args),
    )
    client = _client(cfg)
    bodies = []
    for code in ("нет-такого", "a"):
        resp = client.get("/", params={"event": code})
        assert resp.status_code == 403
        bodies.append(resp.text)
        bad = client.get("/", params={"event": code}, headers={ACCESS_HEADER: "not-a-jwt"})
        assert bad.status_code == 403
        bodies.append(bad.text)
        outsider = client.get(
            "/", params={"event": code}, headers=_auth(monkeypatch, email="other@example.com"),
        )
        assert outsider.status_code == 403
        bodies.append(outsider.text)
    assert len(set(bodies)) == 1
    only = bodies[0]
    assert "нет-такого" not in only
    assert "Альфа" not in only
    assert "Бета" not in only
    assert notices == []


def test_unknown_event_after_login_is_404_and_does_not_open_process_db(tmp_path, monkeypatch):
    cfg, _path_a, _path_b = _two(tmp_path)
    opened = []
    monkeypatch.setattr(dash_main, "read_conn", lambda path: opened.append(path))
    resp = _client(cfg).get("/", params={"event": "нет-такого"}, headers=_auth(monkeypatch))
    assert resp.status_code == 404
    assert "Такого события в сводке нет." in resp.text
    assert 'href="/compare"' in resp.text
    assert "нет-такого" not in resp.text
    assert "Альфа" not in resp.text
    assert "Бета" not in resp.text
    prefix = _client(cfg).get("/", params={"event": "ab"}, headers=_auth(monkeypatch))
    assert prefix.status_code == 404
    assert opened == []


def _alpha_beta(tmp_path):
    path_a = _make_event_db(
        tmp_path, "a.db",
        settings={
            "event_name": "Альфа",
            "dashboard_block_referrals": "on",
            "dashboard_block_ambassadors": "on",
            "dashboard_block_delegations": "on",
        },
        users=[_user(1, participant_type="кейс", referrer_id=99, is_ambassador=1)],
        delegation_answers=[{
            "form_id": 1, "answer_id": "row-1", "ta_status": "ok", "university": "МГУ",
        }],
    )
    path_b = _make_event_db(
        tmp_path, "b.db",
        settings={"event_name": "Бета"},
        users=[_user(1)],
    )
    events = (EventSource(code="a", db_path=path_a), EventSource(code="b", db_path=path_b))
    return _cfg(events)


def test_event_page_renders_september_sections_from_that_db(tmp_path, monkeypatch):
    cfg = _alpha_beta(tmp_path)
    resp = _client(cfg).get("/", params={"event": "a"}, headers=_auth(monkeypatch))
    assert resp.status_code == 200
    text = resp.text
    assert 'id="moderation"' in text
    assert 'id="who"' in text
    assert 'id="delegations"' in text
    assert 'class="block-title">Рефералы' in text
    assert 'class="block-title">Амбассадоры' in text
    assert "Альфа" in text
    assert "Бета" not in text
    assert 'id="applications"' not in text


def test_section_pages_read_the_same_event(tmp_path, monkeypatch):
    cfg = _alpha_beta(tmp_path)
    client = _client(cfg)
    headers = _auth(monkeypatch)
    for path in ("/chat", "/forum", "/sources", "/feedback"):
        resp = client.get(path, params={"event": "a"}, headers=headers)
        assert resp.status_code == 200, path
        title = resp.text.split("<title>", 1)[1].split("</title>", 1)[0]
        assert "Альфа" in title
        assert "Бета" not in resp.text
    payload = client.get("/feedback.json", params={"event": "a"}, headers=headers)
    assert payload.status_code == 200
    assert payload.headers["content-type"].startswith("application/json")
    csv_resp = client.get("/feedback.csv", params={"event": "a"}, headers=headers)
    assert csv_resp.status_code == 200
    assert csv_resp.headers["content-type"].startswith("text/csv")
    outsider = client.get(
        "/feedback.csv", params={"event": "a"},
        headers=_auth(monkeypatch, email="other@example.com"),
    )
    assert outsider.status_code == 403
    assert outsider.headers["content-type"].startswith("application/json")
    assert outsider.json()["error"] == "no_access"
    assert "text/csv" not in outsider.headers["content-type"]


def test_broken_event_db_is_human(tmp_path, monkeypatch):
    path_a = _make_event_db(tmp_path, "a.db", settings={"event_name": "Альфа"})
    bad = str(tmp_path / "missing-dir" / "forum.db")
    cfg = _cfg((
        EventSource(code="a", db_path=path_a),
        EventSource(code="bad", db_path=bad),
    ))
    resp = _client(cfg).get("/", params={"event": "bad"}, headers=_auth(monkeypatch))
    assert resp.status_code == 200
    assert "Traceback" not in resp.text
    assert bad not in resp.text
    assert 'href="/compare"' in resp.text
    assert "файл базы не открылся" in resp.text


def test_single_mode_event_query_does_not_skip_login(tmp_path):
    solo = _make_event_db(tmp_path, "solo.db", settings={"event_name": "Альфа"})
    for events in ((), (EventSource(code="a", db_path=solo),)):
        cfg = _cfg(events)
        client = _client(cfg)
        resp = client.get("/", params={"event": "a"}, follow_redirects=False)
        assert resp.status_code == 302
        assert resp.headers["location"] == "/login"
        missing = client.get("/compare")
        assert missing.status_code == 404


def test_third_event_is_only_a_registry_row(tmp_path, monkeypatch):
    path_a = _make_event_db(tmp_path, "a.db", settings={"event_name": "Альфа"})
    path_b = _make_event_db(tmp_path, "b.db", settings={"event_name": "Бета"})
    path_c = _make_event_db(tmp_path, "c.db", settings={"event_name": "Гамма"})
    cfg = _cfg((
        EventSource(code="a", db_path=path_a),
        EventSource(code="b", db_path=path_b),
        EventSource(code="c", db_path=path_c),
    ))
    client = _client(cfg)
    headers = _auth(monkeypatch)
    compared = client.get("/compare", headers=headers)
    assert compared.status_code == 200
    assert "Гамма" in compared.text
    page = client.get("/", params={"event": "c"}, headers=headers)
    assert page.status_code == 200
    assert "Гамма" in page.text
    assert "Альфа" not in page.text
    assert not hasattr(cfg, "events_json")


def test_logo_suppressed_when_db_is_not_process_db(tmp_path, monkeypatch):
    file_id = "A" * 24
    path_a = _make_event_db(
        tmp_path, "a.db",
        settings={"event_name": "Альфа", "miniapp_logo": file_id},
    )
    path_b = _make_event_db(tmp_path, "b.db", settings={"event_name": "Бета"})
    cfg = _cfg((EventSource(code="a", db_path=path_a), EventSource(code="b", db_path=path_b)))
    assert cfg.db_path != path_a
    resp = _client(cfg).get("/", params={"event": "a"}, headers=_auth(monkeypatch))
    assert resp.status_code == 200
    assert "/api/file/" not in resp.text


def test_compare_name_links_to_event(tmp_path, monkeypatch):
    path_a = _make_event_db(tmp_path, "a.db", settings={"event_name": "Юлид"})
    bad = str(tmp_path / "missing-dir" / "forum.db")
    cfg = _cfg((
        EventSource(code="a", db_path=path_a),
        EventSource(code="bad", db_path=bad),
    ))
    resp = _client(cfg).get("/compare", headers=_auth(monkeypatch))
    assert resp.status_code == 200
    assert 'href="/?event=a"' in resp.text
    assert ">Юлид</a>" in resp.text
    assert "event=bad" not in resp.text
    assert ">a</a>" not in resp.text
