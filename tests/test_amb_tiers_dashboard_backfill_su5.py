"""Квалифицированная амбассадорка СкиллАп 5: блок дашборда и разовый пересчёт ступеней.

- `dashboard/amb_tiers_block.py` — четыре агрегата без персональных данных, паритет
  определений с ботом (`database/amb_tiers_db.referral_counts`), старая БД без таблиц;
- `services.amb_tiers.preview_backfill` + `tools/backfill_amb_tiers.py` — предпросмотр по
  умолчанию, `--apply` тихо, `--apply --notify` с одним событием на амбассадора, отказы.

pytest-asyncio нет — async через `asyncio.run()`.
"""
from __future__ import annotations

import asyncio
import json
import sqlite3

import pytest

from config import config
from dashboard import db as dash_db
from dashboard.amb_tiers_block import amb_tiers_block
from dashboard.queries import Scope, ambassador_block
from database import amb_tiers_db as tdb
from database import db
from services import amb_tiers
from tests._dbtpl import fast_init_db

SEASON = "SU26"


def _run(coro):
    return asyncio.run(coro)


def _ready(tmp_path, name="test_amb_tiers_dash_su5.db", *, program="on"):
    config.DB_PATH = str(tmp_path / name)
    fast_init_db()
    _run(db.set_setting("event_season", SEASON))
    _run(db.set_setting("amb_qualified_program", program))
    return config.DB_PATH


def _sql(query, params=()):
    conn = sqlite3.connect(config.DB_PATH)
    try:
        rows = conn.execute(query, params).fetchall()
        conn.commit()
        return rows
    finally:
        conn.close()


def _seed_user(tid, *, referrer_id=None, status="pending", full_name=None, approved_at=None):
    _run(db.add_user({
        "telegram_id": tid,
        "full_name": full_name or f"Delegate {tid}",
        "registration_date": "2026-09-01 00:00:00",
        "referrer_id": referrer_id,
        "season": SEASON,
    }))
    _run(db.set_user_status(tid, status))
    if approved_at:
        _sql("UPDATE users SET approved_at = ? WHERE telegram_id = ?", (approved_at, tid))


def _make_ambassador(tid, username=None):
    _seed_user(tid, status="approved", full_name=f"Амбассадор {tid}")
    _run(db.set_ambassador_flag(tid, active=True, at="2026-09-01 10:00:00"))
    if username:
        _sql("UPDATE users SET username = ? WHERE telegram_id = ?", (username, tid))


def _fixture():
    """Два амбассадора: у 100 трое прошли отбор + ступень 2 выдана, у 110 — никого; у 100 ещё
    один исключённый одобренный приглашённый (не считается)."""
    _make_ambassador(100)
    _make_ambassador(110)
    for tid in (201, 202, 203):
        _seed_user(tid, referrer_id=100, status="approved", full_name="Иван Уникальный")
    _seed_user(204, referrer_id=100, status="approved")
    _run(tdb.exclude_invitee(204, "накрутка", 1, "2026-10-01 10:00:00"))
    _seed_user(301, referrer_id=110)
    _run(tdb.claim_new_tiers(100, [1, 2], "2026-10-01 10:00:00", 15))


def _outbox_events():
    return [json.loads(r[0]) for r in _sql(
        "SELECT payload FROM miniapp_outbox WHERE kind = 'amb_tier_reached' ORDER BY id"
    )]


# ── дашборд ─────────────────────────────────────────────────────────────────────────────

def test_block_none_when_program_off(tmp_path):
    path = _ready(tmp_path, program="off")
    _fixture()
    with dash_db.read_conn(path) as conn:
        assert amb_tiers_block(conn, Scope()) is None


def test_block_aggregates_on_fixture(tmp_path):
    path = _ready(tmp_path)
    _run(db.set_setting("amb_o2o_quota", "12"))
    _fixture()
    with dash_db.read_conn(path) as conn:
        block = amb_tiers_block(conn, Scope())
    assert block == {"active": 1, "qualified_total": 3, "o2o_granted": 1, "o2o_quota": 12, "waitlist": 0}


def test_block_parity_with_bot_counts(tmp_path):
    path = _ready(tmp_path)
    _fixture()
    _sql("UPDATE users SET referrer_id = 110 WHERE telegram_id = 110")  # сам себя — не считается
    bot_total = sum(_run(tdb.referral_counts(rid, SEASON))["qualified"] for rid in (100, 110))
    with dash_db.read_conn(path) as conn:
        block = amb_tiers_block(conn, Scope())
    assert block["qualified_total"] == bot_total == 3


def test_block_none_on_old_db_without_tables(tmp_path):
    path = _ready(tmp_path)
    _sql("DROP TABLE ambassador_tiers")
    with dash_db.read_conn(path) as conn:
        assert amb_tiers_block(conn, Scope()) is None


