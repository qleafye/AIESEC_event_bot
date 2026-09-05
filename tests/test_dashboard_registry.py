"""Phase 26.1 Plan 01 (SD-01): разбор реестра событий супердашборда + bootstrap-ключи
периметра Cloudflare Access в `dashboard.config`.
"""
from __future__ import annotations

import logging
import os

import pytest

from dashboard.config import DashboardConfig, load_config
from dashboard.registry import EventSource, multi_mode, parse_events

BASE_ENV = {"DASHBOARD_SESSION_SECRET": "s3cr3t"}


# ── parse_events: разбор валидной строки ────────────────────────────────────────────────

def test_parse_events_both_separators():
    events = parse_events("a=/x/1.db;b=/x/2.db")
    assert events == (
        EventSource(code="a", db_path=os.path.abspath("/x/1.db")),
        EventSource(code="b", db_path=os.path.abspath("/x/2.db")),
    )


def test_parse_events_newline_separator():
    events = parse_events("a=/x/1.db\nb=/x/2.db")
    assert [e.code for e in events] == ["a", "b"]


def test_parse_events_order_preserved():
    events = parse_events("z=/x/z.db;a=/x/a.db;m=/x/m.db")
    assert [e.code for e in events] == ["z", "a", "m"]


def test_parse_events_relative_path_absolutized():
    events = parse_events("a=relative/path.db")
    assert events[0].db_path == os.path.abspath("relative/path.db")
    assert os.path.isabs(events[0].db_path)


# ── fail-soft по записи ──────────────────────────────────────────────────────────────────

def test_parse_events_empty_string_returns_empty_tuple():
    assert parse_events("") == ()
    assert parse_events(None) == ()  # type: ignore[arg-type]


def test_parse_events_skips_record_without_equals(caplog):
    with caplog.at_level(logging.WARNING):
        events = parse_events("a=/x/a.db;garbage;b=/x/b.db")
    assert [e.code for e in events] == ["a", "b"]
    assert any("=" in rec.message or "garbage" in rec.message for rec in caplog.records)


def test_parse_events_skips_record_with_empty_path(caplog):
    with caplog.at_level(logging.WARNING):
        events = parse_events("a=;b=/x/b.db")
    assert [e.code for e in events] == ["b"]
    assert any("пустой путь" in rec.message for rec in caplog.records)


def test_parse_events_skips_uppercase_code(caplog):
    with caplog.at_level(logging.WARNING):
        events = parse_events("YL26=/x/a.db;b=/x/b.db")
    assert [e.code for e in events] == ["b"]
    assert any("не подходит под формат" in rec.message for rec in caplog.records)


def test_parse_events_skips_cyrillic_code(caplog):
    with caplog.at_level(logging.WARNING):
        events = parse_events("юлид=/x/a.db;b=/x/b.db")
    assert [e.code for e in events] == ["b"]
    assert any("не подходит под формат" in rec.message for rec in caplog.records)


def test_parse_events_skips_duplicate_code(caplog):
    with caplog.at_level(logging.WARNING):
        events = parse_events("a=/x/1.db;a=/x/2.db")
    assert len(events) == 1
    assert events[0].db_path == os.path.abspath("/x/1.db")
    assert any("уже занят" in rec.message for rec in caplog.records)


def test_parse_events_one_bad_record_does_not_drop_neighbours():
    events = parse_events("garbage;a=/x/1.db;also garbage;b=/x/2.db")
    assert [e.code for e in events] == ["a", "b"]


# ── multi_mode ────────────────────────────────────────────────────────────────────────────

def test_multi_mode_zero_events():
    assert multi_mode(()) is False


def test_multi_mode_one_event():
    assert multi_mode(parse_events("a=/x/1.db")) is False


def test_multi_mode_two_events():
    assert multi_mode(parse_events("a=/x/1.db;b=/x/2.db")) is True


# ── load_config: новые ключи ─────────────────────────────────────────────────────────────

def test_load_config_events_and_access_defaults_when_absent():
    cfg = load_config(env=dict(BASE_ENV))
    assert cfg.events == ()
    assert cfg.superadmin_emails == ()
    assert cfg.access_team_domain == ""
    assert cfg.access_aud == ""
    assert cfg.access_dev_bypass is False


def test_load_config_parses_events_and_access_keys():
    env = dict(BASE_ENV)
    env.update({
        "DASHBOARD_EVENTS": "yl26=/app/data-yl26/forum.db;rt26=/app/data-rt26/forum.db",
        "DASHBOARD_SUPERADMIN_EMAILS": "dxp@aiesec.ru, Manager@AIESEC.ru",
        "DASHBOARD_ACCESS_TEAM_DOMAIN": "aiesec",
        "DASHBOARD_ACCESS_AUD": "abc123",
        "DASHBOARD_ACCESS_DEV_BYPASS": "1",
    })
    cfg = load_config(env=env)
    assert [e.code for e in cfg.events] == ["yl26", "rt26"]
    assert cfg.superadmin_emails == ("dxp@aiesec.ru", "manager@aiesec.ru")
    assert cfg.access_team_domain == "aiesec"
    assert cfg.access_aud == "abc123"
    assert cfg.access_dev_bypass is True


