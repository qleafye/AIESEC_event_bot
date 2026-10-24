"""Слой БД анкеты из нескольких вопросов: миграция polls, CRUD вопросов, копирование, прогресс
делегата в БД, атомарный переход, ожидание текста, каскад удаления и purge делегата.

pytest-asyncio в проекте нет — async гоняется через asyncio.run(); БД — tmp_path.
"""
import asyncio
import sqlite3

from config import config
from database import db, survey_db
from tests._dbtpl import fast_init_db

ADMIN_ID = 910001
USER_ID = 910002
OTHER_ID = 910003


def _ready(tmp_path):
    config.DB_PATH = str(tmp_path / "surveys.db")
    config.ADMIN_IDS = [ADMIN_ID]
    fast_init_db()


def _run(coro):
    return asyncio.run(coro)


def _cols(path, table):
    conn = sqlite3.connect(path)
    try:
        return {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}
    finally:
        conn.close()


async def _mk_survey(n_questions=2, title="Анкета"):
    pid = await survey_db.create_survey_draft(title, ADMIN_ID)
    qids = []
    for i in range(n_questions):
        qids.append(await survey_db.add_survey_question(pid, "single", f"Вопрос {i + 1}",
                                                        options=[{"t": "Да", "more": None}]))
    return pid, qids


def _set_status(pid, status):
    async def go():
        await db.set_poll_status(pid, status)
    _run(go())


# ── Миграция ─────────────────────────────────────────────────────────────────────────────────

def test_migration_adds_columns_and_old_rows_are_polls(tmp_path):
    config.DB_PATH = str(tmp_path / "old.db")
    conn = sqlite3.connect(config.DB_PATH)
    conn.execute(
        "CREATE TABLE polls (id INTEGER PRIMARY KEY AUTOINCREMENT, question TEXT NOT NULL, "
        "options_json TEXT NOT NULL, is_anonymous INTEGER NOT NULL DEFAULT 0, "
        "allows_multiple INTEGER NOT NULL DEFAULT 0, created_by INTEGER, created_at TEXT, "
        "city TEXT, audience_json TEXT, status TEXT NOT NULL DEFAULT 'scheduled', "
        "scheduled_at TEXT, sending_since TEXT, closed_at TEXT)"
    )
    conn.execute("INSERT INTO polls (question, options_json) VALUES ('Старый?', '[\"A\",\"B\"]')")
    conn.commit()
    conn.close()
    assert "kind" not in _cols(config.DB_PATH, "polls")

    _run(db.init_db())

    assert {"kind", "intro_text", "outro_text"} <= _cols(config.DB_PATH, "polls")
    poll = _run(db.get_poll(1))
    assert poll["kind"] == "poll" and poll["question"] == "Старый?"
    # повторный init_db аддитивен
    _run(db.init_db())
    assert _run(db.get_poll(1))["kind"] == "poll"


def test_create_poll_stays_poll_and_draft_is_survey(tmp_path):
    _ready(tmp_path)

    async def go():
        pid = await db.create_poll("Q?", ["A", "B"], is_anonymous=False, allows_multiple=False,
                                   created_by=ADMIN_ID, city=None, audience=[],
                                   scheduled_at="2026-01-01 10:00:00")
        sid = await survey_db.create_survey_draft("Анкета", ADMIN_ID, "msk")
        return await db.get_poll(pid), await db.get_poll(sid)

    poll, survey = _run(go())
    assert poll["kind"] == "poll"
    assert survey["kind"] == "survey" and survey["status"] == "draft"
    assert survey["options_json"] == "[]" and survey["question"] == "Анкета"
    assert survey["city"] == "msk"


def test_draft_label_exists():
    assert "draft" in db.POLL_STATUS_LABELS


# ── Вопросы ──────────────────────────────────────────────────────────────────────────────────

