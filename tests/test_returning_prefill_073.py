"""Phase 07.3 Plan 04 (RET-02) tests: prefill of a returning delegate's registration.

Covers: the `_ask_step_or_recall` wrapper (recall screen / fall-through parity), `STEP_TO_COLUMN`
/`RECALLABLE_STEPS`, the `recall_keep`/`recall_change` callbacks (value reaches FSM -> DB, aliased
columns, stale-tap guard), the resume keep/upload carve-out, and `finalize_registration`'s
season/prev_season/payment-reset wiring.

Style matches tests/test_returning_delegate_073.py (direct handler calls, real FSMContext over
MemoryStorage, config.DB_PATH pointed at tmp_path, hand-rolled Fake* doubles) -- pytest-asyncio
is unavailable in this env; no conftest.py exists, so this file is self-contained.
"""
import asyncio

from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import InlineKeyboardMarkup

from config import config
from database import db
from handlers import registration as reg
from handlers.states import Registration

UID = 810001
OTHER_UID = 810002


def _use_tmp_db(tmp_path):
    config.DB_PATH = str(tmp_path / "test_returning_prefill_073.db")


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
    """Records (text, reply_markup, parse_mode) triples from answer/answer_document."""

    def __init__(self, uid, username=None):
        self.from_user = _FakeUser(uid, username)
        self.chat = _FakeChat(uid)
        self.sent = []  # list[(text, reply_markup, parse_mode)]
        self.edit_markup_calls = 0

    async def answer(self, text=None, reply_markup=None, parse_mode=None, *a, **k):
        self.sent.append((text, reply_markup, parse_mode))
        return None

    async def answer_document(self, *a, caption=None, reply_markup=None, parse_mode=None, **k):
        self.sent.append((caption, reply_markup, parse_mode))
        return None

    async def edit_reply_markup(self, reply_markup=None):
        self.edit_markup_calls += 1
        return None

    def model_copy(self, update=None):
        new = _KBCapturingMessage(self.from_user.id, self.from_user.username)
        new.sent = self.sent
        new.edit_markup_calls = self.edit_markup_calls
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
                     full_name="Тест Тестов", **extra):
    row = {
        "telegram_id": uid, "full_name": full_name, "username": username,
        "registration_date": "2026-08-18 10:00:00",
        "season": season, "prev_season": prev_season,
    }
    row.update(extra)
    await db.add_user(row)
    await db.set_user_status(uid, status)


# ── Task 1: STEP_TO_COLUMN / RECALLABLE_STEPS ────────────────────────────────────────────────

def test_step_to_column_explicit():
    assert reg.STEP_TO_COLUMN["vk"] == "vk_username"
    assert reg.STEP_TO_COLUMN["ambassador"] == "is_ambassador_candidate"
    assert "resume" not in reg.RECALLABLE_STEPS
    for step_key, _setting_key, _t in reg.REG_FLOW:
        assert step_key in reg.STEP_TO_COLUMN


# ── Task 1: wrapper parity / recall screen ───────────────────────────────────────────────────

def test_no_prior_answers_falls_through(tmp_path):
    _use_tmp_db(tmp_path)

    async def go():
        await db.init_db()
        msg = _KBCapturingMessage(UID, "delegate")
        state = _new_state(UID)
        await state.update_data(participant_type="full")
        await reg._ask_step_or_recall("university", msg, state, 1, 5)

        assert any(t and "ВУЗ" in t for t in _texts(msg))
        assert await state.get_state() == Registration.university.state

    asyncio.run(go())


def test_prior_empty_value_falls_through(tmp_path):
    _use_tmp_db(tmp_path)

    async def go():
        await db.init_db()
        msg = _KBCapturingMessage(UID, "delegate")
        state = _new_state(UID)
        await state.update_data(participant_type="full", _prior_answers={"university": "-"})
        await reg._ask_step_or_recall("university", msg, state, 1, 5)

        assert await state.get_state() == Registration.university.state

    asyncio.run(go())