def test_dashboard_config_constructs_with_old_kwargs_only():
    """Существующие хелперы (`tests/test_dashboard_render.py::_cfg` и аналоги) продолжают
    собирать `DashboardConfig` старым набором kwargs без единого нового поля."""
    cfg = DashboardConfig(
        db_path="data/forum.db",
        public_url="https://example.com",
        session_secret="s",
        bot_username="bot",
        bot_token="123:abc",
        admin_ids=(1,),
        proxy_url=None,
        event_city_default="msk",
        trusted_proxies="172.31.0.0/16",
    )
    assert cfg.events == ()
    assert cfg.superadmin_emails == ()
    assert cfg.access_dev_bypass is False


# ── _parse_emails (через load_config) ────────────────────────────────────────────────────

def test_parse_emails_semicolon_and_newline_separators():
    env = dict(BASE_ENV)
    env["DASHBOARD_SUPERADMIN_EMAILS"] = "a@x.ru; b@x.ru\nc@x.ru"
    cfg = load_config(env=env)
    assert cfg.superadmin_emails == ("a@x.ru", "b@x.ru", "c@x.ru")


def test_parse_emails_dedup_preserves_order():
    env = dict(BASE_ENV)
    env["DASHBOARD_SUPERADMIN_EMAILS"] = "a@x.ru, A@X.ru, b@x.ru"
    cfg = load_config(env=env)
    assert cfg.superadmin_emails == ("a@x.ru", "b@x.ru")


def test_parse_emails_skips_token_without_at(caplog):
    env = dict(BASE_ENV)
    env["DASHBOARD_SUPERADMIN_EMAILS"] = "a@x.ru, oops"
    with caplog.at_level(logging.WARNING):
        cfg = load_config(env=env)
    assert cfg.superadmin_emails == ("a@x.ru",)
    assert any("оп" in rec.message.lower() or "@" in rec.message for rec in caplog.records)


def test_parse_emails_empty_is_deny_all():
    cfg = load_config(env=dict(BASE_ENV))
    assert cfg.superadmin_emails == ()


# ── DASHBOARD_ACCESS_TEAM_DOMAIN normalization ───────────────────────────────────────────

@pytest.mark.parametrize("raw,expected", [
    ("aiesec", "aiesec"),
    ("https://aiesec.cloudflareaccess.com", "aiesec"),
    ("http://aiesec.cloudflareaccess.com/", "aiesec"),
    ("aiesec.cloudflareaccess.com", "aiesec"),
    ("", ""),
])
def test_access_team_domain_normalization(raw, expected):
    env = dict(BASE_ENV)
    env["DASHBOARD_ACCESS_TEAM_DOMAIN"] = raw
    cfg = load_config(env=env)
    assert cfg.access_team_domain == expected


# ── DASHBOARD_ACCESS_DEV_BYPASS — включается только значением "1" ───────────────────────

@pytest.mark.parametrize("raw,expected", [
    ("1", True),
    ("true", False),
    ("yes", False),
    ("0", False),
    ("", False),
])
def test_access_dev_bypass_only_value_one_enables(raw, expected):
    env = dict(BASE_ENV)
    env["DASHBOARD_ACCESS_DEV_BYPASS"] = raw
    cfg = load_config(env=env)
    assert cfg.access_dev_bypass is expected


# ── DASHBOARD_SUPERADMINS не заводится (вход не через Telegram) ─────────────────────────

def test_no_dashboard_superadmins_key_read_from_env():
    """Решение владельца 06.09: вход не через Telegram, `DASHBOARD_SUPERADMINS` (telegram_id)
    не заводится. Проверяем, что ключ нигде не ЧИТАЕТСЯ из окружения (а не что строка вообще
    не встречается — комментарий, объясняющий ОТСУТСТВИЕ ключа, размещать можно)."""
    import pathlib

    dashboard_dir = pathlib.Path(__file__).resolve().parent.parent / "dashboard"
    for py_file in dashboard_dir.rglob("*.py"):
        text = py_file.read_text(encoding="utf-8")
        assert '"DASHBOARD_SUPERADMINS"' not in text, py_file
        assert "'DASHBOARD_SUPERADMINS'" not in text, py_file

    env_example = pathlib.Path(__file__).resolve().parent.parent / ".env.example"
    assert "DASHBOARD_SUPERADMINS=" not in env_example.read_text(encoding="utf-8")