def test_questions_add_list_decode_and_positions(tmp_path):
    _ready(tmp_path)

    async def go():
        pid = await survey_db.create_survey_draft("A", ADMIN_ID)
        q1 = await survey_db.add_survey_question(
            pid, "multi", "Что любите?", max_choices=2, required=False,
            options=[{"t": "Кофе", "more": "Какой?"}, {"t": "Чай", "more": None}])
        q2 = await survey_db.add_survey_question(
            pid, "table", "Оцени", rows=["Еда", "Сцена"], scale_lo="плохо", scale_hi="отлично",
            na_label="Не могу оценить")
        return pid, q1, q2, await survey_db.list_survey_questions(pid)

    pid, q1, q2, qs = _run(go())
    assert [q["position"] for q in qs] == [1, 2]
    assert qs[0]["options"][0] == {"t": "Кофе", "more": "Какой?"}
    assert qs[0]["max_choices"] == 2 and qs[0]["required"] is False
    assert qs[1]["rows"] == ["Еда", "Сцена"] and qs[1]["na_label"] == "Не могу оценить"
    assert _run(survey_db.get_survey_question(q2))["qtype"] == "table"
    assert _run(survey_db.get_survey_question(99999)) is None


def test_delete_and_move_renumber_without_gaps(tmp_path):
    _ready(tmp_path)

    async def go():
        pid, (a, b, c) = await _mk_survey(3)
        assert await survey_db.move_survey_question(a, -1) is False       # край сверху
        assert await survey_db.move_survey_question(c, +1) is False       # край снизу
        assert await survey_db.move_survey_question(c, -1) is True
        order = [q["id"] for q in await survey_db.list_survey_questions(pid)]
        assert order == [a, c, b]
        await survey_db.delete_survey_question(a)
        qs = await survey_db.list_survey_questions(pid)
        return [(q["id"], q["position"]) for q in qs], c, b

    got, c, b = _run(go())
    assert got == [(c, 1), (b, 2)]


def test_update_question_text(tmp_path):
    _ready(tmp_path)

    async def go():
        _, (q,) = await _mk_survey(1)
        await survey_db.update_survey_question_text(q, "Новый текст")
        return (await survey_db.get_survey_question(q))["text"]

    assert _run(go()) == "Новый текст"


def test_update_survey_texts(tmp_path):
    _ready(tmp_path)

    async def go():
        pid, _ = await _mk_survey(0)
        await survey_db.update_survey_texts(pid, title="Т", intro="Привет", outro="Спасибо")
        return await db.get_poll(pid)

    p = _run(go())
    assert (p["question"], p["intro_text"], p["outro_text"]) == ("Т", "Привет", "Спасибо")


def test_copy_survey_makes_draft_with_same_questions(tmp_path):
    _ready(tmp_path)

    async def go():
        pid, _ = await _mk_survey(2, title="Исходная")
        await survey_db.update_survey_texts(pid, intro="Вступление", outro="Финал")
        await survey_db.publish_survey_draft(pid, audience=[{"k": "v"}], city="msk",
                                             scheduled_at="2026-01-01 10:00:00")
        new_id = await survey_db.copy_survey(pid, ADMIN_ID)
        return (pid, new_id, await db.get_poll(new_id),
                await survey_db.list_survey_questions(pid),
                await survey_db.list_survey_questions(new_id))

    pid, new_id, copy, src_q, new_q = _run(go())
    assert new_id != pid and copy["status"] == "draft" and copy["kind"] == "survey"
    assert copy["audience"] == [] and copy["intro_text"] == "Вступление"
    assert copy["question"].startswith("Исходная")
    assert [(q["qtype"], q["text"], q["options"], q["position"]) for q in new_q] == \
           [(q["qtype"], q["text"], q["options"], q["position"]) for q in src_q]
    assert {q["id"] for q in new_q}.isdisjoint({q["id"] for q in src_q})


