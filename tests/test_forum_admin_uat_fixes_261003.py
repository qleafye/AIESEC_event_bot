"""Живая приёмка админ-экранов форума 03.10: навигация и подтверждения хаба «🎪 Форум: функции»,
«✅ Отметки на форуме», «👥 Роли», рассылки QR / «Не пришли», мастер сессии."""
from __future__ import annotations

import asyncio

from database import db
from handlers import admin_checkin
from tests.test_admin_checkin_260924 import ADMIN_ID, _db_ready, _FakeCallback, _set_season


def _kb_callbacks(kb) -> list[str]:
    return [b.callback_data for row in kb.inline_keyboard for b in row]


def _seed_spb(tmp_path):
    _db_ready(tmp_path)
    asyncio.run(_set_season("YL'26"))
    asyncio.run(db.set_setting("checkin_qr_enabled", "on"))
    asyncio.run(db.set_setting("event_city_enabled", "on"))


# ── «✅ Отметки на форуме»: есть «Назад» ────────────────────────────────────────────────────

def test_checkin_screen_has_back_button(tmp_path):
    _seed_spb(tmp_path)
    cb = _FakeCallback("admin_checkin", ADMIN_ID)
    asyncio.run(admin_checkin.show_admin_checkin(cb))
    kb = cb.message.sent[-1][1]
    last = kb.inline_keyboard[-1][0]
    assert "Назад" in last.text
    assert last.callback_data.startswith("admin_sec:"), last.callback_data
