"""Вход отмечается только в день форума города делегата.

Проба сканера накануне на «🚪 Вход» не ставит настоящую отметку (иначе «Пришёл» в листе =
накануне, а утром нет приветствия). Волонтёр без привязки к городу 03.10 в СПб не отмечает
молча зелёным делегата Москвы (форум 30.10) — жёлтая плашка «форум не сегодня». Когда форум
сегодня в нескольких городах — на успешной плашке город делегата крупно. Выгрузка CSV
отмечает, но предупреждает строкой отчёта. Без даты форума — как раньше."""
from __future__ import annotations

from datetime import datetime

from cities import per_city_key
from database import db as bot_db
from services import checkin_csv_import
from tests.test_miniapp_checkin_260924 import (
    BASE,
    _freeze_now,
    _grant_checkin_to_bound_manager,
    _grant_checkin_to_game_manager,
    _insert_user,
    _qr,
    _run,
    client_with,
)
from tests.test_miniapp_routes import BOUND_MANAGER_ID, GAME_MANAGER_ID, _hdr


def _forum(city: str, date: str, days: int = 1):
    _run(bot_db.set_setting(per_city_key("forum_date", city), date))
    _run(bot_db.set_setting(per_city_key("sos_active_days", city), str(days)))


def _setup(tmp_path, monkeypatch, now: datetime):
    client = client_with(tmp_path)
    _run(bot_db.set_setting("event_city_enabled", "on"))
    _forum("spb", "03.10.2026")
    _forum("tyumen", "03.10.2026")
    _forum("msk", "30.10.2026", days=2)
    _freeze_now(monkeypatch, now)
    return client


def _entry_rows(uid: int) -> int:
    async def _q():
        async with bot_db._connect() as conn:
            async with conn.execute("SELECT COUNT(*) FROM checkins WHERE telegram_id = ? AND point = 'entry'", (uid,)) as cur:
                return (await cur.fetchone())[0]
    return _run(_q())


def test_trial_scan_day_before_forum_writes_nothing(tmp_path, monkeypatch):
    client = _setup(tmp_path, monkeypatch, datetime(2026, 10, 2, 18, 0))
    _grant_checkin_to_bound_manager()  # привязан к spb
    _run(_insert_user(952001, city="spb"))
    body = client.post(f"{BASE}/scan", json={"payload": _qr(952001)}, headers=_hdr(BOUND_MANAGER_ID)).json()
    assert body["status"] == "not_forum_day"
    assert "Сегодня не день форума" in body["reason_text"]
    assert "Тренировка" in body["reason_text"]
    assert _entry_rows(952001) == 0


def test_manual_mark_day_before_forum_writes_nothing(tmp_path, monkeypatch):
    client = _setup(tmp_path, monkeypatch, datetime(2026, 10, 2, 18, 0))
    _grant_checkin_to_bound_manager()
    _run(_insert_user(952002, city="spb"))
    body = client.post(f"{BASE}/manual", json={"telegram_id": 952002}, headers=_hdr(BOUND_MANAGER_ID)).json()
    assert body["status"] == "not_forum_day"
    assert _entry_rows(952002) == 0


def test_forum_day_marks_and_training_still_works_day_before(tmp_path, monkeypatch):
    client = _setup(tmp_path, monkeypatch, datetime(2026, 10, 3, 9, 0))
    _grant_checkin_to_bound_manager()
    _run(_insert_user(952003, city="spb"))
    body = client.post(f"{BASE}/scan", json={"payload": _qr(952003)}, headers=_hdr(BOUND_MANAGER_ID)).json()
    assert body["status"] == "new"
    assert "city_emphasis" not in body  # волонтёр привязан к городу — не нужно
    assert _entry_rows(952003) == 1


