"""Часовой пояс города (смещение от МСК, настройка `city_tz_offset`).

Смещение 0 / не задано — поведение как раньше; Тюмень (+2) — «идёт сейчас», отзыв о сессии,
рассылка QR, тихие часы, показ времени. Метки в БД остаются московскими.
"""
import asyncio
from datetime import datetime

from config import config
from database import db
import cities
from services import timeutil
from tests._dbtpl import fast_init_db


def _run(coro):
    return asyncio.run(coro)


def _db(tmp_path, name="tz.db"):
    config.DB_PATH = str(tmp_path / name)
    fast_init_db()


async def _tyumen_plus2():
    await db.set_setting("event_city_enabled", "on")
    await db.set_setting(cities.per_city_key("city_tz_offset", "tyumen"), "2")


# ── чистые помощники ─────────────────────────────────────────────────────────────────────────

def test_clamp_offset_and_labels():
    assert timeutil.clamp_offset("2") == 2
    assert timeutil.clamp_offset("-1") == -1
    assert timeutil.clamp_offset(None) == 0
    assert timeutil.clamp_offset("мусор") == 0
    assert timeutil.clamp_offset("42") == 0
    assert timeutil.offset_label(0) == "МСК"
    assert timeutil.offset_label(2) == "МСК+2"
    assert timeutil.offset_label(-1) == "МСК−1"


def test_registry_entry_is_buttons_with_msk_default():
    from settings_schema import SETTINGS_SCHEMA, option_labels
    entry = SETTINGS_SCHEMA["city_tz_offset"]
    assert entry["per_city"] is True and entry["default"] == "0"
    labels = option_labels("city_tz_offset")
    assert labels["0"] == "МСК" and labels["2"] == "МСК+2" and labels["-1"] == "МСК−1"
    assert set(labels) == set(entry["options"])


# ── чтение настройки ─────────────────────────────────────────────────────────────────────────

def test_offset_default_is_zero_and_city_now_equals_msk(tmp_path):
    _db(tmp_path)

    async def scenario():
        assert await timeutil.city_offset_hours("tyumen") == 0
        assert await timeutil.city_offset_hours(None) == 0
        a = timeutil.msk_now()
        b = await timeutil.city_now("tyumen")
        assert abs((b - a).total_seconds()) < 5

    _run(scenario())


def test_offset_per_city_override(tmp_path):
    _db(tmp_path)

    async def scenario():
        await _tyumen_plus2()
        assert await timeutil.city_offset_hours("tyumen") == 2
        assert await timeutil.city_offset_hours("spb") == 0
        assert await timeutil.city_offset_hours("msk") == 0
        shown = await timeutil.to_city_time(datetime(2026, 10, 3, 8, 5), "tyumen")
        assert shown == datetime(2026, 10, 3, 10, 5)
        same = await timeutil.to_city_time(datetime(2026, 10, 3, 8, 5), "spb")
        assert same == datetime(2026, 10, 3, 8, 5)

    _run(scenario())


def test_offset_module_off_ignores_city_override(tmp_path):
    _db(tmp_path)

    async def scenario():
        await db.set_setting(cities.per_city_key("city_tz_offset", "tyumen"), "2")
        assert await timeutil.city_offset_hours("tyumen") == 0

    _run(scenario())


# ── экран выбора ─────────────────────────────────────────────────────────────────────────────

def test_tz_screen_has_buttons_and_marks_current(tmp_path):
    _db(tmp_path)

    async def scenario():
        from handlers.admin_forum_tz import tz_cfg_text_kb
        await _tyumen_plus2()
        text, kb = await tz_cfg_text_kb("tyumen")
        flat = [b for row in kb.inline_keyboard for b in row]
        texts = [b.text for b in flat]
        assert "✅ МСК+2" in texts and "МСК" in texts and "МСК+9" in texts and "МСК−1" in texts
        assert "forumtz_set:tyumen:2" in [b.callback_data for b in flat]
        assert "МСК+2" in text

    _run(scenario())
