"""Конструктор анкеты в админке: права, развилка входа, черновик, правка, превью, отправка."""
import asyncio
from datetime import timedelta

from database import db
from database import survey_db as sdb
from services import scheduler as sched
from handlers.access.admin_caps import required_capability
from handlers.comms import admin_survey_builder as sb
from tests.test_roles_phase8 import _flat_callback_data
from tests.test_polls_260822 import (
    ADMIN_ID, _ready, _state, _cb, _msg, _add_user, FakeBot,
)


def _run(c):
    return asyncio.run(c)


def test_survey_callbacks_and_states_require_broadcast():
    for key in ("psv_new", "psv_draft:1", "psv_qadd:1", "psv_res:1:0", "psv_copy:1", "poll_new_single"):
        assert required_capability(callback_data=key) == "broadcast", key
    assert required_capability(raw_state="SurveyBuild:title") == "broadcast"
    assert required_capability(raw_state="SurveyBuild:q_options") == "broadcast"
    assert required_capability(callback_data="sv:s:1") is None


def _new_draft(state, bot, title="Итоги дня"):
    _cb("psv_new", state, bot)
    _msg(title, state, bot)
    return _run(db.list_polls(statuses=("draft",)))[0]["id"]


def _q(pid, text="Как вам?"):
    return _run(sdb.add_survey_question(pid, "text", text))


def test_new_creates_draft(tmp_path):
    _ready(tmp_path)
    state, bot = _state(ADMIN_ID), FakeBot()
    _cb("psv_new", state, bot)
    assert _run(state.get_state()) == "SurveyBuild:title"
    _msg("Итоги дня", state, bot)
    assert _run(state.get_state()) is None
    polls = _run(db.list_polls(statuses=("draft",)))
    assert len(polls) == 1 and polls[0]["question"] == "Итоги дня" and polls[0]["kind"] == "survey"
    assert _run(state.get_data())["pid"] == polls[0]["id"]


def test_title_validation_keeps_state(tmp_path):
    _ready(tmp_path)
    state, bot = _state(ADMIN_ID), FakeBot()
    _cb("psv_new", state, bot)
    _, ev = _msg("x" * 301, state, bot)
    assert "Например" in ev.answers[-1][0]
    assert _run(state.get_state()) == "SurveyBuild:title"
    _msg("   ", state, bot)
    assert _run(state.get_state()) == "SurveyBuild:title"


def test_draft_screen_lists_questions(tmp_path):
    _ready(tmp_path)
    state, bot = _state(ADMIN_ID), FakeBot()
    pid = _new_draft(state, bot)
    q1, q2 = _q(pid, "Первый"), _q(pid, "Второй")
    text, kb = _run(sb.render_survey_draft(pid))
    data = _flat_callback_data(kb)
    assert f"psv_q:{q1}" in data and f"psv_q:{q2}" in data and f"psv_qadd:{pid}" in data
    assert f"psv_go:{pid}" in data and f"psv_prev:{pid}" in data
    assert "Вопросов: 2 из 15" in text and "не задано" in text
    assert _run(sb.render_survey_draft(99999)) is None


def test_move_up_down_and_edges(tmp_path):
    _ready(tmp_path)
    state, bot = _state(ADMIN_ID), FakeBot()
    pid = _new_draft(state, bot)
    q1, q2 = _q(pid, "Первый"), _q(pid, "Второй")
    _, ev = _cb(f"psv_qup:{q1}", state, bot)
    assert ev.answers[-1][1] is True and "первый" in ev.answers[-1][0]
    _cb(f"psv_qdn:{q1}", state, bot)
    assert [q["id"] for q in _run(sdb.list_survey_questions(pid))] == [q2, q1]
    _, ev = _cb(f"psv_qdn:{q1}", state, bot)
    assert "последний" in ev.answers[-1][0]