def test_training_point_is_not_blocked_day_before(tmp_path, monkeypatch):
    client = _setup(tmp_path, monkeypatch, datetime(2026, 10, 2, 18, 0))
    _grant_checkin_to_bound_manager()
    _run(_insert_user(952004, city="spb"))
    body = client.post(
        f"{BASE}/scan", json={"payload": _qr(952004), "point": "training"}, headers=_hdr(BOUND_MANAGER_ID),
    ).json()
    assert body["status"] != "not_forum_day"
    assert _entry_rows(952004) == 0


def test_unbound_volunteer_moscow_delegate_in_spb_gets_yellow_not_green(tmp_path, monkeypatch):
    client = _setup(tmp_path, monkeypatch, datetime(2026, 10, 3, 9, 0))
    _grant_checkin_to_game_manager()  # без привязки к городу
    _run(_insert_user(952005, city="msk"))
    body = client.post(f"{BASE}/scan", json={"payload": _qr(952005)}, headers=_hdr(GAME_MANAGER_ID)).json()
    assert body["status"] == "not_forum_day"
    assert "Делегат другого города:" in body["reason_text"]
    assert "30.10" in body["reason_text"]
    assert _entry_rows(952005) == 0


def test_unbound_volunteer_sees_city_big_when_two_forums_today(tmp_path, monkeypatch):
    client = _setup(tmp_path, monkeypatch, datetime(2026, 10, 3, 9, 0))
    _grant_checkin_to_game_manager()
    _run(_insert_user(952006, city="tyumen"))
    body = client.post(f"{BASE}/scan", json={"payload": _qr(952006)}, headers=_hdr(GAME_MANAGER_ID)).json()
    assert body["status"] == "new"
    assert body["city_emphasis"] is True
    assert body["city_label"]


def test_moscow_second_day_is_forum_day(tmp_path, monkeypatch):
    client = _setup(tmp_path, monkeypatch, datetime(2026, 10, 31, 9, 0))
    _grant_checkin_to_game_manager()
    _run(_insert_user(952007, city="msk"))
    body = client.post(f"{BASE}/scan", json={"payload": _qr(952007)}, headers=_hdr(GAME_MANAGER_ID)).json()
    assert body["status"] == "new"
    assert "city_emphasis" not in body  # 31.10 форум только в Москве


def test_no_forum_date_keeps_old_behaviour(tmp_path, monkeypatch):
    client = client_with(tmp_path)
    _grant_checkin_to_game_manager()
    _freeze_now(monkeypatch, datetime(2026, 10, 2, 18, 0))
    _run(_insert_user(952008))
    body = client.post(f"{BASE}/scan", json={"payload": _qr(952008)}, headers=_hdr(GAME_MANAGER_ID)).json()
    assert body["status"] == "new"


def test_csv_upload_marks_but_warns_about_off_day(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch, datetime(2026, 10, 3, 12, 0))
    from services.checkin import build_payload
    _run(_insert_user(952009, city="spb"))
    token = _run(bot_db.get_or_create_checkin_token(952009))
    from handlers import admin_checkin
    res = _run(checkin_csv_import.import_records(
        [{"qr": build_payload("YL26", "И", "spb", token), "scanned_at": "2026-10-02 18:00:00"}], "entry",
        session=None, bound_city=None, staff_id=1, bot=None, labels=admin_checkin._DENIAL_LABELS,
    ))
    assert res["new"] == 1 and res["off_day"] == 1
    lines = _run(checkin_csv_import.report_lines(res, row_limit=20))
    assert any("Вход не в день форума делегата: 1" in line for line in lines)


def test_delegate_without_city_is_not_treated_as_moscow(tmp_path, monkeypatch):
    """Пустой город делегата нормализовался бы в Москву (форум 30.10) — день не проверяем."""
    client = _setup(tmp_path, monkeypatch, datetime(2026, 10, 3, 9, 0))
    _grant_checkin_to_game_manager()  # без привязки к городу
    _run(_insert_user(952010, city=None))
    body = client.post(f"{BASE}/scan", json={"payload": _qr(952010)}, headers=_hdr(GAME_MANAGER_ID)).json()
    assert body["status"] == "new"
    assert _entry_rows(952010) == 1
    from services import checkin_forum_day
    assert _run(checkin_forum_day.off_day_for_scan({"event_city": ""}, "2026-10-01 10:00:00")) is False


