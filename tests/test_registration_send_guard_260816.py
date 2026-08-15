"""Ночное ревью registration.md, находки #1 и #2 (quick 260816-3av).

Симптом в проде: main.py:170 ставит глобальный parse_mode=HTML, а @dp.errors()
(main.py:199-212) молча глотает исключения. Любая отправка вопроса, упавшая на разборе
HTML (текст промпта, введённый менеджером) или на лимите в 4096 символов, поднимала
исключение ДО state.set_state(...) — делегат не получал ни вопроса, ни ошибки и
оставался запертым на прежнем шаге навсегда (#2), а раздутый summary клинил форму
перед подтверждением (#1).

Фикс — хелпер reg._safe_answer: он никогда не поднимает исключение, поэтому код
после отправки (set_state) выполняется всегда.

pytest-asyncio в этом окружении нет — асинхронщина драйвится через asyncio.run(),
БД — временная (config.DB_PATH = tmp_path/...), как в tests/test_registration_phase5.py.
"""
import asyncio
import inspect
import logging
import re

from aiogram.exceptions import TelegramBadRequest
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage

from config import config
from database import db
from handlers import registration as reg
from handlers.states import Registration


def _use_tmp_db(tmp_path):
    config.DB_PATH = str(tmp_path / "send_guard.db")


def _state(uid=777001):
    return FSMContext(storage=MemoryStorage(), key=StorageKey(bot_id=1, chat_id=uid, user_id=uid))


class _FakeChat:
    def __init__(self, chat_id):
        self.id = chat_id


class _FakeMessage:
    """Минимальный message: _ask_step/_advance трогают только .chat.id и .answer."""

    def __init__(self, chat_id=777001, fail_times=0):
        self.chat = _FakeChat(chat_id)
        self.calls = []
        self._fail_times = fail_times  # -1 == падать всегда

    async def answer(self, text, **kwargs):
        self.calls.append((text, kwargs))
        if self._fail_times == -1 or len(self.calls) <= self._fail_times:
            raise TelegramBadRequest(method=None, message="Bad Request: can't parse entities")
        return "sent:%d" % len(self.calls)


# ── Юнит: контракт _safe_answer ───────────────────────────────────────────────

def test_safe_answer_happy_path_returns_result_and_forwards_kwargs():
    msg = _FakeMessage()
    kb = object()

    result = asyncio.run(reg._safe_answer(msg, "Привет", reply_markup=kb, parse_mode="HTML"))

    assert result == "sent:1"
    assert len(msg.calls) == 1
    text, kwargs = msg.calls[0]
    assert text == "Привет"  # текст не изменён
    assert kwargs["reply_markup"] is kb
    assert kwargs["parse_mode"] == "HTML"


def test_safe_answer_retries_once_without_parse_mode_on_html_failure():
    msg = _FakeMessage(fail_times=1)
    kb = object()

    result = asyncio.run(reg._safe_answer(msg, "<b>сломанный", reply_markup=kb, parse_mode="HTML"))

    assert result == "sent:2"
    assert len(msg.calls) == 2, "ровно один повтор, не больше"
    first_text, _ = msg.calls[0]
    second_text, second_kwargs = msg.calls[1]
    assert second_text == first_text  # тот же текст
    assert second_kwargs["reply_markup"] is kb  # та же клавиатура
    assert second_kwargs["parse_mode"] is None  # разметка отключена


def test_safe_answer_swallows_double_failure_and_logs_error(caplog):
    msg = _FakeMessage(fail_times=-1)

    with caplog.at_level(logging.ERROR, logger="handlers.registration"):
        result = asyncio.run(reg._safe_answer(msg, "текст", reply_markup=None, parse_mode="HTML"))

    assert result is None  # НЕ поднимает исключение — иначе шаг не переключится
    assert len(msg.calls) == 2
    assert any(r.levelno >= logging.ERROR for r in caplog.records)


def test_safe_answer_truncates_oversized_text_before_first_send():
    msg = _FakeMessage()

    asyncio.run(reg._safe_answer(msg, "я" * 6000))

    sent_text, _ = msg.calls[0]
    assert len(sent_text) <= 4096, "первая же отправка должна укладываться в лимит Telegram"
    assert len(msg.calls) == 1  # усечённый текст уходит с первого раза


# ── Поведение: находка #2 — упавшая отправка не должна ронять set_state ────────

def _ask_and_get_state(tmp_path, step_key, uid, prepare=None):
    _use_tmp_db(tmp_path)

    async def go():
        await db.init_db()
        if prepare is not None:
            await prepare()
        msg = _FakeMessage(chat_id=uid, fail_times=-1)  # каждая отправка падает
        state = _state(uid)
        await reg._ask_step(step_key, msg, state, 1, 5)  # не должно поднять исключение
        return await state.get_state(), msg.calls

    return asyncio.run(go())


def test_ask_step_text_branch_reaches_set_state_when_send_always_fails(tmp_path):
    state_name, calls = _ask_and_get_state(tmp_path, "age", 777011)
    assert calls, "вопрос всё-таки пытались отправить"
    assert state_name == Registration.age.state


def test_ask_step_keyboard_branch_reaches_set_state_when_send_always_fails(tmp_path):
    state_name, _ = _ask_and_get_state(tmp_path, "city", 777012)
    assert state_name == Registration.city.state


def test_ask_step_admin_text_branch_reaches_set_state_when_send_always_fails(tmp_path):
    async def prepare():
        # промпт подставляет админский event_name — частый источник битого HTML
        await db.set_setting("event_name", "YouLead <b>26")

    state_name, _ = _ask_and_get_state(tmp_path, "expectations", 777013, prepare)
    assert state_name == Registration.expectations.state


# ── Поведение: находка #1 — раздутый summary не должен клинить форму ──────────

def test_advance_reaches_confirm_state_when_oversized_summary_send_fails(tmp_path):
    _use_tmp_db(tmp_path)
    uid = 777021

    async def go():
        await db.init_db()
        state = _state(uid)
        await state.update_data(
            participant_type="full",
            full_name="Тест Тестов",
            # 5000 символов в одном поле — summary заведомо длиннее лимита Telegram
            comments="ю" * 5000,
        )
        msg = _FakeMessage(chat_id=uid, fail_times=-1)
        # берём последний включённый шаг, чтобы _advance гарантированно пошёл в ветку summary
        enabled = await reg._get_enabled_steps(await state.get_data())
        after_step = enabled[-1] if enabled else "comments"
        await reg._advance(after_step, msg, state, bot=None)
        return await state.get_state(), msg.calls

    state_name, calls = asyncio.run(go())
    assert calls, "summary всё-таки пытались отправить"
    assert state_name == Registration.confirm.state


# ── Гейты по исходнику: незащищённых отправок не осталось ────────────────────

def _strip_comment_lines(src: str) -> str:
    """Убрать строки-комментарии: иначе комментарий, упоминающий отправку, сам себя
    инвалидирует (в файле есть якорные комментарии про consent-ветку)."""
    return "\n".join(l for l in src.splitlines() if not l.strip().startswith("#"))


def test_ask_step_keeps_only_the_two_consent_sends():
    src = _strip_comment_lines(inspect.getsource(reg._ask_step))
    hits = re.findall(r"message\.answer\(", src)
    assert len(hits) == 2, (
        "все отправки вопросов идут через _safe_answer; прямыми остаются ровно две "
        "в consent-ветке, где фолбэк parse_mode=None уже написан вручную"
    )


def test_advance_has_no_unguarded_sends():
    src = _strip_comment_lines(inspect.getsource(reg._advance))
    assert re.findall(r"message\.answer\(", src) == []
