"""Phase 12 (FORUM-CHECKIN.md, D-08/D-12/D-13, идея №9): сканер отметки на форуме в Mini App —
`/app/api/checkin/*` (`miniapp/routers/checkin.py`). Харнесс — `tests/test_miniapp_routes.py`,
тот же приём, что `tests/test_miniapp_admin_tasks.py`.

Капа `checkin` НЕ входит ни в один `default_caps` роли (`handlers/admin_caps.py::ROLES`) —
менеджер добавляет её вручную через `role_caps_*`; здесь она примешивается settings-сидом,
как `tests/test_dashboard_auth.py` делает для остальных прав."""
from __future__ import annotations

import asyncio
from datetime import datetime

import aiosqlite

import cities as cities_mod
from config import config as bot_config
from database import db as bot_db
from services import timeutil as timeutil_mod
from services.checkin import ENTRY_POINT, build_payload

from tests.test_miniapp_routes import (
    ADMIN_ID,
    BOUND_MANAGER_ID,
    GAME_MANAGER_ID,
    _cfg,
    _client,
    _hdr,
    _seed,
    _set,
    _standard_seed,
    _use_tmp_db,
)

BASE = "/app/api/checkin"
TAG = "YL26"


def _run(coro):
    return asyncio.run(coro)


async def _insert_user(
    telegram_id, *, full_name="Иванов Иван", status="approved", season="YL'26",
    city="msk", username=None, university=None,
):
    async with bot_db._connect() as conn:
        await conn.execute(
            "INSERT INTO users (telegram_id, full_name, status, season, event_city, username, university) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (telegram_id, full_name, status, season, city, username, university),
        )
        await conn.commit()


def _token(telegram_id) -> str:
    return _run(bot_db.get_or_create_checkin_token(telegram_id))


def _qr(telegram_id, *, tag=TAG, full_name="Иванов Иван", city="Казань") -> str:
    return build_payload(tag, full_name, city, _token(telegram_id))


def _grant_checkin_to_game_manager():
    """GAME_MANAGER_ID (не привязан к городу) получает checkin — базовые сценарии без
    городского скоупа."""
    _set("role_caps_game_manager", "moderate_game;checkin")


def _grant_checkin_to_bound_manager():
    """BOUND_MANAGER_ID (`reg_manager`, привязан к spb в `_standard_seed`) получает checkin —
    сценарии городского скоупа A2/B2."""
    _set("role_caps_reg_manager", "moderate_reg;moderate_receipts;checkin")


def _seed_ready(tmp_path):
    db_path = _use_tmp_db(tmp_path, "miniapp_checkin.db")
    _standard_seed()
    _run(bot_db.set_setting("event_season", "YL'26"))
    return db_path


def client_with(tmp_path):
    db_path = _seed_ready(tmp_path)
    return _client(_cfg(db_path))


# ── /scan ────────────────────────────────────────────────────────────────────────────────

def test_scan_without_cap_is_403(tmp_path):
    client = client_with(tmp_path)
    uid = 950001
    _run(_insert_user(uid))
    payload = _qr(uid)
    resp = client.post(f"{BASE}/scan", json={"payload": payload}, headers=_hdr(GAME_MANAGER_ID))
    assert resp.status_code == 403
    assert resp.json()["reason"] == "no_cap"


def test_scan_new_then_duplicate(tmp_path):
    client = client_with(tmp_path)
    _grant_checkin_to_game_manager()
    uid = 950002
    _run(_insert_user(uid, full_name="Петров Пётр", city="spb"))
    payload = _qr(uid, full_name="Петров Пётр", city="СПб")

    r1 = client.post(f"{BASE}/scan", json={"payload": payload}, headers=_hdr(GAME_MANAGER_ID))
    assert r1.status_code == 200
    body1 = r1.json()
    assert body1["status"] == "new"
    assert body1["full_name"] == "Петров Пётр"
    assert body1["city"] == "spb"

    r2 = client.post(f"{BASE}/scan", json={"payload": payload}, headers=_hdr(GAME_MANAGER_ID))
    body2 = r2.json()
    assert body2["status"] == "duplicate"
    assert body2["scanned_at"] == body1["scanned_at"]