def test_manager_can_mark_anyway_volunteer_gets_hint(tmp_path, monkeypatch):
    """Дата форума введена неверно: менеджер регистраций видит «Отметить всё равно» и
    отмечает с force_day; волонтёр без этого права — подсказку позвать менеджера, а force_day
    от него игнорируется."""
    client = _setup(tmp_path, monkeypatch, datetime(2026, 10, 2, 18, 0))
    _grant_checkin_to_bound_manager()  # reg_manager: moderate_reg + checkin
    _grant_checkin_to_game_manager()  # только checkin
    _run(_insert_user(952011, city="spb"))
    mgr = client.post(f"{BASE}/scan", json={"payload": _qr(952011)}, headers=_hdr(BOUND_MANAGER_ID)).json()
    assert mgr["status"] == "not_forum_day" and mgr["day_override"] is True
    vol = client.post(f"{BASE}/scan", json={"payload": _qr(952011)}, headers=_hdr(GAME_MANAGER_ID)).json()
    assert vol["status"] == "not_forum_day" and "day_override" not in vol
    assert "позовите менеджера" in vol["hint"]
    forced = client.post(
        f"{BASE}/manual", json={"telegram_id": 952011, "force_day": True}, headers=_hdr(GAME_MANAGER_ID),
    ).json()
    assert forced["status"] == "not_forum_day"
    assert _entry_rows(952011) == 0
    forced = client.post(
        f"{BASE}/manual", json={"telegram_id": 952011, "force_day": True}, headers=_hdr(BOUND_MANAGER_ID),
    ).json()
    assert forced["status"] == "new"
    assert _entry_rows(952011) == 1


def test_scanner_has_mark_anyway_button_with_confirm():
    from pathlib import Path
    js = (Path(__file__).resolve().parent.parent / "miniapp/static/js/screens/scanner.js").read_text(encoding="utf-8")
    body = js[js.index("function dayOverrideButton"):js.index("function closeScanPopup")]
    assert "askConfirm(" in body and "force_day: true" in body
    assert "res.day_override" in js


def _csv_untimed(rec_extra: dict, now: datetime, tmp_path, monkeypatch, uid: int):
    _setup(tmp_path, monkeypatch, now)
    from services.checkin import build_payload
    from handlers import admin_checkin
    _run(_insert_user(uid, city="spb"))
    token = _run(bot_db.get_or_create_checkin_token(uid))
    res = _run(checkin_csv_import.import_records(
        [{"qr": build_payload("YL26", "И", "spb", token), "scanned_at": None, **rec_extra}], "entry",
        session=None, bound_city=None, staff_id=1, bot=None, labels=admin_checkin._DENIAL_LABELS,
    ))

    async def _stamp():
        async with bot_db._connect() as conn:
            async with conn.execute("SELECT scanned_at, approx_time FROM checkins WHERE telegram_id = ? AND point = 'entry'", (uid,)) as cur:
                return tuple(await cur.fetchone())
    return res, _run(_stamp()), _run(checkin_csv_import.report_lines(res, row_limit=20))


def test_csv_without_time_next_day_lands_on_forum_day(tmp_path, monkeypatch):
    """Файл без даты и времени загрузили 04.10 — вход ложится на день форума (03.10), а не на
    день загрузки, и это отдельной строкой в отчёте."""
    res, stamp, lines = _csv_untimed({}, datetime(2026, 10, 4, 11, 0), tmp_path, monkeypatch, 952012)
    assert stamp == ("2026-10-03 12:00:00", 1)
    assert res["forum_day_assumed"] == 1 and res["untimed"] == 0
    assert any("первый день форума" in line for line in lines)


