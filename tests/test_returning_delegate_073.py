"""Phase 07.3 Plan 03 (RET-02) tests: returning-delegate recognition.

Covers: the `_is_returning_row` predicate (pure, no DB), the new returning-delegate branch in
`cmd_start` (banner + rereg_start button, deep-link preservation, fail-soft posture, parity
with every other /start branch), the `rereg_start` callback (self-guard, own-row-only snapshot,
city/track re-ask via the extracted `_city_fork_then_continue`), and the `_prior_answers`
snapshot surviving `_start_registration_flow`'s `state.clear()`.

Style matches tests/test_admin_rereg_260817.py / tests/test_city_flow_phase71.py (direct
handler calls, real FSMContext over MemoryStorage, config.DB_PATH pointed at tmp_path) —
pytest-asyncio is unavailable in this env; no conftest.py exists, so this file is self-contained.
"""
import asyncio

from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import InlineKeyboardMarkup, ReplyKeyboardMarkup

import cities as cities_mod
from config import config
from database import db
from handlers import registration as reg
from handlers.states import Registration

UID = 800001
OTHER_UID = 800002


def _use_tmp_db(tmp_path):
    config.DB_PATH = str(tmp_path / "test_returning_073.db")


def _new_state(uid: int) -> FSMContext:
    return FSMContext(storage=MemoryStorage(), key=StorageKey(bot_id=1, chat_id=uid, user_id=uid))


class _FakeUser:
    def __init__(self, uid, username=None):
        self.id = uid
        self.username = username


class _FakeChat:
    def __init__(self, cid):
        self.id = cid


class _KBCapturingMessage:
    """Records (text, reply_markup, parse_mode) triples from answer/answer_photo."""

    def __init__(self, uid, username=None):
        self.from_user = _FakeUser(uid, username)
        self.chat = _FakeChat(uid)
        self.sent = []  # list[(text, reply_markup, parse_mode)]

    async def answer(self, text=None, reply_markup=None, parse_mode=None, *a, **k):
        self.sent.append((text, reply_markup, parse_mode))
        return None

    async def answer_photo(self, *a, caption=None, reply_markup=None, parse_mode=None, **k):
        self.sent.append((caption if caption is not None else "<photo>", reply_markup, parse_mode))
        return None

    async def edit_reply_markup(self, reply_markup=None):
        return None

    def model_copy(self, update=None):
        new = _KBCapturingMessage(self.from_user.id, self.from_user.username)
        new.sent = self.sent
        if update and "from_user" in update:
            new.from_user = update["from_user"]
        return new


class _FakeCallback:
    def __init__(self, data, user_id, username=None):
        self.data = data
        self.from_user = _FakeUser(user_id, username)
        self.message = _KBCapturingMessage(0)
        self.answers = []  # list[(text, show_alert)]

    async def answer(self, text=None, show_alert=False):
        self.answers.append((text, show_alert))
        return None


class FakeCommand:
    def __init__(self, args=None):
        self.args = args


def _texts(msg: _KBCapturingMessage):
    return [t for (t, _, _) in msg.sent]


def _inline_kb_msgs(msg: _KBCapturingMessage):
    return [(t, rm, p) for (t, rm, p) in msg.sent if isinstance(rm, InlineKeyboardMarkup)]


def _callback_datas(markup):
    rows = getattr(markup, "inline_keyboard", None)
    if not rows:
        return []
    return [btn.callback_data for row in rows for btn in row]


async def _register(uid, username=None, status="approved", season=None, prev_season=None,
                     event_city=None, full_name="Тест Тестов"):
    await db.add_user({
        "telegram_id": uid, "full_name": full_name, "username": username,
        "registration_date": "2026-08-18 10:00:00",
        "season": season, "prev_season": prev_season, "event_city": event_city,
    })
    await db.set_user_status(uid, status)


# ── Task 1: _is_returning_row predicate table ────────────────────────────────────────────────

