"""«✅ Одобрить» уже отклонённого с карточки /find: кнопка, город, флип, письмо, оплата."""
from __future__ import annotations

import asyncio

import pytest

import domain.cities as cities
from config import config
from database import db
from handlers.access.admin_caps import ADMIN_CAPS, required_capability, role_caps_key
from tests._dbtpl import fast_init_db

ADMIN_ID = 261106201
BOUND_MSK_ID = 261106202
DELEGATE_ID = 261106301

_CITIES = [
    {"code": "msk", "label": "Москва", "tab_base": "", "enabled": 1, "sort_order": 0},
    {"code": "spb", "label": "Санкт-Петербург", "tab_base": "СПб", "enabled": 1, "sort_order": 1},
]


def _run(coro):
    return asyncio.run(coro)


@pytest.fixture(autouse=True)
def _city_registry():
    saved = cities.all_cities()
    cities.set_cities_for_test([dict(c) for c in _CITIES])
    yield
    cities.set_cities_for_test(saved)


def _db_ready(tmp_path):
    config.DB_PATH = str(tmp_path / "test_approve_rejected.db")
    fast_init_db()
    config.ADMIN_IDS = [ADMIN_ID]
    config.GOOGLE_SHEET_ID = ""
    config.GOOGLE_CREDENTIALS_FILE = ""


async def _seed(status="rejected", city="msk"):
    await db.add_user({
        "telegram_id": DELEGATE_ID, "full_name": "Анна Отклонена", "username": "anna",
        "registration_date": "2026-01-01", "event_city": city,
    })
    async with db._connect() as conn:
        await conn.execute(
            "UPDATE users SET status = ? WHERE telegram_id = ?", (status, DELEGATE_ID),
        )
        await conn.commit()


class _User:
    def __init__(self, uid):
        self.id = uid


class _Msg:
    def __init__(self):
        self.edits = []

    async def edit_text(self, text, parse_mode=None, reply_markup=None):
        self.edits.append((text, reply_markup))


class _Cb:
    def __init__(self, data, uid, bot=None):
        self.data = data
        self.from_user = _User(uid)
        self.message = _Msg()
        self.bot = bot if bot is not None else object()
        self.answers = []

    async def answer(self, text=None, show_alert=False):
        self.answers.append((text, show_alert))


def _cbs(kb):
    return [b.callback_data for row in kb.inline_keyboard for b in row]


def _find_card(*, tid=DELEGATE_ID):
    import handlers.admin as admin_mod

    class _M:
        def __init__(self):
            self.text = f"/find {tid}"
            self.from_user = _User(ADMIN_ID)
            self.captured = {}

        async def answer(self, body, parse_mode=None, reply_markup=None):
            self.captured = {"text": body, "kb": reply_markup}

    msg = _M()
    _run(admin_mod.cmd_find_user(msg))
    return msg.captured


def test_atomic_flips_rejected_only(tmp_path):
    _db_ready(tmp_path)
    _run(_seed(status="rejected"))
    assert _run(db.approve_rejected_atomic(DELEGATE_ID)) is True
    user = _run(db.get_user(DELEGATE_ID))
    assert user["status"] == "approved"
    assert user["approved_at"]
    assert _run(db.approve_rejected_atomic(DELEGATE_ID)) is False


def test_atomic_skips_pending_and_approved(tmp_path):
    _db_ready(tmp_path)
    _run(_seed(status="pending"))
    assert _run(db.approve_rejected_atomic(DELEGATE_ID)) is False
    assert _run(db.get_user(DELEGATE_ID))["status"] == "pending"

    _run(db.set_user_status(DELEGATE_ID, "approved"))
    assert _run(db.approve_rejected_atomic(DELEGATE_ID)) is False
    assert _run(db.get_user(DELEGATE_ID))["status"] == "approved"


def test_approve_user_atomic_still_skips_rejected(tmp_path):
    _db_ready(tmp_path)
    _run(_seed(status="rejected"))
    assert _run(db.approve_user_atomic(DELEGATE_ID)) is False
    assert _run(db.get_user(DELEGATE_ID))["status"] == "rejected"


def test_card_shows_button_only_for_rejected(tmp_path):
    _db_ready(tmp_path)
    _run(_seed(status="rejected"))
    card = _find_card()
    assert f"apprej_start:{DELEGATE_ID}" in _cbs(card["kb"])

    _run(db.set_user_status(DELEGATE_ID, "pending"))
    card = _find_card()
    assert f"apprej_start:{DELEGATE_ID}" not in _cbs(card["kb"])

    _run(db.set_user_status(DELEGATE_ID, "approved"))
    card = _find_card()
    assert f"apprej_start:{DELEGATE_ID}" not in _cbs(card["kb"])


def test_start_confirm_without_payment_step(tmp_path):
    from handlers.applications import admin_approve_rejected as h

    _db_ready(tmp_path)

    async def scenario():
        await _seed()
        await db.set_setting("payment_enabled", "off")
        cb = _Cb(f"apprej_start:{DELEGATE_ID}", ADMIN_ID)
        await h.apprej_start(cb)
        return cb

    cb = _run(scenario())
    text, kb = cb.message.edits[0]
    assert "Одобрить отклонённую" in text
    assert "оплата выключена" in text.lower()
    assert _cbs(kb) == [f"apprej_apply:{DELEGATE_ID}", f"apprej_cancel:{DELEGATE_ID}"]