def test_csv_with_date_only_uses_that_date(tmp_path, monkeypatch):
    res, stamp, lines = _csv_untimed({"day": "2026-10-03"}, datetime(2026, 10, 5, 11, 0), tmp_path, monkeypatch, 952013)
    assert stamp == ("2026-10-03 12:00:00", 1)
    assert res["date_only"] == 1 and res["off_day"] == 0
    assert any("только дата" in line for line in lines)


def test_csv_without_time_on_forum_day_keeps_upload_time(tmp_path, monkeypatch):
    res, stamp, _lines = _csv_untimed({}, datetime(2026, 10, 3, 15, 30), tmp_path, monkeypatch, 952014)
    assert stamp[1] == 1  # время загрузки (часы БД не заморожены), «примерное»
    assert res["untimed"] == 1 and res["forum_day_assumed"] == 0


def test_csv_ambiguous_us_date_is_swapped_into_forum_window(tmp_path, monkeypatch):
    """«03/10/2026 10:15 AM» разбор читает как 10 марта (AM/PM = американский м/д). В окно форума
    СПб (03.10) попадает только перестановка — её и берём, со строкой отчёта."""
    from services.checkin import find_checkin_records, build_payload
    from handlers import admin_checkin
    _setup(tmp_path, monkeypatch, datetime(2026, 10, 3, 12, 0))
    _run(_insert_user(952015, city="spb"))
    token = _run(bot_db.get_or_create_checkin_token(952015))
    recs = find_checkin_records(f"time,text\n03/10/2026 10:15 AM,{build_payload('YL26', 'И', 'spb', token)}\n", "YL26")
    assert recs[0]["scanned_at"] == "2026-03-10 10:15:00" and recs[0]["alt"] == "2026-10-03 10:15:00"
    res = _run(checkin_csv_import.import_records(
        recs, "entry", session=None, bound_city=None, staff_id=1, bot=None, labels=admin_checkin._DENIAL_LABELS,
    ))
    assert res["swapped"] == 1 and res["off_day"] == 0

    async def _stamp():
        async with bot_db._connect() as conn:
            async with conn.execute("SELECT scanned_at FROM checkins WHERE telegram_id = ? AND point = 'entry'", (952015,)) as cur:
                return (await cur.fetchone())[0]
    assert _run(_stamp()) == "2026-10-03 10:15:00"
    lines = _run(checkin_csv_import.report_lines(res, row_limit=20))
    assert any("прочитана наоборот" in line for line in lines)


def test_csv_unambiguous_or_fitting_date_is_not_swapped(tmp_path, monkeypatch):
    from services.checkin import find_checkin_records
    assert "alt" not in find_checkin_records("t,x\n10/13/2026 10:15 AM,YL26·И·spb·tok1\n", "YL26")[0]
    assert "alt" not in find_checkin_records("t,x\n03.10.2026 10:15,YL26·И·spb·tok1\n", "YL26")[0]
    rec = find_checkin_records("t,x\n03/10/2026 10:15,YL26·И·spb·tok1\n", "YL26")[0]
    assert rec["scanned_at"] == "2026-10-03 10:15:00"  # без AM/PM — д/м, уже в окне


def test_day_check_reads_settings_in_one_snapshot(tmp_path, monkeypatch):
    """Проверка дня — на каждом скане входа: настройки одним снимком, а не соединением на ключ."""
    from datetime import date
    from services import checkin_forum_day
    _setup(tmp_path, monkeypatch, datetime(2026, 10, 2, 18, 0))
    reads = []
    real = bot_db._load_settings_snapshot

    async def _counting():
        reads.append(1)
        return await real()
    monkeypatch.setattr(bot_db, "_load_settings_snapshot", _counting)
    denial = _run(checkin_forum_day.entry_day_denial({"event_city": "msk"}, date(2026, 10, 3)))
    assert denial["status"] == "not_forum_day"
    assert _run(checkin_forum_day.city_emphasis(date(2026, 10, 3))) is True
    assert reads == [1, 1]


