"""Приёмка 09.10 — анкета делегата в чате (прокликивание стенда).

- «Продолжить» со сводки возвращал на образование и резюме: чат не ставил в черновик маркер
  «все шаги отвечены» (`reg_engine.STEP_DONE`), его писало только приложение.

Fake-объекты aiogram и сиды — из `tests/test_reg_resume_draft.py`.
"""
import asyncio

from config import config  # noqa: F401  (side effect: env-конфиг)
from database import db
from handlers import registration as reg
from handlers.states import Registration

import reg_engine

from tests.test_reg_resume_draft import (
    USER_ID,
    _FakeCallback,
    _KBCapturingMessage,
    _new_state,
    _seed_new_draft,
    _texts,
    _use_tmp_db,
)


# ── «Продолжить» со сводки ────────────────────────────────────────────────────────────────

def test_summary_in_chat_marks_draft_done(tmp_path):
    """Ответ на последний включённый шаг в чате -> сводка, а в черновике маркер STEP_DONE,
    а не только что отвеченный шаг (иначе «Продолжить» переспрашивает его)."""
    _use_tmp_db(tmp_path, "uat261009_c1a.db")

    async def go():
        msg = _KBCapturingMessage(USER_ID, "delegate")
        state = _new_state(USER_ID)
        await state.update_data(participant_type="full", _draft_kind="new", age="20")
        enabled = await reg._get_enabled_steps(await state.get_data())
        await reg._advance(enabled[-1], msg, state, bot=object())
        return await db.get_reg_draft(USER_ID), await state.get_state(), msg

    draft, fsm_state, msg = asyncio.run(go())
    assert fsm_state == Registration.confirm.state
    assert any("Проверь свои ответы" in (t or "") for t in _texts(msg))
    assert draft["step"] == reg_engine.STEP_DONE


def test_continue_from_done_draft_shows_summary_not_questions(tmp_path, monkeypatch):
    """«Продолжить» на дочитанной анкете -> снова сводка «Всё верно / Изменить», без
    повторных вопросов и без отправки заявки мимо подтверждения."""
    _use_tmp_db(tmp_path, "uat261009_c1b.db")

    async def go():
        await _seed_new_draft(
            USER_ID, step=reg_engine.STEP_DONE, event_city="msk",
            patch={"full_name": "Иванова Мария", "age": "22", "resume_file_id": "FILE_1"},
        )
        from handlers import reg_resume

        calls = []

        async def fake_finalize(message, state, bot):
            calls.append("finalize")

        async def fake_ask(step_key, message, state, step, total):
            calls.append(f"ask:{step_key}")

        monkeypatch.setattr(reg_resume, "finalize_registration", fake_finalize)
        monkeypatch.setattr(reg_resume, "_ask_step_or_recall", fake_ask)

        callback = _FakeCallback("reg_resume:continue", USER_ID, "delegate")
        state = _new_state(USER_ID)
        await reg_resume.reg_resume_continue(callback, state, bot=object())
        return calls, await state.get_state(), callback.message

    calls, fsm_state, msg = asyncio.run(go())
    assert calls == []
    assert fsm_state == Registration.confirm.state
    assert any("Проверь свои ответы" in (t or "") for t in _texts(msg))


# ── «Пропадут уже введённые ответы (N)» ───────────────────────────────────────────────────

def test_restart_confirm_counts_answered_questions_not_draft_fields(tmp_path):
    """Резюме файлом — три поля черновика, служебные поля (`resume_type`) — тоже не вопросы.
    Делегат видит число отвеченных вопросов анкеты, не больше числа её шагов."""
    _use_tmp_db(tmp_path, "uat261009_c2.db")

    async def go():
        await db.set_setting("reg_q_age", "on")
        await db.set_setting("reg_q_resume", "on")
        await _seed_new_draft(USER_ID, patch={
            "full_name": "Иванова Мария", "age": "22", "resume_type": "file",
            "resume_file_id": "FILE_1", "resume_file_name": "cv.pdf",
        })
        from handlers import reg_resume
        enabled = await reg._get_enabled_steps({"participant_type": "full", "age": "22"})
        callback = _FakeCallback("reg_resume:restart", USER_ID, "delegate")
        await reg_resume.reg_resume_restart(callback, _new_state(USER_ID))
        return enabled, callback.message

    enabled, msg = asyncio.run(go())
    assert "age" in enabled and "resume" in enabled
    confirm = [t for t in _texts(msg) if t and "(" in t]
    assert confirm, _texts(msg)
    assert "(2)" in confirm[0], confirm[0]


