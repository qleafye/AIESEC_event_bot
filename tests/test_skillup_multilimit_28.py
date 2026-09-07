"""Phase 28 (28-03, SU-02, A-06 CONTEXT): сторожа лимита мультивыбора — реестровый ключ
`reg_multi_max_<step>`, единый судья `reg_engine.validate_answer` (второй барьер для веб-
PATCH), гейт в чате (`handlers/reg_flow.py::process_multi_toggle/process_multi_done`) и спека
шага для Mini App (`reg_engine.step_spec` публикует `max_select`). pytest-asyncio недоступен —
async через `asyncio.run()`, фикстура временной БД — тот же приём, что
`tests/test_skillup_core_28.py::_ready(tmp_path)`.
"""
import asyncio

import pytest

from config import config
from database import db
import reg_engine
from handlers.states import Registration


def _ready(tmp_path, name="test_skillup_multilimit_28.db"):
    config.DB_PATH = str(tmp_path / name)
    asyncio.run(db.init_db())


async def _set(key, value):
    await db.set_setting(key, value)


# ── Задача 1: multi_max + validate_answer второй барьер + step_spec ────────────────────────

def test_no_limit_is_previous_behaviour(tmp_path):
    """Пустая настройка — три варианта проходят валидатор так же, как до фазы 28-03."""
    _ready(tmp_path)
    value, err = reg_engine.validate_answer("goal", ["А", "Б", "В"])
    assert err is None
    assert value == "А, Б, В"


def test_limit_rejects_extra(tmp_path):
    """Лимит 2, переданы три варианта — ошибка, текст называет число."""
    _ready(tmp_path)

    async def run():
        return reg_engine.validate_answer(
            "goal", ["А", "Б", "В"], max_select=2, limit_error_text="Можно выбрать не больше {max} вариантов.",
        )

    value, err = asyncio.run(run())
    assert value is None
    assert err is not None
    assert "2" in err


def test_limit_allows_exact(tmp_path):
    """Ровно на лимите (не больше) — успех."""
    _ready(tmp_path)
    value, err = reg_engine.validate_answer(
        "goal", ["А", "Б"], max_select=2, limit_error_text="Можно выбрать не больше {max} вариантов.",
    )
    assert err is None
    assert value == "А, Б"


def test_limit_none_is_byte_for_byte_previous_behaviour(tmp_path):
    """Дефолт `max_select=None` — старое поведение не сдвинулось: пустой список -> прежняя
    ошибка «Выбери хотя бы один вариант.», не текст лимита."""
    _ready(tmp_path)
    value, err = reg_engine.validate_answer("goal", [])
    assert value is None
    assert err == "Выбери хотя бы один вариант."


def test_multi_max_reads_registry_int(tmp_path):
    """`reg_engine.multi_max` читает `reg_multi_max_<step>` — int больше нуля, иначе None."""
    _ready(tmp_path)

    async def run():
        before = await reg_engine.multi_max("stack")
        await db.set_setting("reg_multi_max_stack", "5")
        after = await reg_engine.multi_max("stack")
        await db.set_setting("reg_multi_max_stack", "0")
        zeroed = await reg_engine.multi_max("stack")
        await db.set_setting("reg_multi_max_stack", "garbage")
        garbage = await reg_engine.multi_max("stack")
        return before, after, zeroed, garbage

    before, after, zeroed, garbage = asyncio.run(run())
    assert before is None
    assert after == 5
    assert zeroed is None
    assert garbage is None


def test_step_spec_publishes_max_select(tmp_path):
    """Без настройки — `max_select` есть в спеке и равен `None` (существующие мультивыборы не
    меняются). С настройкой — число, счётчик и подсказка формата подставлены в help."""
    _ready(tmp_path)

    async def run():
        spec_before = await reg_engine.step_spec("goal")
        await db.set_setting("reg_multi_max_goal", "2")
        spec_after = await reg_engine.step_spec("goal")
        return spec_before, spec_after

    spec_before, spec_after = asyncio.run(run())
    assert spec_before["max_select"] is None
    assert "limit_counter_text" not in spec_before

    assert spec_after["max_select"] == 2
    assert spec_after["limit_counter_text"] == "Выбрано {selected} из 2"
    assert "2" in spec_after["help"]


# ── Задача 2: гейт лимита в чате + второй барьер веб-PATCH ──────────────────────────────────

class _FakeMessage:
    def __init__(self):
        self.edit_calls = 0

    async def edit_reply_markup(self, reply_markup=None):
        self.edit_calls += 1


class _FakeCallback:
    def __init__(self, data, uid=900280099):
        self.data = data
        self.message = _FakeMessage()
        self.from_user = type("U", (), {"id": uid, "language_code": "ru"})()
        self.answer_calls = []

    async def answer(self, text=None, show_alert=False):
        self.answer_calls.append((text, show_alert))


class _FakeState:
    def __init__(self, data):
        self._data = dict(data)

    async def get_data(self):
        return dict(self._data)

    async def update_data(self, **kwargs):
        self._data.update(kwargs)

    async def set_state(self, state=None):
        self._data["_state"] = state


def test_bot_toggle_blocks_over_limit(tmp_path):
    """Тап шестого варианта при лимите 5 не меняет `_multi_stack` в FSM и отдаёт алерт."""
    from handlers import reg_flow

    _ready(tmp_path)

    async def run():
        await db.set_setting("reg_multi_max_stack", "5")
        options = await reg_engine.options("stack")
        state = _FakeState({"_multi_stack": list(range(5))})
        callback = _FakeCallback(f"regmulti:stack:5")
        await reg_flow.process_multi_toggle(callback, state)
        data = await state.get_data()
        return data.get("_multi_stack"), callback.answer_calls, len(options)

    selected, answer_calls, n_options = asyncio.run(run())
    assert selected == list(range(5)), "выбор не должен был измениться"
    assert len(answer_calls) == 1
    text, show_alert = answer_calls[0]
    assert show_alert is True
    assert text  # объясняет лимит, а не молчит


def test_bot_toggle_allows_deselect_at_limit(tmp_path):
    """На лимите снятие уже выбранного варианта разрешено всегда."""
    from handlers import reg_flow

    _ready(tmp_path)

    async def run():
        await db.set_setting("reg_multi_max_stack", "5")
        state = _FakeState({"_multi_stack": list(range(5))})
        callback = _FakeCallback("regmulti:stack:4")
        await reg_flow.process_multi_toggle(callback, state)
        data = await state.get_data()
        return data.get("_multi_stack"), callback.answer_calls

    selected, answer_calls = asyncio.run(run())
    assert selected == [0, 1, 2, 3]
    # снятие выбора не должно отдавать алерт лимита (обычный ответ callback.answer() без текста)
    assert not any(show_alert for _text, show_alert in answer_calls)


def test_web_patch_rejects_over_limit(tmp_path):
    """`validate_answer` со свежим лимитом отклоняет PATCH шести вариантов текстом с числом —
    второй барьер работает независимо от клавиатуры бота."""
    _ready(tmp_path)

    async def run():
        await db.set_setting("reg_multi_max_stack", "5")
        limit = await reg_engine.multi_max("stack")
        limit_text = await db.get_setting("reg_multi_limit_error_text")
        options = await reg_engine.options("stack")
        chosen = options[:6]
        return reg_engine.validate_answer(
            "stack", chosen, max_select=limit, limit_error_text=limit_text,
        )

    value, err = asyncio.run(run())
    assert value is None
    assert "5" in err
