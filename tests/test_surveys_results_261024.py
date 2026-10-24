"""Итоги анкеты: подсчёт, страницы ≤ 4096, свободные ответы; карточка менеджера, итоги,
навигация, копирование, закрытие, удаление, черновики в списке."""
import asyncio

from database import db
from database import survey_db as sdb
from services.comms import survey_report as rep
from handlers.comms import admin_survey_results  # noqa: F401
from handlers.access.admin_caps import required_capability
from tests.test_roles_phase8 import _flat_callback_data
from tests.test_polls_260822 import ADMIN_ID, _ready, _state, _cb, _add_user, FakeBot


def _run(c):
    return asyncio.run(c)


def _mk(title="Анкета"):
    return _run(sdb.create_survey_draft(title, ADMIN_ID))


def _opts(n, more_at=None, length=5):
    return [{"t": f"В{i}" + "я" * length, "more": "Поясните" if i == more_at else None} for i in range(n)]


def _results(pid):
    return _run(rep.build_survey_results(pid))


def test_counts_single_multi_scale_table_text(tmp_path):
    _ready(tmp_path)
    pid = _mk()
    q1 = _run(sdb.add_survey_question(pid, "single", "Один", options=_opts(2)))
    q2 = _run(sdb.add_survey_question(pid, "multi", "Много", options=_opts(3)))
    q3 = _run(sdb.add_survey_question(pid, "scale", "Шкала"))
    q4 = _run(sdb.add_survey_question(pid, "table", "Табл", rows=["А", "Б"]))
    q5 = _run(sdb.add_survey_question(pid, "text", "Текст"))
    for u in (1, 2):
        _run(sdb.upsert_survey_answer(pid, u, q1, "", 0))
        _run(sdb.upsert_survey_answer(pid, u, q2, "", [0, 2]))
    _run(sdb.upsert_survey_answer(pid, 1, q3, "", 4))
    _run(sdb.upsert_survey_answer(pid, 2, q3, "", 5))
    _run(sdb.upsert_survey_answer(pid, 1, q4, "r0", 4))
    _run(sdb.upsert_survey_answer(pid, 2, q4, "r0", 0))  # «не могу оценить» не в среднем
    _run(sdb.upsert_survey_answer(pid, 1, q5, "", "Супер"))
    r = _results(pid)
    assert r[0]["counts"] == [2, 0] and r[0]["respondents"] == 2
    assert r[1]["counts"] == [2, 0, 2]
    assert r[2]["dist"] == [0, 0, 0, 1, 1] and r[2]["avg"] == 4.5
    assert r[3]["rows"][0] == {"name": "А", "n": 1, "avg": 4.0}
    assert r[3]["rows"][1]["avg"] is None
    assert r[4]["respondents"] == 1


def test_pages_fit_limit_on_reference_survey(tmp_path):
    _ready(tmp_path)
    pid = _mk("Большая")
    for k in range(13):
        _run(sdb.add_survey_question(pid, "single", f"Вопрос {k} " + "в" * 280, options=_opts(8, length=84)))
    poll = _run(db.get_poll(pid))
    pages = rep.render_results_pages(poll, _results(pid), 10, 7)
    assert len(pages) > 1
    assert all(len(p) <= 4096 for p in pages)
    assert "Начали: 10 · Закончили: 7" in pages[0]


def test_text_answers_pagination_and_escape(tmp_path):
    _ready(tmp_path)
    pid = _mk()
    q = _run(sdb.add_survey_question(pid, "single", "С уточнением", options=_opts(2, more_at=1)))
    qt = _run(sdb.add_survey_question(pid, "text", "Текст"))
    for u in range(1, 13):
        _run(sdb.upsert_survey_answer(pid, u, qt, "", f"ответ <b>{u}</b>"))
    _run(sdb.upsert_survey_answer(pid, 1, q, "m1", "потому что"))
    text, pages = _run(rep.render_text_answers_page(pid, qt, 0))
    assert pages == 2 and "&lt;b&gt;" in text and "<b>1</b>" not in text.split("\n", 2)[2]
    text2, _ = _run(rep.render_text_answers_page(pid, qt, 1))
    assert text2.count("•") == 2
    textm, pm = _run(rep.render_text_answers_page(pid, q, 0))
    assert pm == 1 and "потому что" in textm and "[В1" in textm
    assert _results(pid)[0]["texts"] == 1


# ── карточка и навигация ─────────────────────────────────────────────────────────────────────

def _open_survey(title="Анкета"):
    pid = _mk(title)
    qs = _run(sdb.add_survey_question(pid, "text", "Как вам?"))
    _run(db.set_poll_status(pid, "open"))
    return pid, qs


def test_callbacks_require_broadcast():
    for key in ("psv_res:1:0", "psv_txt:1:0", "psv_copy:1"):
        assert required_capability(callback_data=key) == "broadcast", key