def test_scan_denied_not_approved_with_human_reason(tmp_path):
    client = client_with(tmp_path)
    _grant_checkin_to_game_manager()
    uid = 950003
    _run(_insert_user(uid, status="pending"))
    payload = _qr(uid)
    resp = client.post(f"{BASE}/scan", json={"payload": payload}, headers=_hdr(GAME_MANAGER_ID))
    body = resp.json()
    assert body["status"] == "denied"
    assert body["reason_text"] == "Заявка ещё на рассмотрении"


def test_scan_denied_past_season(tmp_path):
    client = client_with(tmp_path)
    _grant_checkin_to_game_manager()
    uid = 950004
    _run(_insert_user(uid, status="approved", season="YL'25"))
    payload = _qr(uid)
    resp = client.post(f"{BASE}/scan", json={"payload": payload}, headers=_hdr(GAME_MANAGER_ID))
    body = resp.json()
    assert body["status"] == "denied"
    assert body["reason_text"] == "Делегат прошлого сезона"


def test_scan_not_found_unknown_token(tmp_path):
    client = client_with(tmp_path)
    _grant_checkin_to_game_manager()
    payload = build_payload(TAG, "Чужой Чужаков", "Тюмень", "no-such-token")
    resp = client.post(f"{BASE}/scan", json={"payload": payload}, headers=_hdr(GAME_MANAGER_ID))
    body = resp.json()
    assert body["status"] == "not_found"
    assert body["reason_text"] == "QR не найден — отправьте на стойку проблемных случаев"
    assert body["full_name"] == "Чужой Чужаков"  # видно из самого QR (T-12-01, недоверенный ввод)


def test_scan_foreign_event_tag(tmp_path):
    client = client_with(tmp_path)
    _grant_checkin_to_game_manager()
    uid = 950005
    _run(_insert_user(uid))
    payload = _qr(uid, tag="OTHERFEST")
    resp = client.post(f"{BASE}/scan", json={"payload": payload}, headers=_hdr(GAME_MANAGER_ID))
    body = resp.json()
    assert body["status"] == "foreign_event"
    assert body["reason_text"] == "QR другого мероприятия"


# ── /manual ──────────────────────────────────────────────────────────────────────────────

def test_manual_records_checkin_without_qr(tmp_path):
    client = client_with(tmp_path)
    _grant_checkin_to_game_manager()
    uid = 950006
    _run(_insert_user(uid, full_name="Сидоров Сидор"))
    resp = client.post(f"{BASE}/manual", json={"telegram_id": uid}, headers=_hdr(GAME_MANAGER_ID))
    body = resp.json()
    assert body["status"] == "new"
    assert body["full_name"] == "Сидоров Сидор"
    assert _run(bot_db.count_checkins_by_point("entry")) == 1


def test_manual_denied_reason_for_unapproved(tmp_path):
    client = client_with(tmp_path)
    _grant_checkin_to_game_manager()
    uid = 950007
    _run(_insert_user(uid, status="rejected"))
    resp = client.post(f"{BASE}/manual", json={"telegram_id": uid}, headers=_hdr(GAME_MANAGER_ID))
    assert resp.json()["status"] == "denied"


# ── /search ──────────────────────────────────────────────────────────────────────────────

def test_search_eligible_delegate_first_with_city_username_university(tmp_path):
    client = client_with(tmp_path)
    _grant_checkin_to_game_manager()
    _run(_insert_user(
        950010, full_name="Иванова Мария", status="rejected", city="msk",
        username="maria_i", university="ВШЭ",
    ))
    _run(_insert_user(
        950011, full_name="Иванова Марина", status="approved", city="spb",
        username="marina_i", university="СПбГУ",
    ))
    resp = client.get(f"{BASE}/search", params={"q": "Иванова"}, headers=_hdr(GAME_MANAGER_ID))
    assert resp.status_code == 200
    items = resp.json()["items"]
    assert len(items) == 2
    assert items[0]["telegram_id"] == 950011  # одобренный текущего сезона -- первым (D-12)
    assert items[0]["eligible"] is True
    assert items[0]["username"] == "marina_i"
    assert items[0]["university"] == "СПбГУ"
    assert items[1]["telegram_id"] == 950010
    assert items[1]["eligible"] is False
    assert items[1]["reason_text"]


