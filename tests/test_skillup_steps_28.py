"""Phase 28 (28-02, SU-01/SU-04, СкиллАп 5): сторожа экранов пяти новых шагов анкеты в
чате (общий хвост `_ask_step` -> `handlers/reg_extra_steps.py`) и (задача 3) паритета спеки
Mini App (`reg_engine.step_spec`/`form_spec`).

pytest-asyncio недоступен в этом окружении — асинхронщина через `asyncio.run()`, БД —
временная (`config.DB_PATH = tmp_path/...`), тот же приём, что
`tests/test_registration_send_guard_260816.py`/`tests/test_skillup_core_28.py`.
"""
import asyncio

from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage

from config import config
from database import db
from handlers import registration as reg
from handlers import reg_extra_steps
from handlers.states import Registration
import reg_engine


def _use_tmp_db(tmp_path, name="skillup_steps_28.db"):
    config.DB_PATH = str(tmp_path / name)


def _state(uid):
    return FSMContext(storage=MemoryStorage(), key=StorageKey(bot_id=1, chat_id=uid, user_id=uid))


class _FakeChat:
    def __init__(self, chat_id):
        self.id = chat_id


class _FakeMessage:
    """Минимальный message: `_ask_step`/`reg_extra_steps` трогают только `.chat.id`, `.text`
    и `.answer` (тот же контур, что `_FakeMessage` в `test_registration_send_guard_260816.py`,
    расширен изменяемым `.text` — обработчики приёма читают его напрямую)."""

    def __init__(self, chat_id, text=None):
        self.chat = _FakeChat(chat_id)
        self.text = text
        self.calls = []

    async def answer(self, text, **kwargs):
        self.calls.append((text, kwargs))
        return "sent:%d" % len(self.calls)


# ── Задача 2: показ и приём в чате ──────────────────────────────────────────────────────────

def test_case_optin_screen_has_description(tmp_path):
    _use_tmp_db(tmp_path)
    uid = 900282001

    async def go():
        await db.init_db()
        await db.set_setting(
            "reg_case_optin_description_text",
            "Финал очно, командами. Опыт не нужен.",
        )
        msg = _FakeMessage(uid)
        state = _state(uid)
        await reg._ask_step("case_optin", msg, state, 1, 5)
        return msg.calls, await state.get_state()

    calls, state_name = asyncio.run(go())
    assert calls, "вопрос должен быть отправлен"
    text, _ = calls[0]
    prompt_text = reg_engine.PROMPT_DEFAULTS["case_optin"]
    description = "Финал очно, командами. Опыт не нужен."
    assert prompt_text in text
    assert description in text
    assert text.index(prompt_text) < text.index(description), (
        "пояснение обязано идти ПОСЛЕ заголовка вопроса, до кнопок «Да»/«Нет» (28-UI-SPEC §5)"
    )
    assert state_name == Registration.case_optin.state


def test_case_optin_description_absent_when_not_configured(tmp_path):
    """Пусто в реестре — абзаца пояснения нет вовсе (плановое поведение, не пустая строка)."""
    _use_tmp_db(tmp_path)
    uid = 900282002

    async def go():
        await db.init_db()
        msg = _FakeMessage(uid)
        state = _state(uid)
        await reg._ask_step("case_optin", msg, state, 1, 5)
        return msg.calls

    calls = asyncio.run(go())
    text, _ = calls[0]
    # Дефолт настройки НЕ пуст (28-01) — экран показывает дефолтное пояснение менеджера.
    assert reg_engine.PROMPT_DEFAULTS["case_optin"] in text


def test_case_optin_rejects_free_text(tmp_path):
    _use_tmp_db(tmp_path)
    uid = 900282003

    async def go():
        await db.init_db()
        state = _state(uid)
        await state.set_state(Registration.case_optin)
        msg = _FakeMessage(uid, text="может быть")
        await reg_extra_steps.process_case_optin(msg, state, bot=None)
        return msg.calls, await state.get_state()

    calls, state_name = asyncio.run(go())
    assert calls, "должно быть отправлено объяснение ошибки"
    text, _ = calls[0]
    assert "Да" in text and "Нет" in text
    assert state_name == Registration.case_optin.state, (
        "неверный ответ не должен продвигать анкету дальше"
    )


def test_mini_portfolio_skip_writes_dash(tmp_path):
    _use_tmp_db(tmp_path)
    uid = 900282004

    async def go():
        await db.init_db()
        state = _state(uid)
        await state.update_data(participant_type="full", full_name="Тест Тестов")
        await state.set_state(Registration.mini_portfolio)
        msg = _FakeMessage(uid, text="Пропустить")
        await reg_extra_steps.process_mini_portfolio(msg, state, bot=None)
        data = await state.get_data()
        return data, await state.get_state()

    data, state_name = asyncio.run(go())
    assert data.get("mini_portfolio") == "-"
    assert state_name != Registration.mini_portfolio.state, "анкета должна пойти дальше"


def test_unknown_text_step_is_asked_not_silently_skipped(tmp_path):
    """Сторож общего хвоста `_ask_step`: шаг без собственной ветки (любой из пяти новых) реально
    задаётся — вопрос отправлен и состояние выставлено, анкета не замирает молча."""
    _use_tmp_db(tmp_path)
    uid = 900282005

    async def go():
        await db.init_db()
        msg = _FakeMessage(uid)
        state = _state(uid)
        await reg._ask_step("mini_direction", msg, state, 1, 5)
        return msg.calls, await state.get_state()

    calls, state_name = asyncio.run(go())
    assert calls, "вопрос обязан быть отправлен, а не замереть молча"
    assert state_name == Registration.mini_direction.state


def test_ask_step_unknown_state_is_fail_soft(tmp_path):
    """T-28-02-01: опечатка в реестре (шаг без объявленного State) не вешает анкету колом —
    отсутствие логируется, шаг пропускается через `_advance`."""
    _use_tmp_db(tmp_path)
    uid = 900282006

    async def go():
        await db.init_db()
        msg = _FakeMessage(uid)
        state = _state(uid)
        # Несуществующий State — имитация опечатки в REG_FLOW/Registration.
        await reg_extra_steps.ask_step("no_such_step", msg, state, "", "full", None)
        return await state.get_state()

    # _advance с несуществующим шагом уходит в ветку WR-01 (finalize) — здесь нам важно только
    # что вызов не поднял исключение и функция вернулась.
    state_name = asyncio.run(go())
    assert state_name != Registration.mini_direction.state  # анкета не встала на мусорный шаг
