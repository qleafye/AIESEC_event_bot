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