def test_search_yo_e_fold(tmp_path):
    """Ё=е при поиске по фамилии (D-12) — общий сервис person_search уже это гарантирует,
    здесь только проверяем, что ручка сканера не потеряла это поведение при переиспользовании."""
    client = client_with(tmp_path)
    _grant_checkin_to_game_manager()
    _run(_insert_user(950012, full_name="Фёдоров Фёдор", status="approved"))
    resp = client.get(f"{BASE}/search", params={"q": "федоров"}, headers=_hdr(GAME_MANAGER_ID))
    items = resp.json()["items"]
    assert any(it["telegram_id"] == 950012 for it in items)


# ── /stats (A2 breakdown) ────────────────────────────────────────────────────────────────

def test_stats_bound_manager_sees_only_own_city(tmp_path):
    client = client_with(tmp_path)
    _grant_checkin_to_bound_manager()
    _run(bot_db.set_setting("event_city_enabled", "on"))
    _run(_insert_user(950020, city="spb"))
    _run(_insert_user(950021, city="msk"))
    _run(bot_db.record_checkin(950020, "entry", source="miniapp"))

    resp = client.get(f"{BASE}/stats", headers=_hdr(BOUND_MANAGER_ID))
    body = resp.json()
    assert body == {"arrived": 1, "approved": 1, "cities": None}


def test_stats_unbound_admin_sees_breakdown_by_city(tmp_path):
    client = client_with(tmp_path)
    _run(bot_db.set_setting("event_city_enabled", "on"))
    _run(_insert_user(950022, city="spb"))
    _run(_insert_user(950023, city="spb"))
    _run(_insert_user(950024, city="msk"))
    _run(bot_db.record_checkin(950022, "entry", source="miniapp"))
    # `_standard_seed()` уже посадил DELEGATE_ID approved БЕЗ event_city (NULL) -- дефолт-город
    # (msk) считает такую строку своей (city_scope("msk") = исключающая форма, ловит NULL) —
    # учитываем эту строку в ожиданиях явно, а не прячем её переустановкой сида.

    resp = client.get(f"{BASE}/stats", headers=_hdr(ADMIN_ID))  # суперадмин -- никогда не скопирован
    body = resp.json()
    assert body["arrived"] == 1 and body["approved"] == 4
    by_code = {c["code"]: c for c in body["cities"]}
    assert by_code["spb"]["arrived"] == 1 and by_code["spb"]["approved"] == 2
    assert by_code["msk"]["arrived"] == 0 and by_code["msk"]["approved"] == 2  # 950024 + DELEGATE_ID
    assert "tyumen" not in by_code  # нет одобренных -- строки нет (A2)


def test_stats_cities_module_off_is_unscoped(tmp_path):
    client = client_with(tmp_path)
    _grant_checkin_to_game_manager()
    _run(_insert_user(950025, city="spb"))
    _run(_insert_user(950026, city="msk"))
    # + DELEGATE_ID approved из `_standard_seed()` -> 3 одобренных всего.
    resp = client.get(f"{BASE}/stats", headers=_hdr(GAME_MANAGER_ID))
    body = resp.json()
    assert body == {"arrived": 0, "approved": 3, "cities": None}


# ── раздел выключен чекбоксом ────────────────────────────────────────────────────────────

def test_section_off_gates_with_403(tmp_path):
    client = client_with(tmp_path)
    _grant_checkin_to_game_manager()
    _set("miniapp_section_checkin", "off")
    resp = client.get(f"{BASE}/stats", headers=_hdr(GAME_MANAGER_ID))
    assert resp.status_code == 403
    assert resp.json()["reason"] == "section_off"


# ── /points (форум-ночь п.5, D-18): «Вход» + сессии сегодня, «идёт сейчас» первыми ──────────