def test_cities_off_day_before_forum_writes_nothing(tmp_path, monkeypatch):
    """Модуль городов выключен (стенд, конференция): у всех пустой город, но проверка дня идёт
    по общей `forum_date` — проба сканера накануне не ставит настоящую отметку."""
    client = client_with(tmp_path)
    _run(bot_db.set_setting("event_city_enabled", "off"))
    _run(bot_db.set_setting("forum_date", "03.10.2026"))
    _run(bot_db.set_setting("sos_active_days", "1"))
    _freeze_now(monkeypatch, datetime(2026, 10, 2, 18, 0))
    _grant_checkin_to_game_manager()
    for uid, city in ((952020, None), (952021, "msk")):
        _run(_insert_user(uid, city=city))
        body = client.post(f"{BASE}/scan", json={"payload": _qr(uid)}, headers=_hdr(GAME_MANAGER_ID)).json()
        assert body["status"] == "not_forum_day", city
        assert _entry_rows(uid) == 0
    from services import checkin_forum_day
    assert _run(checkin_forum_day.off_day_for_scan({"event_city": None}, "2026-10-02 10:00:00")) is True
    assert _run(checkin_forum_day.off_day_for_scan({"event_city": None}, "2026-10-03 10:00:00")) is False


def test_cities_off_forum_day_marks_delegate_without_city(tmp_path, monkeypatch):
    client = client_with(tmp_path)
    _run(bot_db.set_setting("event_city_enabled", "off"))
    _run(bot_db.set_setting("forum_date", "03.10.2026"))
    _freeze_now(monkeypatch, datetime(2026, 10, 3, 9, 0))
    _grant_checkin_to_game_manager()
    _run(_insert_user(952022, city=None))
    body = client.post(f"{BASE}/scan", json={"payload": _qr(952022)}, headers=_hdr(GAME_MANAGER_ID)).json()
    assert body["status"] == "new"
    assert _entry_rows(952022) == 1


def test_cities_off_csv_untimed_lands_on_common_forum_day(tmp_path, monkeypatch):
    """Без городов CSV без времени, загруженный на следующий день, тоже ложится на день форума."""
    from services import timeutil
    client_with(tmp_path)
    _run(bot_db.set_setting("event_city_enabled", "off"))
    _run(bot_db.set_setting("forum_date", "03.10.2026"))
    _run(bot_db.set_setting("sos_active_days", "1"))
    monkeypatch.setattr(timeutil, "msk_now", lambda: datetime(2026, 10, 4, 15, 0))
    stamp = _run(checkin_csv_import._untimed_stamp({"scanned_at": None}, {"event_city": None}, "entry"))
    assert stamp == ("2026-10-03 12:00:00", "forum_day_assumed")


def test_csv_without_time_on_second_forum_day_keeps_upload_time(tmp_path, monkeypatch):
    """Москва, 2 дня (30–31.10): файл без даты и времени, загруженный во 2-й день, — время
    загрузки, а не 1-й день: иначе у вошедшего 30.10 проход 31.10 ушёл бы в «уже был»."""
    from services import timeutil
    _setup(tmp_path, monkeypatch, datetime(2026, 10, 30, 15, 0))
    rec, user = {"scanned_at": None}, {"event_city": "msk"}
    assert _run(checkin_csv_import._untimed_stamp(rec, user, "entry")) == (None, "untimed")
    monkeypatch.setattr(timeutil, "msk_now", lambda: datetime(2026, 10, 31, 15, 0))
    assert _run(checkin_csv_import._untimed_stamp(rec, user, "entry")) == (None, "untimed")
    monkeypatch.setattr(timeutil, "msk_now", lambda: datetime(2026, 11, 1, 10, 0))
    assert _run(checkin_csv_import._untimed_stamp(rec, user, "entry")) == ("2026-10-30 12:00:00", "forum_day_assumed")
