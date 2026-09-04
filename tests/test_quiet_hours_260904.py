"""Quick 260904-dq1: «🌙 Тихие часы» для уведомлений делегатам.

Файл наполняется по задачам плана:
- Task 1 (этот срез) — реестровая часть: четыре ключа существуют, дефолты/типы/группы
  верные, валидатор `format: "time"` принимает/отклоняет по правилам.
- Task 2 — окно (`services.quiet_hours.is_quiet`/`next_window_end`/`window_for_city`),
  очередь `delayed_notifications`, джоба `flush_due`.
- Task 3 — обёртка `apply_decision_effects`, дедуп «последнее решение», приписка менеджеру.
- Task 4 — гейма/монеты/напоминания.
- Task 5 — предупреждение о рассылке, счётчик очереди.

pytest-asyncio недоступен в этом окружении — async через `asyncio.run()`, `config.DB_PATH`
указывает на файл в `tmp_path` (конвенция сьюта, см. `tests/test_settings_int_validation_260819.py`).
"""
from __future__ import annotations

import asyncio

import pytest

from config import config
from database import db
from settings_schema import SETTINGS_SCHEMA
from settings_validation import validate_setting_value


# ── Task 1: реестр ──────────────────────────────────────────────────────────────────────

def test_quiet_hours_enabled_is_toggles_enum_default_off():
    entry = SETTINGS_SCHEMA["quiet_hours_enabled"]
    assert entry["type"] == "enum"
    assert entry["group"] == "toggles"
    assert entry["options"] == ["on", "off"]
    assert entry["prompt"] is None
    assert entry["default"] == "off"
    assert not entry.get("per_city")


@pytest.mark.parametrize("key", ["quiet_hours_start", "quiet_hours_end"])
def test_quiet_hours_time_keys_are_per_city_text_with_time_format(key):
    entry = SETTINGS_SCHEMA[key]
    assert entry["type"] == "text"
    assert entry["group"] == "apps"
    assert entry.get("per_city") is True
    assert entry.get("format") == "time"


def test_quiet_hours_start_end_defaults():
    assert SETTINGS_SCHEMA["quiet_hours_start"]["default"] == "22:00"
    assert SETTINGS_SCHEMA["quiet_hours_end"]["default"] == "09:00"


def test_quiet_hours_manager_notice_text_is_global_text():
    entry = SETTINGS_SCHEMA["quiet_hours_manager_notice_text"]
    assert entry["type"] == "text"
    assert entry["group"] == "apps"
    assert not entry.get("per_city")
    assert "{time}" in entry["default"]


@pytest.mark.parametrize("raw, expected", [
    ("22:00", "22:00"),
    ("9:00", "09:00"),
    ("00:00", "00:00"),
    (" 09:05 ", "09:05"),
])
def test_time_format_validator_accepts_and_normalizes(raw, expected):
    value, error = validate_setting_value("quiet_hours_start", raw)
    assert error is None
    assert value == expected


@pytest.mark.parametrize("raw", ["25:00", "22:60", "22-00", "вечером", "", "9", "99:99"])
def test_time_format_validator_rejects_with_example(raw):
    value, error = validate_setting_value("quiet_hours_end", raw)
    assert value is None
    assert error
    assert "22:00" in error
    assert "«-»" in error


def test_time_format_validator_per_city_composite_uses_base_type():
    value, error = validate_setting_value("quiet_hours_start__city__msk", "25:99")
    assert value is None and error
    value, error = validate_setting_value("quiet_hours_start__city__msk", "9:00")
    assert value == "09:00" and error is None


def _ready(tmp_path, name):
    config.DB_PATH = str(tmp_path / name)
    asyncio.run(db.init_db())


# ── Task 2: механика — окно, очередь delayed_notifications, джоба разбора ─────────────────

from datetime import datetime, time as dtime, timedelta

from cities import per_city_key
import services.quiet_hours as qh
from tests.test_miniapp_labels_drift import _loaded_aiogram

DELEGATE = 940901