def _freeze_now(monkeypatch, dt: datetime):
    monkeypatch.setattr(timeutil_mod, "msk_now", lambda: dt)


def test_points_bound_manager_gets_entry_and_todays_sessions_live_first(tmp_path, monkeypatch):
    client = client_with(tmp_path)
    _grant_checkin_to_bound_manager()
    _run(bot_db.set_setting("event_city_enabled", "on"))
    _freeze_now(monkeypatch, datetime(2026, 10, 3, 10, 30))
    later = _run(bot_db.create_program_session("spb", "2026-10-03", "11:00", "12:00", "Позже"))
    now_id = _run(bot_db.create_program_session("spb", "2026-10-03", "10:00", "11:00", "Идёт сейчас"))
    resp = client.get(f"{BASE}/points", headers=_hdr(BOUND_MANAGER_ID))
    body = resp.json()
    assert body["city"] == "spb"
    assert body["cities"] is None
    points = body["points"]
    assert points[0]["point"] == "entry"
    assert points[1]["point"] == f"session:{now_id}"
    assert points[1]["live"] is True
    assert points[2]["point"] == f"session:{later}"
    assert points[2]["live"] is False


def test_points_unbound_manager_needs_city_picker_without_query(tmp_path):
    client = client_with(tmp_path)
    _grant_checkin_to_game_manager()
    _run(bot_db.set_setting("event_city_enabled", "on"))
    resp = client.get(f"{BASE}/points", headers=_hdr(GAME_MANAGER_ID))
    body = resp.json()
    assert body["city"] is None
    assert body["cities"] is not None
    codes = {c["code"] for c in body["cities"]}
    assert "spb" in codes
    assert body["points"] == [{
        "point": "entry", "label": "🚪 Вход", "live": None, "count": 0, "capacity": None,
    }]


def test_points_unbound_manager_resolves_with_city_query(tmp_path, monkeypatch):
    client = client_with(tmp_path)
    _grant_checkin_to_game_manager()
    _run(bot_db.set_setting("event_city_enabled", "on"))
    _freeze_now(monkeypatch, datetime(2026, 10, 3, 10, 30))
    sid = _run(bot_db.create_program_session("spb", "2026-10-03", "10:00", "11:00", "Открытие"))
    resp = client.get(f"{BASE}/points", params={"city": "spb"}, headers=_hdr(GAME_MANAGER_ID))
    body = resp.json()
    assert body["city"] == "spb"
    assert body["cities"] is None
    assert [p["point"] for p in body["points"]] == ["entry", f"session:{sid}"]


def test_points_module_off_uses_default_city_without_picker(tmp_path, monkeypatch):
    client = client_with(tmp_path)
    _grant_checkin_to_game_manager()
    default_code = cities_mod.default_city_code()
    _freeze_now(monkeypatch, datetime(2026, 10, 3, 10, 30))
    sid = _run(bot_db.create_program_session(default_code, "2026-10-03", "10:00", "11:00", "Открытие"))
    resp = client.get(f"{BASE}/points", headers=_hdr(GAME_MANAGER_ID))
    body = resp.json()
    assert body["city"] == default_code
    assert body["cities"] is None
    assert [p["point"] for p in body["points"]] == ["entry", f"session:{sid}"]


def test_points_carries_count_and_capacity(tmp_path, monkeypatch):
    client = client_with(tmp_path)
    _grant_checkin_to_game_manager()
    default_code = cities_mod.default_city_code()
    _freeze_now(monkeypatch, datetime(2026, 10, 3, 10, 30))
    hall_id = _run(bot_db.create_program_hall(default_code, "Большой зал", capacity=120))
    sid = _run(bot_db.create_program_session(
        default_code, "2026-10-03", "10:00", "11:00", "Открытие", hall_id=hall_id,
    ))
    _run(_insert_user(950030, city=default_code))
    _run(bot_db.record_session_checkin(950030, sid, [], source="miniapp"))
    resp = client.get(f"{BASE}/points", headers=_hdr(GAME_MANAGER_ID))
    session_point = next(p for p in resp.json()["points"] if p["point"] == f"session:{sid}")
    assert session_point == {
        "point": f"session:{sid}", "label": "10:00–11:00 · Большой зал · Открытие",
        "live": True, "capacity": 120, "count": 1,
    }


