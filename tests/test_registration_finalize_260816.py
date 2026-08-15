"""Ночное ревью registration.md, находки #3 и #4 (quick 260816-3av).

#3 — re-entrancy: состояние Registration.confirm живёт до ~20 секунд (внутри finalize идёт
загрузка резюме в Nextcloud с таймаутом 20 с), а reply-клавиатура «Всё верно» никуда не
девается. Двойной тап давал два параллельных finalize → две строки в users/Google-таблице.

#4 — `await add_user(data)` был единственным неогороженным await в finalize. Падение SQLite
(«database is locked») уходило в глобальный @dp.errors() (main.py), который его молча глотал:
делегат не видел ничего, заявка не сохранялась, и он считал, что зарегистрировался.

pytest-asyncio в этом окружении нет — asyncio.run() + временная БД, как в соседних файлах.
"""
import asyncio
import logging
import sqlite3

import pytest

from config import config
from database import db
from handlers import registration as reg


class _FakeFromUser:
    def __init__(self, telegram_id, username="tester"):
        self.id = telegram_id
        self.username = username


class _FakeChat:
    def __init__(self, chat_id):
        self.id = chat_id


class _FakeMessage:
    def __init__(self, telegram_id):
        self.from_user = _FakeFromUser(telegram_id)
        self.chat = _FakeChat(telegram_id)
        self.answers = []

    async def answer(self, *args, **kwargs):
        self.answers.append((args, kwargs))


class _FakeState:
    def __init__(self, data):
        self._data = data
        self.cleared = 0

    async def get_data(self):
        return dict(self._data)

    async def clear(self):
        self.cleared += 1


def _patch_appenders(monkeypatch):
    """Заглушить оба аппендера Google-таблицы и вернуть их счётчики вызовов."""
    named_calls = []
    main_calls = []

    async def fake_named(tab_name, data):
        named_calls.append((tab_name, data))

    async def fake_main(data):
        main_calls.append(data)

    monkeypatch.setattr(reg, "append_to_named_sheet", fake_named)
    monkeypatch.setattr(reg, "append_to_sheet", fake_main)
    return named_calls, main_calls


def _offline(tmp_path, monkeypatch, name):
    config.DB_PATH = str(tmp_path / name)
    monkeypatch.setattr(config, "GOOGLE_SHEET_ID", "")
    monkeypatch.setattr(config, "GOOGLE_CREDENTIALS_FILE", "")


async def _drain():
    """Аппенд в таблицу уходит через _spawn (fire-and-forget) — дождаться задач."""
    pending = [t for t in asyncio.all_tasks() if t is not asyncio.current_task()]
    if pending:
        await asyncio.gather(*pending)


def _data(name="Тест Тестов"):
    return {"full_name": name, "participant_type": "full"}


# ── Находка #3: re-entrancy guard ─────────────────────────────────────────────

def test_double_tap_confirm_writes_exactly_one_user_and_one_append(tmp_path, monkeypatch):
    _offline(tmp_path, monkeypatch, "reentrancy.db")
    _named, main_calls = _patch_appenders(monkeypatch)
    uid = 880001
    add_calls = []

    async def slow_add_user(data):
        # окно, в котором раньше успевал проскочить второй тап «Всё верно»
        add_calls.append(data)
        await asyncio.sleep(0.05)

    monkeypatch.setattr(reg, "add_user", slow_add_user)

    async def go():
        await db.init_db()
        await db.set_setting("full_approval", "manual")  # заявка остаётся pending, bot не нужен
        await asyncio.gather(
            reg.finalize_registration(_FakeMessage(uid), _FakeState(_data()), bot=None),
            reg.finalize_registration(_FakeMessage(uid), _FakeState(_data()), bot=None),
        )
        await _drain()

    asyncio.run(go())
    assert len(add_calls) == 1, "второй параллельный finalize обязан выйти сразу"
    assert len(main_calls) == 1, "ровно один append в Google-таблицу"