def test_prior_value_shows_recall_screen(tmp_path):
    _use_tmp_db(tmp_path)

    async def go():
        await db.init_db()
        msg = _KBCapturingMessage(UID, "delegate")
        state = _new_state(UID)
        await state.update_data(participant_type="full", _prior_answers={"university": "МГУ"})
        await reg._ask_step_or_recall("university", msg, state, 3, 9)

        texts = _texts(msg)
        assert any(t and "Прошлый ответ" in t and "МГУ" in t for t in texts)
        inline = _inline_kb_msgs(msg)
        assert len(inline) == 1
        assert _callback_datas(inline[0][1]) == ["recall_keep:university", "recall_change:university"]
        assert await state.get_state() == Registration.recall_pending.state

    asyncio.run(go())


def test_recall_screen_escapes_value(tmp_path):
    _use_tmp_db(tmp_path)

    async def go():
        await db.init_db()
        msg = _KBCapturingMessage(UID, "delegate")
        state = _new_state(UID)
        await state.update_data(participant_type="full", _prior_answers={"university": "<b>x</b>"})
        await reg._ask_step_or_recall("university", msg, state, 1, 5)

        texts = _texts(msg)
        assert any(t and "&lt;b&gt;" in t for t in texts)
        assert not any(t and "<b>x</b>" in t for t in texts)

    asyncio.run(go())


def test_recall_bool_display(tmp_path):
    _use_tmp_db(tmp_path)

    async def go():
        await db.init_db()
        msg = _KBCapturingMessage(UID, "delegate")
        state = _new_state(UID)
        await state.update_data(participant_type="full", _prior_answers={"is_ambassador_candidate": 1})
        await reg._ask_step_or_recall("ambassador", msg, state, 1, 5)
        assert any(t and "Да" in t for t in _texts(msg))

        msg2 = _KBCapturingMessage(UID, "delegate")
        state2 = _new_state(UID)
        await state2.update_data(participant_type="full", _prior_answers={"is_ambassador_candidate": 0})
        await reg._ask_step_or_recall("ambassador", msg2, state2, 1, 5)
        assert any(t and "Нет" in t for t in _texts(msg2))

    asyncio.run(go())


def test_recall_stamps_dropout_step(tmp_path, monkeypatch):
    _use_tmp_db(tmp_path)

    async def go():
        await db.init_db()
        stamped = []
        real_set_reg_step = reg.set_reg_step

        async def spy(chat_id, step_key, partial_json=None):
            stamped.append(step_key)
            return await real_set_reg_step(chat_id, step_key, partial_json)

        monkeypatch.setattr(reg, "set_reg_step", spy)

        msg = _KBCapturingMessage(UID, "delegate")
        state = _new_state(UID)
        await state.update_data(participant_type="full", _prior_answers={"university": "МГУ"})
        await reg._ask_step_or_recall("university", msg, state, 1, 5)

        assert "university" in stamped

    asyncio.run(go())


def test_recall_ignore_text(tmp_path):
    _use_tmp_db(tmp_path)

    async def go():
        await db.init_db()
        msg = _KBCapturingMessage(UID, "delegate")
        state = _new_state(UID)
        await state.set_state(Registration.recall_pending)
        await reg.process_recall_ignore(msg)

        assert any(t and "Оставить" in t and "Изменить" in t for t in _texts(msg))
        assert await state.get_state() == Registration.recall_pending.state

    asyncio.run(go())


def test_consents_never_recalled(tmp_path):
    _use_tmp_db(tmp_path)

    async def go():
        await db.init_db()
        await db.set_setting("consent_enabled", "on")
        msg = _KBCapturingMessage(UID, "delegate")
        state = _new_state(UID)
        await state.update_data(_prior_answers={"full_name": "Прошлый Прошлов"})
        await reg._start_registration_flow(msg, state)

        inline = _inline_kb_msgs(msg)
        for (_t, rm, _p) in inline:
            for cd in _callback_datas(rm):
                assert not (cd or "").startswith("recall_keep:")
        assert await state.get_state() != Registration.recall_pending.state

    asyncio.run(go())


