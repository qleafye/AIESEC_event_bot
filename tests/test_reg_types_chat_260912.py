"""Phase 30 (30-06, задачи 1-3, A2-03/A2-04/A2-05): чат-проекция трёх сложных типов —
`lookup`/`composite`/`repeatable`. pytest-asyncio недоступен в этом окружении — асинхронщина
через `asyncio.run()`, БД — временная (`config.DB_PATH = tmp_path/...`), Fake-объекты aiogram —
тот же приём, что `tests/test_skillup_resume_fork_ui_28.py`/`tests/test_reg_resume_draft.py`.

Диспетчер (план 30-06, задача 4) — точка входа для всех сценариев ниже: тесты гоняют
`handlers.registration._ask_step(...)`, а не сразу модульные функции, чтобы заодно проверить
саму врезку в `_ask_step`.

Задача 1 (A2-03, `handlers/reg_types_lookup.py`): «напиши первые буквы» → до пяти кнопок
совпадений + «Другое». Задачи 2/3 (composite/repeatable) дописываются последующими коммитами
этого же плана.
"""
import asyncio

from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import InlineKeyboardMarkup

from config import config
from database import db
from handlers import registration as reg
from handlers import reg_types_lookup
from handlers.states import Registration

UID = 300912000


def _use_tmp_db(tmp_path, name="test_reg_types_chat_260912.db"):
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
    """Минимальный message — обработчики трогают только `.chat.id`/`.from_user`/`.text`/
    `.answer` (тот же контур, что `tests/test_skillup_resume_fork_ui_28.py`)."""

    def __init__(self, uid, text=None):
        self.chat = _FakeChat(uid)
        self.from_user = _FakeUser(uid)
        self.text = text
        self.sent = []

    async def answer(self, text=None, reply_markup=None, parse_mode=None, *a, **k):
        self.sent.append((text, reply_markup, parse_mode))
        return "sent:%d" % len(self.sent)

    async def edit_reply_markup(self, reply_markup=None):
        return None

    def model_copy(self, update=None):
        new = _FakeMessage(self.from_user.id, text=self.text)
        new.sent = self.sent
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


def _kbs(msg: _FakeMessage):
    return [rm for (_, rm, _) in msg.sent if isinstance(rm, InlineKeyboardMarkup)]


def _callback_datas(kb: InlineKeyboardMarkup):
    return [btn.callback_data for row in kb.inline_keyboard for btn in row if btn.callback_data]


async def _insert_lookup(kind: str, canonical: str, alias: str) -> None:
    from services.lookup import normalize_alias
    from database.db import _connect

    async with _connect() as conn:
        await conn.execute(
            "INSERT OR IGNORE INTO lookup_entries "
            "(kind, canonical, alias, alias_norm, source, pinned, created_at) "
            "VALUES (?, ?, ?, ?, 'test', 0, '2026-09-12 00:00:00')",
            (kind, canonical, alias, normalize_alias(alias)),
        )
        await conn.commit()


async def _enable_v2(**extra):
    await db.set_setting("reg_form_v2_enabled", "on")
    for key, value in extra.items():
        await db.set_setting(key, value)


# ── lookup (задача 1, A2-03) ─────────────────────────────────────────────────────────────────

def test_lookup_disabled_falls_back_to_legacy_branch(tmp_path):
    """v2 выключен целиком — university остаётся сегодняшней веткой `_ask_step` (список/текст),
    не швом reg_types_lookup (инвариант «выключенный v2 = поведение прежнее»)."""
    _use_tmp_db(tmp_path)
    uid = UID + 1

    async def go():
        state = _state(uid)
        await state.update_data(participant_type="full")
        await reg._ask_step("university", _FakeMessage(uid), state, 3, 10)
        return await state.get_state()

    state_name = asyncio.run(go())
    assert state_name == Registration.university.state


