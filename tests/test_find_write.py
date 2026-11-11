"""«✉️ Написать» на карточке /find: кнопка, город менеджера, копирование сообщения, блок бота."""
from __future__ import annotations

import asyncio

import pytest
from aiogram.exceptions import TelegramForbiddenError
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage

import domain.cities as cities
from config import config
from database import db
from handlers.access.admin_caps import ADMIN_CAPS, required_capability, role_caps_key
from handlers.states import FindWrite
from tests._dbtpl import fast_init_db

ADMIN_ID = 261106001
BOUND_MSK_ID = 261106002
DELEGATE_ID = 261106101
STARTED_ID = 261106102

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
    config.DB_PATH = str(tmp_path / "test_find_write.db")
    fast_init_db()
    config.ADMIN_IDS = [ADMIN_ID]


async def _seed(status="approved", city="msk"):
    await db.add_user({
        "telegram_id": DELEGATE_ID, "full_name": "Иван Иванов", "username": "ivan",
        "registration_date": "2026-01-01", "event_city": city,
    })
    async with db._connect() as conn:
        await conn.execute("UPDATE users SET status = ? WHERE telegram_id = ?", (status, DELEGATE_ID))
        await conn.commit()


def _fsm(storage, tid):
    return FSMContext(storage=storage, key=StorageKey(bot_id=1, chat_id=tid, user_id=tid))


class _User:
    def __init__(self, uid):
        self.id = uid


class _Msg:
    def __init__(self):
        self.edits = []

    async def edit_text(self, text, parse_mode=None, reply_markup=None):
        self.edits.append((text, reply_markup))


class _Cb:
    def __init__(self, data, uid):
        self.data = data
        self.from_user = _User(uid)
        self.message = _Msg()
        self.answers = []

    async def answer(self, text=None, show_alert=False):
        self.answers.append((text, show_alert))


class _Incoming:
    def __init__(self, uid, text=None, photo=None, copy_error=None):
        self.from_user = _User(uid)
        self.text = text
        self.caption = None
        self.photo = photo
        self.document = None
        self.video = None
        self.animation = None
        self.audio = None
        self.voice = None
        self.video_note = None
        self.sticker = None
        self.location = None
        self.contact = None
        self.copy_error = copy_error
        self.copies = []
        self.answers = []

    async def send_copy(self, chat_id, **kwargs):
        if self.copy_error is not None:
            raise self.copy_error
        self.copies.append(chat_id)
        return object()

    async def answer(self, text, parse_mode=None, reply_markup=None):
        self.answers.append(text)


def _cbs(kb):
    return [b.callback_data for row in kb.inline_keyboard for b in row]


def _blocked():
    return TelegramForbiddenError(method=None, message="Forbidden: bot was blocked by the user")


def _find_card(*, tid=DELEGATE_ID, text=None):
    import handlers.admin as admin_mod

    class _M:
        def __init__(self):
            self.text = text or f"/find {tid}"
            self.from_user = _User(ADMIN_ID)
            self.captured = {}

        async def answer(self, body, parse_mode=None, reply_markup=None):
            self.captured = {"text": body, "kb": reply_markup}

    msg = _M()
    _run(admin_mod.cmd_find_user(msg))
    return msg.captured


def test_card_has_write_button_for_delegate(tmp_path):
    _db_ready(tmp_path)
    _run(_seed())
    card = _find_card()
    assert f"findw_start:{DELEGATE_ID}" in _cbs(card["kb"])


def test_card_has_write_button_for_started_only(tmp_path):
    _db_ready(tmp_path)
    _run(db.mark_reg_started(STARTED_ID, "onlystart", event_city="msk"))
    card = _find_card(tid=STARTED_ID)
    assert "анкету не подавал" in card["text"]
    assert f"findw_start:{STARTED_ID}" in _cbs(card["kb"])


def test_start_asks_for_message_and_sets_state(tmp_path):
    from handlers.applications import admin_find_write as h

    _db_ready(tmp_path)
    storage = MemoryStorage()

    async def scenario():
        await _seed()
        state = _fsm(storage, ADMIN_ID)
        cb = _Cb(f"findw_start:{DELEGATE_ID}", ADMIN_ID)
        await h.findw_start(cb, state)
        return cb, await state.get_state(), await state.get_data()

    cb, after_state, after_data = _run(scenario())
    text, kb = cb.message.edits[0]
    assert "Иван Иванов" in text
    assert "одним сообщением" in text
    assert _cbs(kb) == [f"findw_cancel:{DELEGATE_ID}"]
    assert after_state == FindWrite.waiting.state
    assert after_data["findw_tid"] == DELEGATE_ID


