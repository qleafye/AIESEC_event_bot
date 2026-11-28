"""Письмо из приложения не уходит второй раз, если бот перезапустился посреди пачки.

Строка помечается сразу после отправки. Уже обработанную перед отправкой не шлём.
После пятой ошибки владельцу бота уходит то же предупреждение, что при сбое таблицы.
"""
import asyncio

import aiosqlite

from config import config
from database import db as bot_db
from services.infra import miniapp_outbox
from tests._dbtpl import fast_init_db


def _init(tmp_path, name="outbox_once.db") -> str:
    path = str(tmp_path / name)
    config.DB_PATH = path
    fast_init_db()
    return path


def _run(coro):
    return asyncio.run(coro)


def _enqueue(kind, payload):
    return _run(bot_db.enqueue_miniapp_outbox(kind, payload, "2026-11-28 12:00:00"))


async def _fetchall(query, params=()):
    async with bot_db._connect() as conn:
        conn.row_factory = aiosqlite.Row
        async with conn.execute(query, params) as cur:
            return [dict(r) for r in await cur.fetchall()]


class FakeBot:
    pass


def test_row_is_marked_before_the_next_one_is_sent(tmp_path, monkeypatch):
    _init(tmp_path)
    first = _enqueue("task_changed", {"task_id": 1})
    _enqueue("task_changed", {"task_id": 2})
    sent = []

    async def handle(bot, kind, payload):
        if payload["task_id"] == 2:
            row = (await _fetchall(
                "SELECT processed_at FROM miniapp_outbox WHERE id = ?", (first,)
            ))[0]
            assert row["processed_at"]
        sent.append(payload["task_id"])

    monkeypatch.setattr(miniapp_outbox, "_handle_row", handle)

    done = _run(miniapp_outbox.drain(FakeBot()))

    assert done == 2
    assert sent == [1, 2]


def test_already_processed_row_in_the_snapshot_is_not_sent(tmp_path, monkeypatch):
    _init(tmp_path)
    row_id = _enqueue("task_changed", {"task_id": 7, "submitter_name": "СекретноеИмя"})
    _run(bot_db.mark_miniapp_outbox_processed([row_id], "2026-11-28 12:01:00"))
    calls = []

    async def fake_list(limit=50):
        return [{"id": row_id, "kind": "task_changed", "payload": {"task_id": 7}, "attempts": 0}]

    async def handle(bot, kind, payload):
        calls.append(payload)

    monkeypatch.setattr(miniapp_outbox, "list_unprocessed_miniapp_outbox", fake_list)
    monkeypatch.setattr(miniapp_outbox, "_handle_row", handle)

    assert _run(miniapp_outbox.drain(FakeBot())) == 0
    assert calls == []


def test_missing_row_in_the_snapshot_is_not_sent(tmp_path, monkeypatch):
    _init(tmp_path)
    calls = []

    async def fake_list(limit=50):
        return [{"id": 99999, "kind": "task_changed", "payload": {}, "attempts": 0}]

    async def handle(bot, kind, payload):
        calls.append(1)

    monkeypatch.setattr(miniapp_outbox, "list_unprocessed_miniapp_outbox", fake_list)
    monkeypatch.setattr(miniapp_outbox, "_handle_row", handle)

    assert _run(miniapp_outbox.drain(FakeBot())) == 0
    assert calls == []


def test_fifth_failure_alerts_admins_the_same_way_as_the_sheet(tmp_path, monkeypatch):
    _init(tmp_path)
    alerts = []

    async def boom(bot, kind, payload):
        raise RuntimeError("permanently broken")

    async def fake_alert(text):
        row = (await _fetchall(
            "SELECT processed_at FROM miniapp_outbox WHERE id = ?", (row_id,)
        ))[0]
        assert row["processed_at"]
        alerts.append(text)

    monkeypatch.setattr(miniapp_outbox, "_handle_row", boom)
    monkeypatch.setattr("services.sheets.sheets._send_admin_alert", fake_alert)
    row_id = _enqueue(
        "submission_created",
        {"submission_id": 1, "submitter_name": "СекретноеИмя"},
    )

    for _ in range(miniapp_outbox.MAX_ATTEMPTS - 1):
        assert _run(miniapp_outbox.drain(FakeBot())) == 0
    assert alerts == []

    assert _run(miniapp_outbox.drain(FakeBot())) == 1
    assert len(alerts) == 1
    text = alerts[0]
    assert "5" in text
    assert "СекретноеИмя" not in text
    assert "submission_created" in text
    row = _run(_fetchall(
        "SELECT attempts, processed_at FROM miniapp_outbox WHERE id = ?", (row_id,)
    ))[0]
    assert row["attempts"] == miniapp_outbox.MAX_ATTEMPTS
    assert row["processed_at"]
    assert _run(miniapp_outbox.drain(FakeBot())) == 0


def test_giveup_alert_failure_still_drops_the_row(tmp_path, monkeypatch):
    _init(tmp_path)

    async def boom(bot, kind, payload):
        raise RuntimeError("permanently broken")

    async def fake_alert(text):
        raise RuntimeError("telegram down")

    monkeypatch.setattr(miniapp_outbox, "_handle_row", boom)
    monkeypatch.setattr("services.sheets.sheets._send_admin_alert", fake_alert)
    row_id = _enqueue("task_changed", {"task_id": 3})

    for _ in range(miniapp_outbox.MAX_ATTEMPTS - 1):
        assert _run(miniapp_outbox.drain(FakeBot())) == 0
    assert _run(miniapp_outbox.drain(FakeBot())) == 1
    row = _run(_fetchall(
        "SELECT processed_at FROM miniapp_outbox WHERE id = ?", (row_id,)
    ))[0]
    assert row["processed_at"]