def test_predicate_table():
    cases = [
        (None, "YL'26", False),
        ({"status": "rejected"}, None, True),
        ({"status": "rejected"}, "YL'26", True),
        ({"status": "approved", "season": None}, "YL'26", False),
        ({"status": "approved", "season": "YL'25"}, "YL'26", True),
        ({"status": "approved", "season": "YL'26"}, "YL'26", False),
        ({"status": "approved", "season": "YL'25"}, None, False),
    ]
    for user, event_season, expected in cases:
        assert reg._is_returning_row(user, event_season) is expected, (user, event_season)


# ── Task 1: cmd_start returning branch ───────────────────────────────────────────────────────

def test_start_returning_shows_button(tmp_path):
    _use_tmp_db(tmp_path)

    async def go():
        await db.init_db()
        await db.set_setting("event_season", "YL'26")
        await _register(UID, "delegate", status="approved", season="YL'25")

        msg = _KBCapturingMessage(UID, "delegate")
        state = _new_state(UID)
        await reg.cmd_start(msg, state, bot=object(), command=None)

        inline = _inline_kb_msgs(msg)
        assert len(inline) == 1
        assert _callback_datas(inline[0][1]) == ["rereg_start"]
        assert any(t and "YL'25" in t for (t, _, _) in msg.sent)

    asyncio.run(go())


def test_start_returning_uses_registry_text(tmp_path):
    _use_tmp_db(tmp_path)

    async def go():
        await db.init_db()
        await db.set_setting("event_season", "YL'26")
        await db.set_setting("start_text_returning", "Привет, {season}! <b>снова</b> {tricky}")
        await _register(UID, "delegate", status="approved", season="YL'25")

        msg = _KBCapturingMessage(UID, "delegate")
        state = _new_state(UID)
        # must not raise even though the text has an unrelated {tricky} placeholder
        await reg.cmd_start(msg, state, bot=object(), command=None)

        texts = [t for (t, _, _) in msg.sent if t]
        assert any("YL'25" in t and "<b>снова</b>" in t and "{tricky}" in t for t in texts)

    asyncio.run(go())


def test_start_rejected_gets_button(tmp_path):
    _use_tmp_db(tmp_path)

    async def go():
        await db.init_db()
        await _register(UID, "delegate", status="rejected", season=None)

        msg = _KBCapturingMessage(UID, "delegate")
        state = _new_state(UID)
        await reg.cmd_start(msg, state, bot=object(), command=None)

        inline = _inline_kb_msgs(msg)
        assert len(inline) == 1
        assert _callback_datas(inline[0][1]) == ["rereg_start"]
        assert not any(t and "Отлично, начинаем регистрацию." in t for t in _texts(msg))

    asyncio.run(go())


def test_start_current_season_parity(tmp_path):
    _use_tmp_db(tmp_path)

    async def go():
        await db.init_db()
        await db.set_setting("event_season", "YL'26")
        await _register(UID, "delegate", status="approved", season="YL'26")

        msg = _KBCapturingMessage(UID, "delegate")
        state = _new_state(UID)
        await reg.cmd_start(msg, state, bot=object(), command=None)

        inline = _inline_kb_msgs(msg)
        assert all("rereg_start" not in _callback_datas(rm) for (_, rm, _) in inline)
        reply_kb = [m for m in msg.sent if isinstance(m[1], ReplyKeyboardMarkup)]
        assert len(reply_kb) == 1

    asyncio.run(go())


def test_start_no_season_configured_parity(tmp_path):
    _use_tmp_db(tmp_path)

    async def go():
        await db.init_db()
        await _register(UID, "delegate", status="approved", season=None)

        msg = _KBCapturingMessage(UID, "delegate")
        state = _new_state(UID)
        await reg.cmd_start(msg, state, bot=object(), command=None)

        inline = _inline_kb_msgs(msg)
        assert all("rereg_start" not in _callback_datas(rm) for (_, rm, _) in inline)

    asyncio.run(go())