# ── Task 2: recall_keep / recall_change / resume carve-out ─────────────────────────────────

def test_keep_writes_value_into_fsm(tmp_path):
    _use_tmp_db(tmp_path)

    async def go():
        await db.init_db()
        state = _new_state(UID)
        await state.update_data(
            participant_type="full", _prior_answers={"university": "МГУ"},
            _recall_step="university", _reg_step=3, _reg_total=9,
        )
        await state.set_state(Registration.recall_pending)
        callback = _FakeCallback("recall_keep:university", UID, "delegate")

        await reg.recall_keep(callback, state, bot=object())

        data = await state.get_data()
        assert data.get("university") == "МГУ"
        assert data.get("_reg_step", 0) >= 3

    asyncio.run(go())


def test_keep_maps_aliased_columns(tmp_path):
    _use_tmp_db(tmp_path)

    async def go():
        await db.init_db()
        state = _new_state(UID)
        await state.update_data(
            participant_type="full",
            _prior_answers={"vk_username": "@old_vk"},
            _recall_step="vk", _reg_step=1, _reg_total=5,
        )
        await state.set_state(Registration.recall_pending)
        callback = _FakeCallback("recall_keep:vk", UID, "delegate")
        await reg.recall_keep(callback, state, bot=object())

        data = await state.get_data()
        assert data.get("vk_username") == "@old_vk"
        assert "vk" not in data

    async def go2():
        state = _new_state(UID)
        await state.update_data(
            participant_type="full",
            _prior_answers={"is_ambassador_candidate": 1},
            _recall_step="ambassador", _reg_step=1, _reg_total=5,
        )
        await state.set_state(Registration.recall_pending)
        callback = _FakeCallback("recall_keep:ambassador", UID, "delegate")
        await reg.recall_keep(callback, state, bot=object())

        data = await state.get_data()
        assert data.get("is_ambassador_candidate") == 1
        assert "ambassador" not in data

    asyncio.run(go())
    asyncio.run(go2())


def test_keep_value_reaches_db(tmp_path):
    _use_tmp_db(tmp_path)

    async def go():
        await db.init_db()
        state = _new_state(UID)
        await state.update_data(
            full_name="Тест Тестов", participant_type="full",
            _prior_answers={"university": "МГУ", "course": "3"},
            _recall_step="university", _reg_step=1, _reg_total=2,
        )
        await state.set_state(Registration.recall_pending)
        c1 = _FakeCallback("recall_keep:university", UID, "delegate")
        await reg.recall_keep(c1, state, bot=object())

        await state.update_data(_recall_step="course")
        await state.set_state(Registration.recall_pending)
        c2 = _FakeCallback("recall_keep:course", UID, "delegate")
        await reg.recall_keep(c2, state, bot=object())

        msg = _KBCapturingMessage(UID, "delegate")
        await reg.finalize_registration(msg, state, bot=object())

        row = await db.get_user(UID)
        assert row["university"] == "МГУ"
        assert row["course"] == "3"

    asyncio.run(go())


def test_change_asks_real_question(tmp_path):
    _use_tmp_db(tmp_path)

    async def go():
        await db.init_db()
        state = _new_state(UID)
        await state.update_data(
            participant_type="full", _prior_answers={"university": "МГУ"},
            _recall_step="university", _reg_step=1, _reg_total=5,
        )
        await state.set_state(Registration.recall_pending)
        callback = _FakeCallback("recall_change:university", UID, "delegate")
        await reg.recall_change(callback, state)

        assert await state.get_state() == Registration.university.state
        texts = _texts(callback.message)
        assert not any(t and "Прошлый ответ" in t for t in texts)

    asyncio.run(go())