def test_start_confirm_mentions_payment_when_on(tmp_path):
    from handlers.applications import admin_approve_rejected as h

    _db_ready(tmp_path)

    async def scenario():
        await _seed()
        await db.set_setting("payment_enabled", "on")
        cb = _Cb(f"apprej_start:{DELEGATE_ID}", ADMIN_ID)
        await h.apprej_start(cb)
        return cb

    cb = _run(scenario())
    text, _ = cb.message.edits[0]
    assert "шаг оплаты" in text.lower()
    assert "выключена" not in text.lower()


def test_start_denied_for_other_city_manager(tmp_path):
    from handlers.applications import admin_approve_rejected as h

    _db_ready(tmp_path)

    async def scenario():
        await db.set_setting("event_city_enabled", "on")
        await db.set_setting(role_caps_key("reg_manager"), "moderate_reg")
        await db.add_staff(BOUND_MSK_ID, "reg_manager", ADMIN_ID)
        await db.set_staff_city(BOUND_MSK_ID, "msk")
        await _seed(city="spb")
        cb = _Cb(f"apprej_start:{DELEGATE_ID}", BOUND_MSK_ID)
        await h.apprej_start(cb)
        return cb

    cb = _run(scenario())
    assert cb.message.edits == []
    assert cb.answers[0][1] is True
    assert _run(db.get_user(DELEGATE_ID))["status"] == "rejected"


def test_apply_flips_and_sends_same_letter(tmp_path, monkeypatch):
    from handlers.applications import admin_approve_rejected as h

    _db_ready(tmp_path)
    effects = []

    async def _effects(bot, tid, status, reason=None, **kwargs):
        effects.append((tid, status, kwargs))

    monkeypatch.setattr(
        "services.applications.application_effects.apply_decision_effects", _effects,
    )

    async def scenario():
        await _seed()
        await db.set_setting("payment_enabled", "off")
        cb = _Cb(f"apprej_apply:{DELEGATE_ID}", ADMIN_ID)
        await h.apprej_apply(cb)
        user = await db.get_user(DELEGATE_ID)
        actions = await db.list_admin_actions(DELEGATE_ID)
        return cb, user, actions

    cb, user, actions = _run(scenario())
    assert user["status"] == "approved"
    assert "одобрен" in cb.message.edits[0][0].lower()
    assert "оплата выключена" in cb.message.edits[0][0].lower()
    assert effects == [(DELEGATE_ID, "approved", {})]
    assert actions[0]["action"] == "approve_rejected"
    assert actions[0]["actor_id"] == ADMIN_ID
    assert actions[0]["before"] == {"status": "rejected"}
    assert actions[0]["after"] == {"status": "approved"}


def test_apply_on_pending_refuses(tmp_path, monkeypatch):
    from handlers.applications import admin_approve_rejected as h

    _db_ready(tmp_path)
    effects = []

    async def _effects(*args, **kwargs):
        effects.append(1)

    monkeypatch.setattr(
        "services.applications.application_effects.apply_decision_effects", _effects,
    )

    async def scenario():
        await _seed(status="pending")
        cb = _Cb(f"apprej_apply:{DELEGATE_ID}", ADMIN_ID)
        await h.apprej_apply(cb)
        return cb, await db.get_user(DELEGATE_ID)

    cb, user = _run(scenario())
    assert user["status"] == "pending"
    assert "Не одобрил" in cb.message.edits[0][0]
    assert effects == []


def test_cancel_changes_nothing(tmp_path):
    from handlers.applications import admin_approve_rejected as h

    _db_ready(tmp_path)

    async def scenario():
        await _seed()
        cb = _Cb(f"apprej_cancel:{DELEGATE_ID}", ADMIN_ID)
        await h.apprej_cancel(cb)
        return cb, await db.get_user(DELEGATE_ID)

    cb, user = _run(scenario())
    assert "отменено" in cb.message.edits[0][0]
    assert user["status"] == "rejected"


def test_service_preview_and_apply(tmp_path):
    from services.applications.approve_rejected import approve_rejected, preview_approve_rejected

    _db_ready(tmp_path)

    async def scenario():
        await _seed()
        await db.set_setting("payment_enabled", "off")
        preview = await preview_approve_rejected(DELEGATE_ID)
        report = await approve_rejected(DELEGATE_ID, by_admin=ADMIN_ID)
        second = await approve_rejected(DELEGATE_ID, by_admin=ADMIN_ID)
        return preview, report, second, await db.get_user(DELEGATE_ID)

    preview, report, second, user = _run(scenario())
    assert preview["ok"] is True
    assert preview["payment_enabled"] is False
    assert report["ok"] is True
    assert second["ok"] is False
    assert user["status"] == "approved"


@pytest.mark.parametrize("name", ["apprej_start", "apprej_apply", "apprej_cancel"])
def test_callbacks_require_moderate_reg(name):
    assert required_capability(callback_data=f"{name}:{DELEGATE_ID}") == "moderate_reg"
    assert f"{name}:*" in ADMIN_CAPS


def test_parse_tid_rejects_negative():
    from handlers.applications.admin_approve_rejected import _parse_tid

    assert _parse_tid("-1") is None
    assert _parse_tid(str(DELEGATE_ID)) == DELEGATE_ID