def test_start_denied_for_other_city_manager(tmp_path):
    from handlers.applications import admin_find_write as h

    _db_ready(tmp_path)
    storage = MemoryStorage()

    async def scenario():
        await db.set_setting("event_city_enabled", "on")
        await db.set_setting(role_caps_key("reg_manager"), "moderate_reg")
        await db.add_staff(BOUND_MSK_ID, "reg_manager", ADMIN_ID)
        await db.set_staff_city(BOUND_MSK_ID, "msk")
        await _seed(city="spb")
        state = _fsm(storage, BOUND_MSK_ID)
        cb = _Cb(f"findw_start:{DELEGATE_ID}", BOUND_MSK_ID)
        await h.findw_start(cb, state)
        return cb, await state.get_state()

    cb, after_state = _run(scenario())
    assert cb.message.edits == []
    assert cb.answers[0][1] is True
    assert after_state is None


def test_cancel_clears_state(tmp_path):
    from handlers.applications import admin_find_write as h

    _db_ready(tmp_path)
    storage = MemoryStorage()

    async def scenario():
        state = _fsm(storage, ADMIN_ID)
        await state.set_state(FindWrite.waiting)
        await state.update_data(findw_tid=DELEGATE_ID)
        cb = _Cb(f"findw_cancel:{DELEGATE_ID}", ADMIN_ID)
        await h.findw_cancel(cb, state)
        return cb, await state.get_state()

    cb, after_state = _run(scenario())
    assert "отменена" in cb.message.edits[0][0]
    assert after_state is None


def test_receive_copies_text_and_records_action(tmp_path):
    from handlers.applications import admin_find_write as h

    _db_ready(tmp_path)
    storage = MemoryStorage()

    async def scenario():
        await _seed()
        state = _fsm(storage, ADMIN_ID)
        await state.set_state(FindWrite.waiting)
        await state.update_data(findw_tid=DELEGATE_ID)
        msg = _Incoming(ADMIN_ID, text="Подойдите к стойке 3")
        await h.findw_receive(msg, state)
        return msg, await state.get_state(), await db.list_admin_actions(DELEGATE_ID)

    msg, after_state, actions = _run(scenario())
    assert msg.copies == [DELEGATE_ID]
    assert "Отправлено: Иван Иванов" in msg.answers[0]
    assert after_state is None
    assert actions[0]["action"] == "write_message"
    assert actions[0]["actor_id"] == ADMIN_ID


def test_receive_blocked_explains_and_does_not_copy(tmp_path):
    from handlers.applications import admin_find_write as h

    _db_ready(tmp_path)
    storage = MemoryStorage()

    async def scenario():
        await _seed()
        state = _fsm(storage, ADMIN_ID)
        await state.set_state(FindWrite.waiting)
        await state.update_data(findw_tid=DELEGATE_ID)
        msg = _Incoming(ADMIN_ID, text="привет", copy_error=_blocked())
        await h.findw_receive(msg, state)
        return msg, await state.get_state()

    msg, after_state = _run(scenario())
    assert msg.copies == []
    assert "заблокировал бота" in msg.answers[0]
    assert after_state is None


def test_receive_empty_keeps_waiting(tmp_path):
    from handlers.applications import admin_find_write as h

    _db_ready(tmp_path)
    storage = MemoryStorage()

    async def scenario():
        await _seed()
        state = _fsm(storage, ADMIN_ID)
        await state.set_state(FindWrite.waiting)
        await state.update_data(findw_tid=DELEGATE_ID)
        msg = _Incoming(ADMIN_ID)
        await h.findw_receive(msg, state)
        return msg, await state.get_state()

    msg, after_state = _run(scenario())
    assert msg.copies == []
    assert "текста" in msg.answers[0].lower() or "файл" in msg.answers[0].lower()
    assert after_state == FindWrite.waiting.state


def test_receive_command_cancels_instead_of_sending(tmp_path):
    from handlers.applications import admin_find_write as h

    _db_ready(tmp_path)
    storage = MemoryStorage()

    async def scenario():
        state = _fsm(storage, ADMIN_ID)
        await state.set_state(FindWrite.waiting)
        await state.update_data(findw_tid=DELEGATE_ID)
        msg = _Incoming(ADMIN_ID, text="/find")
        await h.findw_cancel_text(msg, state)
        return msg, await state.get_state()

    msg, after_state = _run(scenario())
    assert msg.copies == []
    assert "отменена" in msg.answers[0]
    assert after_state is None


@pytest.mark.parametrize("name", ["findw_start", "findw_cancel"])
def test_write_callbacks_require_moderate_reg(name):
    assert required_capability(callback_data=f"{name}:{DELEGATE_ID}") == "moderate_reg"
    assert f"{name}:*" in ADMIN_CAPS


def test_write_state_requires_moderate_reg():
    assert required_capability(raw_state="FindWrite:waiting") == "moderate_reg"
    assert ADMIN_CAPS["state:FindWrite:*"] == "moderate_reg"


def test_parse_tid_rejects_negative():
    from handlers.applications.admin_find_write import _parse_tid

    assert _parse_tid("-1") is None
    assert _parse_tid(str(DELEGATE_ID)) == DELEGATE_ID


