"""Хаб Mini App на `services.amb_screen.delegate_view` (паритет с «Моя ссылка» в боте) и
счётчики амбассадоров на дашборде. Харнесс — как у `tests/test_miniapp_referral_hub_260917.py`."""
from __future__ import annotations

import asyncio

import pytest

from services import amb_screen
from settings_schema import SETTINGS_SCHEMA
from tests.test_miniapp_routes import (
    DELEGATE_ID, _cfg, _client, _hdr, _set, _standard_seed, _use_tmp_db,
)


@pytest.fixture
def client(tmp_path):
    db_path = _use_tmp_db(tmp_path, "amb_hub_34.db")
    _standard_seed()
    return _client(_cfg(db_path))


def _patch_view(monkeypatch, view):
    async def _dv(tid, user=None, need_state=True):
        if isinstance(view, Exception):
            raise view
        return view
    monkeypatch.setattr(amb_screen, "delegate_view", _dv)


def _ref(client):
    return client.get("/app/api/hub", headers=_hdr(DELEGATE_ID)).json()["referral"]


def _view(**kw):
    base = {"state": "open", "status_key": None, "referral_points": None,
            "wave_place": None, "is_ambassador": False}
    base.update(kw)
    return base


def _default(key):
    return SETTINGS_SCHEMA[key]["default"]


def test_ambassador_full_block(client, monkeypatch):
    _patch_view(monkeypatch, _view(
        state="active_pack", status_key="amb_status_pack_text", referral_points=7, is_ambassador=True,
        wave_place={"wave": "Волна 2", "number": 2, "place": 3, "total": 40}))
    r = _ref(client)
    assert r["status_text"] == _default("amb_status_pack_text")
    assert "7" in r["points_text"]
    assert "3" in r["wave_text"] and "40" in r["wave_text"]
    assert r["invites_text"]


def test_candidate_and_full(client, monkeypatch):
    _patch_view(monkeypatch, _view(state="candidate", status_key="amb_status_candidate_text"))
    r = _ref(client)
    assert r["status_text"] == _default("amb_status_candidate_text")
    assert r["points_text"] is None and r["wave_text"] is None
    _patch_view(monkeypatch, _view(state="full", status_key="amb_slots_full_text"))
    assert _ref(client)["status_text"] == _default("amb_slots_full_text")


def test_no_limit_no_status(client, monkeypatch):
    _patch_view(monkeypatch, _view(state="active_pack", is_ambassador=True))
    assert _ref(client)["status_text"] is None


def test_fail_soft(client, monkeypatch):
    _patch_view(monkeypatch, RuntimeError("boom"))
    r = _ref(client)
    assert r["status_text"] is None and r["points_text"] is None and r["wave_text"] is None
    assert r["link"] and r["label"] and r["invites_text"]


def test_module_off_same_as_before(client):
    r = _ref(client)
    assert r["status_text"] is None and r["points_text"] is None and r["wave_text"] is None
    assert r["link"] and r["invites_text"]


def test_english_lines(client, monkeypatch):
    from services import i18n
    _patch_view(monkeypatch, _view(
        state="active_pack", status_key="amb_status_pack_text", referral_points=5, is_ambassador=True))
    ru_status = _default("amb_status_pack_text")
    tr_map = {i18n.src_hash(ru_status): "EN status"}

    async def _ctx(tid, language_code=None):
        return "en", tr_map
    monkeypatch.setattr(i18n, "context", _ctx)
    r = _ref(client)
    assert r["status_text"] == "EN status"
    assert r["points_text"]  # нет перевода — русский fail-soft, строка не пропадает


# ── дашборд: команда амбассадоров ───────────────────────────────────────────────────────

def _dash_ready(tmp_path, *, module="on", limit="5"):
    import sqlite3
    from config import config
    from database import db
    from tests._dbtpl import fast_init_db

    config.DB_PATH = str(tmp_path / "amb_dash_34.db")
    fast_init_db()
    asyncio.run(db.set_setting("event_season", "SU26"))
    asyncio.run(db.set_setting("dashboard_block_ambassadors", "on"))
    asyncio.run(db.set_setting("amb_team_selection_enabled", module))
    asyncio.run(db.set_setting("amb_slots_limit", limit))
    conn = sqlite3.connect(config.DB_PATH)
    rows = [  # tid, status, slot_at, is_amb
        (1, "active", "2026-09-01", 1), (2, "active", "2026-09-01", 1), (3, "active", None, 1),
        (4, "candidate", None, 0), (5, "candidate", None, 0), (6, "declined", None, 0),
    ]
    for tid, st, slot, amb in rows:
        conn.execute(
            "INSERT INTO users (telegram_id, full_name, username, status, season, "
            "ambassador_status, ambassador_slot_at, is_ambassador) VALUES (?,?,?,?,?,?,?,?)",
            (tid, f"Имя{tid}", f"u{tid}", "approved", "SU26", st, slot, amb),
        )
    conn.commit()
    conn.close()
    return config.DB_PATH


def _block(path):
    from dashboard import db as dash_db
    from dashboard.queries import Scope, ambassador_block
    with dash_db.read_conn(path) as conn:
        return ambassador_block(conn, Scope())


def test_dashboard_team_counts(tmp_path):
    team = _block(_dash_ready(tmp_path))["team"]
    assert team == {"team": 3, "candidates": 2, "declined": 1, "with_pack": 2,
                    "without_pack": 1, "slots_limit": 5, "slots_taken": 2}


def test_dashboard_no_limit_no_slots_line(tmp_path):
    team = _block(_dash_ready(tmp_path, limit="0"))["team"]
    assert team["slots_limit"] is None and team["slots_taken"] is None


def test_dashboard_module_off_no_team(tmp_path):
    block = _block(_dash_ready(tmp_path, module="off"))
    assert block["team"] is None


def test_dashboard_old_schema_no_columns(tmp_path):
    import sqlite3
    from dashboard import db as dash_db
    from dashboard.queries import _amb_team_stats

    path = _dash_ready(tmp_path)
    conn = sqlite3.connect(path)
    conn.execute("ALTER TABLE users DROP COLUMN ambassador_slot_at")
    conn.commit()
    conn.close()
    with dash_db.read_conn(path) as c:
        assert _amb_team_stats(c, [], ()) is None


def test_dashboard_render_team_numbers_only(tmp_path):
    from tests.test_dashboard_render import _stats_manager_client, _use_tmp_db

    db_path = _use_tmp_db(tmp_path)
    asyncio.run(__import__("database.db", fromlist=["x"]).set_setting("event_season", "SU26"))
    import sqlite3
    from database import db
    asyncio.run(db.set_setting("amb_team_selection_enabled", "on"))
    conn = sqlite3.connect(db_path)
    for tid, st, amb in ((1, "active", 1), (2, "candidate", 0)):
        conn.execute(
            "INSERT INTO users (telegram_id, full_name, username, status, season, "
            "ambassador_status, is_ambassador) VALUES (?,?,?,?,?,?,?)",
            (tid, "Уникальныйфио" + str(tid), f"uniq{tid}", "approved", "SU26", st, amb),
        )
    conn.commit()
    conn.close()
    client = _stats_manager_client(db_path, extra_settings={"dashboard_block_ambassadors": "on"})
    resp = client.get("/")
    assert resp.status_code == 200
    assert "Команда амбассадоров" in resp.text and "Кандидатов" in resp.text
    assert "Уникальныйфио" not in resp.text and "uniq2" not in resp.text