# ── Выбор кнопкой подтверждается в переписке ──────────────────────────────────────────────

def test_lookup_pick_echoes_chosen_value(tmp_path):
    """Тап по подсказке ВУЗа гасит кнопки — выбранное должно остаться в переписке строкой."""
    _use_tmp_db(tmp_path, "uat261009_c5a.db")

    async def go():
        from handlers import reg_types_lookup
        state = _new_state(USER_ID)
        await state.update_data(
            participant_type="full", _draft_kind="new", _reg_step=3, _reg_total=10,
            _lookup_step="university", _lookup_results=[{"canonical": "СПбГУ"}],
        )
        callback = _FakeCallback("reglookup:pick:0", USER_ID, "delegate")
        await reg_types_lookup.reglookup_pick(callback, state, bot=None)
        return callback.message

    msg = asyncio.run(go())
    assert _texts(msg) and _texts(msg)[0] == "✅ СПбГУ", _texts(msg)


def test_multi_done_echoes_chosen_options(tmp_path):
    """«Готово» в мультивыборе гасит чекбоксы — выбранные варианты остаются в переписке."""
    _use_tmp_db(tmp_path, "uat261009_c5b.db")

    async def go():
        from handlers import reg_flow
        state = _new_state(USER_ID)
        await state.update_data(
            participant_type="full", _draft_kind="new", _reg_step=3, _reg_total=10,
            _current_multi_step="goal", _multi_goal=[0, 2],
        )
        await state.set_state(Registration.multi_input)
        callback = _FakeCallback("regmulti_done:goal", USER_ID, "delegate")
        await reg_flow.process_multi_done(callback, state, bot=None)
        return callback.message

    msg = asyncio.run(go())
    texts = _texts(msg)
    assert texts, "после «Готово» что-то должно прийти"
    echo = texts[0] or ""
    assert "✅ Найти возможность трудоустройства" in echo, texts
    assert "✅ Пообщаться с людьми из моей сферы, нетворкинг" in echo, texts


# ── Шаг резюме снимает клавиатуру прошлого вопроса ────────────────────────────────────────

def test_resume_fork_removes_previous_reply_keyboard(tmp_path):
    """Развилка резюме — инлайн-кнопки, а «Да! / Пока нет» прошлого вопроса оставались внизу.
    Шаг обязан снять reply-клавиатуру и при этом показать четыре кнопки способа."""
    from aiogram.types import InlineKeyboardMarkup, ReplyKeyboardRemove

    _use_tmp_db(tmp_path, "uat261009_c6.db")

    async def go():
        await db.set_setting("reg_resume_mode", "fork")
        msg = _KBCapturingMessage(USER_ID, "delegate")
        state = _new_state(USER_ID)
        await state.update_data(participant_type="full", full_name="Тест Тестов")
        await reg._ask_step("resume", msg, state, 14, 14)
        return msg, await state.get_state()

    msg, state_name = asyncio.run(go())
    markups = [rm for (_, rm, _) in msg.sent]
    assert any(isinstance(rm, ReplyKeyboardRemove) for rm in markups), msg.sent
    assert sum(isinstance(rm, InlineKeyboardMarkup) for rm in markups) == 1
    # вопрос идёт первым и снимает клавиатуру; кнопки — под ним (подсказка «кнопкой выше» не врёт)
    assert isinstance(msg.sent[0][1], ReplyKeyboardRemove)
    assert "резюме" in (msg.sent[0][0] or "").lower()
    assert isinstance(msg.sent[-1][1], InlineKeyboardMarkup)
    assert state_name == Registration.resume.state