def test_delete_asks_confirmation_then_deletes(tmp_path):
    _ready(tmp_path)
    state, bot = _state(ADMIN_ID), FakeBot()
    pid = _new_draft(state, bot)
    q1, q2 = _q(pid, "Первый"), _q(pid, "Второй")
    _, ev = _cb(f"psv_qdel:{q1}", state, bot)
    assert "Пропадёт" in ev.message.text
    assert len(_run(sdb.list_survey_questions(pid))) == 2
    _cb(f"psv_qdelgo:{q1}", state, bot)
    left = _run(sdb.list_survey_questions(pid))
    assert [q["id"] for q in left] == [q2] and left[0]["position"] == 1


def test_edit_question_text(tmp_path):
    _ready(tmp_path)
    state, bot = _state(ADMIN_ID), FakeBot()
    pid = _new_draft(state, bot)
    q1 = _q(pid)
    _cb(f"psv_qtxt:{q1}", state, bot)
    assert _run(state.get_state()) == "SurveyBuild:q_edit_text"
    _msg("x" * 301, state, bot)
    assert _run(state.get_state()) == "SurveyBuild:q_edit_text"
    _msg("Новый текст", state, bot)
    assert _run(sdb.get_survey_question(q1))["text"] == "Новый текст"
    assert _run(state.get_state()) is None


def test_texts_title_intro_outro(tmp_path):
    _ready(tmp_path)
    state, bot = _state(ADMIN_ID), FakeBot()
    pid = _new_draft(state, bot)
    for cb, st, text in (("psv_intro", "SurveyBuild:intro", "Привет!"),
                         ("psv_outro", "SurveyBuild:outro", "Пока!"),
                         ("psv_title", "SurveyBuild:edit_title", "Другое")):
        _cb(f"{cb}:{pid}", state, bot)
        assert _run(state.get_state()) == st
        _msg(text, state, bot)
    poll = _run(db.get_poll(pid))
    assert poll["intro_text"] == "Привет!" and poll["outro_text"] == "Пока!" and poll["question"] == "Другое"


def test_go_requires_questions_and_intro(tmp_path):
    _ready(tmp_path)
    state, bot = _state(ADMIN_ID), FakeBot()
    pid = _new_draft(state, bot)
    _, ev = _cb(f"psv_go:{pid}", state, bot)
    assert "Добавить вопрос" in ev.answers[-1][0]
    _q(pid)
    _, ev = _cb(f"psv_go:{pid}", state, bot)
    assert "Приглашение" in ev.answers[-1][0]
    _run(sdb.update_survey_texts(pid, intro="Привет"))
    _, ev = _cb(f"psv_go:{pid}", state, bot)
    assert _run(state.get_state()) == "SurveyBuild:audience"
    data = _flat_callback_data(ev.message.markup)
    assert f"psv_draft:{pid}" in data and "poll_cancel" not in data and "poll_aud:all" in data


def _ready_to_send(state, bot):
    pid = _new_draft(state, bot)
    _q(pid)
    _run(sdb.update_survey_texts(pid, intro="Привет"))
    _cb(f"psv_go:{pid}", state, bot)
    return pid


def test_audience_to_confirm_and_preview(tmp_path):
    _ready(tmp_path)
    _run(_add_user(1))
    state, bot = _state(ADMIN_ID), FakeBot()
    pid = _ready_to_send(state, bot)
    _, ev = _cb("poll_aud:approved", state, bot)
    assert _run(state.get_state()) == "SurveyBuild:confirm"
    assert f"psv_send:{pid}" in _flat_callback_data(ev.message.answers[-1][2])
    assert "1 чел." in ev.message.answers[-1][0]


