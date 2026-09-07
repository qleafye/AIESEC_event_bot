"""Phase 28 (28-05, SU-04, СкиллАп 5): развилка резюме — делегатская половина.

Задача 1: шов `handlers/reg_resume_fork.py` (экран R1, приём ссылки R2b, «Назад» на развилку).
pytest-asyncio недоступен в этом окружении — async через `asyncio.run()`, стиль Fake-объектов
aiogram — тот же приём, что `tests/test_reg_resume_draft.py`/`tests/test_skillup_steps_28.py`.
"""
import asyncio

from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import InlineKeyboardMarkup

from config import config
from database import db
from handlers import registration as reg
from handlers import reg_extra_steps
from handlers import reg_resume_fork
from handlers.states import Registration

UID = 900805000


def _use_tmp_db(tmp_path, name="test_skillup_resume_fork_ui_28.db"):
    config.DB_PATH = str(tmp_path / name)
    asyncio.run(db.init_db())


def _state(uid: int) -> FSMContext:
    return FSMContext(storage=MemoryStorage(), key=StorageKey(bot_id=1, chat_id=uid, user_id=uid))


class _FakeUser:
    def __init__(self, uid):
        self.id = uid


class _FakeChat:
    def __init__(self, cid):
        self.id = cid


class _FakeMessage:
    """Минимальный message: обработчики трогают только `.chat.id`, `.from_user`, `.text` и
    `.answer` (тот же контур, что `_FakeMessage`/`_KBCapturingMessage` в
    `tests/test_skillup_steps_28.py`/`tests/test_reg_resume_draft.py`)."""

    def __init__(self, uid, text=None):
        self.chat = _FakeChat(uid)
        self.from_user = _FakeUser(uid)
        self.text = text
        self.sent = []
        self.document = None

    async def answer(self, text=None, reply_markup=None, parse_mode=None, *a, **k):
        self.sent.append((text, reply_markup, parse_mode))
        return "sent:%d" % len(self.sent)

    async def edit_reply_markup(self, reply_markup=None):
        return None

    def model_copy(self, update=None):
        new = _FakeMessage(self.from_user.id, text=self.text)
        new.sent = self.sent
        new.document = self.document
        if update and "from_user" in update:
            new.from_user = update["from_user"]
        return new


class _FakeCallback:
    def __init__(self, data, uid):
        self.data = data
        self.from_user = _FakeUser(uid)
        self.message = _FakeMessage(uid)
        self.answers = []

    async def answer(self, text=None, show_alert=False):
        self.answers.append((text, show_alert))
        return None


def _texts(msg: _FakeMessage):
    return [t for (t, _, _) in msg.sent]


def _inline_kbs(msg: _FakeMessage):
    return [rm for (_, rm, _) in msg.sent if isinstance(rm, InlineKeyboardMarkup)]


def _callback_datas(kb: InlineKeyboardMarkup):
    return [btn.callback_data for row in kb.inline_keyboard for btn in row if btn.callback_data]


# ══════════════════════════════════════════════════════════════════════════════════════════
# Задача 1: развилка в чате
# ══════════════════════════════════════════════════════════════════════════════════════════

def test_fork_screen_shows_three_buttons(tmp_path):
    _use_tmp_db(tmp_path)
    uid = UID + 1

    async def go():
        await db.set_setting("reg_resume_mode", "fork")
        msg = _FakeMessage(uid)
        state = _state(uid)
        await state.update_data(participant_type="full", full_name="Тест Тестов")
        await reg._ask_step("resume", msg, state, 1, 5)
        return msg, await state.get_state()

    msg, state_name = asyncio.run(go())
    kbs = _inline_kbs(msg)
    assert len(kbs) == 1, "экран развилки обязан прийти с инлайн-клавиатурой"
    assert _callback_datas(kbs[0]) == ["regfork:file", "regfork:link", "regfork:mini"]
    assert state_name == Registration.resume.state


