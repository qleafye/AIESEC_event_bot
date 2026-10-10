"""Часовой пояс города (смещение от МСК, настройка `city_tz_offset`).

Смещение 0 / не задано — поведение как раньше; Тюмень (+2) — «идёт сейчас», отзыв о сессии,
рассылка QR, тихие часы, показ времени. Метки в БД остаются московскими.
"""
import asyncio
from datetime import datetime

from config import config
from database import db
import domain.cities as cities
from services.infra import timeutil
from tests._dbtpl import fast_init_db


def _run(coro):
    return asyncio.run(coro)


def _db(tmp_path, name="tz.db"):
    config.DB_PATH = str(tmp_path / name)
    fast_init_db()


async def _tyumen_plus2():
    await db.set_setting("event_city_enabled", "on")
    await db.set_setting(cities.per_city_key("city_tz_offset", "tyumen"), "2")


async def _forum_dates_2037():
    """Модуль городов включён — у каждого города своя дата форума (общая не наследуется)."""
    for code in ("tyumen", "spb"):
        await db.set_setting(cities.per_city_key("forum_date", code), "03.10.2037")


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
    from domain.settings.schema import SETTINGS_SCHEMA, option_labels
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
        from handlers.forum.admin_forum_tz import tz_cfg_text_kb
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
    from services.forum.program import checkin_session_points
    _seed_tyumen_sessions()
    _freeze(monkeypatch, datetime(2026, 10, 3, 10, 30))
    points = _run(checkin_session_points("tyumen"))
    assert [p["live"] for p in points] == [True, False]


def test_checkin_points_tyumen_plus2_uses_local_clock(tmp_path, monkeypatch):
    """08:30 МСК = 10:30 в Тюмени: живая «Открытие». 10:30 МСК = 12:30 в Тюмени: живая «Вторая»."""
    _db(tmp_path)
    from services.forum.program import checkin_session_points
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
    from services.forum.program import build_delegate_program
    _run(_tyumen_plus2())
    _seed_tyumen_sessions()
    _freeze(monkeypatch, datetime(2026, 10, 3, 8, 30))  # 10:30 в Тюмени
    days = _run(build_delegate_program("tyumen"))
    sessions = [s for d in days for slot in d["slots"] for s in slot["sessions"]]
    by_title = {s["title"]: s for s in sessions}
    assert by_title["Открытие"]["now"] is True
    assert by_title["Вторая"]["now"] is False


# ── отзыв о сессии: момент отправки в МСК ────────────────────────────────────────────────────

def test_feedback_run_at_tyumen_plus2_and_default(tmp_path):
    _db(tmp_path)
    from services.session_feedback import _run_at_for_session

    async def scenario():
        session = {"city": "tyumen", "day": "2026-10-03", "end_time": "11:00"}
        before = await _run_at_for_session(session)  # настройки нет — как раньше
        await _tyumen_plus2()
        after = await _run_at_for_session(session)
        assert (before - after).total_seconds() == 2 * 3600
        # 11:00 местного + задержка (по умолчанию) - 2 ч
        assert after.hour == 9 and after.day == 3
        other = await _run_at_for_session({**session, "city": "spb"})
        assert other == before

    _run(scenario())


# ── тихие часы: окно считается по часам города делегата ──────────────────────────────────────

async def _seed_delegate(uid, city):
    await db.add_user({
        "telegram_id": uid, "full_name": "Иванов Иван", "registration_date": "2026-01-01 00:00:00",
        "event_city": city,
    })
    async with db._connect() as conn:
        await conn.execute("UPDATE users SET status = 'approved', season = NULL WHERE telegram_id = ?", (uid,))
        await conn.commit()


def test_quiet_hours_tyumen_uses_local_window(tmp_path):
    _db(tmp_path)
    from services import quiet_hours

    async def scenario():
        await _tyumen_plus2()
        await db.set_setting("quiet_hours_enabled", "on")
        await _seed_delegate(7001, "tyumen")
        await _seed_delegate(7002, "spb")
        # 07:30 МСК: в Тюмени уже 09:30 (тишина кончилась), в СПб ещё 07:30 (тишина).
        now = datetime(2026, 10, 3, 7, 30)
        assert await quiet_hours.defer_until(now, 7001) is None
        assert await quiet_hours.defer_until(now, 7002) == datetime(2026, 10, 3, 9, 0)
        # 21:00 МСК: в Тюмени 23:00 (тишина) -> конец в 09:00 местного = 07:00 МСК следующего дня.
        evening = datetime(2026, 10, 3, 21, 0)
        assert await quiet_hours.defer_until(evening, 7001) == datetime(2026, 10, 4, 7, 0)
        assert await quiet_hours.defer_until(evening, 7002) is None

    _run(scenario())