def test_publish_draft_once_and_not_for_plain_poll(tmp_path):
    _ready(tmp_path)

    async def go():
        pid, _ = await _mk_survey(1)
        first = await survey_db.publish_survey_draft(pid, audience=[], city=None,
                                                     scheduled_at="2026-01-01 10:00:00")
        second = await survey_db.publish_survey_draft(pid, audience=[], city=None,
                                                      scheduled_at="2026-01-01 10:00:00")
        plain = await db.create_poll("Q?", ["A", "B"], is_anonymous=False, allows_multiple=False,
                                     created_by=ADMIN_ID, city=None, audience=[],
                                     scheduled_at="2026-01-01 10:00:00")
        third = await survey_db.publish_survey_draft(plain, audience=[], city=None,
                                                     scheduled_at="2026-01-01 10:00:00")
        return first, second, third, (await db.get_poll(pid))["status"]

    assert _run(go()) == (1, 0, 0, "scheduled")


# ── Прогресс ─────────────────────────────────────────────────────────────────────────────────

def test_start_progress_is_idempotent(tmp_path):
    _ready(tmp_path)

    async def go():
        pid, (q1, q2) = await _mk_survey(2)
        a = await survey_db.start_survey_progress(pid, USER_ID, q1)
        await survey_db.advance_survey(pid, USER_ID, q1, q2)
        b = await survey_db.start_survey_progress(pid, USER_ID, q1)
        return q2, a, b

    q2, a, b = _run(go())
    assert a["state"] == "active" and b["current_qid"] == q2


def test_advance_is_atomic_and_finish_once(tmp_path):
    _ready(tmp_path)

    async def go():
        pid, (q1, q2) = await _mk_survey(2)
        await survey_db.start_survey_progress(pid, USER_ID, q1)
        r1 = await survey_db.advance_survey(pid, USER_ID, q1, q2)
        r2 = await survey_db.advance_survey(pid, USER_ID, q1, q2)
        f1 = await survey_db.finish_survey(pid, USER_ID, q2)
        f2 = await survey_db.finish_survey(pid, USER_ID, q2)
        return r1, r2, f1, f2, await survey_db.get_survey_progress(pid, USER_ID)

    r1, r2, f1, f2, prog = _run(go())
    assert (r1, r2, f1, f2) == (True, False, True, False)
    assert prog["state"] == "done" and prog["finished_at"]


def test_progress_survives_new_connection(tmp_path):
    _ready(tmp_path)

    async def first():
        pid, (q1, q2) = await _mk_survey(2)
        await survey_db.start_survey_progress(pid, USER_ID, q1)
        await survey_db.set_survey_await(pid, USER_ID, q1, {"qid": q1, "sub": "more:0"})
        await survey_db.set_survey_draft(pid, USER_ID, q1, {"picked": [0, 2]})
        return pid, q1

    pid, q1 = _run(first())
    prog = _run(survey_db.get_survey_progress(pid, USER_ID))   # новый event loop и соединение
    assert prog["current_qid"] == q1 and prog["state"] == "awaiting_text"
    assert prog["await"] == {"qid": q1, "sub": "more:0"} and prog["draft"] == {"picked": [0, 2]}


def test_set_await_and_draft_ignore_stale_question(tmp_path):
    _ready(tmp_path)

    async def go():
        pid, (q1, q2) = await _mk_survey(2)
        await survey_db.start_survey_progress(pid, USER_ID, q1)
        stale_await = await survey_db.set_survey_await(pid, USER_ID, q2, {"qid": q2})
        stale_draft = await survey_db.set_survey_draft(pid, USER_ID, q2, {"x": 1})
        cleared = await survey_db.set_survey_await(pid, USER_ID, q1, None)
        return stale_await, stale_draft, cleared, await survey_db.get_survey_progress(pid, USER_ID)

    sa, sd, cl, prog = _run(go())
    assert (sa, sd, cl) == (False, False, True) and prog["state"] == "active"


