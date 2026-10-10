"""Приёмка 10.10 — остатки по анкете делегата в чате после перепроверки стенда.

- Экран «Прошлый ответ» при правке со сводки показывал служебную подпись «💬 Ожидания (общие)».
- «✏️ Изменить» на «Цели участия» сначала показывал лишний экран «Проверь образование».
- Кнопка на дочитанной анкете — «Продолжить с шага 14 из 14», хотя ведёт на сводку.
- «👇 Выбери способ:» (кнопки развилки резюме) приходило раньше самого вопроса о резюме.

Fake-объекты aiogram и сиды — из `tests/test_reg_resume_draft.py`.
"""
import asyncio

from config import config  # noqa: F401  (side effect: env-конфиг)
from database import db
from handlers import registration as reg
from handlers.states import Registration

import domain.regform.engine as reg_engine

from tests.test_reg_resume_draft import (
    USER_ID,
    _KBCapturingMessage,
    _new_state,
    _texts,
    _use_tmp_db,
)


# ── Экран «Прошлый ответ»: подпись для делегата ──────────────────────────────────────────

def test_recall_screen_uses_delegate_label(tmp_path):
    """«(общие)» в админке отличает вопрос от варианта трека; делегату на экране правки
    это ничего не говорит — подпись та же, что в анкете приложения (`DELEGATE_LABELS`)."""
    _use_tmp_db(tmp_path, "uat261010_label.db")

    async def go():
        msg = _KBCapturingMessage(USER_ID, "delegate")
        state = _new_state(USER_ID)
        await state.update_data(participant_type="full", _draft_kind="new")
        data = await state.get_data()
        await reg._show_recall_screen("expectations", "Спикеры из IT", msg, state, data, 9, 14)
        return msg

    msg = asyncio.run(go())
    text = _texts(msg)[-1] or ""
    assert "Ожидания" in text, text
    assert "(общие)" not in text, text


def test_admin_dropout_label_keeps_service_suffix():
    """Аналитика отвалов в админке по-прежнему различает «общие» ожидания и трековые."""
    from handlers.reg.reg_schema import dropout_step_label

    assert "(общие)" in dropout_step_label("expectations")


# ── «✏️ Изменить» на одном вопросе не проходит через рекап образования ─────────────────────

def test_recall_change_on_goal_skips_education_recap(tmp_path):
    """Рекап «Проверь образование» решает «группа только что закончена» по наличию ответов в
    FSM — на правке со сводки они есть всегда. «Изменить» на «Цели участия» обязан сразу
    спросить цель, а не показывать карточку образования, которое делегат не трогал."""
    from handlers.reg import reg_types_composite
    from tests.test_reg_resume_draft import _FakeCallback

    _use_tmp_db(tmp_path, "uat261010_edu.db")

    async def go():
        await db.set_setting("reg_form_v2_enabled", "on")
        await db.set_setting("reg_form_edu_card", "on")
        state = _new_state(USER_ID)
        await state.update_data(
            participant_type="full", _draft_kind="new",
            education_status="Да, в ВУЗе или колледже", course="2", university="СПбГУ",
            study_field="Информационные технологии", goal="Нетворкинг",
            _recall_step="goal", _reg_step=10, _reg_total=14,
        )
        await state.set_state(Registration.recall_pending)
        callback = _FakeCallback("recall_change:goal", USER_ID, "delegate")
        await reg.recall_change(callback, state)
        return callback.message, await state.get_state()

    msg, fsm_state = asyncio.run(go())
    assert fsm_state != reg_types_composite._CompositeChat.confirm.state, _texts(msg)
    assert not any("Проверь образование" in (t or "") for t in _texts(msg)), _texts(msg)