def test_quiet_hours_module_does_not_load_aiogram():
    loaded = _loaded_aiogram("import services.quiet_hours")
    assert loaded == [], f"services.quiet_hours потянул aiogram: {loaded}"


# ── чистые функции ──────────────────────────────────────────────────────────────────────

def test_is_quiet_normal_window():
    start, end = dtime(9, 0), dtime(22, 0)
    assert qh.is_quiet(datetime(2026, 9, 4, 10, 0), start, end) is True
    assert qh.is_quiet(datetime(2026, 9, 4, 8, 59), start, end) is False
    assert qh.is_quiet(datetime(2026, 9, 4, 22, 0), start, end) is False  # [start, end)


def test_is_quiet_through_midnight():
    start, end = dtime(22, 0), dtime(9, 0)
    assert qh.is_quiet(datetime(2026, 9, 4, 23, 30), start, end) is True
    assert qh.is_quiet(datetime(2026, 9, 4, 2, 0), start, end) is True
    assert qh.is_quiet(datetime(2026, 9, 4, 9, 0), start, end) is False
    assert qh.is_quiet(datetime(2026, 9, 4, 12, 0), start, end) is False
    assert qh.is_quiet(datetime(2026, 9, 4, 22, 0), start, end) is True  # включительно start


def test_is_quiet_start_equals_end_means_no_window():
    same = dtime(10, 0)
    assert qh.is_quiet(datetime(2026, 9, 4, 10, 0), same, same) is False
    assert qh.is_quiet(datetime(2026, 9, 4, 23, 59), same, same) is False


def test_next_window_end_today_vs_tomorrow():
    start, end = dtime(22, 0), dtime(9, 0)
    assert qh.next_window_end(datetime(2026, 9, 4, 2, 0), start, end) == datetime(2026, 9, 4, 9, 0)
    assert qh.next_window_end(datetime(2026, 9, 4, 23, 30), start, end) == datetime(2026, 9, 5, 9, 0)


def test_parse_hhmm():
    assert qh.parse_hhmm("22:00") == dtime(22, 0)
    assert qh.parse_hhmm("9:05") == dtime(9, 5)
    assert qh.parse_hhmm(None) is None
    assert qh.parse_hhmm("") is None
    assert qh.parse_hhmm("вечером") is None
    assert qh.parse_hhmm("25:00") is None


# ── window_for_city ──────────────────────────────────────────────────────────────────────

def test_window_for_city_toggle_off_returns_none_even_with_hours_set(tmp_path):
    _ready(tmp_path, "test_qh_toggle_off.db")

    async def scenario():
        await db.set_setting("quiet_hours_start", "22:00")
        await db.set_setting("quiet_hours_end", "09:00")
        assert await qh.window_for_city(None) is None

    asyncio.run(scenario())


def test_window_for_city_per_city_hours_differ(tmp_path):
    _ready(tmp_path, "test_qh_percity.db")

    async def scenario():
        await db.set_setting("quiet_hours_enabled", "on")
        await db.set_setting("event_city_enabled", "on")
        await db.set_setting("quiet_hours_start", "22:00")
        await db.set_setting("quiet_hours_end", "09:00")
        await db.set_setting(per_city_key("quiet_hours_start", "spb"), "23:00")
        await db.set_setting(per_city_key("quiet_hours_end", "spb"), "08:00")

        assert await qh.window_for_city("msk") == (dtime(22, 0), dtime(9, 0))
        assert await qh.window_for_city("spb") == (dtime(23, 0), dtime(8, 0))

    asyncio.run(scenario())


def test_window_for_city_cities_module_off_uses_global(tmp_path):
    _ready(tmp_path, "test_qh_cities_off.db")

    async def scenario():
        await db.set_setting("quiet_hours_enabled", "on")
        # event_city_enabled НЕ выставляется -> модуль городов выключен
        await db.set_setting("quiet_hours_start", "22:00")
        await db.set_setting("quiet_hours_end", "09:00")
        await db.set_setting(per_city_key("quiet_hours_start", "spb"), "23:30")

        assert await qh.window_for_city("spb") == (dtime(22, 0), dtime(9, 0))

    asyncio.run(scenario())


