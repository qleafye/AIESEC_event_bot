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
