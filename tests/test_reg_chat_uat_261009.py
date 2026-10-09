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
