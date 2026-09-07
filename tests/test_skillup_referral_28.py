"""Phase 28 (28-06, СкиллАп 5 P1): реф-ссылка `amb_<id>` — шестой деп-линк-экстрактор,
пропуск «Источника» у пришедших по реф-ссылке, финальный экран «Хочу свою ссылку»/«Позже».

pytest-asyncio недоступен в этом окружении — async через `asyncio.run()`, стиль Fake-объектов
aiogram — тот же приём, что `tests/test_city_flow_phase71.py`/`tests/test_skillup_resume_fork_ui_28.py`.

Задача 1: `reg_engine.extract_ambassador_ref`/`resolve_referrer` + пропуск шага «Источник»
(`reg_skip_source_for_referred`) + тумблер «засчитывать только амбассадоров»
(`reg_referrer_must_be_ambassador`). Числовой формат `?start=<id>` НЕ проверяется на
существование реферера (D-06 byte-for-byte, tests/test_city_flow_phase71.py::
test_attribution_survives_city_pick_referrer) — `resolve_referrer` применяется ТОЛЬКО к
новому `amb_`-формату (CONTEXT OQ-2).

Задача 2 (шов `handlers/reg_ambassador.py`, финальный экран) добавляется отдельным коммитом
в этот же файл — см. секцию «Задача 2» ниже, дописанную вместе с самим швом.
"""
import asyncio

from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage

from config import config
from database import db
import reg_engine
from handlers import registration as reg

UID = 900806000


def _use_tmp_db(tmp_path, name="test_skillup_referral_28.db"):
    config.DB_PATH = str(tmp_path / name)
    asyncio.run(db.init_db())


def _state(uid: int) -> FSMContext:
    return FSMContext(storage=MemoryStorage(), key=StorageKey(bot_id=1, chat_id=uid, user_id=uid))


class _FakeUser:
    def __init__(self, uid, username=None):
        self.id = uid
        self.username = username


class _FakeChat:
    def __init__(self, cid):
        self.id = cid


class _FakeBotMe:
    def __init__(self, username="TestBot"):
        self.username = username


class _FakeBot:
    """`get_me()` — тот же фолбэк-приём, что `services/scheduler.py::_nudge_keyboard`."""

    def __init__(self, username="TestBot", fail=False):
        self._username = username
        self._fail = fail

    async def get_me(self):
        if self._fail:
            raise RuntimeError("get_me boom")
        return _FakeBotMe(self._username)


class _FakeMessage:
    def __init__(self, uid, username=None, bot=None):
        self.from_user = _FakeUser(uid, username)
        self.chat = _FakeChat(uid)
        self.bot = bot or _FakeBot()
        self.sent = []

    async def answer(self, text=None, reply_markup=None, parse_mode=None, *a, **k):
        self.sent.append((text, reply_markup, parse_mode))
        return None

    async def edit_reply_markup(self, reply_markup=None):
        return None

    def model_copy(self, update=None):
        new = _FakeMessage(self.from_user.id, self.from_user.username, bot=self.bot)
        new.sent = self.sent
        if update and "from_user" in update:
            new.from_user = update["from_user"]
        return new


class _FakeCallback:
    def __init__(self, data, uid, bot=None):
        self.data = data
        self.from_user = _FakeUser(uid)
        self.message = _FakeMessage(uid, bot=bot)
        self.bot = bot or self.message.bot
        self.answers = []

    async def answer(self, text=None, show_alert=False):
        self.answers.append((text, show_alert))
        return None


class FakeCommand:
    def __init__(self, args=None):
        self.args = args


def _texts(msg: _FakeMessage):
    return [t for (t, _, _) in msg.sent]


# ══════════════════════════════════════════════════════════════════════════════════════════
# Задача 1: extract_ambassador_ref / resolve_referrer / пропуск «Источника»
# ══════════════════════════════════════════════════════════════════════════════════════════

def test_amb_link_sets_referrer(tmp_path):
    _use_tmp_db(tmp_path)
    uid = UID + 1
    referrer = UID + 2

    async def go():
        await db.add_user({
            "telegram_id": referrer, "full_name": "Реферер Тестов",
            "registration_date": "2026-09-07",
        })
        state = _state(uid)
        msg = _FakeMessage(uid, "u")
        await reg.cmd_start(msg, state, bot=_FakeBot(), command=FakeCommand(f"amb_{referrer}"))
        return await state.get_data()

    data = asyncio.run(go())
    assert data.get("referrer_id") == referrer


def test_amb_self_link_ignored(tmp_path):
    _use_tmp_db(tmp_path)
    uid = UID + 3

    async def go():
        await db.add_user({
            "telegram_id": uid, "full_name": "Сам Себе Реферер",
            "registration_date": "2026-09-07",
        })
        state = _state(uid)
        msg = _FakeMessage(uid, "u")
        await reg.cmd_start(msg, state, bot=_FakeBot(), command=FakeCommand(f"amb_{uid}"))
        return await state.get_data()

    data = asyncio.run(go())
    assert data.get("referrer_id") is None


def test_amb_unknown_user_falls_through(tmp_path):
    _use_tmp_db(tmp_path)
    uid = UID + 4
    nonexistent = UID + 999

    async def go():
        state = _state(uid)
        msg = _FakeMessage(uid, "u")
        # Не должно падать и не должно проставлять referrer_id — обычный путь без ошибки.
        await reg.cmd_start(msg, state, bot=_FakeBot(), command=FakeCommand(f"amb_{nonexistent}"))
        return await state.get_data(), msg

    data, msg = asyncio.run(go())
    assert data.get("referrer_id") is None
    assert msg.sent, "делегат должен увидеть обычный экран приветствия, не тишину"


def test_source_step_skipped_for_referred_when_toggle_on(tmp_path):
    _use_tmp_db(tmp_path)

    async def go():
        await db.set_setting("reg_skip_source_for_referred", "on")
        return await reg_engine.enabled_steps({"referrer_id": 123, "participant_type": "full"})

    steps = asyncio.run(go())
    assert "source" not in steps


def test_source_step_asked_when_toggle_off(tmp_path):
    _use_tmp_db(tmp_path)

    async def go():
        # Дефолт off (D-06) — прежнее поведение YL/РилТолк.
        return await reg_engine.enabled_steps({"referrer_id": 123, "participant_type": "full"})

    steps = asyncio.run(go())
    assert "source" in steps


def test_referrer_must_be_ambassador_gate(tmp_path):
    _use_tmp_db(tmp_path)
    plain_referrer = UID + 10
    amb_referrer = UID + 11

    async def go():
        await db.add_user({
            "telegram_id": plain_referrer, "full_name": "Обычный Реферер",
            "registration_date": "2026-09-07",
        })
        await db.add_user({
            "telegram_id": amb_referrer, "full_name": "Амбассадор Реферер",
            "registration_date": "2026-09-07",
        })
        await db.update_user_answers(
            amb_referrer, {"is_ambassador": 1}, allowed_columns=["is_ambassador"],
        )
        await db.set_setting("reg_referrer_must_be_ambassador", "on")
        plain_result = await reg_engine.resolve_referrer(plain_referrer)
        amb_result = await reg_engine.resolve_referrer(amb_referrer)
        return plain_result, amb_result

    plain_result, amb_result = asyncio.run(go())
    assert plain_result is None
    assert amb_result == amb_referrer
