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


# ── Хаб: «🎟 Вход по QR» — через подтверждение, после — снова хаб ─────────────────────────

class _EditMsg:
    def __init__(self, markup=None):
        self.sent, self.edits = [], []
        self.reply_markup = markup

    async def answer(self, text=None, parse_mode=None, reply_markup=None, **k):
        self.sent.append((text, reply_markup))

    async def edit_text(self, text=None, parse_mode=None, reply_markup=None, **k):
        self.edits.append((text, reply_markup))
        self.reply_markup = reply_markup


class _CB(_FakeCallback):
    def __init__(self, data, uid=ADMIN_ID, markup=None):
        super().__init__(data, uid)
        self.message = _EditMsg(markup)


def _hub_cbs(code="spb"):
    from handlers import admin_forum_functions as aff
    _text, kb = asyncio.run(aff._render_hub(ADMIN_ID, code))
    return _kb_callbacks(kb)


def test_hub_qr_button_opens_confirm_not_toggle(tmp_path):
    _seed_spb(tmp_path)
    cbs = _hub_cbs()
    assert "toggle_checkin_qr_enabled" not in cbs
    assert "forumfn_qr:spb" in cbs


def test_qr_confirm_names_all_cities_and_does_not_toggle(tmp_path):
    from handlers import admin_forum_hub_nav as nav
    _seed_spb(tmp_path)
    cb = _CB("forumfn_qr:spb")
    asyncio.run(nav.forumfn_qr_screen(cb))
    text, kb = cb.message.edits[-1]
    assert "ВСЕХ городах" in text and "Санкт-Петербург" in text and "Москва" in text
    assert "«🎟 Мой QR»" in text
    assert asyncio.run(db.get_setting("checkin_qr_enabled")) == "on"  # просмотр ничего не меняет
    assert "forumfn_qr_set:off:spb" in _kb_callbacks(kb)
    assert "forumfn_back:spb" in _kb_callbacks(kb)


def test_qr_set_off_returns_to_hub_and_is_idempotent(tmp_path):
    from handlers import admin_forum_hub_nav as nav
    _seed_spb(tmp_path)
    for _ in range(2):  # повторный тап по той же кнопке не включает обратно
        cb = _CB("forumfn_qr_set:off:spb")
        asyncio.run(nav.forumfn_qr_set(cb))
        assert asyncio.run(db.get_setting("checkin_qr_enabled")) == "off"
    text, _kb = cb.message.edits[-1]
    assert text.startswith("🎪 <b>Форум: функции</b>")
    assert "🎟 QR для входа: ❌ Выкл" in text