# ── /scan, /manual на точках сессий (D-18..D-20) ─────────────────────────────────────────────

def test_scan_session_point_wrong_city_is_denied(tmp_path):
    client = client_with(tmp_path)
    _grant_checkin_to_game_manager()
    uid = 950040
    _run(_insert_user(uid, city="spb"))
    sid = _run(bot_db.create_program_session("msk", "2026-10-03", "10:00", "11:00", "Открытие"))
    payload = _qr(uid)
    resp = client.post(
        f"{BASE}/scan", json={"payload": payload, "point": f"session:{sid}"}, headers=_hdr(GAME_MANAGER_ID),
    )
    body = resp.json()
    assert body["status"] == "wrong_city"
    assert body["reason_text"]
    assert _run(bot_db.count_checkins_by_point(f"session:{sid}")) == 0
    assert _run(bot_db.count_checkins_by_point("entry")) == 0


def test_scan_session_point_new_auto_marks_entry(tmp_path, monkeypatch):
    client = client_with(tmp_path)
    _grant_checkin_to_game_manager()
    uid = 950041
    _run(_insert_user(uid, full_name="Сидоров Сидор", city="msk"))
    sid = _run(bot_db.create_program_session("msk", "2026-10-03", "10:00", "11:00", "Открытие"))
    # ревью (D-18): /scan никогда не передаёт scanned_at -- эффективный день сессии сверяется с
    # РЕАЛЬНЫМ "сегодня" (services.checkin.record_arrival), сессия обязана идти сегодня.
    _freeze_now(monkeypatch, datetime(2026, 10, 3, 10, 5))
    payload = _qr(uid, city="Москва")
    resp = client.post(
        f"{BASE}/scan", json={"payload": payload, "point": f"session:{sid}"}, headers=_hdr(GAME_MANAGER_ID),
    )
    body = resp.json()
    assert body["status"] == "new"
    assert body["full_name"] == "Сидоров Сидор"
    assert _run(bot_db.count_checkins_by_point(f"session:{sid}")) == 1
    assert _run(bot_db.count_checkins_by_point("entry")) == 1


def test_manual_session_point_moved_between_parallel_sessions(tmp_path, monkeypatch):
    client = client_with(tmp_path)
    _grant_checkin_to_game_manager()
    uid = 950042
    _run(_insert_user(uid, city="msk"))
    sid1 = _run(bot_db.create_program_session("msk", "2026-10-03", "10:00", "11:00", "Зал А"))
    sid2 = _run(bot_db.create_program_session("msk", "2026-10-03", "10:30", "11:30", "Зал Б"))
    # ревью (D-18): /manual тоже никогда не передаёт scanned_at -- см. комментарий в
    # test_scan_session_point_new_auto_marks_entry.
    _freeze_now(monkeypatch, datetime(2026, 10, 3, 10, 5))
    r1 = client.post(f"{BASE}/manual", json={"telegram_id": uid, "point": f"session:{sid1}"}, headers=_hdr(GAME_MANAGER_ID))
    assert r1.json()["status"] == "new"
    r2 = client.post(f"{BASE}/manual", json={"telegram_id": uid, "point": f"session:{sid2}"}, headers=_hdr(GAME_MANAGER_ID))
    body2 = r2.json()
    assert body2["status"] == "moved"
    assert body2["previous_title"] == "Зал А"
    assert _run(bot_db.count_checkins_by_point(f"session:{sid1}")) == 0
    assert _run(bot_db.count_checkins_by_point(f"session:{sid2}")) == 1


