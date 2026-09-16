"""Приёмка 17.09 (находки 2 и 3): единый источник правды «сколько вариантов нужно выбрать» на
multi-шаге — `reg_engine.multi_min_select`/`multi_requirement_hint`, публикуется в
`step_spec()["min_select"]`/`["help"]`/`["pick_min_text"]`/`["v2_texts"]["pick_min"]` и той же
функцией дописывается в текст вопроса чата (`handlers/registration.py`). Стиль — тот же приём,
что `tests/test_skillup_multilimit_28.py` (async через `asyncio.run()`, временная БД)."""
import asyncio

import pytest
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage

from config import config
from database import db
import reg_engine
from handlers import registration as reg


def _ready(tmp_path, name="test_reg_form_multi_min_260917.db"):
    config.DB_PATH = str(tmp_path / name)
    asyncio.run(db.init_db())


# ── multi_min_select: единая точка правды ───────────────────────────────────────────────────

def test_multi_min_select_is_one_for_any_step_not_in_skip_allowed():
    assert reg_engine.multi_min_select("formats") == 1
    assert reg_engine.multi_min_select("goal") == 1
    assert reg_engine.multi_min_select("stack") == 1


def test_multi_min_select_is_zero_for_hypothetical_skip_allowed_step(monkeypatch):
    """Сегодня ни один multi-шаг не входит в `_SKIP_ALLOWED_STEPS` (см.
    `test_reg_flow_skip_parity_260917.py::test_no_multi_step_is_skippable_today`) — эмулируем
    будущий случай через monkeypatch, не заводя реальный skip-allowed multi-шаг раньше срока."""
    monkeypatch.setattr(reg_engine, "_SKIP_ALLOWED_STEPS", reg_engine._SKIP_ALLOWED_STEPS | {"formats"})
    assert reg_engine.multi_min_select("formats") == 0


# ── multi_requirement_hint: одна строка на минимум+максимум ────────────────────────────────

def test_requirement_hint_min_only_without_limit(tmp_path):
    _ready(tmp_path)
    hint = asyncio.run(reg_engine.multi_requirement_hint("formats"))
    assert hint == "Отметь вариантов: не меньше 1."


def test_requirement_hint_merges_min_and_max_into_one_line(tmp_path):
    _ready(tmp_path)

    async def run():
        await db.set_setting("reg_multi_max_goal", "2")
        return await reg_engine.multi_requirement_hint("goal")

    hint = asyncio.run(run())
    assert hint == "Выбери от 1 до 2."


def test_requirement_hint_empty_when_no_min_and_no_max(monkeypatch, tmp_path):
    """Гипотетический необязательный multi без лимита — подсказки нет вовсе (нечего требовать)."""
    _ready(tmp_path)
    monkeypatch.setattr(reg_engine, "_SKIP_ALLOWED_STEPS", reg_engine._SKIP_ALLOWED_STEPS | {"formats"})
    hint = asyncio.run(reg_engine.multi_requirement_hint("formats"))
    assert hint == ""


# ── step_spec: min_select/help/pick_min_text/v2_texts.pick_min ─────────────────────────────

def test_step_spec_publishes_min_select_and_help_hint(tmp_path):
    _ready(tmp_path)
    spec = asyncio.run(reg_engine.step_spec("formats"))
    assert spec["min_select"] == 1
    assert "Отметь вариантов: не меньше 1." in spec["help"]
    assert spec["pick_min_text"] == "Выбери минимум 1"


def test_step_spec_help_combines_min_and_max_not_two_lines(tmp_path):
    _ready(tmp_path)

    async def run():
        await db.set_setting("reg_multi_max_goal", "2")
        return await reg_engine.step_spec("goal")

    spec = asyncio.run(run())
    assert spec["min_select"] == 1
    assert spec["max_select"] == 2
    # Одна строка «от-до», не отдельная строка лимита (владелец 17.09: «объедини в одну
    # строку») — старый текст лимита-без-минимума в help отсутствует.
    assert "Выбери от 1 до 2." in spec["help"]
    assert "Можно выбрать до 2." not in spec["help"]