def test_lookup_ask_step_shows_hint_and_sets_dedicated_state(tmp_path):
    _use_tmp_db(tmp_path)
    uid = UID + 2

    async def go():
        await _enable_v2(reg_form_lookup_search="on")
        state = _state(uid)
        await state.update_data(participant_type="full")
        msg = _FakeMessage(uid)
        await reg._ask_step("university", msg, state, 3, 10)
        return msg, await state.get_state()

    msg, state_name = asyncio.run(go())
    assert msg.sent, "вопрос шага обязан быть задан"
    assert state_name == reg_types_lookup._LookupChat.waiting.state


def test_lookup_search_returns_matches_and_pick_stores_canonical(tmp_path):
    _use_tmp_db(tmp_path)
    uid = UID + 3

    async def go():
        await _enable_v2(reg_form_lookup_search="on")
        # Уникальные фикстуры (не пересекаются с реальным офлайн-сидом `seed_lookup_from_snapshot`,
        # который тоже накатывается в init_db() — коллизия alias_norm иначе отдаёт чужую каноники).
        await _insert_lookup("university", "Тестовый Институт Кода №260912", "институткода260912")
        state = _state(uid)
        await state.update_data(participant_type="full", _reg_step=3, _reg_total=10)
        ask_msg = _FakeMessage(uid)
        await reg._ask_step("university", ask_msg, state, 3, 10)

        search_msg = _FakeMessage(uid, text="институткода260912")
        await reg_types_lookup.receive_lookup_text(search_msg, state, bot=None)
        kbs = _kbs(search_msg)
        assert kbs, "результаты поиска обязаны прийти с кнопками"
        callback_datas = _callback_datas(kbs[0])
        assert any(cd.startswith("reglookup:pick:") for cd in callback_datas)
        assert "reglookup:other" in callback_datas

        callback = _FakeCallback("reglookup:pick:0", uid)
        # у callback-хендлера свой `_FakeMessage` — стадия ищется в FSM data, не в объекте msg
        await reg_types_lookup.reglookup_pick(callback, state, bot=None)
        data = await state.get_data()
        return data

    data = asyncio.run(go())
    assert data.get("university") == "Тестовый Институт Кода №260912"


def test_lookup_empty_result_offers_other_button(tmp_path):
    _use_tmp_db(tmp_path)
    uid = UID + 4

    async def go():
        await _enable_v2(reg_form_lookup_search="on")
        state = _state(uid)
        await state.update_data(participant_type="full")
        await reg._ask_step("university", _FakeMessage(uid), state, 1, 5)
        search_msg = _FakeMessage(uid, text="ЗЗЗЗЗЗ")
        await reg_types_lookup.receive_lookup_text(search_msg, state, bot=None)
        return search_msg

    msg = asyncio.run(go())
    kbs = _kbs(msg)
    assert kbs
    assert _callback_datas(kbs[0]) == ["reglookup:other"]


def test_lookup_other_button_then_free_text_enqueues_merge(tmp_path):
    _use_tmp_db(tmp_path)
    uid = UID + 5

    async def go():
        await _enable_v2(reg_form_lookup_search="on")
        state = _state(uid)
        await state.update_data(participant_type="full")
        await reg._ask_step("university", _FakeMessage(uid), state, 1, 5)
        search_msg = _FakeMessage(uid, text="Незнаневедомыйвуз")
        await reg_types_lookup.receive_lookup_text(search_msg, state, bot=None)

        other_cb = _FakeCallback("reglookup:other", uid)
        await reg_types_lookup.reglookup_pick(other_cb, state, bot=None)

        free_text_msg = _FakeMessage(uid, text="Мой институт мечты")
        await reg_types_lookup.receive_lookup_text(free_text_msg, state, bot=None)
        data = await state.get_data()

        from database.db import _connect
        async with _connect() as conn:
            cursor = await conn.execute(
                "SELECT raw_text, status FROM lookup_merge_queue WHERE kind = 'university'"
            )
            rows = await cursor.fetchall()
        return data, rows

    data, rows = asyncio.run(go())
    assert data.get("university") == "Мой институт мечты"
    assert rows and rows[0][0] == "Мой институт мечты" and rows[0][1] == "new"
