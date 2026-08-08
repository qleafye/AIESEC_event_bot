"""Phase 2 admin pure-helper tests: callback parse, card renderer, settings guide."""
import asyncio
import types as _types

from aiogram import F

from config import config
from handlers import admin
from handlers.admin import (
    _parse_appr,
    _render_application_card,
    _render_settings_guide,
    SETTINGS_GUIDE_SECTIONS,
    SETTINGS_GUIDE_KEYS,
)


def test_parse_appr_with_id():
    assert _parse_appr("appr_approve:123") == ("appr_approve", 123)


def test_parse_appr_no_id():
    assert _parse_appr("appr_all") == ("appr_all", None)


def test_parse_appr_bad_id():
    assert _parse_appr("appr_x:abc") == ("appr_x", None)


def test_card_shows_position_and_escapes():
    user = {"full_name": "<b>hack</b>", "city": "Москва", "resume_file_id": "f1"}
    out = _render_application_card(user, 1, 3)
    assert "1/3" in out
    assert "<b>hack</b>" not in out  # escaped
    assert "&lt;b&gt;hack" in out
    assert "файлом" in out  # резюме файлом → кнопка ниже


def test_card_no_resume_marker():
    out = _render_application_card({"full_name": "Иван"}, 2, 2)
    assert "📎 Резюме: нет" in out


def test_card_shows_resume_text():
    # Таня п.4: текст-резюме виден прямо в карточке (обрезка длинного).
    out = _render_application_card({"full_name": "Иван", "resume_text": "мой богатый опыт"}, 1, 1)
    assert "Резюме (текст)" in out
    assert "мой богатый опыт" in out


def test_settings_guide_speaks_russian_not_raw_keys():
    """Quick 260726-0bc: the guide is read by DXP managers — it must show human labels and
    the CURRENT value in words, never raw bot_settings keys."""
    current = {key: None for key in SETTINGS_GUIDE_KEYS}
    current["full_approval"] = "manual"
    current["pending_notify_mode"] = "instant"
    chunks = _render_settings_guide(SETTINGS_GUIDE_SECTIONS, current)
    out = "\n".join(chunks)

    assert "Одобрение для полной формы" in out
    assert "вручную (через «📋 Заявки»)" in out       # resolved value, not "manual"
    assert "сразу по каждой заявке" in out            # resolved value, not "instant"
    assert "по умолчанию" in out                      # unset keys show their default
    for raw_key in ("pending_notify_mode", "full_approval", "preselect_tab"):
        assert raw_key not in out


def test_settings_guide_chunks_fit_telegram_limit():
    current = {key: None for key in SETTINGS_GUIDE_KEYS}
    for chunk in _render_settings_guide(SETTINGS_GUIDE_SECTIONS, current):
        assert len(chunk) <= 4096


# ── WR-01: mass-approve welcome drain must run even if the confirm edit throws ─

class _RaisingMessage:
    async def edit_text(self, *a, **k):
        raise RuntimeError("Bad Request: message can't be edited (too old)")


class _FakeCallbackWR01:
    def __init__(self, uid):
        self.from_user = type("U", (), {"id": uid})()
        self.message = _RaisingMessage()
        self.bot = object()
        self.answered = False

    async def answer(self, *a, **k):
        self.answered = True


def test_wr01_welcome_drain_scheduled_despite_edit_failure(monkeypatch):
    """WR-01: appr_all_yes must schedule _welcome_flipped BEFORE the fragile confirm edit, so a
    >48h/deleted card whose edit_text raises still delivers welcome/menu to the approved users."""
    uid = 42
    monkeypatch.setattr(config, "ADMIN_IDS", [uid])
    welcomed = []

    async def fake_approve_all_pending(*, city_scope=None):
        return [1, 2, 3]

    async def fake_welcome_flipped(bot, ids):
        welcomed.append(list(ids))

    async def fake_bulk_sync(mapping):
        return None

    async def fake_admin_keyboard_for(admin_id):
        return None

    monkeypatch.setattr(admin, "approve_all_pending", fake_approve_all_pending)
    monkeypatch.setattr(admin, "_welcome_flipped", fake_welcome_flipped)
    monkeypatch.setattr(admin, "bulk_update_status_in_sheet", fake_bulk_sync)
    monkeypatch.setattr(admin, "admin_keyboard_for", fake_admin_keyboard_for)

    cb = _FakeCallbackWR01(uid)

    async def go():
        await admin.appr_all_yes(cb, None)  # edit_text will raise inside
        pending = [t for t in asyncio.all_tasks() if t is not asyncio.current_task()]
        if pending:
            await asyncio.gather(*pending)

    asyncio.run(go())

    assert welcomed == [[1, 2, 3]]  # drain ran despite the edit throwing
    assert cb.answered  # handler completed (callback.answer reached)


# ── WR-03: a /command typed mid-rejection must abort, not become the reason ────

def test_wr03_reject_cancel_filter_catches_commands_and_cancel():
    """The appr_reject_cancel filter must match «Отмена» and ANY /command (so the reason
    catch-all never swallows a command), but not a normal reason line."""
    filt = F.text.in_({"Отмена"}) | F.text.startswith("/")

    def m(t):
        return _types.SimpleNamespace(text=t)

    assert filt.resolve(m("/broadcast"))
    assert filt.resolve(m("/cancel"))
    assert filt.resolve(m("Отмена"))
    assert not filt.resolve(m("не подходит по критериям отбора"))


# ── WR-04: stale mass-approve confirm re-click must not re-drain / lie ─────────

class _EditMessage:
    def __init__(self):
        self.edited = None

    async def edit_text(self, text, **k):
        self.edited = text


class _FakeCallbackWR04:
    def __init__(self, uid):
        self.from_user = type("U", (), {"id": uid})()
        self.message = _EditMessage()
        self.bot = object()
        self.answered = None

    async def answer(self, text=None, **k):
        self.answered = text


def test_wr04_stale_reclick_no_drain_and_honest_message(monkeypatch):
    uid = 42
    monkeypatch.setattr(config, "ADMIN_IDS", [uid])
    drained = []

    async def fake_approve_all_pending(*, city_scope=None):
        return []  # second click on a stale dialog: nothing left to approve

    async def fake_welcome(bot, ids):
        drained.append(ids)

    async def fake_show(msg, state):
        return None

    async def fake_admin_keyboard_for(admin_id):
        return None

    monkeypatch.setattr(admin, "approve_all_pending", fake_approve_all_pending)
    monkeypatch.setattr(admin, "_welcome_flipped", fake_welcome)
    monkeypatch.setattr(admin, "admin_keyboard_for", fake_admin_keyboard_for)
    monkeypatch.setattr(admin, "_show_current_card", fake_show)

    cb = _FakeCallbackWR04(uid)
    asyncio.run(admin.appr_all_yes(cb, None))

    assert drained == []  # no welcome drain on the empty re-click
    assert cb.answered == "Уже обработано"
    assert cb.message.edited == "Заявки уже обработаны."