def test_recall_change_then_next_step_skips_education_recap(tmp_path):
    """Ревью 10.10: пропуск рекапа по «Изменить» был разовым — после ответа на правку
    следующий `_ask_step` (уже не в `recall_pending`) снова видел «группа образования
    закончена» и показывал «Проверь образование». Правка одного вопроса снимает рекап групп,
    к которым этот вопрос не относится, до конца сессии."""
    from handlers.reg import reg_types_composite
    from tests.test_reg_resume_draft import _FakeCallback

    _use_tmp_db(tmp_path, "uat261010_edu_next.db")

    async def go():
        await db.set_setting("reg_form_v2_enabled", "on")
        await db.set_setting("reg_form_edu_card", "on")
        state = _new_state(USER_ID)
        await state.update_data(
            participant_type="full", _draft_kind="new",
            education_status="Да, в ВУЗе или колледже", course="2", university="СПбГУ",
            study_field="Информационные технологии", goal="Нетворкинг",
            _recall_step="goal", _reg_step=10, _reg_total=14,
        )
        await state.set_state(Registration.recall_pending)
        await reg.recall_change(_FakeCallback("recall_change:goal", USER_ID, "delegate"), state)
        # Делегат ответил на правку — дальше бот спрашивает следующий вопрос обычным путём.
        await state.update_data(goal="Карьера")
        await state.set_state(None)
        msg = _KBCapturingMessage(USER_ID, "delegate")
        await reg._ask_step("expectations", msg, state, 11, 14)
        return msg, await state.get_state()

    msg, fsm_state = asyncio.run(go())
    assert fsm_state != reg_types_composite._CompositeChat.confirm.state, _texts(msg)
    assert not any("Проверь образование" in (t or "") for t in _texts(msg)), _texts(msg)


# ── Кнопка «Продолжить» на дочитанной анкете ──────────────────────────────────────────────

async def _continue_label(draft_step):
    from handlers.reg import reg_resume
    from tests.test_reg_resume_draft import _seed_new_draft

    await _seed_new_draft(USER_ID, step=draft_step, patch={"full_name": "Иванова Мария", "age": "22"})
    msg = _KBCapturingMessage(USER_ID, "delegate")
    await reg_resume.offer_resume(msg, await db.get_reg_draft(USER_ID))
    for _t, markup, _p in msg.sent:
        for row in getattr(markup, "inline_keyboard", None) or []:
            for btn in row:
                if btn.callback_data == "reg_resume:continue":
                    return btn.text
    raise AssertionError(msg.sent)


def test_done_draft_continue_button_leads_to_review(tmp_path):
    """Все шаги пройдены (STEP_DONE) — «Продолжить» ведёт на сводку, подпись это и говорит,
    а не «Продолжить с шага 14 из 14»."""
    _use_tmp_db(tmp_path, "uat261010_done.db")
    label = asyncio.run(_continue_label(reg_engine.STEP_DONE))
    assert label == "▶️ К проверке ответов", label


def test_unfinished_draft_keeps_step_label(tmp_path):
    _use_tmp_db(tmp_path, "uat261010_unfinished.db")

    async def go():
        await db.set_setting("reg_q_age", "on")
        return await _continue_label("age")

    label = asyncio.run(go())
    assert label.startswith("▶️ Продолжить с шага "), label


def test_review_label_has_english():
    from services.i18n import i18n_form_manual
    from domain.settings.schema import SETTINGS_SCHEMA

    entry = SETTINGS_SCHEMA["reg_resume_review_label"]
    assert entry["group"] == "reg" and entry["type"] == "text"
    assert "reg_resume" not in entry["label"]
    assert entry["default"] in i18n_form_manual._REGISTRY_TEXTS_EN


# ── Развилка резюме: кнопки способа — под самим вопросом ──────────────────────────────────

class _SentMessage:
    def __init__(self, owner, index, fail_edit=False):
        self.owner, self.index, self.fail_edit = owner, index, fail_edit

    async def edit_reply_markup(self, reply_markup=None):
        from aiogram.exceptions import TelegramBadRequest, TelegramNetworkError
        from aiogram.methods import EditMessageReplyMarkup

        method = EditMessageReplyMarkup(chat_id=USER_ID, message_id=self.index + 1)
        if self.fail_edit == "timeout":
            # Разметка встала, а ответ Telegram не дошёл.
            text, _old, parse_mode = self.owner.sent[self.index]
            self.owner.sent[self.index] = (text, reply_markup, parse_mode)
            raise TelegramNetworkError(method=method, message="Request timeout error")
        if self.fail_edit:
            raise TelegramBadRequest(method=method, message="Bad Request: message can't be edited")
        text, _old, parse_mode = self.owner.sent[self.index]
        self.owner.sent[self.index] = (text, reply_markup, parse_mode)
        return self


