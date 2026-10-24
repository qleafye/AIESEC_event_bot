"""Анкета: ответ свободным текстом ловится только по флагу ожидания в БД.

Команды, кнопки меню и активные FSM-состояния не перехватываются; хендлер стоит в
user_actions.router раньше фолбэка reg_handoff_idle_fallback.
"""
import asyncio
from types import SimpleNamespace

import handlers.user_actions as ua
from handlers.comms import survey_delegate as sd
from database import db, survey_db
from tests.test_surveys_flow_261024 import (
    U1, _add_user, _ready, _run, _survey, _tap, FakeMsg,
)


class FakeTextMsg(FakeMsg):
    def __init__(self, text, uid=U1, chat_type="private"):
        super().__init__()
        self.text = text
        self.from_user = SimpleNamespace(id=uid)
        self.chat = SimpleNamespace(type=chat_type)


async def _ready_text_survey(questions, **kw):
    await _add_user(U1)
    pid, qids = await _survey(questions, status="open", **kw)
    await db.record_poll_message(pid, U1, None, 1, True)
    return pid, qids


def _filter(msg, text_only=True):
    return _run(sd._AwaitingSurveyText(text_only=text_only)(msg))


def test_text_answer_recorded_and_next_question_comes(tmp_path):
    _ready(tmp_path)
    pid, qids = _run(_ready_text_survey([("text", {}), ("single", {})]))
    _tap(sd.sv_start, f"sv:s:{pid}")
    msg = FakeTextMsg("Мой ответ")
    res = _filter(msg)
    assert res and res["survey_wait"]["current_qid"] == qids[0]
    _run(sd.survey_text_step(msg, **res))
    assert msg.answers[0][0] == "Записал"
    assert "Вопрос 2 из 2" in msg.answers[1][0]
    answers = {(a["question_id"], a["sub"]): a["value"] for a in _run(survey_db.list_survey_answers(pid))}
    assert answers == {(qids[0], ""): "Мой ответ"}
    assert _filter(FakeTextMsg("ещё")) is False  # ожидание снято


def test_too_long_text_keeps_waiting(tmp_path):
    _ready(tmp_path)
    pid, qids = _run(_ready_text_survey([("text", {})]))
    _tap(sd.sv_start, f"sv:s:{pid}")
    msg = FakeTextMsg("я" * 1001)
    res = _filter(msg)
    _run(sd.survey_text_step(msg, **res))
    assert "длиннее" in msg.answers[0][0]
    assert _run(survey_db.list_survey_answers(pid)) == []
    assert _filter(FakeTextMsg("ок"))


def test_clarification_text_saved_as_sub_and_queue_goes_on(tmp_path):
    _ready(tmp_path)
    opts = [{"t": "A", "more": "Что A?"}, {"t": "B", "more": "Что B?"}]
    pid, qids = _run(_ready_text_survey([("multi", {"options": opts}), ("single", {})]))
    q = qids[0]
    _tap(sd.sv_start, f"sv:s:{pid}")
    _tap(sd.sv_option, f"sv:o:{q}:0")
    _tap(sd.sv_option, f"sv:o:{q}:1")
    _tap(sd.sv_done, f"sv:d:{q}")
    msg = FakeTextMsg("про A")
    _run(sd.survey_text_step(msg, **_filter(msg)))
    assert "Что B?" in msg.answers[1][0]
    msg = FakeTextMsg("про B")
    _run(sd.survey_text_step(msg, **_filter(msg)))
    assert "Вопрос 2 из 2" in msg.answers[1][0]
    answers = {(a["question_id"], a["sub"]): a["value"] for a in _run(survey_db.list_survey_answers(pid))}
    assert answers[(q, "m0")] == "про A" and answers[(q, "m1")] == "про B" and answers[(q, "")] == [0, 1]


def test_filter_rejects_command_no_wait_closed_and_groups(tmp_path):
    _ready(tmp_path)
    pid, _ = _run(_ready_text_survey([("text", {})]))
    assert _filter(FakeTextMsg("привет")) is False  # анкета не начата
    _tap(sd.sv_start, f"sv:s:{pid}")
    assert _filter(FakeTextMsg("/start")) is False
    assert _filter(FakeTextMsg("привет", chat_type="group")) is False
    assert _filter(FakeTextMsg("привет"))
    _run(db.set_poll_status(pid, "closed"))
    assert _filter(FakeTextMsg("привет")) is False


def test_non_text_during_wait_asks_for_text(tmp_path):
    _ready(tmp_path)
    pid, _ = _run(_ready_text_survey([("text", {})]))
    photo = FakeTextMsg(None)
    assert _filter(photo, text_only=False) is False  # ожидания ещё нет
    _tap(sd.sv_start, f"sv:s:{pid}")
    assert _filter(photo, text_only=True) is False
    res = _filter(photo, text_only=False)
    _run(sd.survey_non_text_step(photo, **res))
    assert photo.answers[0][0] == "Пришли ответ текстом одним сообщением"
    assert _run(survey_db.list_survey_answers(pid)) == []


def test_handler_order_before_idle_fallback():
    names = [h.callback.__name__ for h in ua.router.message.handlers]
    assert names.index("survey_text_step") < names.index("reg_handoff_idle_fallback")
    assert names.index("survey_non_text_step") < names.index("reg_handoff_idle_fallback")


def test_handlers_have_state_filter_none():
    by_name = {h.callback.__name__: h for h in ua.router.message.handlers}
    for name in ("survey_text_step", "survey_non_text_step"):
        kinds = [type(f.callback).__name__ for f in by_name[name].filters]
        assert "StateFilter" in kinds
