"""Живая приёмка админ-экранов форума 03.10: навигация и подтверждения хаба «🎪 Форум: функции»,
«✅ Отметки на форуме», «👥 Роли», рассылки QR / «Не пришли», мастер сессии."""
from __future__ import annotations

import asyncio

from database import db
from handlers import admin_checkin
from services.checkin_broadcast import qr_send_report as cbc_report
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


# ── Экраны, открытые из хаба: «Назад» ведёт в хаб и переживает перерисовку ─────────────────

class _State:
    async def clear(self):
        pass


def _open_from_hub(target, code="spb"):
    from handlers import admin_forum_hub_nav as nav
    cb = _CB(f"forumfn_open:{target}:{code}")
    asyncio.run(nav.forumfn_open(cb, _State()))
    return cb.message.edits[-1]


def test_hub_rows_open_native_screens_with_hub_back(tmp_path):
    _seed_spb(tmp_path)
    cbs = _hub_cbs()
    for target in ("chk", "app", "menu", "fb"):
        assert f"forumfn_open:{target}:spb" in cbs
        _text, kb = _open_from_hub(target)
        last = kb.inline_keyboard[-1][0]
        assert last.callback_data == "forumfn_back:spb", (target, last)
        assert "Форум: функции" in last.text


def test_hub_back_survives_toggle_redraw(tmp_path):
    from handlers import admin_miniapp, admin_reg_config, session_feedback
    _seed_spb(tmp_path)

    _t, kb = _open_from_hub("menu")
    cb = _CB("menu_toggle:menu_sos", markup=kb)
    asyncio.run(admin_reg_config.toggle_menu_button(cb))
    assert cb.message.edits[-1][1].inline_keyboard[-1][0].callback_data == "forumfn_back:spb"

    _t, kb = _open_from_hub("app")
    cb = _CB("miniapp_toggle_staff_only", markup=kb)
    asyncio.run(admin_miniapp.toggle_miniapp_staff_only(cb))
    assert cb.message.edits[-1][1].inline_keyboard[-1][0].callback_data == "forumfn_back:spb"

    _t, kb = _open_from_hub("fb")
    cb = _CB("prog_fbtoggle:spb", markup=kb)
    asyncio.run(session_feedback.prog_fbtoggle(cb))
    assert cb.message.edits[-1][1].inline_keyboard[-1][0].callback_data == "forumfn_back:spb"


def test_native_entry_keeps_section_back(tmp_path):
    from handlers import admin_reg_config
    _seed_spb(tmp_path)
    cb = _CB("menu_toggle:menu_sos")  # открыт из раздела — клавиатуры хаба не было
    asyncio.run(admin_reg_config.toggle_menu_button(cb))
    assert cb.message.edits[-1][1].inline_keyboard[-1][0].callback_data == "menu_back"


def test_hub_back_redraws_hub_in_place(tmp_path):
    from handlers import admin_forum_hub_nav as nav
    _seed_spb(tmp_path)
    cb = _CB("forumfn_back:spb")
    asyncio.run(nav.forumfn_back(cb))
    assert cb.message.sent == []
    assert cb.message.edits[-1][0].startswith("🎪 <b>Форум: функции</b> — Санкт-Петербург")


# ── Хаб и «Готовность» правят сообщение, где нажата кнопка ─────────────────────────────────

def test_hub_entry_and_ready_edit_in_place(tmp_path, monkeypatch):
    from handlers import admin_forum_functions as aff
    from handlers import admin_forum_ready as afr
    _seed_spb(tmp_path)
    cb = _CB("admin_forum_functions")
    asyncio.run(aff.admin_forum_functions_entry(cb))
    assert cb.message.sent == [] and "Форум: функции" in cb.message.edits[-1][0]

    async def _fake_render(admin_id, code, bot):
        from aiogram.types import InlineKeyboardMarkup
        return "🚦 Готовность", InlineKeyboardMarkup(inline_keyboard=[])
    monkeypatch.setattr(afr, "render_ready", _fake_render)
    cb = _CB("forum_ready:spb")
    asyncio.run(afr.forum_ready_open(cb))
    assert cb.message.sent == [] and cb.message.edits[-1][0] == "🚦 Готовность"


def test_edit_falls_back_to_new_message_for_photo(tmp_path):
    from aiogram.exceptions import TelegramBadRequest
    from aiogram.types import InlineKeyboardMarkup
    from handlers import admin_forum_hub_nav as nav

    class _PhotoMsg(_EditMsg):
        async def edit_text(self, *a, **k):
            raise TelegramBadRequest(method=None, message="there is no text in the message to edit")

    cb = _CB("x")
    cb.message = _PhotoMsg()
    asyncio.run(nav.edit_or_answer(cb, "t", InlineKeyboardMarkup(inline_keyboard=[])))
    assert cb.message.sent[-1][0] == "t"