class _ReturningMessage(_KBCapturingMessage):
    """Как в Telegram: `answer` возвращает отправленное сообщение, его разметку можно сменить."""

    def __init__(self, *a, fail_edit=False, **k):
        super().__init__(*a, **k)
        self.fail_edit = fail_edit

    async def answer(self, text=None, reply_markup=None, parse_mode=None, *a, **k):
        self.sent.append((text, reply_markup, parse_mode))
        return _SentMessage(self, len(self.sent) - 1, self.fail_edit)


async def _ask_resume_fork(msg):
    await db.set_setting("reg_resume_mode", "fork")
    state = _new_state(USER_ID)
    await state.update_data(participant_type="full", full_name="Тест Тестов")
    await reg._ask_step("resume", msg, state, 14, 14)
    return await state.get_state()


def test_resume_fork_buttons_attach_to_question(tmp_path):
    """«👇 Выбери способ:» отдельным сообщением на стенде оказывалось выше вопроса. Кнопки
    способа теперь вешаются на само сообщение с вопросом — порядок перепутать нечем, а
    reply-клавиатура прошлого вопроса всё равно снимается этим же сообщением."""
    from aiogram.types import InlineKeyboardMarkup
    from handlers.reg import reg_resume_fork

    _use_tmp_db(tmp_path, "uat261010_fork.db")
    msg = _ReturningMessage(USER_ID, "delegate")
    fsm_state = asyncio.run(_ask_resume_fork(msg))

    assert fsm_state == Registration.resume.state
    assert len(msg.sent) == 1, msg.sent
    text, markup, _ = msg.sent[0]
    assert "резюме" in (text or "").lower()
    assert isinstance(markup, InlineKeyboardMarkup)
    datas = [btn.callback_data for row in markup.inline_keyboard for btn in row]
    assert datas and all(d.startswith("regfork:") for d in datas), datas
    assert reg_resume_fork.FORK_PICK_TITLE not in _texts(msg)


def test_resume_fork_falls_back_to_separate_buttons_message(tmp_path):
    """Telegram не дал сменить разметку — кнопки уходят отдельным сообщением ПОСЛЕ вопроса."""
    from aiogram.types import InlineKeyboardMarkup, ReplyKeyboardRemove
    from handlers.reg import reg_resume_fork

    _use_tmp_db(tmp_path, "uat261010_fork_fb.db")
    msg = _ReturningMessage(USER_ID, "delegate", fail_edit=True)
    asyncio.run(_ask_resume_fork(msg))

    assert len(msg.sent) == 2, msg.sent
    assert isinstance(msg.sent[0][1], ReplyKeyboardRemove)
    assert "резюме" in (msg.sent[0][0] or "").lower()
    assert msg.sent[1][0] == reg_resume_fork.FORK_PICK_TITLE
    assert isinstance(msg.sent[1][1], InlineKeyboardMarkup)


def test_resume_fork_timeout_does_not_send_second_pick(tmp_path):
    """Ревью 10.10: таймаут после того, как разметка встала, — не повод слать запасное
    «Выбери способ»: делегат увидел бы кнопки дважды. Запасное сообщение — только на отказ
    Telegram (`TelegramBadRequest`)."""
    import pytest
    from aiogram.exceptions import TelegramNetworkError
    from handlers.reg import reg_resume_fork

    _use_tmp_db(tmp_path, "uat261010_fork_timeout.db")
    msg = _ReturningMessage(USER_ID, "delegate", fail_edit="timeout")
    with pytest.raises(TelegramNetworkError):
        asyncio.run(_ask_resume_fork(msg))

    assert len(msg.sent) == 1, msg.sent
    assert reg_resume_fork.FORK_PICK_TITLE not in _texts(msg)
