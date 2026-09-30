"""tools/amb_status_reset.py: сброс мигрированных статусов амбассадора с предпросмотром.

По умолчанию инструмент только показывает, кого сбросит; `--apply` сбрасывает статус в none
через единственного писателя `amb_status_db.set_status`. Сообщений никому не шлёт.
Async — через `asyncio.run()`, временная БД — тот же приём, что tests/test_amb_status_34.py.
"""
from __future__ import annotations

import asyncio
import sqlite3
from pathlib import Path

import pytest

from config import config
from database import amb_status_db as sdb
from database import db
from tests._dbtpl import fast_init_db
from tools import amb_status_reset as tool

SEASON = "RT 26"
PAST = "RT 25"
AT = "2026-09-30 12:00:00"
REPO = Path(__file__).resolve().parent.parent


def _run(coro):
    return asyncio.run(coro)


def _ready(tmp_path):
    config.DB_PATH = str(tmp_path / "test_amb_status_reset_tool_34.db")
    fast_init_db()
    _run(db.set_setting("event_season", SEASON))


def _sql(query, params=()):
    conn = sqlite3.connect(config.DB_PATH)
    try:
        rows = conn.execute(query, params).fetchall()
        conn.commit()
        return rows
    finally:
        conn.close()


def _seed(tid, status, *, season=SEASON):
    _run(db.add_user({
        "telegram_id": tid,
        "full_name": f"Delegate {tid}",
        "registration_date": f"2026-09-01 00:00:{tid % 60:02d}",
        "season": season,
    }))
    _run(db.set_user_status(tid, "approved"))
    if status:
        assert _run(sdb.set_status(tid, status, at=AT))


def _status(tid):
    return _sql("SELECT ambassador_status, is_ambassador FROM users WHERE telegram_id = ?",
                (tid,))[0]


def _world(tmp_path):
    _ready(tmp_path)
    _seed(1, "candidate")                 # кандидат текущего сезона
    _seed(2, "active")                    # амбассадор текущего сезона
    _seed(3, "candidate", season=PAST)    # кандидат прошлого сезона
    _seed(4, "active", season=PAST)       # амбассадор прошлого сезона
    _seed(5, None, season=PAST)           # без статуса — не трогаем
    _seed(6, "candidate")                 # ещё кандидат текущего сезона


def _report(scope, apply):
    code, lines = _run(tool.run(scope=scope, apply=apply))
    return code, "\n".join(lines)


def test_preview_past_seasons_changes_nothing(tmp_path):
    _world(tmp_path)
    before = _sql("SELECT telegram_id, ambassador_status, is_ambassador FROM users ORDER BY 1")
    code, text = _report("past-seasons", apply=False)
    assert code == 0
    assert "Будет сброшено: 2" in text
    assert "кандидат" in text and "амбассадор" in text
    assert f"«{PAST}»" in text
    assert "--apply" in text
    after = _sql("SELECT telegram_id, ambassador_status, is_ambassador FROM users ORDER BY 1")
    assert before == after


def test_apply_past_seasons_resets_only_past_and_mirrors(tmp_path):
    _world(tmp_path)
    code, text = _report("past-seasons", apply=True)
    assert code == 0
    assert "Сброшено: 2" in text
    assert _status(3) == (None, 0)
    assert _status(4) == (None, 0)          # зеркало is_ambassador снято
    assert _status(1) == ("candidate", 0)   # текущий сезон не тронут
    assert _status(2) == ("active", 1)
    assert _status(6) == ("candidate", 0)

    code, text = _report("past-seasons", apply=True)
    assert code == 0
    assert "Сбрасывать нечего" in text


def test_candidates_all_resets_only_current_candidates(tmp_path):
    _world(tmp_path)
    code, text = _report("candidates-all", apply=False)
    assert "Будет сброшено: 2" in text
    assert _status(1) == ("candidate", 0)

    code, text = _report("candidates-all", apply=True)
    assert code == 0
    assert "Сброшено: 2" in text
    assert _status(1) == (None, 0)
    assert _status(6) == (None, 0)
    assert _status(2) == ("active", 1)            # амбассадор не кандидат — остаётся
    assert _status(3) == ("candidate", 0)         # прошлый сезон — не этот режим


def test_status_changed_between_preview_and_apply_is_skipped(tmp_path, monkeypatch):
    """Статус сменился после выборки (менеджер нажал «Взять») — expect не совпал, строка не
    трогается и считается отдельно."""
    _world(tmp_path)
    real_collect = tool.collect

    async def collect_then_take(scope):
        plan = await real_collect(scope)
        assert await sdb.set_status(1, "active", at=AT)
        return plan

    monkeypatch.setattr(tool, "collect", collect_then_take)
    code, text = _report("candidates-all", apply=True)
    assert code == 0
    assert "Сброшено: 1" in text
    assert "Пропущено" in text
    assert _status(1) == ("active", 1)
    assert _status(6) == (None, 0)


def test_missing_column_asks_to_restart_bot(tmp_path):
    config.DB_PATH = str(tmp_path / "old.db")
    conn = sqlite3.connect(config.DB_PATH)
    conn.execute("CREATE TABLE users (telegram_id INTEGER PRIMARY KEY, season TEXT)")
    conn.execute("CREATE TABLE bot_settings (key TEXT PRIMARY KEY, value TEXT)")
    conn.commit()
    conn.close()
    code, text = _report("past-seasons", apply=True)
    assert code == 2
    assert "Сначала перезапустите бота на новой версии" in text


def test_flags_are_mutually_exclusive():
    parser = tool.build_parser()
    with pytest.raises(SystemExit):
        parser.parse_args(["--past-seasons", "--candidates-all"])
    assert parser.parse_args([]).scope == "past-seasons"
    assert parser.parse_args(["--candidates-all"]).scope == "candidates-all"
    assert parser.parse_args(["--apply"]).apply is True


def test_tool_sends_nothing_and_writes_only_through_set_status():
    src = (REPO / "tools" / "amb_status_reset.py").read_text(encoding="utf-8")
    for marker in ("send_message", "aiogram", "send_or_queue", "coins"):
        assert marker not in src, marker
    assert "set_status(" in src
    assert "UPDATE" not in src.upper().replace("UPDATED", "")
