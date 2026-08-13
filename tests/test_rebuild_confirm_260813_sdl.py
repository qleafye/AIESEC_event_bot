"""Quick task 260813-sdl: «♻️ Пересобрать таблицу» requires an explicit confirmation.

rebuild_main_sheet() clears the whole main tab and rewrites every row from the bot DB, so any
manual note a manager typed straight into the sheet is gone. Until now the button ran that on a
single tap — while the neighbouring destructive row («🧹 Убрать дубли») had a confirm screen all
along. The gate mirrors the dedupe pattern: `admin_rebuild_sheet` only renders the warning,
`admin_rebuild_sheet_go` does the work.

Conventions follow tests/test_sheets_main_tab_pin_260813.py (plain def test_*, asyncio.run(go()),
monkeypatch, no real Google API).
"""
import asyncio

from handlers import admin as admin_mod
from handlers import admin_caps


class _FakeUser:
    def __init__(self, uid):
        self.id = uid


class _FakeMessage:
    def __init__(self):
        self.text = None
        self.markup = None

    async def edit_text(self, text, parse_mode=None, reply_markup=None):
        self.text = text
        self.markup = reply_markup


class _FakeCallback:
    def __init__(self, user_id):
        self.from_user = _FakeUser(user_id)
        self.message = _FakeMessage()
        self.answers = []

    async def answer(self, text=None, show_alert=False):
        self.answers.append((text, show_alert))


ADMIN_ID = 900813


def _callbacks(markup):
    return [b.callback_data for row in markup.inline_keyboard for b in row]


def test_rebuild_confirm_does_not_touch_the_sheet(monkeypatch):
    """The tap that used to wipe the tab must now only draw a screen."""
    called = []

    async def boom(headers, rows):
        called.append((headers, rows))
        return 0

    monkeypatch.setattr(admin_mod, "rebuild_main_sheet", boom)

    callback = _FakeCallback(ADMIN_ID)

    async def go():
        await admin_mod.rebuild_sheet_confirm(callback)

    asyncio.run(go())

    assert called == []  # nothing cleared, nothing written
    assert "admin_rebuild_sheet_go" in _callbacks(callback.message.markup)
    assert "admin_menu" in _callbacks(callback.message.markup)  # escape hatch


def test_rebuild_confirm_warns_about_losing_manual_edits(monkeypatch):
    """A confirm screen that doesn't name the actual damage is decoration, not a gate."""
    callback = _FakeCallback(ADMIN_ID)

    async def go():
        await admin_mod.rebuild_sheet_confirm(callback)

    asyncio.run(go())

    text = callback.message.text.lower()
    assert "⚠️" in callback.message.text
    assert "ручные правки" in text
    assert "пропадут" in text


def test_rebuild_go_runs_the_rebuild(monkeypatch):
    """The _go callback keeps the original behaviour — the gate must not disarm the button."""
    called = []

    async def fake_headers():
        return ["A"]

    async def fake_users():
        return []

    async def fake_rebuild(headers, rows):
        called.append((headers, rows))
        return 0

    monkeypatch.setattr(admin_mod, "active_sheet_headers", fake_headers)
    monkeypatch.setattr(admin_mod, "get_all_users_dicts", fake_users)
    monkeypatch.setattr(admin_mod, "rebuild_main_sheet", fake_rebuild)

    callback = _FakeCallback(ADMIN_ID)

    async def go():
        await admin_mod.rebuild_sheet(callback)

    asyncio.run(go())

    assert len(called) == 1
    assert "пересобрана" in callback.message.text.lower()


def test_rebuild_go_is_capability_mapped():
    """An unmapped callback would be rejected by the capability gate — the confirm screen would
    render a button that does nothing. Same capability as the row that opens it."""
    caps = admin_caps.ADMIN_CAPS
    assert caps["admin_rebuild_sheet_go"] == caps["admin_rebuild_sheet"]
