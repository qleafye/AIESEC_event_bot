"""Quick 260917 (перф-приёмка, горячий путь анкеты — чат бота): один ответ делегата на
текстовый/choice/date-шаг зовёт `handlers/registration.py::_advance`, а тот —
`reg_engine.enabled_steps` (цикл по ~51 REG_FLOW-ключу, `get_setting_typed` на каждый) и
`_ask_step` (`reg_engine.form_v2_flags` — ещё девять `get_setting_typed_for_city` подряд, плюс
`prompt()`/опции следующего вопроса) — до фикса 60-75 отдельных SQLite-соединений НА ОДИН ОТВЕТ
делегата (сезон 1000-1500 человек, вечерние пики — CLAUDE.md constraint).

Фикс — `database.db.settings_snapshot()` вокруг `_advance` (тонкая обёртка, `_advance_impl`
хвост) и вокруг `reg_engine.enabled_steps`/`form_v2_flags` по отдельности (та же функция может
быть вызвана и без внешнего снимка — `services/scheduler.py`, `miniapp/routers/profile.py`,
`handlers/reg_resume.py`). Ни один вызов в подграфе `_advance` (reg_flow/reg_steps/
reg_extra_steps/reg_types_*/reg_resume_fork) не порождает asyncio.create_task/ensure_future —
проверено статически ниже, а не только докстрингом.

Харнесс — тот же, что `tests/test_registration_send_guard_260816.py` (FakeMessage/FSMContext,
БД — tmp_path, asyncio.run(), pytest-asyncio недоступен)."""
from __future__ import annotations

import asyncio
import re
from pathlib import Path

from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage

from config import config
from database import db as bot_db
from handlers.states import Registration

MAX_CONNECTS_PER_STEP_ANSWER = 15
# /start для НОВОГО делегата (без deep-link) на этом сценарии ни разу не доходит до
# `_ask_step`/`enabled_steps` внутри ОДНОГО вызова cmd_start (реально шлёт только приветствие +
# экран выбора города/трека) -- снимок из этого плана его пассивно не касается, замер
# «было»/«стало» здесь ОДИНАКОВ (36). Сам cmd_start (~300 строк, вперемешку settings-чтения и
# Telegram-отправки) сознательно НЕ обёрнут снимком целиком в этом плане -- см. докстринг
# `_advance`. Порог фиксирует ТЕКУЩЕЕ число как потолок регрессии, не как «после фикса».
MAX_CONNECTS_START = 40


class _ConnectCounter:
    def __init__(self):
        self.n = 0
        self._orig = bot_db._connect

    def __enter__(self):
        counter = self

        def counting_connect():
            counter.n += 1
            return counter._orig()

        bot_db._connect = counting_connect
        return self

    def __exit__(self, *exc):
        bot_db._connect = self._orig


def _run(coro):
    return asyncio.run(coro)


def _use_tmp_db(tmp_path, name="registration_hotpath_perf.db"):
    config.DB_PATH = str(tmp_path / name)
    _run(bot_db.init_db())


def _state(uid):
    return FSMContext(storage=MemoryStorage(), key=StorageKey(bot_id=1, chat_id=uid, user_id=uid))


class _FakeChat:
    def __init__(self, chat_id):
        self.id = chat_id


class _FakeMessage:
    """Минимальный message — _advance/_ask_step трогают только .chat.id и .answer/
    .answer_document (тот же контракт, что test_registration_send_guard_260816.py::_FakeMessage)."""

    def __init__(self, chat_id, text=""):
        self.chat = _FakeChat(chat_id)
        self.text = text
        self.from_user = None
        self.calls = []

    async def answer(self, text, **kwargs):
        self.calls.append((text, kwargs))
        return "sent:%d" % len(self.calls)

    async def answer_document(self, *a, **kw):
        self.calls.append(("doc", kw))


def test_text_step_answer_uses_bounded_connections_not_n_plus_one(tmp_path):
    _use_tmp_db(tmp_path)
    from handlers import reg_steps

    state = _state(1)
    _run(state.update_data(full_name="Иван Иванов", participant_type="full", _reg_step=1, _reg_total=15))
    _run(state.set_state(Registration.age))
    msg = _FakeMessage(1, text="25")

    with _ConnectCounter() as counter:
        _run(reg_steps.process_age(msg, state, bot=None))

    assert counter.n <= MAX_CONNECTS_PER_STEP_ANSWER, (
        f"{counter.n} SQLite-соединений на один текстовый ответ делегата (process_age) — "
        f"снимок bot_settings не работает (N+1 регрессия, было 74 до фикса 260917)"
    )


def test_choice_step_answer_uses_bounded_connections(tmp_path):
    _use_tmp_db(tmp_path)
    from handlers import reg_steps

    state = _state(2)
    _run(state.update_data(full_name="Пётр Петров", participant_type="full", age="25", _reg_step=2, _reg_total=15))
    _run(state.set_state(Registration.city))
    msg = _FakeMessage(2, text="Москва")

    with _ConnectCounter() as counter:
        _run(reg_steps.process_city(msg, state, bot=None))

    assert counter.n <= MAX_CONNECTS_PER_STEP_ANSWER, (
        f"{counter.n} SQLite-соединений на choice-ответ (process_city) — было 63 до фикса 260917"
    )


