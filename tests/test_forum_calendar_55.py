"""Файл календаря из даты форума. pytest-asyncio нет — корутины через asyncio.run."""
from __future__ import annotations

import asyncio
from pathlib import Path

from config import config
from database import db
from domain.settings.validation import validate_setting_value
from handlers.reg import reg_schema
from services.forum import calendar_file
from services.settings import reschedule
from tests._dbtpl import fast_init_db


def _run(coro):
    return asyncio.run(coro)


def _ready(tmp_path):
    config.DB_PATH = str(tmp_path / "ics.db")
    config.ADMIN_IDS = [1]
    fast_init_db()


class _Bot:
    def __init__(self, *, fail=False):
        self.docs = []
        self.fail = fail

    async def send_message(self, chat_id, text, **kwargs):
        return None

    async def send_document(self, chat_id, document, **kwargs):
        if self.fail:
            raise RuntimeError("нет сети")
        self.docs.append((chat_id, document))

    async def send_photo(self, chat_id, photo, **kwargs):
        return None


def test_ics_skipped_without_forum_date(tmp_path):
    _ready(tmp_path)
    assert _run(calendar_file.calendar_for_city(None)) is None
    bot = _Bot()
    _run(calendar_file.send_forum_calendar(bot, 550501, None))
    assert bot.docs == []


def test_ics_uses_forum_date_city_and_default_end(tmp_path):
    _ready(tmp_path)
    _run(db.set_setting("forum_date", "15.10.2026"))
    _run(db.set_setting("forum_time", "10:00"))
    _run(db.set_setting("event_place_name", "Москва"))
    _run(db.set_setting("event_name", "Юлид"))
    built = _run(calendar_file.calendar_for_city(None))
    assert built is not None
    data, filename = built
    assert filename == "forum.ics"
    text = data.decode("utf-8")
    assert "DTSTART:20261015T070000Z" in text
    assert "DTEND:20261015T205900Z" in text
    assert "Москва" in text
    assert "Юлид" in text
    assert "\n" not in text.replace("\r\n", "")


def test_ics_end_time_same_day_when_manager_sets_it(tmp_path):
    _ready(tmp_path)
    _run(db.set_setting("forum_date", "15.10.2026"))
    _run(db.set_setting("forum_time", "10:00"))
    _run(db.set_setting("forum_end_time", "18:00"))
    _run(db.set_setting("forum_timezone", "Europe/Moscow"))
    data, _name = _run(calendar_file.calendar_for_city(None))
    assert "DTEND:20261015T150000Z" in data.decode("utf-8")


def test_ics_missing_date_does_not_break_approval(tmp_path):
    _ready(tmp_path)
    _run(db.add_user({
        "telegram_id": 550501,
        "full_name": "Анна",
        "registration_date": "2026-11-01 00:00:00",
    }))
    bot = _Bot(fail=True)
    err = _run(reg_schema.send_completion_and_bonus(bot, 550501, with_menu=False))
    assert err is None
    assert bot.docs == []


def test_ics_goes_out_with_approval_letter(tmp_path):
    _ready(tmp_path)
    _run(db.add_user({
        "telegram_id": 550501,
        "full_name": "Анна",
        "registration_date": "2026-11-01 00:00:00",
    }))
    _run(db.set_setting("forum_date", "15.10.2026"))
    _run(db.set_setting("event_place_name", "Москва"))
    bot = _Bot()
    err = _run(reg_schema.send_completion_and_bonus(bot, 550501, with_menu=False))
    assert err is None
    assert len(bot.docs) == 1
    assert bot.docs[0][0] == 550501
    assert "DTSTART:20261015T070000Z" in bot.docs[0][1].data.decode("utf-8")


def test_forum_date_screen_mentions_ics_and_time_is_not_a_job_trigger():
    src = Path("handlers/forum/admin_forum_date.py").read_text(encoding="utf-8")
    assert "ics" in src
    assert "forum_time" in src and "forum_timezone" in src
    triggers = set().union(*(spec[1] for spec in reschedule._SPECS))
    assert "forum_time" not in triggers
    assert "forum_end_time" not in triggers
    assert "forum_timezone" not in triggers
    assert "city_tz_offset" in triggers


def test_forum_timezone_example_and_refusal():
    saved, err = validate_setting_value("forum_timezone", "Europe/Moscow")
    assert err is None and saved == "Europe/Moscow"
    saved, err = validate_setting_value("forum_timezone", "Нет такого")
    assert saved is None and "Europe/Moscow" in err and "Пришлите" in err
    saved, err = validate_setting_value("forum_time", "10:00")
    assert err is None and saved == "10:00"