def test_pick_link_asks_link_step(tmp_path):
    _use_tmp_db(tmp_path)
    uid = UID + 2

    async def go():
        await db.set_setting("reg_resume_mode", "fork")
        await db.set_setting("reg_q_resume_link", "on")
        state = _state(uid)
        await state.update_data(
            participant_type="full", full_name="Тест", _reg_step=3, _reg_total=6,
        )
        callback = _FakeCallback("regfork:link", uid)
        await reg_resume_fork.regfork_pick(callback, state)
        data = await state.get_data()
        return data, callback.message, await state.get_state()

    data, msg, state_name = asyncio.run(go())
    assert data.get("resume_type") == "link"
    assert state_name == Registration.resume_link.state
    assert msg.sent, "вопрос про ссылку обязан быть задан"


def test_pick_mini_asks_three_substeps_in_order(tmp_path):
    _use_tmp_db(tmp_path)
    uid = UID + 3

    async def go():
        await db.set_setting("reg_resume_mode", "fork")
        await db.set_setting("reg_q_mini_projects", "on")
        await db.set_setting("reg_q_mini_portfolio", "on")
        await db.set_setting("reg_q_mini_direction", "on")
        state = _state(uid)
        await state.update_data(
            participant_type="full", full_name="Тест", _reg_step=3, _reg_total=6,
        )
        callback = _FakeCallback("regfork:mini", uid)
        await reg_resume_fork.regfork_pick(callback, state)
        state_after_pick = await state.get_state()

        msg1 = _FakeMessage(uid, text="Делал сайт для клиента, роль — фронтенд")
        await reg_extra_steps.process_mini_projects(msg1, state, bot=None)
        state_after_1 = await state.get_state()

        msg2 = _FakeMessage(uid, text="Пропустить")
        await reg_extra_steps.process_mini_portfolio(msg2, state, bot=None)
        state_after_2 = await state.get_state()
        return state_after_pick, state_after_1, state_after_2

    s0, s1, s2 = asyncio.run(go())
    assert s0 == Registration.mini_projects.state
    assert s1 == Registration.mini_portfolio.state
    assert s2 == Registration.mini_direction.state


def test_back_from_any_branch_returns_to_fork(tmp_path):
    _use_tmp_db(tmp_path)
    uid = UID + 4

    async def go():
        await db.set_setting("reg_resume_mode", "fork")
        state = _state(uid)
        await state.update_data(
            participant_type="full", full_name="Тест", resume_type="mini",
            _reg_step=3, _reg_total=6,
        )
        await state.set_state(Registration.mini_portfolio)
        msg = _FakeMessage(uid, text="⬅️ Назад")
        await reg_extra_steps.process_mini_portfolio(msg, state, bot=None)
        return await state.get_state(), msg

    state_name, msg = asyncio.run(go())
    assert state_name == Registration.resume.state
    kbs = _inline_kbs(msg)
    assert kbs and _callback_datas(kbs[0]) == ["regfork:file", "regfork:link", "regfork:mini"]


def test_back_resets_resume_type(tmp_path):
    _use_tmp_db(tmp_path)
    uid = UID + 5

    async def go():
        await db.set_setting("reg_resume_mode", "fork")
        state = _state(uid)
        await state.update_data(
            participant_type="full", full_name="Тест", resume_type="mini",
            _reg_step=3, _reg_total=6,
        )
        msg = _FakeMessage(uid)
        await reg_resume_fork.back_to_fork(msg, state)
        return await state.get_data()

    data = asyncio.run(go())
    assert data.get("resume_type") is None


def test_file_or_text_mode_unchanged(tmp_path):
    """Дефолт (`reg_resume_mode` не задан) — экран резюме БЕЗ инлайн-кнопок, тот же
    `ReplyKeyboardRemove`, что всегда (D-06 byte-for-byte)."""
    _use_tmp_db(tmp_path)
    uid = UID + 6

    async def go():
        msg = _FakeMessage(uid)
        state = _state(uid)
        await state.update_data(participant_type="full", full_name="Тест")
        await reg._ask_step("resume", msg, state, 1, 5)
        return msg, await state.get_state()

    msg, state_name = asyncio.run(go())
    assert not _inline_kbs(msg), "старый режим не должен получить инлайн-клавиатуру развилки"
    assert state_name == Registration.resume.state