def test_window_for_city_start_equals_end_is_no_window(tmp_path):
    _ready(tmp_path, "test_qh_percity_same.db")

    async def scenario():
        await db.set_setting("quiet_hours_enabled", "on")
        await db.set_setting("quiet_hours_start", "10:00")
        await db.set_setting("quiet_hours_end", "10:00")
        assert await qh.window_for_city(None) is None

    asyncio.run(scenario())


# ── defer_until ──────────────────────────────────────────────────────────────────────────

def test_defer_until_toggle_off_skips_city_resolution(tmp_path, monkeypatch):
    _ready(tmp_path, "test_qh_defer_off.db")
    from services import game_digest

    calls = []

    async def fake_resolve(uid):
        calls.append(uid)
        return None

    monkeypatch.setattr(game_digest, "resolve_submitter_city", fake_resolve)

    async def scenario():
        return await qh.defer_until(datetime(2026, 9, 4, 23, 0), DELEGATE)

    assert asyncio.run(scenario()) is None
    assert calls == [], "тумблер выключен -- город делегата резолвиться не должен вовсе"


def test_defer_until_in_window_returns_next_end_for_delegate_city(tmp_path):
    _ready(tmp_path, "test_qh_defer_in.db")

    async def scenario():
        await db.add_user({
            "telegram_id": DELEGATE, "event_city": "spb",
            "registration_date": "2026-09-04 00:00:00",
        })
        await db.set_setting("quiet_hours_enabled", "on")
        await db.set_setting("event_city_enabled", "on")
        await db.set_setting("quiet_hours_start", "22:00")
        await db.set_setting("quiet_hours_end", "09:00")
        await db.set_setting(per_city_key("quiet_hours_end", "spb"), "08:00")

        due = await qh.defer_until(datetime(2026, 9, 4, 23, 0), DELEGATE)
        assert due == datetime(2026, 9, 5, 8, 0)  # городской конец окна, не глобальный

    asyncio.run(scenario())


def test_defer_until_outside_window_returns_none(tmp_path):
    _ready(tmp_path, "test_qh_defer_out.db")

    async def scenario():
        await db.add_user({
            "telegram_id": DELEGATE, "event_city": "spb",
            "registration_date": "2026-09-04 00:00:00",
        })
        await db.set_setting("quiet_hours_enabled", "on")
        await db.set_setting("quiet_hours_start", "22:00")
        await db.set_setting("quiet_hours_end", "09:00")

        due = await qh.defer_until(datetime(2026, 9, 4, 12, 0), DELEGATE)
        assert due is None

    asyncio.run(scenario())


# ── enqueue / дедуп ──────────────────────────────────────────────────────────────────────

def test_enqueue_replace_keeps_one_row_per_user_and_kind(tmp_path):
    _ready(tmp_path, "test_qh_enqueue_replace.db")

    async def scenario():
        now = datetime(2026, 9, 4, 23, 0)
        due = datetime(2026, 9, 5, 9, 0)
        await qh.enqueue(DELEGATE, qh.KIND_APPLICATION_DECISION, {"status": "approved"}, due, now)
        await qh.enqueue(DELEGATE, qh.KIND_APPLICATION_DECISION, {"status": "rejected"}, due, now)
        rows = await db.list_due_delayed_notifications(due.strftime("%Y-%m-%d %H:%M:%S"))
        assert len(rows) == 1
        assert rows[0]["payload"]["status"] == "rejected"

    asyncio.run(scenario())


def test_enqueue_non_replaceable_kind_accumulates(tmp_path):
    _ready(tmp_path, "test_qh_enqueue_accum.db")

    async def scenario():
        now = datetime(2026, 9, 4, 23, 0)
        due = datetime(2026, 9, 5, 9, 0)
        await qh.enqueue(DELEGATE, qh.KIND_TEXT, {"text": "first"}, due, now)
        await qh.enqueue(DELEGATE, qh.KIND_TEXT, {"text": "second"}, due, now)
        rows = await db.list_due_delayed_notifications(due.strftime("%Y-%m-%d %H:%M:%S"))
        assert len(rows) == 2

    asyncio.run(scenario())


