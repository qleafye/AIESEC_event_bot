"""Один текст менеджера после первой отметки входа.

Пустой текст молчит и флаг не ставит. Удачная отправка ставит флаг, повторный скан молчит.
Сбой отправки флаг не ставит — следующий заход может повторить.
"""
from __future__ import annotations

import asyncio

from config import config
from database import db
from domain.settings.schema import SETTINGS_SCHEMA
from services.forum.checkin import (
    ENTRY_POINT, record_arrival, register_checkin_followup, send_checkin_followup,
)
from services.forum import checkin as checkin_mod
from tests._dbtpl import fast_init_db

UID = 550261


class _Bot:
    def __init__(self, fail=False):
        self.sent = []
        self.fail = fail

    async def send_message(self, chat_id, text, **kwargs):
        if self.fail:
            raise RuntimeError("сеть")
        self.sent.append((chat_id, text))


def _run(coro):
    return asyncio.run(coro)


def _setup(tmp_path, text="Подойдите к стойке своего города"):
    config.DB_PATH = str(tmp_path / "checkin_followup.db")
    fast_init_db()
    checkin_mod.clear_first_entry_listeners()
    _run(db.add_user({
        "telegram_id": UID, "full_name": "Иванов Иван", "registration_date": "2026-01-01",
        "event_city": "msk", "status": "approved",
    }))
    if text is not None:
        _run(db.set_setting("checkin_message_text", text))
    return _run(db.get_user(UID))


def _flag():
    user = _run(db.get_user(UID))
    return int(user["checkin_message_sent"] or 0)


def test_first_entry_scan_sends_manager_text_once(tmp_path):
    user = _setup(tmp_path)
    bot = _Bot()
    register_checkin_followup()
    label = SETTINGS_SCHEMA["checkin_message_text"]["label"]
    assert "сообщение после входа" in label.lower()
    assert "checkin_message_text" not in label
    r1 = _run(record_arrival(
        user, ENTRY_POINT, source="miniapp", scanned_at="2026-10-30 09:15:00", bot=bot,
    ))
    assert r1["status"] == "new"
    assert bot.sent == [(UID, "Подойдите к стойке своего города")]
    assert _flag() == 1
    r2 = _run(record_arrival(
        user, ENTRY_POINT, source="miniapp", scanned_at="2026-10-30 12:00:00", bot=bot,
    ))
    assert r2["status"] == "duplicate"
    assert len(bot.sent) == 1


def test_empty_text_on_entry_sends_nothing_and_keeps_flag(tmp_path):
    user = _setup(tmp_path, text="")
    bot = _Bot()
    register_checkin_followup()
    _run(send_checkin_followup(bot, UID, "msk", "2026-10-30", first_of_forum=True))
    assert bot.sent == []
    assert _flag() == 0
    _run(db.set_setting("checkin_message_text", "Теперь есть текст"))
    _run(record_arrival(
        user, ENTRY_POINT, source="manual", scanned_at="2026-10-30 09:00:00", bot=bot,
    ))
    assert bot.sent == [(UID, "Теперь есть текст")]
    assert _flag() == 1


def test_failed_send_on_entry_retries_next_scan(tmp_path):
    user = _setup(tmp_path, text="Шаг от менеджера")
    bot = _Bot(fail=True)
    register_checkin_followup()
    _run(record_arrival(
        user, ENTRY_POINT, source="csv", scanned_at="2026-10-30 09:00:00", bot=bot,
    ))
    assert bot.sent == []
    assert _flag() == 0
    bot.fail = False
    _run(send_checkin_followup(bot, UID, "msk", "2026-10-31", first_of_forum=False))
    assert bot.sent == [(UID, "Шаг от менеджера")]
    assert _flag() == 1
    _run(send_checkin_followup(bot, UID, "msk", "2026-10-31", first_of_forum=False))
    assert len(bot.sent) == 1