def test_guard_released_after_normal_completion(tmp_path, monkeypatch):
    _offline(tmp_path, monkeypatch, "release_ok.db")
    _patch_appenders(monkeypatch)
    uid = 880002
    add_calls = []

    async def counting_add_user(data):
        add_calls.append(data)

    monkeypatch.setattr(reg, "add_user", counting_add_user)

    async def go():
        await db.init_db()
        await db.set_setting("full_approval", "manual")
        for _ in range(2):  # ПОСЛЕДОВАТЕЛЬНО — гвард не должен блокировать навсегда
            await reg.finalize_registration(_FakeMessage(uid), _FakeState(_data()), bot=None)
            await _drain()

    asyncio.run(go())
    assert len(add_calls) == 2
    assert uid not in reg._FINALIZING_USERS


def test_guard_released_when_finalize_raises(tmp_path, monkeypatch):
    _offline(tmp_path, monkeypatch, "release_boom.db")
    _patch_appenders(monkeypatch)
    uid = 880003

    def boom(*args, **kwargs):
        raise RuntimeError("boom")

    monkeypatch.setattr(reg, "_decide_status", boom)

    async def go():
        await db.init_db()
        await db.set_setting("full_approval", "manual")
        await reg.finalize_registration(_FakeMessage(uid), _FakeState(_data()), bot=None)

    with pytest.raises(RuntimeError):
        asyncio.run(go())
    assert uid not in reg._FINALIZING_USERS, "release обязан быть в finally, иначе делегат заперт"


# ── Находка #4: падение add_user не должно быть тихим ─────────────────────────

def _run_locked_db_finalize(tmp_path, monkeypatch, uid):
    _offline(tmp_path, monkeypatch, "locked.db")
    named_calls, main_calls = _patch_appenders(monkeypatch)

    async def locked_add_user(data):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(reg, "add_user", locked_add_user)

    msg = _FakeMessage(uid)
    state = _FakeState(_data())

    async def go():
        await db.init_db()
        await db.set_setting("full_approval", "manual")
        await db.mark_reg_started(uid, "tester", participant_type="full")
        await reg.finalize_registration(msg, state, bot=None)  # не должно поднять исключение
        await _drain()
        return await db.get_reg_started_track(uid)

    track = asyncio.run(go())
    return msg, state, track, named_calls, main_calls


def test_add_user_failure_tells_the_delegate_and_keeps_the_confirm_keyboard(tmp_path, monkeypatch):
    msg, _state, _track, _n, _m = _run_locked_db_finalize(tmp_path, monkeypatch, 880011)

    assert msg.answers, "делегат обязан увидеть сообщение, а не тишину"
    args, kwargs = msg.answers[-1]
    text = args[0] if args else kwargs.get("text", "")
    assert "Всё верно" in text, "текст обязан объяснить, что делать — нажать кнопку ещё раз"
    assert kwargs.get("reply_markup") is not None, (
        "one-time клавиатура уже сложилась — без неё повторить подтверждение нечем"
    )


def test_add_user_failure_keeps_state_and_reg_started(tmp_path, monkeypatch):
    _msg, state, track, _n, _m = _run_locked_db_finalize(tmp_path, monkeypatch, 880012)

    assert state.cleared == 0, "FSM обязан остаться в Registration.confirm для повторного тапа"
    assert track == "full", "reg_started жив — регистрация действительно не завершена"


def test_add_user_failure_appends_nothing_and_logs_error(tmp_path, monkeypatch, caplog):
    with caplog.at_level(logging.ERROR, logger="handlers.registration"):
        _msg, _state, _track, named_calls, main_calls = _run_locked_db_finalize(
            tmp_path, monkeypatch, 880013
        )

    assert named_calls == [] and main_calls == [], "несохранённая заявка не едет в таблицу"
    assert any(r.levelno >= logging.ERROR for r in caplog.records)


# ── Регресс: обёртка ничего не сломала в обычном пути ─────────────────────────

def test_happy_path_still_creates_the_user_row(tmp_path, monkeypatch):
    _offline(tmp_path, monkeypatch, "happy.db")
    _patch_appenders(monkeypatch)
    uid = 880021

    async def go():
        await db.init_db()
        await db.set_setting("full_approval", "manual")
        await reg.finalize_registration(_FakeMessage(uid), _FakeState(_data("Обычный Путь")), bot=None)
        await _drain()
        return await db.get_user(uid)

    assert asyncio.run(go()) is not None