def test_enqueue_replace_does_not_touch_already_sent_rows(tmp_path):
    _ready(tmp_path, "test_qh_enqueue_sent.db")

    async def scenario():
        now = datetime(2026, 9, 4, 23, 0)
        due = datetime(2026, 9, 5, 9, 0)
        row_id = await qh.enqueue(
            DELEGATE, qh.KIND_APPLICATION_DECISION, {"status": "approved"}, due, now,
        )
        await db.mark_delayed_notification_sent(row_id, "2026-09-05 09:00:00")
        await qh.enqueue(DELEGATE, qh.KIND_APPLICATION_DECISION, {"status": "rejected"}, due, now)
        rows = await db.list_due_delayed_notifications(due.strftime("%Y-%m-%d %H:%M:%S"))
        assert len(rows) == 1
        assert rows[0]["payload"]["status"] == "rejected"
        assert await db.count_pending_delayed_notifications() == 1

    asyncio.run(scenario())


# ── flush_due / джоба ────────────────────────────────────────────────────────────────────

class _FakeBot:
    def __init__(self):
        self.sent = []

    async def send_message(self, chat_id, text, parse_mode=None):
        self.sent.append((chat_id, text, parse_mode))


def test_flush_due_sends_only_due_rows_and_marks_sent(tmp_path):
    _ready(tmp_path, "test_qh_flush.db")
    from services import scheduler as sched
    bot = _FakeBot()
    sched._bot = bot

    async def scenario():
        now = datetime(2026, 9, 5, 9, 0)
        await qh.enqueue(DELEGATE, qh.KIND_TEXT, {"text": "due"}, now - timedelta(minutes=1), now)
        await qh.enqueue(DELEGATE + 1, qh.KIND_TEXT, {"text": "not yet"}, now + timedelta(hours=1), now)
        sent_count = await qh.flush_due(now)
        assert sent_count == 1
        assert bot.sent == [(DELEGATE, "due", "HTML")]
        assert await db.count_pending_delayed_notifications() == 1

    asyncio.run(scenario())


def test_flush_due_unknown_kind_marked_error_never_executed(tmp_path):
    _ready(tmp_path, "test_qh_flush_unknown.db")
    from services import scheduler as sched
    bot = _FakeBot()
    sched._bot = bot

    async def scenario():
        now = datetime(2026, 9, 5, 9, 0)
        await qh.enqueue(DELEGATE, "some_future_kind", {"x": 1}, now - timedelta(minutes=1), now)
        sent_count = await qh.flush_due(now)
        assert sent_count == 1
        assert bot.sent == []
        assert await db.count_pending_delayed_notifications() == 0

    asyncio.run(scenario())


def test_flush_due_twice_does_not_resend(tmp_path):
    _ready(tmp_path, "test_qh_flush_twice.db")
    from services import scheduler as sched
    bot = _FakeBot()
    sched._bot = bot

    async def scenario():
        now = datetime(2026, 9, 5, 9, 0)
        await qh.enqueue(DELEGATE, qh.KIND_TEXT, {"text": "once"}, now - timedelta(minutes=1), now)
        first = await qh.flush_due(now)
        second = await qh.flush_due(now)
        assert first == 1
        assert second == 0
        assert len(bot.sent) == 1

    asyncio.run(scenario())


def test_queued_count_matches_pending(tmp_path):
    _ready(tmp_path, "test_qh_count.db")

    async def scenario():
        now = datetime(2026, 9, 4, 23, 0)
        due = datetime(2026, 9, 5, 9, 0)
        assert await qh.queued_count() == 0
        await qh.enqueue(DELEGATE, qh.KIND_TEXT, {"text": "a"}, due, now)
        assert await qh.queued_count() == 1

    asyncio.run(scenario())