def test_scan_session_point_wrong_real_day_is_denied(tmp_path, monkeypatch):
    """Ревью (D-18): сканер со вчерашним/устаревшим списком точек не должен отмечать делегата
    на сессии, которая физически идёт в ДРУГОЙ день -- отказ словами, ничего не отмечается."""
    client = client_with(tmp_path)
    _grant_checkin_to_game_manager()
    uid = 950043
    _run(_insert_user(uid, full_name="Кузнецов Кузьма", city="msk"))
    sid = _run(bot_db.create_program_session("msk", "2026-10-04", "10:00", "11:00", "Открытие"))
    _freeze_now(monkeypatch, datetime(2026, 10, 3, 10, 5))  # "сегодня" -- НЕ день сессии
    payload = _qr(uid, city="Москва")
    resp = client.post(
        f"{BASE}/scan", json={"payload": payload, "point": f"session:{sid}"}, headers=_hdr(GAME_MANAGER_ID),
    )
    body = resp.json()
    assert body["status"] == "wrong_day"
    assert "04.10" in body["reason_text"]
    assert _run(bot_db.count_checkins_by_point(f"session:{sid}")) == 0
    assert _run(bot_db.count_checkins_by_point("entry")) == 0


# ── ревью (D-15/D-18): волонтёр с привязкой к городу не отмечает на сессии ДРУГОГО города ────

def test_scan_session_point_other_city_denied_for_bound_manager(tmp_path):
    client = client_with(tmp_path)
    _grant_checkin_to_bound_manager()  # BOUND_MANAGER_ID привязан к spb
    _run(bot_db.set_setting("event_city_enabled", "on"))
    uid = 950050
    _run(_insert_user(uid, full_name="Орлов Олег", city="msk"))
    sid = _run(bot_db.create_program_session("msk", "2026-10-03", "10:00", "11:00", "Открытие"))
    payload = _qr(uid, city="Москва")
    resp = client.post(
        f"{BASE}/scan", json={"payload": payload, "point": f"session:{sid}"}, headers=_hdr(BOUND_MANAGER_ID),
    )
    body = resp.json()
    assert body["status"] == "wrong_city_point"
    assert body["reason_text"] == "Сессия другого города — выберите точку заново"
    assert _run(bot_db.count_checkins_by_point(f"session:{sid}")) == 0
    assert _run(bot_db.count_checkins_by_point("entry")) == 0


def test_manual_session_point_other_city_denied_for_bound_manager(tmp_path):
    client = client_with(tmp_path)
    _grant_checkin_to_bound_manager()
    _run(bot_db.set_setting("event_city_enabled", "on"))
    uid = 950051
    _run(_insert_user(uid, city="msk"))
    sid = _run(bot_db.create_program_session("msk", "2026-10-03", "10:00", "11:00", "Открытие"))
    resp = client.post(
        f"{BASE}/manual", json={"telegram_id": uid, "point": f"session:{sid}"}, headers=_hdr(BOUND_MANAGER_ID),
    )
    body = resp.json()
    assert body["status"] == "wrong_city_point"
    assert body["reason_text"] == "Сессия другого города — выберите точку заново"
    assert _run(bot_db.count_checkins_by_point(f"session:{sid}")) == 0


def test_scan_session_point_own_city_allowed_for_bound_manager(tmp_path, monkeypatch):
    client = client_with(tmp_path)
    _grant_checkin_to_bound_manager()
    _run(bot_db.set_setting("event_city_enabled", "on"))
    uid = 950052
    _run(_insert_user(uid, full_name="Волкова Вера", city="spb"))
    sid = _run(bot_db.create_program_session("spb", "2026-10-03", "10:00", "11:00", "Открытие"))
    _freeze_now(monkeypatch, datetime(2026, 10, 3, 10, 5))
    payload = _qr(uid, city="СПб")
    resp = client.post(
        f"{BASE}/scan", json={"payload": payload, "point": f"session:{sid}"}, headers=_hdr(BOUND_MANAGER_ID),
    )
    body = resp.json()
    assert body["status"] == "new"
    assert _run(bot_db.count_checkins_by_point(f"session:{sid}")) == 1