def test_date_step_answer_uses_bounded_connections(tmp_path):
    _use_tmp_db(tmp_path)
    from handlers import reg_flow

    state = _state(4)
    _run(state.update_data(
        full_name="Олег Олегов", participant_type="full", _reg_step=8, _reg_total=15,
        _current_date_step="payment_plan_date",
    ))
    _run(state.set_state(Registration.date_input))
    msg = _FakeMessage(4, text="01.10.2026")

    with _ConnectCounter() as counter:
        _run(reg_flow.process_date_input(msg, state, bot=None))

    assert counter.n <= MAX_CONNECTS_PER_STEP_ANSWER, (
        f"{counter.n} SQLite-соединений на date-ответ (process_date_input) — было 62 до фикса 260917"
    )


def test_start_for_new_delegate_uses_bounded_connections(tmp_path):
    """`/start` не обёрнут снимком целиком (см. докстринг `_advance` — cmd_start вперемешку
    читает настройки и шлёт Telegram-сообщения на ~300 строках, обёртывать весь хендлер вне
    приоритета этого плана), но пассивно выигрывает от снимка внутри `enabled_steps`/
    `form_v2_flags`, которые он зовёт ниже по цепочке (`_city_fork_then_continue` ->
    `_ask_step_or_recall`)."""
    _use_tmp_db(tmp_path)
    from handlers import registration as reg

    state = _state(5)
    msg = _FakeMessage(5, text="/start")
    msg.from_user = type("U", (), {"id": 5, "username": "newdelegate", "full_name": "New Delegate"})()

    with _ConnectCounter() as counter:
        _run(reg.cmd_start(msg, state, bot=None, command=None))

    assert counter.n <= MAX_CONNECTS_START, (
        f"{counter.n} SQLite-соединений на /start нового делегата — регрессия сверх текущего "
        f"потолка (36, этот путь не задет фиксом 260917 — см. комментарий у MAX_CONNECTS_START)"
    )


def test_finalize_data_uses_bounded_connections(tmp_path):
    """`services.reg_finalize.finalize_data` — данные без сети (Sheets/Nextcloud живут в
    `post_finalize`, вне этого замера, см. докстринг `finalize_data`)."""
    _use_tmp_db(tmp_path)
    from services.reg_finalize import finalize_data

    draft = {
        "telegram_id": 6001, "kind": "new", "updated_by": "bot",
        "answers": {
            "full_name": "Новый Делегат", "age": "20", "email": "a@example.com",
            "phone": "+70000000000", "city": "Москва", "participant_type": "full",
            "event_city": None,
        },
    }

    with _ConnectCounter() as counter:
        result = _run(finalize_data(6001, "@newdelegate", draft))

    assert result["mode"] == "new"
    assert counter.n <= 20, (
        f"{counter.n} SQLite-соединений на finalize_data (без Sheets/Nextcloud) — многовато "
        f"для чисто-данных функции"
    )


# ── Статическая проверка: ни один узел горячего пути не порождает фоновую задачу ────────────

_HOTPATH_FILES = [
    "handlers/registration.py",
    "handlers/reg_flow.py",
    "handlers/reg_steps.py",
    "handlers/reg_extra_steps.py",
    "handlers/reg_resume_fork.py",
    "handlers/reg_types_lookup.py",
    "handlers/reg_types_composite.py",
    "handlers/reg_types_repeatable.py",
    "services/reg_finalize.py",
    "reg_engine.py",
]

_TASK_SPAWN_RE = re.compile(r"create_task\(|ensure_future\(|\.spawn\(")


def test_hotpath_files_never_spawn_background_tasks():
    """Статический гвард (не только докстринг): снимок bot_settings, наброшенный на
    `_advance`/`enabled_steps`/`form_v2_flags`/`finalize_data`/`_draft_response`/`hub`,
    безопасен ТОЛЬКО пока ни один вызов внутри их подграфа не создаёт задачу, переживающую
    закрытие снимка (см. `tests/test_reg_miniapp_perf_260917.py::
    test_snapshot_does_not_leak_into_spawned_background_task` — механизм утечки). Если этот
    тест когда-нибудь покраснеет — до оборачивания новой функции в settings_snapshot() нужно
    сначала убедиться, что порождённая задача создаётся ВНЕ снимка (или явно сбрасывает
    ContextVar на старте)."""
    root = Path(__file__).resolve().parents[1]
    offenders = []
    for rel in _HOTPATH_FILES:
        path = root / rel
        if not path.exists():
            continue
        text = path.read_text(encoding="utf-8")
        if _TASK_SPAWN_RE.search(text):
            offenders.append(rel)
    assert not offenders, (
        f"create_task/ensure_future/.spawn( обнаружен в горячем пути анкеты: {offenders} — "
        "снимок settings_snapshot() на _advance/enabled_steps/form_v2_flags/finalize_data "
        "рискует утечь в фоновую задачу (см. докстринг теста)"
    )