def test_ambassador_block_carries_tiers_key(tmp_path):
    path = _ready(tmp_path)
    _run(db.set_setting("dashboard_block_ambassadors", "on"))
    _fixture()
    with dash_db.read_conn(path) as conn:
        block = ambassador_block(conn, Scope())
    assert block["tiers"]["qualified_total"] == 3


def test_dashboard_render_shows_tiers_without_invitee_names(tmp_path):
    from tests.test_dashboard_render import _stats_manager_client, _use_tmp_db

    db_path = _use_tmp_db(tmp_path)
    _run(db.set_setting("event_season", SEASON))
    _run(db.set_setting("amb_qualified_program", "on"))
    _fixture()
    client = _stats_manager_client(db_path, extra_settings={"dashboard_block_ambassadors": "on"})
    resp = client.get("/")
    assert resp.status_code == 200
    assert "Ступени амбассадоров" in resp.text
    assert "Разборов резюме выдано" in resp.text
    assert "Иван Уникальный" not in resp.text


# ── бэкафилл ────────────────────────────────────────────────────────────────────────────

def _three_approved_before_program(tmp_path, program="on"):
    _ready(tmp_path, program=program)
    _make_ambassador(100, username="amb_one")
    for n, tid in enumerate((201, 202, 203)):
        _seed_user(tid, referrer_id=100, status="approved", approved_at=f"2026-09-0{n + 2} 10:00:00")


def test_preview_backfill_writes_nothing(tmp_path):
    _three_approved_before_program(tmp_path)
    preview = _run(amb_tiers.preview_backfill())
    assert len(preview) == 1
    entry = preview[0]
    assert entry["telegram_id"] == 100 and entry["qualified"] == 3
    assert [(t["tier"], t["o2o_status"], t["exists"]) for t in entry["tiers"]] == [
        (1, None, False), (2, "granted", False),
    ]
    assert _run(tdb.list_tiers()) == []


def test_preview_quota_order_by_approval_time(tmp_path):
    _ready(tmp_path)
    _run(db.set_setting("amb_o2o_quota", "1"))
    _make_ambassador(100)
    _make_ambassador(110)
    for tid in (201, 202, 203):
        _seed_user(tid, referrer_id=100, status="approved", approved_at="2026-09-05 10:00:00")
    for tid in (301, 302, 303):
        _seed_user(tid, referrer_id=110, status="approved", approved_at="2026-09-03 10:00:00")
    preview = _run(amb_tiers.preview_backfill())
    assert [e["telegram_id"] for e in preview] == [110, 100]
    assert preview[0]["tiers"][1]["o2o_status"] == "granted"
    assert preview[1]["tiers"][1]["o2o_status"] == "waitlist"

    from tools import backfill_amb_tiers as tool
    assert tool.main(["--apply"]) == 0
    o2o = {r["telegram_id"]: r["o2o_status"] for r in _run(tdb.list_tiers()) if r["tier"] == 2}
    assert o2o == {110: "granted", 100: "waitlist"}


def test_cli_apply_is_silent_and_idempotent(tmp_path, capsys):
    from tools import backfill_amb_tiers as tool
    _three_approved_before_program(tmp_path)
    assert tool.main([]) == 0
    assert "предпросмотр" in capsys.readouterr().out
    assert _run(tdb.list_tiers()) == []

    assert tool.main(["--apply"]) == 0
    rows = _run(tdb.list_tiers(100))
    assert [r["tier"] for r in rows] == [1, 2]
    assert all(r["notified_at"] for r in rows)
    assert _outbox_events() == []

    assert tool.main(["--apply"]) == 0
    assert len(_run(tdb.list_tiers(100))) == 2
    assert "Новых ступеней к выдаче нет" in capsys.readouterr().out


def test_cli_apply_notify_one_event_per_ambassador(tmp_path):
    from tools import backfill_amb_tiers as tool
    _three_approved_before_program(tmp_path)
    assert tool.main(["--apply", "--notify"]) == 0
    events = _outbox_events()
    assert len(events) == 1 and events[0]["telegram_id"] == 100 and events[0]["tier"] == 2


def test_cli_notify_without_apply_is_error(tmp_path):
    from tools import backfill_amb_tiers as tool
    _three_approved_before_program(tmp_path)
    with pytest.raises(SystemExit) as exc:
        tool.main(["--notify"])
    assert exc.value.code == 2


def test_cli_apply_refuses_when_program_off(tmp_path, capsys):
    from tools import backfill_amb_tiers as tool
    _three_approved_before_program(tmp_path, program="off")
    assert tool.main(["--apply"]) == 2
    assert "Включите её в админке" in capsys.readouterr().out
    assert _run(tdb.list_tiers()) == []
    # предпросмотр при выключенной программе работает
    assert tool.main([]) == 0
    assert "выключена" in capsys.readouterr().out
