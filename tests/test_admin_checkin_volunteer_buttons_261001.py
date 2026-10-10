"""Экран «✅ Отметки на форуме» в боте: волонтёр с одним правом `checkin` не видит кнопок,
которые ответят ему «Недостаточно прав» (рассылка QR, «Написать не пришедшим», сводка прихода,
журнал площадки). Менеджер видит их как раньше."""
from __future__ import annotations

import asyncio

from database import db
from handlers import admin_checkin
from tests.test_admin_checkin_260924 import (
    ADMIN_ID,
    BOUND_ID,
    _db_ready,
    _FakeCallback,
    _set_season,
    _setup_bound_manager,
)

_MANAGER_ONLY = ("checkinqr_send:", "checkinqr_cfg:", "cna_send:", "checkin_stats", "checkin_floor", "admin_venue_log")


def _callbacks(uid: int) -> list[str]:
    cb = _FakeCallback("admin_checkin", uid)
    asyncio.run(admin_checkin.show_admin_checkin(cb))
    kb = cb.message.sent[-1][1]
    return [b.callback_data for row in kb.inline_keyboard for b in row]


def _seed(tmp_path):
    _db_ready(tmp_path)
    asyncio.run(_set_season("YL'26"))
    asyncio.run(db.set_setting("checkin_qr_enabled", "on"))
    asyncio.run(db.set_setting("event_city_enabled", "on"))
    asyncio.run(_setup_bound_manager("spb"))
    from services.timeutil import msk_now  # «Написать не пришедшим» — только в день форума
    for code in ("spb", "msk"):
        asyncio.run(db.set_setting(f"forum_date__city__{code}", msk_now().strftime("%d.%m.%Y")))
    async def _user():
        async with db._connect() as conn:
            await conn.execute(
                "INSERT INTO users (telegram_id, full_name, status, season, event_city) VALUES (?, ?, ?, ?, ?)",
                (1, "Иванов Иван", "approved", "YL'26", "spb"),
            )
            await conn.commit()
    asyncio.run(_user())


def test_checkin_only_volunteer_sees_no_manager_buttons(tmp_path):
    _seed(tmp_path)
    cbs = _callbacks(BOUND_ID)
    assert "checkin_upload_start" in cbs
    assert not [c for c in cbs if c.startswith(_MANAGER_ONLY)], cbs


def test_manager_still_sees_qr_and_not_arrived_buttons(tmp_path):
    _seed(tmp_path)
    cbs = _callbacks(ADMIN_ID)
    assert any(c.startswith("checkinqr_send:") for c in cbs), cbs
    assert any(c.startswith("cna_send:") for c in cbs), cbs
    assert "checkin_stats" in cbs


def _screen_cbs(admin_id, city=None):
    text, kb = asyncio.run(admin_checkin.render_admin_checkin(admin_id, city))
    return text, [b.callback_data for row in kb.inline_keyboard for b in row]


def test_not_arrived_only_for_cities_with_forum_today(tmp_path):
    """«Не пришли (сегодня)» и «Написать не пришедшим» — только города, где сегодня форум:
    у Москвы с форумом через месяц кнопка всегда отвечала бы «некому»."""
    _seed(tmp_path)
    async def _msk():
        async with db._connect() as conn:
            await conn.execute(
                "INSERT INTO users (telegram_id, full_name, status, season, event_city) VALUES (?, ?, ?, ?, ?)",
                (2, "Петров Пётр", "approved", "YL'26", "msk"),
            )
            await conn.commit()
    asyncio.run(_msk())
    import domain.cities as cities
    asyncio.run(db.set_setting(f"{cities.ADMIN_CITY_KEY_PREFIX}{ADMIN_ID}", cities.ALL_CITIES))
    asyncio.run(db.set_setting("forum_date__city__msk", "30.10.2099"))
    _text, cbs = _screen_cbs(ADMIN_ID)
    cna = [c for c in cbs if c.startswith("cna_send:")]
    assert cna == [f"cna_send:{admin_checkin._encode_city('spb')}"]
    text, cbs = _screen_cbs(ADMIN_ID, "msk")
    assert not [c for c in cbs if c.startswith("cna_send:")]
    assert "нет форума" in text


def test_screen_from_city_hub_shows_only_that_city(tmp_path):
    """Из хаба города при шапке «Все города» — экран этого города, а не всех."""
    _seed(tmp_path)
    import domain.cities as cities
    asyncio.run(db.set_setting(f"{cities.ADMIN_CITY_KEY_PREFIX}{ADMIN_ID}", cities.ALL_CITIES))
    assert len([c for c in _screen_cbs(ADMIN_ID)[1] if c.startswith("checkinqr_send:")]) > 1
    _text, cbs = _screen_cbs(ADMIN_ID, "spb")
    sends = [c for c in cbs if c.startswith("checkinqr_send:")]
    assert sends == [f"checkinqr_send:{admin_checkin._encode_city('spb')}"]


def test_double_tap_on_point_imports_file_once(tmp_path):
    """Два параллельных колбэка одной точки: файл импортируется один раз, второй тап отвечает
    «уже отмечаю», а не вторым отчётом из одних «уже были»."""
    from tests.test_admin_checkin_260924 import _new_state
    _seed(tmp_path)
    state = _new_state(ADMIN_ID)
    asyncio.run(state.update_data(checkin_records=[{"qr": "мусор", "scanned_at": None}]))
    first = _FakeCallback("checkin_point:entry", ADMIN_ID)
    second = _FakeCallback("checkin_point:entry", ADMIN_ID)

    async def _both():
        await asyncio.gather(admin_checkin.checkin_point_pick(first, state),
                             admin_checkin.checkin_point_pick(second, state))
    asyncio.run(_both())
    reports = [m for cb in (first, second) for m, _kb in cb.message.sent if m and "Отмечено" in m]
    assert len(reports) == 1
    assert any("Уже отмечаю" in (a[0] or "") for a in second.answers + first.answers)
    assert not asyncio.run(state.get_data()).get("checkin_importing")