def test_get_survey_wait_only_for_sending_or_open(tmp_path):
    _ready(tmp_path)

    async def prep():
        pid, (q1, _) = await _mk_survey(2)
        await survey_db.start_survey_progress(pid, USER_ID, q1)
        await survey_db.set_survey_await(pid, USER_ID, q1, {"qid": q1, "sub": "main"})
        return pid, q1

    pid, q1 = _run(prep())
    assert _run(survey_db.get_survey_wait(USER_ID)) is None          # draft
    for status, expect in (("sending", True), ("open", True), ("closed", False),
                           ("scheduled", False)):
        _set_status(pid, status)
        w = _run(survey_db.get_survey_wait(USER_ID))
        assert (w is not None) is expect, status
    _set_status(pid, "open")
    w = _run(survey_db.get_survey_wait(USER_ID))
    assert w["poll_id"] == pid and w["current_qid"] == q1 and w["await"]["sub"] == "main"
    assert _run(survey_db.get_survey_wait(OTHER_ID)) is None


def test_recipient_checks_delivery_row(tmp_path):
    _ready(tmp_path)

    async def go():
        pid, _ = await _mk_survey(1)
        before = await survey_db.survey_recipient(pid, USER_ID)
        await db.record_poll_message(pid, USER_ID, None, None, True)
        await db.record_poll_message(pid, OTHER_ID, None, None, False)
        return (before, await survey_db.survey_recipient(pid, USER_ID),
                await survey_db.survey_recipient(pid, OTHER_ID))

    assert _run(go()) == (False, True, False)


# ── Ответы и счётчики ────────────────────────────────────────────────────────────────────────

def test_upsert_answer_overwrites_and_lists(tmp_path):
    _ready(tmp_path)

    async def go():
        pid, (q1, q2) = await _mk_survey(2)
        await survey_db.upsert_survey_answer(pid, USER_ID, q1, "", [0])
        await survey_db.upsert_survey_answer(pid, USER_ID, q1, "", [1, 2])
        await survey_db.upsert_survey_answer(pid, USER_ID, q2, "m0", "Привет, мир")
        return await survey_db.list_survey_answers(pid), q1

    answers, q1 = _run(go())
    assert len(answers) == 2
    by_q = {(a["question_id"], a["sub"]): a["value"] for a in answers}
    assert by_q[(q1, "")] == [1, 2]
    assert "Привет, мир" in by_q.values()


def test_count_and_list_progress(tmp_path):
    _ready(tmp_path)

    async def go():
        pid, (q1, q2) = await _mk_survey(2)
        await survey_db.start_survey_progress(pid, USER_ID, q1)
        await survey_db.start_survey_progress(pid, OTHER_ID, q1)
        await survey_db.finish_survey(pid, OTHER_ID, q1)
        return (await survey_db.count_survey_progress(pid),
                await survey_db.list_survey_progress(pid))

    counts, rows = _run(go())
    assert counts == (2, 1) and {r["user_id"] for r in rows} == {USER_ID, OTHER_ID}


# ── Удаление ─────────────────────────────────────────────────────────────────────────────────

def _count(path, table):
    conn = sqlite3.connect(path)
    try:
        return conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
    finally:
        conn.close()


def test_delete_poll_cascades_survey_tables(tmp_path):
    _ready(tmp_path)

    async def go():
        pid, (q1, _) = await _mk_survey(2)
        await survey_db.start_survey_progress(pid, USER_ID, q1)
        await survey_db.upsert_survey_answer(pid, USER_ID, q1, "", [0])
        await db.delete_poll(pid)

    _run(go())
    for t in ("survey_questions", "survey_progress", "survey_answers"):
        assert _count(config.DB_PATH, t) == 0, t


def test_purge_user_clears_progress_and_answers(tmp_path):
    _ready(tmp_path)
    tables = {t for t, _, _ in db.USER_PURGE_TABLES}
    assert {"survey_progress", "survey_answers"} <= tables

    async def go():
        pid, (q1, _) = await _mk_survey(2)
        for uid in (USER_ID, OTHER_ID):
            await survey_db.start_survey_progress(pid, uid, q1)
            await survey_db.upsert_survey_answer(pid, uid, q1, "", [0])
        await db.purge_user(USER_ID)
        return await survey_db.list_survey_progress(pid), await survey_db.list_survey_answers(pid)

    prog, answers = _run(go())
    assert [p["user_id"] for p in prog] == [OTHER_ID]
    assert [a["user_id"] for a in answers] == [OTHER_ID]