def test_stale_recall_tap_ignored(tmp_path):
    _use_tmp_db(tmp_path)

    async def go():
        await db.init_db()
        state = _new_state(UID)
        await state.update_data(
            participant_type="full", _prior_answers={"age": 20, "university": "МГУ"},
            _recall_step="university", _reg_step=1, _reg_total=5,
        )
        await state.set_state(Registration.recall_pending)
        callback = _FakeCallback("recall_keep:age", UID, "delegate")
        await reg.recall_keep(callback, state, bot=object())

        data = await state.get_data()
        assert "age" not in data
        assert await state.get_state() == Registration.recall_pending.state

    asyncio.run(go())


def test_resume_keep_is_noop(tmp_path):
    _use_tmp_db(tmp_path)

    async def go():
        await db.init_db()
        # Seed a real prior DB row with resume_file_id="AAA" -- add_user's own COALESCE
        # (database/db.py) is what actually preserves it; "keep" must leave `data` untouched.
        await _register(UID, "delegate", status="rejected", resume_file_id="AAA")
        msg = _KBCapturingMessage(UID, "delegate")
        state = _new_state(UID)
        await state.update_data(
            participant_type="full", full_name="Тест Тестов",
            _prior_answers={"resume_file_id": "AAA"},
        )
        await reg._ask_step_or_recall("resume", msg, state, 14, 14)

        inline = _inline_kb_msgs(msg)
        assert len(inline) == 1
        assert any("Оставить прошлое резюме" in (btn.text or "")
                    for row in inline[0][1].inline_keyboard for btn in row)
        assert await state.get_state() == Registration.recall_pending.state

        callback = _FakeCallback("recall_keep:resume", UID, "delegate")
        await reg.recall_keep(callback, state, bot=object())

        data = await state.get_data()
        assert not any(k.startswith("resume_") for k in data)

        fmsg = _KBCapturingMessage(UID, "delegate")
        await reg.finalize_registration(fmsg, state, bot=object())
        row = await db.get_user(UID)
        assert row["resume_file_id"] == "AAA"

    asyncio.run(go())


def test_resume_screen_hides_file_id(tmp_path):
    _use_tmp_db(tmp_path)

    async def go():
        await db.init_db()
        msg = _KBCapturingMessage(UID, "delegate")
        state = _new_state(UID)
        await state.update_data(participant_type="full", _prior_answers={"resume_file_id": "AAA"})
        await reg._ask_step_or_recall("resume", msg, state, 1, 1)

        texts = _texts(msg)
        assert not any(t and "AAA" in t for t in texts)

    asyncio.run(go())


def test_resume_change_asks_upload(tmp_path):
    _use_tmp_db(tmp_path)

    async def go():
        await db.init_db()
        state = _new_state(UID)
        await state.update_data(
            participant_type="full", _prior_answers={"resume_file_id": "AAA"},
            _recall_step="resume", _reg_step=1, _reg_total=1,
        )
        await state.set_state(Registration.recall_pending)
        callback = _FakeCallback("recall_change:resume", UID, "delegate")
        await reg.recall_change(callback, state)

        assert await state.get_state() == Registration.resume.state
        texts = _texts(callback.message)
        assert any(t and "PDF" in t for t in texts)

    asyncio.run(go())


def test_resume_no_prior_asks_normally(tmp_path):
    _use_tmp_db(tmp_path)

    async def go():
        await db.init_db()
        msg = _KBCapturingMessage(UID, "delegate")
        state = _new_state(UID)
        await state.update_data(participant_type="full")
        await reg._ask_step_or_recall("resume", msg, state, 1, 1)

        texts = _texts(msg)
        assert any(t and "PDF" in t for t in texts)
        assert not any(t and "Оставить прошлое резюме" in t for t in texts)
        assert await state.get_state() == Registration.resume.state

    asyncio.run(go())


# ── Task 3: finalize_registration season / prev_season / payment reset ─────────────────────

def _minimal_finalize_state(uid, **extra):
    state = _new_state(uid)
    return state


async def _run_finalize(uid, data_updates):
    state = _new_state(uid)
    await state.update_data(full_name="Тест Тестов", **data_updates)
    msg = _KBCapturingMessage(uid, "delegate")
    await reg.finalize_registration(msg, state, bot=object())
    return await db.get_user(uid)


