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


def test_app_preview_names_person_and_does_not_flip(tmp_path):
    from services.applications.approve_rejected import NOT_REJECTED, approve_decision, rejected_screen

    _db_ready(tmp_path)

    async def scenario():
        await _seed()
        await db.set_setting("payment_enabled", "off")
        await db.add_user({
            "telegram_id": DELEGATE_ID + 1, "full_name": "Анна Ждёт", "username": "wait",
            "registration_date": "2026-01-01", "event_city": "msk",
        })
        screen = await rejected_screen(ADMIN_ID, "Анна")
        preview = await approve_decision(ADMIN_ID, DELEGATE_ID, confirm=False)
        user = await db.get_user(DELEGATE_ID)
        return screen, preview, user["status"]

    screen, preview, status = _run(scenario())
    assert [person["name"] for person in screen["people"]] == ["Анна Отклонена"]
    assert "Анна Ждёт" not in screen["text"]
    assert str(DELEGATE_ID) not in screen["text"]
    assert "msk" not in screen["text"]
    assert preview["need_confirm"] is True
    assert "enqueue" not in preview
    assert "Анна Отклонена" in preview["text"]
    assert "Отказ будет снят" in preview["text"]
    assert "оплата выключена" in preview["text"].lower()
    assert status == "rejected"
    assert NOT_REJECTED.endswith(".")


def test_app_confirm_flips_one_and_queues_the_same_letter(tmp_path, monkeypatch):
    from services.applications.approve_rejected import approve_decision
    from services.infra.miniapp_outbox import _handle_row

    _db_ready(tmp_path)
    sent = []

    async def _effects(bot, telegram_id, status, reason=None, **kwargs):
        sent.append((telegram_id, status, reason))

    monkeypatch.setattr(
        "services.infra.miniapp_outbox.apply_decision_effects", _effects,
    )

    async def scenario():
        await _seed()
        await db.set_setting("payment_enabled", "off")
        ready = await approve_decision(ADMIN_ID, DELEGATE_ID, confirm=True)
        await _handle_row(object(), "application_decided", ready["enqueue"])
        second = await approve_decision(ADMIN_ID, DELEGATE_ID, confirm=True)
        user = await db.get_user(DELEGATE_ID)
        return ready, second, user["status"]

    ready, second, status = _run(scenario())
    assert ready["enqueue"] == {"telegram_id": DELEGATE_ID, "status": "approved"}
    assert "ids" not in ready["enqueue"]
    assert sent == [(DELEGATE_ID, "approved", None)]
    assert second["ok"] is False
    assert second["text"] == "Одобрить можно только отклонённую заявку."
    assert "enqueue" not in second
    assert status == "approved"


def test_app_other_city_and_pending_do_not_flip(tmp_path):
    from services.applications.approve_rejected import OTHER_CITY, approve_decision, rejected_screen

    _db_ready(tmp_path)

    async def scenario():
        await db.set_setting("event_city_enabled", "on")
        await db.set_setting(role_caps_key("reg_manager"), "moderate_reg")
        await db.add_staff(BOUND_MSK_ID, "reg_manager", ADMIN_ID)
        await db.set_staff_city(BOUND_MSK_ID, "msk")
        await _seed(city="spb")
        hidden = await rejected_screen(BOUND_MSK_ID, "Анна")
        refused = await approve_decision(BOUND_MSK_ID, DELEGATE_ID, confirm=True)
        still = await db.get_user(DELEGATE_ID)
        await db.set_user_status(DELEGATE_ID, "pending")
        await db.set_staff_city(BOUND_MSK_ID, "spb")
        pending = await approve_decision(BOUND_MSK_ID, DELEGATE_ID, confirm=True)
        after = await db.get_user(DELEGATE_ID)
        return hidden, refused, still["status"], pending, after["status"]

    hidden, refused, still, pending, after = _run(scenario())
    assert hidden["people"] == []
    assert refused["ok"] is False
    assert refused["text"] == OTHER_CITY
    assert "enqueue" not in refused
    assert "forbidden" not in refused["text"].lower()
    assert "spb" not in refused["text"]
    assert still == "rejected"
    assert pending["text"] == "Одобрить можно только отклонённую заявку."
    assert after == "pending"


def test_chat_approve_stays_and_site_only_links():
    from pathlib import Path

    from dashboard.forum_day import LIFT_REJECT_HREF, lift_reject_page
    from handlers.applications.admin_approve_rejected import apprej_apply, apprej_start
    from services.applications.approve_rejected import NEED_CAP

    assert apprej_start is not None
    assert apprej_apply is not None
    assert "forbidden" not in NEED_CAP.lower()
    assert "moderate_reg" not in NEED_CAP
    page = lift_reject_page()
    assert LIFT_REJECT_HREF == "/app/#/lift-reject"
    assert LIFT_REJECT_HREF in page
    source = Path("dashboard/forum_day.py").read_text(encoding="utf-8")
    assert "SELECT" not in source
    assert "approve_rejected" not in source
    handler = Path("handlers/applications/admin_approve_rejected.py").read_text(encoding="utf-8")
    assert 'F.data.startswith("apprej_start:")' in handler
    assert 'F.data.startswith("apprej_apply:")' in handler
    screen = Path("miniapp/static/js/screens/lift_reject.js").read_text(encoding="utf-8")
    assert "innerHTML" not in screen
    assert "text: person.name + city" in screen
    router = Path("miniapp/routers/lift_reject.py").read_text(encoding="utf-8")
    assert "application_mass_approved" not in router
    assert "application_decided" in router
