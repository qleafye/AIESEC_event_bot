"""Phase 19 Plan 04 Task 1 (WEBAPP-01, D-01, T-19-24): таблица `miniapp_outbox`, её
аксессоры в `database.db` и тонкая обёртка `miniapp.outbox.enqueue`.

Схемой владеет только бот (`init_db`); `miniapp/` не зовёт `init_db` (грепается).
"""
from __future__ import annotations

import asyncio
import json
import re
from pathlib import Path

import aiosqlite

from config import config as bot_config
from database import db as bot_db

from miniapp import outbox

ROOT = Path(__file__).resolve().parent.parent
MINIAPP_DIR = ROOT / "miniapp"


def _init(tmp_path, name="outbox.db") -> str:
    path = str(tmp_path / name)
    bot_config.DB_PATH = path
    asyncio.run(bot_db.init_db())
    return path


def _run(coro):
    return asyncio.run(coro)


async def _fetchall(query, params=()):
    async with bot_db._connect() as conn:
        conn.row_factory = aiosqlite.Row
        async with conn.execute(query, params) as cur:
            return [dict(r) for r in await cur.fetchall()]


# ── схема ────────────────────────────────────────────────────────────────────────────────

def test_init_db_creates_outbox_table_and_index(tmp_path):
    _init(tmp_path)
    cols = {r["name"]: r for r in _run(_fetchall("PRAGMA table_info(miniapp_outbox)"))}
    assert set(cols) == {"id", "kind", "payload", "created_at", "processed_at", "attempts", "last_error"}
    assert cols["attempts"]["dflt_value"] == "0" and cols["attempts"]["notnull"] == 1
    idx = {r["name"] for r in _run(_fetchall("PRAGMA index_list(miniapp_outbox)"))}
    assert "idx_miniapp_outbox_pending" in idx


def test_init_db_is_idempotent_and_keeps_rows(tmp_path):
    _init(tmp_path)
    row_id = _run(outbox.enqueue("task_changed", {"task_id": 7}))
    assert row_id
    _run(bot_db.init_db())  # повторный запуск — ни ошибки, ни потери строк
    rows = _run(bot_db.list_unprocessed_miniapp_outbox())
    assert [r["id"] for r in rows] == [row_id]


def test_miniapp_package_never_calls_init_db():
    offenders = []
    for path in MINIAPP_DIR.rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        if re.search(r"\binit_db\s*\(", text):
            offenders.append(str(path.relative_to(ROOT)))
    assert not offenders, f"miniapp/ зовёт init_db: {offenders}"


# ── аксессоры ────────────────────────────────────────────────────────────────────────────

def test_enqueue_stores_json_payload_unprocessed(tmp_path):
    _init(tmp_path)
    payload = {"submission_id": 1, "user_id": 2, "task_id": 3, "task_text": "Привет", "submitter_name": "Ира"}
    row_id = _run(outbox.enqueue("submission_created", payload))
    raw = _run(_fetchall("SELECT * FROM miniapp_outbox WHERE id = ?", (row_id,)))[0]
    assert raw["kind"] == "submission_created"
    assert json.loads(raw["payload"]) == payload
    assert "Привет" in raw["payload"]  # ensure_ascii=False — читаемо в sqlite3
    assert raw["processed_at"] is None and raw["attempts"] == 0 and raw["last_error"] is None
    assert raw["created_at"]


def test_list_unprocessed_orders_by_id_and_respects_limit(tmp_path):
    _init(tmp_path)
    ids = [_run(outbox.enqueue("task_changed", {"task_id": i})) for i in range(5)]
    rows = _run(bot_db.list_unprocessed_miniapp_outbox(limit=3))
    assert [r["id"] for r in rows] == ids[:3]
    assert rows[0]["payload"] == {"task_id": 0}  # payload уже разобран
    assert len(_run(bot_db.list_unprocessed_miniapp_outbox())) == 5


def test_mark_processed_hides_rows(tmp_path):
    _init(tmp_path)
    a = _run(outbox.enqueue("task_changed", {"task_id": 1}))
    b = _run(outbox.enqueue("task_changed", {"task_id": 2}))
    _run(bot_db.mark_miniapp_outbox_processed([a], "2026-08-23 12:00:00"))
    rows = _run(bot_db.list_unprocessed_miniapp_outbox())
    assert [r["id"] for r in rows] == [b]
    raw = _run(_fetchall("SELECT processed_at FROM miniapp_outbox WHERE id = ?", (a,)))[0]
    assert raw["processed_at"] == "2026-08-23 12:00:00"
    _run(bot_db.mark_miniapp_outbox_processed([], "x"))  # пустой список — no-op


def test_mark_failed_increments_attempts_and_truncates_error(tmp_path):
    _init(tmp_path)
    a = _run(outbox.enqueue("coins_manual", {"user_id": 1, "delta": 5}))
    _run(bot_db.mark_miniapp_outbox_failed(a, "boom"))
    _run(bot_db.mark_miniapp_outbox_failed(a, "x" * 2000))
    raw = _run(_fetchall("SELECT attempts, last_error, processed_at FROM miniapp_outbox WHERE id = ?", (a,)))[0]
    assert raw["attempts"] == 2
    assert len(raw["last_error"]) == 500
    assert raw["processed_at"] is None  # остаётся в очереди


# ── fail-soft и контракт видов ───────────────────────────────────────────────────────────

def test_enqueue_without_table_does_not_raise(tmp_path, caplog):
    path = str(tmp_path / "old_bot.db")
    bot_config.DB_PATH = path

    async def _bare():
        async with aiosqlite.connect(path) as conn:
            await conn.execute("CREATE TABLE users (telegram_id INTEGER)")
            await conn.commit()

    _run(_bare())
    with caplog.at_level("WARNING"):
        assert _run(outbox.enqueue("submission_created", {"submission_id": 1})) is None
    assert any("outbox" in r.getMessage() for r in caplog.records)


def test_unknown_kind_rejected(tmp_path):
    _init(tmp_path)
    try:
        _run(outbox.enqueue("coins_award", {}))
    except ValueError:
        pass
    else:
        raise AssertionError("неизвестный kind должен отвергаться")


def test_kinds_match_module_docstring():
    """Сторож дрейфа контракта: набор видов в OUTBOX_KINDS == список в докстринге модуля."""
    documented = set(re.findall(r"^\s{4}([a-z_]+)\s+\{", outbox.__doc__, re.M))
    assert documented == set(outbox.OUTBOX_KINDS) == {
        "submission_created", "submission_reviewed", "task_changed", "coins_manual",
        "reg_finalized", "reg_edited", "reg_resume_upload",
        "application_decided", "application_mass_approved",
    }