def test_screen_hides_other_city_and_does_not_say_forbidden(tmp_path):
    from services.applications import personal_letter

    _db_ready(tmp_path)

    async def scenario():
        await db.set_setting("event_city_enabled", "on")
        await db.set_setting(role_caps_key("reg_manager"), "moderate_reg")
        await db.add_staff(BOUND_MSK_ID, "reg_manager", ADMIN_ID)
        await db.set_staff_city(BOUND_MSK_ID, "msk")
        await _seed(city="msk")
        await db.add_user({
            "telegram_id": STARTED_ID, "full_name": "Тест Пётр", "username": "petr",
            "registration_date": "2026-01-01", "event_city": "spb",
        })
        async with db._connect() as conn:
            await conn.execute(
                "UPDATE users SET full_name = ? WHERE telegram_id = ?",
                ("Тест Иван", DELEGATE_ID),
            )
            await conn.commit()
        view = await personal_letter.screen(BOUND_MSK_ID, "тест")
        return view

    view = _run(scenario())
    assert [person["name"] for person in view["people"]] == ["Тест Иван"]
    assert "forbidden" not in view["text"].lower()
    assert "forbidden" not in personal_letter.OTHER_CITY.lower()
    assert "msk" not in view["text"]


def test_preview_does_not_send_and_queue_sends_one_letter(tmp_path):
    from services.applications import personal_letter
    from services.infra.miniapp_outbox import _handle_row

    _db_ready(tmp_path)

    class _Bot:
        def __init__(self):
            self.messages = []

        async def send_message(self, chat_id, text, parse_mode=None):
            self.messages.append((chat_id, text, parse_mode))

    async def scenario():
        await _seed()
        preview = await personal_letter.letter_decision(
            ADMIN_ID, DELEGATE_ID, "Стойка 3, жду. 2 < 3", confirm=False,
        )
        before = await db.list_admin_actions(DELEGATE_ID)
        ready = await personal_letter.letter_decision(
            ADMIN_ID, DELEGATE_ID, "Стойка 3, жду. 2 < 3", confirm=True,
        )
        bot = _Bot()
        await _handle_row(bot, "personal_letter", ready["enqueue"])
        actions = await db.list_admin_actions(DELEGATE_ID)
        user = await db.get_user(DELEGATE_ID)
        return preview, before, ready, bot.messages, actions, user["status"]

    preview, before, ready, messages, actions, status = _run(scenario())
    assert preview["need_confirm"] is True
    assert "enqueue" not in preview
    assert "рассылк" in preview["text"]
    assert before == []
    assert ready["enqueue"]["telegram_id"] == DELEGATE_ID
    assert ready["enqueue"]["text"] == "Стойка 3, жду. 2 < 3"
    assert messages == [(DELEGATE_ID, "Стойка 3, жду. 2 &lt; 3", "HTML")]
    assert actions[0]["action"] == "write_message"
    assert actions[0]["actor_id"] == ADMIN_ID
    assert status == "approved"


def test_other_city_letter_is_not_queued(tmp_path):
    from services.applications import personal_letter

    _db_ready(tmp_path)

    async def scenario():
        await db.set_setting("event_city_enabled", "on")
        await db.set_setting(role_caps_key("reg_manager"), "moderate_reg")
        await db.add_staff(BOUND_MSK_ID, "reg_manager", ADMIN_ID)
        await db.set_staff_city(BOUND_MSK_ID, "msk")
        await _seed(city="spb")
        return await personal_letter.letter_decision(
            BOUND_MSK_ID, DELEGATE_ID, "привет", confirm=True,
        )

    decision = _run(scenario())
    assert decision["ok"] is False
    assert "enqueue" not in decision
    assert "другого города" in decision["text"]
    assert "forbidden" not in decision["text"].lower()


def test_chat_write_stays_and_site_only_links():
    from pathlib import Path

    from handlers.applications.admin_find_write import findw_receive, findw_start
    from dashboard.forum_day import LETTER_HREF, letter_page
    from services.applications.personal_letter import NEED_CAP

    assert findw_start is not None
    assert findw_receive is not None
    assert required_capability(callback_data=f"findw_start:{DELEGATE_ID}") == "moderate_reg"
    assert "forbidden" not in NEED_CAP.lower()
    assert "moderate_reg" not in NEED_CAP
    page = letter_page()
    assert LETTER_HREF == "/app/#/letter"
    assert LETTER_HREF in page
    source = Path("dashboard/forum_day.py").read_text(encoding="utf-8")
    assert "SELECT" not in source
    assert "letter_decision" not in source
    letter_src = Path("services/applications/personal_letter.py").read_text(encoding="utf-8")
    handler_src = Path("handlers/applications/admin_find_write.py").read_text(encoding="utf-8")
    for needle in ("mute_broadcasts_until", "get_muted_today_ids", "notice_blocked_ids"):
        assert needle not in letter_src
        assert needle not in handler_src