def test_step_spec_v2_texts_pick_min_is_raw_template(tmp_path):
    """`v2_texts.pick_min` — сырой шаблон с `{min}` (подстановка на клиенте, тот же приём, что
    `limit_hint_zero/mid/max`) — `_v2_texts_for` считается до того, как известен `min_select`
    конкретного шага, второй async-параметр под это заводить не стали."""
    _ready(tmp_path)

    async def run():
        await db.set_setting("reg_form_v2_enabled", "on")
        return await reg_engine.step_spec("formats")

    spec = asyncio.run(run())
    if spec["degraded_kind"] == "multi":
        assert spec["v2_texts"]["pick_min"] == "Выбери минимум {min}"


@pytest.mark.parametrize("step_key", ["stack", "goal", "formats"])
def test_step_spec_min_select_matches_multi_min_select_helper(tmp_path, step_key):
    _ready(tmp_path)
    spec = asyncio.run(reg_engine.step_spec(step_key))
    assert spec["min_select"] == reg_engine.multi_min_select(step_key)


# ── чат: та же подсказка дописана к тексту вопроса ─────────────────────────────────────────

class _FakeChat:
    def __init__(self, chat_id):
        self.id = chat_id


class _FakeMessage:
    """Минимальный message: `_ask_step` трогает только `.chat.id` и `.answer`."""

    def __init__(self, chat_id):
        self.chat = _FakeChat(chat_id)
        self.calls = []

    async def answer(self, text, **kwargs):
        self.calls.append((text, kwargs))
        return "sent:%d" % len(self.calls)


def _state(uid):
    return FSMContext(storage=MemoryStorage(), key=StorageKey(bot_id=1, chat_id=uid, user_id=uid))


def test_chat_multi_step_message_includes_requirement_hint(tmp_path):
    """Приёмка 17.09 (находка 3, «если в чате та же проблема, добавь подсказку и там тем же
    текстом»): чат раньше не показывал минимум/максимум мультивыбора вовсе — теперь дописывает
    ту же строку, что видит делегат в приложении (`spec.help`)."""
    _ready(tmp_path)

    async def scenario():
        state = _state(880101)
        await state.update_data(participant_type="full")
        msg = _FakeMessage(880101)
        await reg._ask_step("formats", msg, state, 1, 10)
        text, _kwargs = msg.calls[0]
        assert "Отметь вариантов: не меньше 1." in text

    asyncio.run(scenario())


# ── choice-chips/yesno: «Дальше» неактивна, пока ничего не выбрано (находка 3) ─────────────

def test_step_spec_publishes_pick_option_text_for_required_choice_step(tmp_path):
    _ready(tmp_path)
    spec = asyncio.run(reg_engine.step_spec("alumni_status"))
    if spec["type"] in ("choice-chips", "yesno"):
        assert spec["required"] is True
        assert spec["pick_option_text"] == "Выбери вариант"


def test_step_spec_omits_pick_option_text_for_non_choice_types(tmp_path):
    """Текстовые/дата/мульти-шаги не получают поле вовсе — контрол этого типа его не читает,
    но явная проверка ловит случайную утечку поля не туда, куда планировалось."""
    _ready(tmp_path)
    spec = asyncio.run(reg_engine.step_spec("age"))
    assert "pick_option_text" not in spec


def test_chat_multi_step_message_merges_min_and_max_hint(tmp_path):
    _ready(tmp_path)

    async def scenario():
        await db.set_setting("reg_multi_max_goal", "2")
        state = _state(880102)
        await state.update_data(participant_type="full")
        msg = _FakeMessage(880102)
        await reg._ask_step("goal", msg, state, 1, 10)
        text, _kwargs = msg.calls[0]
        assert "Выбери от 1 до 2." in text
        assert "Можно выбрать до 2." not in text

    asyncio.run(scenario())
