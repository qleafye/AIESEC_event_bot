"""Точка расширения «после ПЕРВОЙ отметки входа делегата» (`services.checkin.
register_first_entry_listener`): первая отметка зовёт слушателей, повторная — нет; сбой одного
слушателя не ломает ни отметку, ни остальных слушателей; без `bot` (Mini App) слушатели не
зовутся, событие остаётся в `result["first_entry"]` и доезжает до них через outbox.

БД — шаблонная копия `tests/_dbtpl.py::fast_init_db`, `asyncio.run()` — тот же приём, что у
`tests/test_checkin_sessions_260924.py`."""
from __future__ import annotations

import asyncio

import pytest

from config import config
from database import db
from services import checkin as checkin_mod
from services.checkin import ENTRY_POINT, record_arrival, register_first_entry_listener
from tests._dbtpl import fast_init_db

UID = 260924101
BOT = object()


@pytest.fixture(autouse=True)
def _clean_listeners():
    checkin_mod.clear_first_entry_listeners()
    yield
    checkin_mod.clear_first_entry_listeners()


def _run(coro):
    return asyncio.run(coro)


def _setup(tmp_path, city="msk"):
    config.DB_PATH = str(tmp_path / "test_checkin_first_entry_hook_260924.db")
    fast_init_db()
    _run(db.add_user({
        "telegram_id": UID, "full_name": "Иванов Иван", "registration_date": "2026-01-01",
        "event_city": city, "status": "approved",
    }))
    return _run(db.get_user(UID))


def test_first_entry_calls_listener_once_duplicate_does_not(tmp_path):
    user = _setup(tmp_path)
    calls = []

    async def listener(bot, user_id, city, day, **kwargs):
        calls.append((bot, user_id, city, day, kwargs))

    register_first_entry_listener(listener)
    r1 = _run(record_arrival(
        user, ENTRY_POINT, source="csv", scanned_at="2026-10-30 09:15:00", by_staff_id=77, bot=BOT,
    ))
    assert r1["status"] == "new"
    assert len(calls) == 1
    bot, user_id, city, day, kwargs = calls[0]
    assert (bot, user_id, city, day) == (BOT, UID, "msk", "2026-10-30")
    assert kwargs["source"] == "csv"
    assert kwargs["by_staff_id"] == 77
    assert kwargs["scanned_at"] == "2026-10-30 09:15:00"
    assert kwargs["first_of_day"] is True and kwargs["first_of_forum"] is True

    r2 = _run(record_arrival(user, ENTRY_POINT, source="csv", scanned_at="2026-10-30 12:00:00", bot=BOT))
    assert r2["status"] == "duplicate"
    assert "first_entry" not in r2
    assert len(calls) == 1


def test_second_day_entry_fires_with_first_of_forum_false(tmp_path):
    """Вход каждый день: первый вход второго дня зовёт слушателей ещё раз — с
    `first_of_forum=False`, чтобы приветствие «один раз за форум» не ушло повторно. CSV первого
    дня, загруженный ПОСЛЕ живого скана второго, — тоже не первый за форум."""
    user = _setup(tmp_path)
    calls = []

    async def listener(bot, user_id, city, day, **kwargs):
        calls.append((day, kwargs["first_of_day"], kwargs["first_of_forum"]))

    register_first_entry_listener(listener)
    _run(record_arrival(user, ENTRY_POINT, source="csv", scanned_at="2026-10-31 09:00:00", bot=BOT))
    r = _run(record_arrival(user, ENTRY_POINT, source="csv", scanned_at="2026-10-30 09:00:00", bot=BOT))
    assert r["status"] == "new"
    _run(record_arrival(user, ENTRY_POINT, source="csv", scanned_at="2026-10-31 18:00:00", bot=BOT))
    assert calls == [("2026-10-31", True, True), ("2026-10-30", True, False)]


def test_failing_listener_does_not_break_checkin_or_other_listeners(tmp_path):
    user = _setup(tmp_path)
    seen = []

    async def broken(bot, user_id, city, day, **kwargs):
        raise RuntimeError("boom")

    async def healthy(bot, user_id, city, day, **kwargs):
        seen.append(user_id)

    register_first_entry_listener(broken)
    register_first_entry_listener(healthy)
    result = _run(record_arrival(user, ENTRY_POINT, source="manual", by_staff_id=5, bot=BOT))
    assert result["status"] == "new"
    assert seen == [UID]
    assert _run(db.count_checkins_by_point(ENTRY_POINT)) == 1


def test_auto_session_entry_fires_with_session_source(tmp_path):
    user = _setup(tmp_path)
    sid = _run(db.create_program_session("msk", "2026-10-30", "10:00", "11:00", "Открытие"))
    calls = []

    async def listener(bot, user_id, city, day, **kwargs):
        calls.append(kwargs)

    register_first_entry_listener(listener)
    result = _run(record_arrival(
        user, f"session:{sid}", source="miniapp", scanned_at="2026-10-30 10:05:00", bot=BOT,
    ))
    assert result["status"] == "new"
    assert len(calls) == 1
    assert calls[0]["source"] == "auto_session"
    assert calls[0]["session_id"] == sid


def test_without_bot_listeners_wait_for_outbox(tmp_path):
    """Mini App-процесс (bot=None): слушатели не зовутся на месте, событие — в результате;
    бот зовёт их, разбирая `checkin_first_entry` из очереди."""
    user = _setup(tmp_path)
    calls = []

    async def listener(bot, user_id, city, day, **kwargs):
        calls.append((bot, user_id, city, day, kwargs["source"]))

    register_first_entry_listener(listener)
    result = _run(record_arrival(user, ENTRY_POINT, source="miniapp", by_staff_id=9))
    assert calls == []
    event = result["first_entry"]

    from services.infra.miniapp_outbox import _handle_row
    _run(_handle_row(BOT, "checkin_first_entry", event))
    assert calls == [(BOT, UID, "msk", event["day"], "miniapp")]