def test_send_now_publishes_and_spawns_once(tmp_path, monkeypatch):
    _ready(tmp_path)
    spawned = []

    def fake_spawn(coro):
        spawned.append(1)
        coro.close()

    monkeypatch.setattr(sb, "_spawn", fake_spawn)
    state, bot = _state(ADMIN_ID), FakeBot()
    pid = _ready_to_send(state, bot)
    _cb("poll_aud:all", state, bot)
    _cb(f"psv_send:{pid}", state, bot)
    assert _run(db.get_poll(pid))["status"] == "scheduled" and len(spawned) == 1
    _, ev = _cb(f"psv_send:{pid}", state, bot)
    assert "уже отправлена" in ev.answers[-1][0] and len(spawned) == 1


def test_schedule_validates_and_creates_job(tmp_path, monkeypatch):
    _ready(tmp_path)
    jobs = []
    monkeypatch.setattr(sb, "schedule_poll_job", lambda pid, when: jobs.append((pid, when)))
    state, bot = _state(ADMIN_ID), FakeBot()
    pid = _ready_to_send(state, bot)
    _cb("poll_aud:all", state, bot)
    _cb(f"psv_sched:{pid}", state, bot)
    assert _run(state.get_state()) == "SurveyBuild:schedule_when"
    _, ev = _msg("завтра", state, bot)
    assert "Формат" in ev.answers[-1][0]
    _, ev = _msg("01.01.2020 10:00", state, bot)
    assert "уже прошло" in ev.answers[-1][0]
    when = sched._now_moscow_naive() + timedelta(days=1)
    _msg(when.strftime("%d.%m.%Y %H:%M"), state, bot)
    assert jobs and jobs[0][0] == pid
    assert _run(db.get_poll(pid))["status"] == "scheduled"


def test_cancel_keeps_draft(tmp_path):
    _ready(tmp_path)
    state, bot = _state(ADMIN_ID), FakeBot()
    pid = _new_draft(state, bot)
    _cb(f"psv_intro:{pid}", state, bot)
    _, ev = _msg("Отмена", state, bot)
    assert _run(state.get_state()) is None
    assert "Черновик сохранён" in ev.answers[-1][0]
    assert _run(db.get_poll(pid)) is not None


def test_sent_survey_not_editable(tmp_path):
    _ready(tmp_path)
    state, bot = _state(ADMIN_ID), FakeBot()
    pid = _new_draft(state, bot)
    q1 = _q(pid)
    _run(sdb.publish_survey_draft(pid, audience=[], city=None, scheduled_at="2030-01-01 10:00:00"))
    _, ev = _cb(f"psv_qdelgo:{q1}", state, bot)
    assert "недоступна" in ev.answers[-1][0]
    assert len(_run(sdb.list_survey_questions(pid))) == 1


def test_preview_splits_long_survey(tmp_path):
    _ready(tmp_path)
    state, bot = _state(ADMIN_ID), FakeBot()
    pid = _new_draft(state, bot)
    for _ in range(15):
        _run(sdb.add_survey_question(pid, "single", "В" * 290,
                                     options=[{"t": "О" * 85} for _ in range(10)]))
    _, ev = _cb(f"psv_prev:{pid}", state, bot)
    assert len(ev.message.answers) >= 2
    assert all(len(a[0]) <= 4096 for a in ev.message.answers)
    assert f"psv_draft:{pid}" in _flat_callback_data(ev.message.answers[-1][2])


def test_out_of_scope_city_blocked(tmp_path, monkeypatch):
    _ready(tmp_path)
    state, bot = _state(ADMIN_ID), FakeBot()
    pid = _new_draft(state, bot)

    async def scoped(_a):
        return ("spb", ("msk",)), "Санкт-Петербург"

    async def set_city():
        async with db._connect() as c:
            await c.execute("UPDATE polls SET city='msk' WHERE id=?", (pid,))
            await c.commit()

    import handlers.comms.admin_polls as ap
    monkeypatch.setattr(ap, "_admin_city_view", scoped)
    _run(set_city())
    _, ev = _cb(f"psv_prev:{pid}", state, bot)
    assert "другому городу" in ev.answers[-1][0] and not ev.message.answers