def test_scan_entry_point_other_city_denied_for_bound_manager(tmp_path):
    """D-26 (24.09, уточняет D-15): волонтёр, привязанный к городу, работает только со своим
    городом И НА ВХОДЕ — делегат другого города получает отказ словами, отметка не ставится."""
    client = client_with(tmp_path)
    _grant_checkin_to_bound_manager()  # привязан к spb
    _run(bot_db.set_setting("event_city_enabled", "on"))
    uid = 950053
    _run(_insert_user(uid, full_name="Морозов Марк", city="msk"))  # ДРУГОЙ город, не spb
    payload = _qr(uid, city="Москва")
    resp = client.post(f"{BASE}/scan", json={"payload": payload}, headers=_hdr(BOUND_MANAGER_ID))
    body = resp.json()
    assert body["status"] == "wrong_city"
    assert body["reason_text"]
    assert body["full_name"] == "Морозов Марк"
    assert _run(bot_db.count_checkins_by_point(ENTRY_POINT)) == 0


def test_manual_entry_point_other_city_denied_for_bound_manager(tmp_path):
    """D-26: то же самое для ручной отметки (поиск по фамилии без QR)."""
    client = client_with(tmp_path)
    _grant_checkin_to_bound_manager()  # привязан к spb
    _run(bot_db.set_setting("event_city_enabled", "on"))
    uid = 950058
    _run(_insert_user(uid, full_name="Волков Всеволод", city="msk"))
    resp = client.post(f"{BASE}/manual", json={"telegram_id": uid}, headers=_hdr(BOUND_MANAGER_ID))
    body = resp.json()
    assert body["status"] == "wrong_city"
    assert body["reason_text"]
    assert _run(bot_db.count_checkins_by_point(ENTRY_POINT)) == 0


def test_scan_entry_point_own_city_allowed_for_bound_manager(tmp_path):
    """D-26: делегат СВОЕГО города волонтёра отмечается на входе как обычно."""
    client = client_with(tmp_path)
    _grant_checkin_to_bound_manager()  # привязан к spb
    _run(bot_db.set_setting("event_city_enabled", "on"))
    uid = 950059
    _run(_insert_user(uid, full_name="Соколова Софья", city="spb"))
    payload = _qr(uid, city="СПб")
    resp = client.post(f"{BASE}/scan", json={"payload": payload}, headers=_hdr(BOUND_MANAGER_ID))
    body = resp.json()
    assert body["status"] == "new"
    assert _run(bot_db.count_checkins_by_point(ENTRY_POINT)) == 1


def test_scan_entry_point_unbound_manager_not_scoped(tmp_path):
    """Волонтёр БЕЗ привязки к городу (`GAME_MANAGER_ID`) на входе не ограничен — та же
    трёхветочная логика, что и у точек-сессий."""
    client = client_with(tmp_path)
    _grant_checkin_to_game_manager()
    _run(bot_db.set_setting("event_city_enabled", "on"))
    uid = 950054
    _run(_insert_user(uid, full_name="Морозов Марк", city="msk"))
    payload = _qr(uid, city="Москва")
    resp = client.post(f"{BASE}/scan", json={"payload": payload}, headers=_hdr(GAME_MANAGER_ID))
    body = resp.json()
    assert body["status"] == "new"
    assert _run(bot_db.count_checkins_by_point(ENTRY_POINT)) == 1


def test_scan_session_point_unbound_manager_not_scoped(tmp_path, monkeypatch):
    """Волонтёр БЕЗ привязки к городу (`GAME_MANAGER_ID`) не ограничен точкой-сессией любого
    города — та же трёхветочная логика, что `_bound_city` использует везде в модуле."""
    client = client_with(tmp_path)
    _grant_checkin_to_game_manager()
    _run(bot_db.set_setting("event_city_enabled", "on"))
    uid = 950054
    _run(_insert_user(uid, full_name="Смирнов Семён", city="msk"))
    sid = _run(bot_db.create_program_session("msk", "2026-10-03", "10:00", "11:00", "Открытие"))
    _freeze_now(monkeypatch, datetime(2026, 10, 3, 10, 5))
    payload = _qr(uid, city="Москва")
    resp = client.post(
        f"{BASE}/scan", json={"payload": payload, "point": f"session:{sid}"}, headers=_hdr(GAME_MANAGER_ID),
    )
    assert resp.json()["status"] == "new"