def test_finalize_writes_current_season(tmp_path):
    _use_tmp_db(tmp_path)

    async def go():
        await db.init_db()
        await db.set_setting("event_season", "YL'26")
        row = await _run_finalize(UID, {})
        assert row["season"] == "YL'26"

    asyncio.run(go())


def test_finalize_new_delegate_no_prev_season(tmp_path):
    _use_tmp_db(tmp_path)

    async def go():
        await db.init_db()
        await db.set_setting("event_season", "YL'26")
        row = await _run_finalize(UID, {})
        assert row["prev_season"] is None

    asyncio.run(go())


def test_finalize_returning_sets_prev_season(tmp_path):
    _use_tmp_db(tmp_path)

    async def go():
        await db.init_db()
        await db.set_setting("event_season", "YL'26")
        row = await _run_finalize(UID, {"_prior_answers": {"season": "YL'25"}})
        assert row["prev_season"] == "YL'25"

    asyncio.run(go())


def test_finalize_returning_legacy_prev_season(tmp_path):
    _use_tmp_db(tmp_path)

    async def go():
        await db.init_db()
        await db.set_setting("event_season", "YL'26")
        row = await _run_finalize(UID, {"_prior_answers": {"season": None}})
        assert row["prev_season"] == "legacy"

    asyncio.run(go())


def test_finalize_resets_payment_on_new_season(tmp_path):
    _use_tmp_db(tmp_path)

    async def go():
        await db.init_db()
        await db.set_setting("event_season", "YL'26")
        await _register(UID, "delegate", status="approved", season="YL'25")
        await db.update_payment_status(UID, "receipt_sent")
        await db.update_payment_status(UID, "paid", payment_option="full")

        row = await _run_finalize(UID, {"_prior_answers": {"season": "YL'25"}})

        assert row["payment_status"] == "not_paid"
        assert row["payment_option"] is None
        assert row["payment_due"] is None
        assert row["paid_at"] is None

    asyncio.run(go())


def test_finalize_keeps_payment_same_season(tmp_path):
    _use_tmp_db(tmp_path)

    async def go():
        await db.init_db()
        await db.set_setting("event_season", "YL'26")
        await _register(UID, "delegate", status="rejected", season="YL'26")
        await db.update_payment_status(UID, "receipt_sent")
        await db.update_payment_status(UID, "paid", payment_option="full")

        row = await _run_finalize(UID, {"_prior_answers": {"season": "YL'26"}})

        assert row["payment_status"] == "paid"

    asyncio.run(go())


def test_finalize_no_season_configured(tmp_path):
    _use_tmp_db(tmp_path)

    async def go():
        await db.init_db()
        await _register(UID, "delegate", status="approved", season="YL'25")
        await db.update_payment_status(UID, "receipt_sent")
        await db.update_payment_status(UID, "paid", payment_option="full")

        row = await _run_finalize(UID, {"_prior_answers": {"season": "YL'25"}})

        assert row["season"] is None
        assert row["payment_status"] == "paid"

    asyncio.run(go())


def test_finalize_does_not_inherit_referrer(tmp_path):
    _use_tmp_db(tmp_path)

    async def go():
        await db.init_db()
        await db.set_setting("event_season", "YL'26")
        row = await _run_finalize(UID, {"_prior_answers": {"season": "YL'25", "referrer_id": 777}})
        assert row.get("referrer_id") != 777

    asyncio.run(go())


def test_finalize_season_resolve_failure_is_soft(tmp_path, monkeypatch):
    _use_tmp_db(tmp_path)

    async def go():
        await db.init_db()

        real_get_setting = reg.get_setting

        async def boom(key, *a, **k):
            if key == "event_season":
                raise RuntimeError("boom")
            return await real_get_setting(key, *a, **k)

        monkeypatch.setattr(reg, "get_setting", boom)

        row = await _run_finalize(UID, {})
        assert row is not None
        assert row["season"] is None

    asyncio.run(go())