# ── «📤 Разослать QR сейчас»: итог на месте «⏳», кому не дошло и почему ───────────────────

def test_qr_send_report_names_failures_by_reason():
    text = cbc_report({
        "sent": 3, "failed": 2, "total": 5,
        "failures": [
            {"user": {"telegram_id": 7, "full_name": "Пётр <Иванов>", "username": "@petr"}, "reason": "blocked"},
            {"user": {"telegram_id": 8, "full_name": "Анна", "username": None}, "reason": "other"},
        ],
    })
    assert "доставлено 3 из 5" in text
    assert "Не дошло 1: заблокировали бота" in text
    assert "• Пётр &lt;Иванов&gt; (@petr)" in text
    assert "Не дошло 1: сбой связи" in text and "• Анна" in text


def test_qr_send_go_replaces_progress_message(tmp_path, monkeypatch):
    _seed_spb(tmp_path)

    async def _no_block(code):
        return None

    async def _fake_send(code):
        return {"sent": 1, "failed": 1, "total": 2, "failures": [
            {"user": {"telegram_id": 9, "full_name": "Ольга", "username": "olga"}, "reason": "blocked"}]}
    monkeypatch.setattr(admin_checkin, "manual_send_block_reason", _no_block)
    monkeypatch.setattr(admin_checkin, "send_broadcast", _fake_send)
    cb = _CB("checkinqr_send_go:spb")
    asyncio.run(admin_checkin.checkinqr_send_go(cb))
    assert cb.message.sent == []
    assert cb.message.edits[0][0] == "⏳ Рассылаю QR..."
    assert "Ольга (@olga)" in cb.message.edits[-1][0]


def test_send_broadcast_reports_blocked_user(tmp_path, monkeypatch):
    from aiogram.exceptions import TelegramForbiddenError
    from services import checkin_broadcast as cbc
    _seed_spb(tmp_path)

    async def _users(city):
        return [{"telegram_id": 5, "full_name": "Блок", "event_city": "spb"}]

    async def _qr(user):
        return b"png", ""

    async def _render(*a, **k):
        return "cap", None

    class _Bot:
        async def send_photo(self, *a, **k):
            raise TelegramForbiddenError(method=None, message="bot was blocked by the user")

    async def _nosleep(*_a):
        return None

    monkeypatch.setattr(cbc, "eligible_recipients", _users)
    monkeypatch.setattr(cbc, "build_checkin_qr", _qr)
    monkeypatch.setattr(cbc, "_render_for", _render)
    monkeypatch.setattr(cbc._sched, "_bot", _Bot())
    monkeypatch.setattr(cbc.asyncio, "sleep", _nosleep)
    result = asyncio.run(cbc.send_broadcast("spb"))
    assert result["failed"] == 1
    assert result["failures"][0]["reason"] == "blocked"


# ── «Готовность» → «Таблица»: перевод города и точечная правка строки — тоже записи ─────────

def test_city_move_sheet_writes_mark_last_write(monkeypatch):
    from config import config
    from services import sheets
    monkeypatch.setattr(config, "GOOGLE_SHEET_ID", "x")
    monkeypatch.setattr(config, "GOOGLE_CREDENTIALS_FILE", "y")
    monkeypatch.setattr(sheets, "_write_state", {"ok": None, "fail": None})
    monkeypatch.setattr(sheets, "_append_to_existing_tab_sync", lambda tab, data: "ok")
    assert asyncio.run(sheets.append_to_existing_named_sheet("СПб", [1, "x"])) == "ok"
    assert sheets.last_write_state()["ok"] is not None

    monkeypatch.setattr(sheets, "_write_state", {"ok": None, "fail": None})
    monkeypatch.setattr(sheets, "_delete_row_by_id_sync", lambda tab, tid: "ok")
    assert asyncio.run(sheets.delete_row_by_id("Москва", 1)) == "ok"
    assert sheets.last_write_state()["ok"] is not None

    monkeypatch.setattr(sheets, "_write_state", {"ok": None, "fail": None})
    monkeypatch.setattr(sheets, "_update_row_by_id_sync", lambda *a: ("updated", []))
    assert asyncio.run(sheets.update_row_by_id("СПб", 1, [1])) is True
    assert sheets.last_write_state()["ok"] is not None
