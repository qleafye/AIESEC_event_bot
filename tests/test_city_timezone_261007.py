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


# ── программа: «идёт сейчас» по часам города ─────────────────────────────────────────────────

def _freeze(monkeypatch, dt):
    monkeypatch.setattr(timeutil, "msk_now", lambda: dt)


def _seed_tyumen_sessions():
    async def seed():
        await db.create_program_session("tyumen", "2026-10-03", "10:00", "11:00", "Открытие")
        await db.create_program_session("tyumen", "2026-10-03", "12:00", "13:00", "Вторая")
    _run(seed())


def test_checkin_points_zero_offset_unchanged(tmp_path, monkeypatch):
    """Без настройки Тюмень считается по МСК, как раньше: 10:30 МСК -> «Открытие» живая."""
    _db(tmp_path)
    from services.program import checkin_session_points
    _seed_tyumen_sessions()
    _freeze(monkeypatch, datetime(2026, 10, 3, 10, 30))
    points = _run(checkin_session_points("tyumen"))
    assert [p["live"] for p in points] == [True, False]


def test_checkin_points_tyumen_plus2_uses_local_clock(tmp_path, monkeypatch):
    """08:30 МСК = 10:30 в Тюмени: живая «Открытие». 10:30 МСК = 12:30 в Тюмени: живая «Вторая»."""
    _db(tmp_path)
    from services.program import checkin_session_points
    _run(_tyumen_plus2())
    _seed_tyumen_sessions()
    _freeze(monkeypatch, datetime(2026, 10, 3, 8, 30))
    points = _run(checkin_session_points("tyumen"))
    assert points[0]["live"] and "Открытие" in points[0]["label"]
    _freeze(monkeypatch, datetime(2026, 10, 3, 10, 30))
    points = _run(checkin_session_points("tyumen"))
    assert points[0]["live"] and "Вторая" in points[0]["label"]
    # другой город без настройки не сдвигается
    _run(db.create_program_session("spb", "2026-10-03", "10:00", "11:00", "СПб"))
    assert _run(checkin_session_points("spb"))[0]["live"] is True


def test_delegate_program_tyumen_plus2_now_and_next(tmp_path, monkeypatch):
    _db(tmp_path)
    from services.program import build_delegate_program
    _run(_tyumen_plus2())
    _seed_tyumen_sessions()
    _freeze(monkeypatch, datetime(2026, 10, 3, 8, 30))  # 10:30 в Тюмени
    days = _run(build_delegate_program("tyumen"))
    sessions = [s for d in days for slot in d["slots"] for s in slot["sessions"]]
    by_title = {s["title"]: s for s in sessions}
    assert by_title["Открытие"]["now"] is True
    assert by_title["Вторая"]["now"] is False