def test_not_arrived_send_quiet_hours_tyumen(tmp_path, monkeypatch):
    """В Тюмени 09:30 местного (07:30 МСК) делегат вне тихих часов — шаблон уходит."""
    _db(tmp_path)
    from services.forum import checkin_not_arrived as cna

    class FakeBot:
        def __init__(self):
            self.sent = []

        async def send_message(self, chat_id, text, reply_markup=None):
            self.sent.append(chat_id)
            return type("Msg", (), {"message_id": 1})()

    bot = FakeBot()
    sent = bot.sent

    async def scenario():
        await _tyumen_plus2()
        await db.set_setting("quiet_hours_enabled", "on")
        # Форум города идёт «сегодня» по настоящим часам (фильтр «не пришли» смотрит туда),
        # а часы шаблона заморожены на другой день — окно тихих часов проверяется как обычно.
        await db.set_setting(
            cities.per_city_key("forum_date", "tyumen"), timeutil.msk_now().strftime("%d.%m.%Y"),
        )
        await _seed_delegate(7003, "tyumen")
        monkeypatch.setattr(cna, "msk_now", lambda: datetime(2026, 10, 3, 7, 30))
        monkeypatch.setattr(cna._sched, "_bot", bot)
        return await cna.send(city="tyumen", city_scope=None)

    result = _run(scenario())
    assert result == {"sent": 1, "quiet": 0, "failed": 0, "total": 1}, result
    assert sent == [7003]


# ── рассылка QR: часы города ─────────────────────────────────────────────────────────────────

def test_qr_jobs_fire_at_local_clock_of_city(tmp_path, monkeypatch):
    """Утренний повтор 08:00 в Тюмени (МСК+2) — джоба на 06:00 МСК; вечерний 18:00 — на 16:00 МСК;
    СПб без настройки — как раньше, 08:00/18:00 МСК."""
    from services import checkin_broadcast as cb
    from tests.test_checkin_qr_broadcast_260924 import _forum_setup, _run_scheduled

    _forum_setup(tmp_path)
    _run(_tyumen_plus2())
    _run(_forum_dates_2037())
    monkeypatch.setattr(cb, "msk_now", lambda: datetime(2037, 10, 1, 12, 0))

    async def body(s):
        result = await cb.schedule_city_jobs("tyumen")
        assert result["scheduled"] is True
        ev = s.get_job(cb.evening_job_id("tyumen"))
        morn = s.get_job(cb.morning_job_id("tyumen"))
        assert ev.next_run_time.replace(tzinfo=None) == datetime(2037, 10, 2, 16, 0)
        assert morn.next_run_time.replace(tzinfo=None) == datetime(2037, 10, 3, 6, 0)
        await cb.schedule_city_jobs("spb")
        assert s.get_job(cb.evening_job_id("spb")).next_run_time.replace(tzinfo=None) == datetime(2037, 10, 2, 18, 0)
        assert s.get_job(cb.morning_job_id("spb")).next_run_time.replace(tzinfo=None) == datetime(2037, 10, 3, 8, 0)

    _run_scheduled(tmp_path, monkeypatch, body)


def test_qr_job_wrong_day_uses_city_calendar(tmp_path, monkeypatch):
    """23:30 МСК накануне форума — в Тюмени уже день форума (01:30), вечерняя «канун» не тот день."""
    from services import checkin_broadcast as cb
    from tests.test_checkin_qr_broadcast_260924 import _forum_setup

    _forum_setup(tmp_path)
    _run(_tyumen_plus2())
    _run(_forum_dates_2037())
    monkeypatch.setattr(cb, "msk_now", lambda: datetime(2037, 10, 2, 23, 30))

    async def scenario():
        return (
            await cb.is_forum_day_offset("tyumen", 0),   # в Тюмени уже день форума
            await cb.is_forum_day_offset("spb", 0),      # в СПб ещё канун
        )

    tyumen_today, spb_today = _run(scenario())
    assert tyumen_today is True and spb_today is False


# ── показ времени: SOS, приветствие, лист, отчёт дня ─────────────────────────────────────────

def test_sos_card_shows_city_local_time():
    from services.sos import render_card_text
    report = {"id": 5, "telegram_id": 1, "created_at": "2026-10-03 08:05:00", "city": "tyumen"}
    user = {"full_name": "Иванов"}
    assert "08:05" in render_card_text(report, user, city_label="Тюмень")
    local = render_card_text(report, user, city_label="Тюмень", tz_offset=2)
    assert "03.10.2026 10:05" in local and "08:05" not in local


def test_welcome_time_is_city_local():
    from services.forum.forum_welcome import _format_time
    assert _format_time("2026-10-03 08:05:00") == "08:05"
    assert _format_time("2026-10-03 08:05:00", 2) == "10:05"
    assert _format_time(None, 2) == "сейчас"


def test_sheet_arrival_cell_is_city_local():
    from services.sheets.sheet_arrival_sync import arrival_cell_value
    assert arrival_cell_value("2026-10-03 08:05:00") == "03.10 08:05"
    assert arrival_cell_value("2026-10-03 08:05:00", 2) == "03.10 10:05"
    assert arrival_cell_value("2026-10-03 23:30:00", 2) == "04.10 01:30"
    assert arrival_cell_value(None, 2) == ""