def test_start_returning_predicate_failure_is_soft(tmp_path, monkeypatch):
    _use_tmp_db(tmp_path)

    async def go():
        await db.init_db()
        await _register(UID, "delegate", status="approved", season="YL'25")

        real_get_setting = reg.get_setting

        async def boom(key, *a, **k):
            if key == "event_season":
                raise RuntimeError("boom")
            return await real_get_setting(key, *a, **k)

        monkeypatch.setattr(reg, "get_setting", boom)

        msg = _KBCapturingMessage(UID, "delegate")
        state = _new_state(UID)
        # must not raise
        await reg.cmd_start(msg, state, bot=object(), command=None)

        inline = _inline_kb_msgs(msg)
        assert all("rereg_start" not in _callback_datas(rm) for (_, rm, _) in inline)
        reply_kb = [m for m in msg.sent if isinstance(m[1], ReplyKeyboardMarkup)]
        assert len(reply_kb) == 1  # same as today's already-registered branch

    asyncio.run(go())


def test_start_returning_preserves_deeplink(tmp_path):
    _use_tmp_db(tmp_path)

    async def go():
        await db.init_db()
        await db.set_setting("event_season", "YL'26")
        await _register(UID, "delegate", status="approved", season="YL'25")

        msg = _KBCapturingMessage(UID, "delegate")
        state = _new_state(UID)
        # simulate attribution already carried from an earlier /start this session
        await state.update_data(event_city="msk")

        await reg.cmd_start(msg, state, bot=object(), command=FakeCommand("777"))

        data = await state.get_data()
        assert data.get("referrer_id") == 777
        assert data.get("event_city") == "msk"  # untouched, preserved on top

    asyncio.run(go())


def test_cmd_start_new_user_parity(tmp_path):
    """New user (no row at all) -> /start behaves exactly as before (no returning branch)."""
    _use_tmp_db(tmp_path)

    async def go():
        await db.init_db()
        await db.set_setting("event_season", "YL'26")

        msg = _KBCapturingMessage(UID, "newbie")
        state = _new_state(UID)
        await reg.cmd_start(msg, state, bot=object(), command=None)

        inline = _inline_kb_msgs(msg)
        assert all("rereg_start" not in _callback_datas(rm) for (_, rm, _) in inline)
        # newcomer reaches the registration prompt (no city module -> straight through)
        assert any(t and "Отлично, начинаем регистрацию." in t for t in _texts(msg))

    asyncio.run(go())


# ── Task 2: rereg_start callback, _city_fork_then_continue, _prior_answers ──────────────────

def test_rereg_start_rejects_non_returning(tmp_path):
    _use_tmp_db(tmp_path)

    async def go():
        await db.init_db()
        await db.set_setting("event_season", "YL'26")
        await _register(UID, "delegate", status="approved", season="YL'26")

        state = _new_state(UID)
        callback = _FakeCallback("rereg_start", UID, "delegate")
        await reg.rereg_start(callback, state)

        assert len(callback.answers) == 1
        text, show_alert = callback.answers[0]
        assert "Ты уже зарегистрирован(а) на этот сезон" in text
        assert show_alert is True
        assert await state.get_data() == {}
        assert await state.get_state() is None

    asyncio.run(go())


def test_rereg_start_rejects_unknown_user(tmp_path):
    _use_tmp_db(tmp_path)

    async def go():
        await db.init_db()

        state = _new_state(OTHER_UID)
        callback = _FakeCallback("rereg_start", OTHER_UID, "ghost")
        await reg.rereg_start(callback, state)

        assert len(callback.answers) == 1
        text, show_alert = callback.answers[0]
        assert "Ты уже зарегистрирован(а) на этот сезон" in text
        assert show_alert is True
        assert await state.get_data() == {}

    asyncio.run(go())


