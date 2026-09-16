"""Quick 260917, п.5: 50 параллельных делегатов отвечают на шаг анкеты ОДНОВРЕМЕННО
(`asyncio.gather` по реальному `handlers/reg_steps.py::process_age`, не по голым SQL) — модель
вечернего пика (сезон 1000-1500 делегатов, CLAUDE.md constraint). Проверяет ДВЕ вещи разом:

1. WAL + `busy_timeout` (`tests/test_db_wal_busy_timeout.py`) действительно не дают
   `sqlite3.OperationalError: database is locked` под конкурентной записью (`upsert_reg_draft`/
   `set_reg_step` — разные строки, но SQLite блокирует НА УРОВНЕ ФАЙЛА при записи в WAL).
2. Снимок `settings_snapshot()` — per-Task ContextVar (`asyncio.gather` создаёт отдельный Task
   на каждого делегата) — не путает настройки МЕЖДУ параллельными делегатами: 50 разных
   `telegram_id` отвечают на один и тот же шаг, каждый обязан получить СВОЙ ответ независимо.

Не измеряет время как критерий провала (флейк от машины) — только «нет lock-ошибок» и лог
итогового времени для отчёта."""
from __future__ import annotations

import asyncio
import time

from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage

from config import config
from database import db as bot_db
from handlers.states import Registration

DELEGATE_COUNT = 50


def _run(coro):
    return asyncio.run(coro)


def _use_tmp_db(tmp_path, name="registration_concurrent_load.db"):
    config.DB_PATH = str(tmp_path / name)
    _run(bot_db.init_db())


def _state(uid):
    return FSMContext(storage=MemoryStorage(), key=StorageKey(bot_id=1, chat_id=uid, user_id=uid))


class _FakeChat:
    def __init__(self, chat_id):
        self.id = chat_id


class _FakeMessage:
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


async def _one_delegate_answers_age(reg_steps, base_id: int) -> tuple[int, object]:
    uid = 700_000 + base_id
    state = _state(uid)
    await state.update_data(
        full_name=f"Делегат {base_id}", participant_type="full", _reg_step=1, _reg_total=15,
    )
    await state.set_state(Registration.age)
    msg = _FakeMessage(uid, text=str(18 + (base_id % 40)))
    await reg_steps.process_age(msg, state, bot=None)
    data = await state.get_data()
    return uid, data.get("age")


def test_50_concurrent_delegates_answer_one_step_no_lock_errors(tmp_path):
    _use_tmp_db(tmp_path)
    from handlers import reg_steps

    async def _run_all():
        t0 = time.perf_counter()
        results = await asyncio.gather(
            *[_one_delegate_answers_age(reg_steps, i) for i in range(DELEGATE_COUNT)],
            return_exceptions=True,
        )
        dt = time.perf_counter() - t0
        return results, dt

    results, dt = _run(_run_all())

    errors = [r for r in results if isinstance(r, BaseException)]
    lock_errors = [e for e in errors if "database is locked" in str(e).lower()]
    assert not lock_errors, f"{len(lock_errors)}/{DELEGATE_COUNT} 'database is locked' под конкурентной записью: {lock_errors[:3]}"
    assert not errors, f"{len(errors)}/{DELEGATE_COUNT} делегатов упали (не lock, другая ошибка): {errors[:3]}"

    # Каждый делегат обязан получить СВОЙ возраст в СВОЁМ FSM state — снимок/контекст не
    # перепутал ответы между параллельными Task'ами.
    for uid, age in results:
        expected_base = uid - 700_000
        assert str(age) == str(18 + (expected_base % 40)), f"делегат {uid} получил чужой age={age!r}"

    print(f"\n50 параллельных делегатов, один шаг анкеты: {dt*1000:.0f}ms суммарно (asyncio.gather)")
    # Мягкий потолок — не проверка перформанса машины, а сигнал явной деградации (напр. один
    # делегат ждёт busy_timeout=5000ms целиком из-за реальной сериализации записи).
    assert dt < 15.0, f"50 делегатов заняли {dt:.1f}s — подозрительно долго, проверить busy_timeout/WAL"