def test_card_shows_progress_and_buttons(tmp_path):
    _ready(tmp_path)
    pid, qid = _open_survey()
    _run(sdb.start_survey_progress(pid, 1, qid))
    _run(sdb.start_survey_progress(pid, 2, qid))
    _run(sdb.finish_survey(pid, 2, qid))
    state, bot = _state(ADMIN_ID), FakeBot()
    _, ev = _cb(f"poll_card:{pid}", state, bot)
    assert "Начали: 2 · Закончили: 1" in ev.message.text
    data = _flat_callback_data(ev.message.markup)
    for want in (f"psv_res:{pid}:0", f"poll_export:{pid}", f"psv_copy:{pid}",
                 f"poll_close:{pid}", f"poll_del:{pid}"):
        assert want in data


def test_draft_in_list_and_card_opens_builder(tmp_path):
    _ready(tmp_path)
    pid = _mk("Черновая")
    state, bot = _state(ADMIN_ID), FakeBot()
    _, ev = _cb("admin_polls", state, bot)
    assert "✏️" in str(ev.message.markup) and f"poll_card:{pid}" in _flat_callback_data(ev.message.markup)
    _, ev = _cb(f"poll_card:{pid}", state, bot)
    assert f"psv_go:{pid}" in _flat_callback_data(ev.message.markup)


def test_results_screen_and_text_nav(tmp_path):
    _ready(tmp_path)
    pid, qid = _open_survey()
    for u in range(1, 13):
        _run(sdb.upsert_survey_answer(pid, u, qid, "", f"т{u}"))
    state, bot = _state(ADMIN_ID), FakeBot()
    _, ev = _cb(f"psv_res:{pid}:0", state, bot)
    data = _flat_callback_data(ev.message.markup)
    assert f"psv_txt:{qid}:0" in data and f"poll_card:{pid}" in data
    _, ev = _cb(f"psv_txt:{qid}:0", state, bot)
    data = _flat_callback_data(ev.message.markup)
    assert f"psv_txt:{qid}:1" in data and f"psv_res:{pid}:0" in data


def test_copy_creates_draft_and_opens_it(tmp_path):
    _ready(tmp_path)
    pid, _ = _open_survey("Исходная")
    state, bot = _state(ADMIN_ID), FakeBot()
    _, ev = _cb(f"psv_copy:{pid}", state, bot)
    drafts = _run(db.list_polls(statuses=("draft",)))
    assert len(drafts) == 1 and drafts[0]["id"] != pid
    assert len(_run(sdb.list_survey_questions(drafts[0]["id"]))) == 1
    assert f"psv_go:{drafts[0]['id']}" in _flat_callback_data(ev.message.markup)


def test_close_survey_sets_closed_without_stop_poll(tmp_path):
    _ready(tmp_path)
    pid, _ = _open_survey()
    state, bot = _state(ADMIN_ID), FakeBot()
    _, ev = _cb(f"poll_close:{pid}", state, bot)
    assert _run(db.get_poll(pid))["status"] == "closed"
    assert "Анкета закрыта" in ev.message.text


def test_delete_confirm_names_what_is_lost(tmp_path):
    _ready(tmp_path)
    pid, qid = _open_survey()
    _run(sdb.start_survey_progress(pid, 1, qid))
    state, bot = _state(ADMIN_ID), FakeBot()
    _, ev = _cb(f"poll_del:{pid}", state, bot)
    assert "Пропадут вопросы и ответы 1 чел." in ev.message.text
    assert f"«Опрос {pid}»" in ev.message.text
    _cb(f"poll_del_go:{pid}", state, bot)
    assert _run(db.get_poll(pid)) is None


def test_out_of_scope_blocked(tmp_path, monkeypatch):
    _ready(tmp_path)
    pid, qid = _open_survey()

    async def set_city():
        async with db._connect() as c:
            await c.execute("UPDATE polls SET city='msk' WHERE id=?", (pid,))
            await c.commit()
    _run(set_city())

    async def scoped(_a):
        return ("spb", ("msk",)), "Санкт-Петербург"
    import handlers.comms.admin_polls as ap
    monkeypatch.setattr(ap, "_admin_city_view", scoped)
    state, bot = _state(ADMIN_ID), FakeBot()
    for key in (f"psv_res:{pid}:0", f"psv_txt:{qid}:0", f"psv_copy:{pid}", f"poll_card:{pid}"):
        _, ev = _cb(key, state, bot)
        assert "другому городу" in ev.answers[-1][0], key
    assert not _run(db.list_polls(statuses=("draft",)))


def test_ordinary_poll_card_unchanged(tmp_path):
    _ready(tmp_path)
    pid = _run(db.create_poll("Вопрос?", ["а", "б"], is_anonymous=False, allows_multiple=False,
                              created_by=ADMIN_ID, city=None, audience=[],
                              scheduled_at="2030-01-01 10:00:00"))
    state, bot = _state(ADMIN_ID), FakeBot()
    _, ev = _cb(f"poll_card:{pid}", state, bot)
    data = _flat_callback_data(ev.message.markup)
    assert "Вопрос?" in ev.message.text and f"psv_res:{pid}:0" not in data
    assert f"poll_del:{pid}" in data