def test_sheet_offsets_skip_user_reads_when_no_city_has_offset(tmp_path):
    _db(tmp_path)
    from services.sheets.sheet_arrival_sync import city_offsets_by_user

    async def scenario():
        await _seed_delegate(8001, "tyumen")
        assert await city_offsets_by_user([8001]) == {}
        await _tyumen_plus2()
        assert await city_offsets_by_user([8001]) == {8001: 2}

    _run(scenario())


def test_day_report_peak_hour_in_city_time(tmp_path, monkeypatch):
    _db(tmp_path)
    from services import forum_day_report as fdr
    import database.db as dbmod

    async def fake_peak(day, city_scope=None):
        return ("08", 12)

    monkeypatch.setattr(dbmod, "checkin_peak_hour_for_city_day", fake_peak)

    async def scenario():
        await _tyumen_plus2()
        tyumen = await fdr.build_report_text("tyumen", "2026-10-03")
        spb = await fdr.build_report_text("spb", "2026-10-03")
        return tyumen, spb

    tyumen, spb = _run(scenario())
    assert "10:00–10:59" in tyumen
    assert "08:00–08:59" in spb


# ── CSV сканера: время телефона = местное ────────────────────────────────────────────────────

def test_csv_parser_marks_naive_times_only():
    from services.forum.checkin import build_payload, find_checkin_records
    qr = build_payload("YL26", "И", "tyumen", "tok1")
    qr2 = build_payload("YL26", "И", "tyumen", "tok2")
    qr3 = build_payload("YL26", "И", "tyumen", "tok3")
    text = (
        f"time,text\n2026-10-03 10:00:00,{qr}\n"
        f"2026-10-03T08:00:00Z,{qr2}\n"
        f"1759478400,{qr3}\n"
    )
    recs = {r["qr"]: r for r in find_checkin_records(text, "YL26")}
    assert recs[qr].get("naive") is True
    assert "naive" not in recs[qr2] and "naive" not in recs[qr3]
    assert recs[qr2]["scanned_at"] == "2026-10-03 11:00:00"  # Z -> МСК, как раньше


def test_session_window_check_in_city_local_time():
    from services.forum.program import scanned_outside_session_window
    session = {"day": "2026-10-03", "start_time": "10:00", "end_time": "11:00"}
    # метка МСК 08:15 = 10:15 в Тюмени -> внутри сессии; без смещения — вне окна
    assert scanned_outside_session_window(session, "2026-10-03 08:15:00", offset_hours=2) is False
    assert scanned_outside_session_window(session, "2026-10-03 08:15:00") is True


def test_csv_import_converts_naive_local_stamp_to_msk(tmp_path, monkeypatch):
    from services.forum import checkin_csv_import
    from services.forum.checkin import build_payload
    from handlers.forum import admin_checkin
    from tests.test_checkin_forum_day_261001 import _forum, _insert_user, _setup

    _setup(tmp_path, monkeypatch, datetime(2026, 10, 3, 12, 0))
    _run(_tyumen_plus2())
    _forum("tyumen", "03.10.2026")
    _run(_insert_user(952101, city="tyumen"))
    _run(_insert_user(952102, city="tyumen"))
    t1 = _run(db.get_or_create_checkin_token(952101))
    t2 = _run(db.get_or_create_checkin_token(952102))
    records = [
        {"qr": build_payload("YL26", "И", "tyumen", t1), "scanned_at": "2026-10-03 10:00:00", "naive": True},
        {"qr": build_payload("YL26", "И", "tyumen", t2), "scanned_at": "2026-10-03 08:00:00"},  # абсолютное, уже МСК
    ]
    res = _run(checkin_csv_import.import_records(
        records, "entry", session=None, bound_city=None, staff_id=1, bot=None,
        labels=admin_checkin._DENIAL_LABELS,
    ))
    assert res["new"] == 2

    async def stamp(uid):
        async with db._connect() as conn:
            async with conn.execute(
                "SELECT scanned_at FROM checkins WHERE telegram_id = ? AND point = 'entry'", (uid,),
            ) as cur:
                return (await cur.fetchone())[0]

    assert _run(stamp(952101)) == "2026-10-03 08:00:00"
    assert _run(stamp(952102)) == "2026-10-03 08:00:00"


def test_floor_report_live_flag_uses_each_sessions_city_clock(tmp_path):
    """«Сейчас на площадке»: в 08:30 МСК у Тюмени (МСК+2) идёт сессия 10:00–11:00, у СПб — нет."""
    _db(tmp_path)
    from services.forum import checkin_arrival

    async def scenario():
        await _tyumen_plus2()
        await db.create_program_session("tyumen", "2026-10-03", "10:00", "11:00", "Тюменская")
        await db.create_program_session("spb", "2026-10-03", "10:00", "11:00", "Питерская")
        report = await checkin_arrival.floor_report(None, None, datetime(2026, 10, 3, 8, 30))
        return report

    report = _run(scenario())
    live = {s["title"]: s["live"] for s in report["sessions"]}
    assert live == {"Тюменская": True, "Питерская": False}