def test_rereg_start_snapshots_own_row_only(tmp_path):
    _use_tmp_db(tmp_path)
    a_id, b_id = UID, OTHER_UID

    async def go():
        await db.init_db()
        await _register(a_id, "alice", status="rejected", season=None, full_name="Алиса Алисова")
        await _register(b_id, "bob", status="rejected", season=None, full_name="Борис Борисов")

        state = _new_state(a_id)
        callback = _FakeCallback("rereg_start", a_id, "alice")
        await reg.rereg_start(callback, state)

        data = await state.get_data()
        prior = data.get("_prior_answers")
        assert prior is not None
        assert prior.get("full_name") == "Алиса Алисова"
        assert prior.get("telegram_id") == a_id

    asyncio.run(go())


def test_rereg_start_shows_city_fork(tmp_path):
    _use_tmp_db(tmp_path)
    saved = list(cities_mod.CITIES)

    async def go():
        await db.init_db()
        await db.set_setting("event_city_enabled", "on")
        cities_mod.set_cities_for_test([
            {"code": "msk", "label": "Москва", "tab_base": "", "enabled": 1, "sort_order": 0},
            {"code": "spb", "label": "СПб", "tab_base": "", "enabled": 1, "sort_order": 1},
        ])
        await _register(UID, "delegate", status="rejected", season=None, event_city="msk")

        state = _new_state(UID)
        callback = _FakeCallback("rereg_start", UID, "delegate")
        await reg.rereg_start(callback, state)

        texts = _texts(callback.message)
        city_fork_text = await db.get_setting("city_fork_text") or reg.DEFAULT_CITY_FORK_TEXT
        assert any(t == city_fork_text for t in texts)

        data = await state.get_data()
        assert data.get("event_city") is None

    try:
        asyncio.run(go())
    finally:
        cities_mod.set_cities_for_test(saved)


def test_rereg_start_city_module_off_no_city_screen(tmp_path):
    _use_tmp_db(tmp_path)

    async def go():
        await db.init_db()
        await _register(UID, "delegate", status="rejected", season=None)

        state = _new_state(UID)
        callback = _FakeCallback("rereg_start", UID, "delegate")
        await reg.rereg_start(callback, state)

        assert await state.get_state() == Registration.full_name.state
        texts = _texts(callback.message)
        assert any(t and "Отлично, начинаем регистрацию." in t for t in texts)

    asyncio.run(go())


def test_prior_answers_survives_state_clear(tmp_path):
    _use_tmp_db(tmp_path)

    async def go():
        await db.init_db()
        prior = {"telegram_id": UID, "full_name": "Прошлый Прошлов"}
        state = _new_state(UID)
        await state.update_data(_prior_answers=prior)

        msg = _KBCapturingMessage(UID, "delegate")
        await reg._start_registration_flow(msg, state)

        data = await state.get_data()
        assert data.get("_prior_answers") == prior

    asyncio.run(go())


def test_prior_answers_not_in_incomplete_snapshot(tmp_path, monkeypatch):
    _use_tmp_db(tmp_path)

    async def go():
        await db.init_db()
        await db.set_setting("consent_enabled", "on")

        recorded = []
        real_set_reg_step = reg.set_reg_step

        async def spy(chat_id, step_key, partial_json=None):
            recorded.append(partial_json)
            return await real_set_reg_step(chat_id, step_key, partial_json)

        monkeypatch.setattr(reg, "set_reg_step", spy)

        state = _new_state(UID)
        await state.update_data(_prior_answers={"telegram_id": UID, "full_name": "X"})

        msg = _KBCapturingMessage(UID, "delegate")
        await reg._start_registration_flow(msg, state)

        assert recorded  # at least one snapshot call happened (the first consent step)
        for partial_json in recorded:
            if partial_json is None:
                continue
            assert "_prior_answers" not in partial_json

    asyncio.run(go())
