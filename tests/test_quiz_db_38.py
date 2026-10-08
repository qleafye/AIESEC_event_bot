"""Тест компетенций — слой БД: схема, контент, версия, попытки."""
from __future__ import annotations

from database import db, quiz_db as qz
from tests._enroll38 import CITY, ready, run


def test_quiz_schema_and_single_per_city(tmp_path):
    ready(tmp_path)

    async def go():
        a = await qz.get_or_create_quiz(CITY)
        b = await qz.get_or_create_quiz(CITY)
        assert a["id"] == b["id"]
        assert (a["enabled"], a["score_mode"], a["content_version"]) == (0, "percent", 1)
        assert a["allow_retake"] == 0
        assert await qz.count_questions(a["id"]) == 0
        other = await qz.get_or_create_quiz("spb")
        assert other["id"] != a["id"]
        # контент не сидится
        async with db._connect() as conn:
            for t in ("quiz_questions", "quiz_options", "quiz_levels", "quiz_attempts"):
                async with conn.execute(f"SELECT COUNT(*) FROM {t}") as cur:
                    assert (await cur.fetchone())[0] == 0

    run(go())


def test_update_quiz_allow_list(tmp_path):
    ready(tmp_path)

    async def go():
        q = await qz.get_or_create_quiz(CITY)
        assert await qz.update_quiz(q["id"], title="Т", enabled=1, allow_retake=1,
                                    score_mode="points", city="spb")
        got = await qz.get_quiz(q["id"])
        assert (got["title"], got["enabled"], got["score_mode"], got["city"]) == (
            "Т", 1, "points", CITY)
        assert not await qz.update_quiz(q["id"], score_mode="bogus")
        assert not await qz.update_quiz(q["id"], city="x")

    run(go())


def test_questions_options_points(tmp_path):
    ready(tmp_path)

    async def go():
        q = await qz.get_or_create_quiz(CITY)
        v0 = q["content_version"]
        qid = await qz.create_question(q["id"], "Вопрос?")
        oid = await qz.create_option(qid, "Да")
        await qz.set_option_points(oid, 7, 3)
        assert (await qz.list_options(qid))[0]["points"] == {7: 3}
        await qz.set_option_points(oid, 7, 0)
        assert (await qz.list_options(qid))[0]["points"] == {}
        v1 = (await qz.get_quiz(q["id"]))["content_version"]
        assert v1 > v0
        await qz.delete_option(oid)
        v2 = (await qz.get_quiz(q["id"]))["content_version"]
        assert v2 == v1 + 1
        await qz.delete_question(qid)
        assert (await qz.get_quiz(q["id"]))["content_version"] == v2 + 1
        assert await qz.list_questions(q["id"]) == []

    run(go())


def test_move_question_and_levels(tmp_path):
    ready(tmp_path)

    async def go():
        q = await qz.get_or_create_quiz(CITY)
        a = await qz.create_question(q["id"], "A")
        b = await qz.create_question(q["id"], "B")
        assert await qz.move_question(b, -1)
        assert [x["text"] for x in await qz.list_questions(q["id"])] == ["B", "A"]
        assert not await qz.move_question(b, -1)
        hi = await qz.create_level(q["id"], "Высокий", 80, "ок")
        await qz.create_level(q["id"], "Низкий", 0, "")
        assert [x["name"] for x in await qz.list_levels(q["id"])] == ["Низкий", "Высокий"]
        assert await qz.update_level(hi, threshold=90, bogus=1)
        assert (await qz.get_level(hi))["threshold"] == 90
        assert await qz.delete_level(hi)
        del a

    run(go())


def test_replace_content(tmp_path):
    ready(tmp_path)

    async def go():
        q = await qz.get_or_create_quiz(CITY)
        old = await qz.create_question(q["id"], "Старый")
        await qz.create_option(old, "x")
        v0 = (await qz.get_quiz(q["id"]))["content_version"]
        content = [
            {"text": f"В{i}", "options": [{"text": f"О{j}", "points": {5: j}} for j in range(3)]}
            for i in range(2)
        ]
        v = await qz.replace_content(q["id"], content)
        assert v == v0 + 1
        qs = await qz.list_questions(q["id"])
        assert [x["text"] for x in qs] == ["В0", "В1"]
        opts = await qz.list_options_for_quiz(q["id"])
        assert [o["text"] for o in opts[qs[0]["id"]]] == ["О0", "О1", "О2"]
        assert opts[qs[0]["id"]][2]["points"] == {5: 2}
        assert opts[qs[0]["id"]][0]["points"] == {}  # нулевые баллы не хранятся
        async with db._connect() as conn:
            async with conn.execute("SELECT COUNT(*) FROM quiz_options") as cur:
                assert (await cur.fetchone())[0] == 6

    run(go())


def test_attempt_answers_persist(tmp_path):
    ready(tmp_path)

    async def go():
        q = await qz.get_or_create_quiz(CITY)
        aid = await qz.create_attempt(1, q["id"], q["content_version"])
        assert await qz.record_answer(aid, 10, 100)
        assert await qz.record_answer(aid, 10, 101)  # перезапись
        assert await qz.record_answer(aid, 11, 200)
        open_att = await qz.get_open_attempt(1, q["id"])  # новое соединение = «после рестарта»
        assert open_att["id"] == aid and open_att["answers"] == {10: 101, 11: 200}
        assert await qz.count_open_attempts(q["id"]) == 1
        assert await qz.finish_attempt(aid, {5: 3})
        assert not await qz.finish_attempt(aid, {5: 9})
        assert not await qz.record_answer(aid, 12, 300)
        assert await qz.get_open_attempt(1, q["id"]) is None
        assert (await qz.get_last_finished_attempt(1, q["id"]))["scores"] == {5: 3}
        aid2 = await qz.create_attempt(1, q["id"], 1)
        assert await qz.delete_open_attempts(1, q["id"]) == 1
        assert await qz.get_attempt(aid2) is None

    run(go())


def test_attempt_counts_and_scores(tmp_path):
    ready(tmp_path)

    async def go():
        q = await qz.get_or_create_quiz(CITY)
        a1 = await qz.create_attempt(1, q["id"], 1)
        await qz.create_attempt(2, q["id"], 1)
        await qz.finish_attempt(a1, {5: 1})
        a1b = await qz.create_attempt(1, q["id"], 1)
        await qz.finish_attempt(a1b, {5: 4})
        assert await qz.attempt_counts(q["id"]) == {"started": 2, "finished": 1}
        scores = await qz.list_finished_scores(q["id"])
        assert [(s["telegram_id"], s["scores"]) for s in scores] == [(1, {5: 4})]

    run(go())


def test_active_quiz_for_city(tmp_path):
    ready(tmp_path)

    async def go():
        q = await qz.get_or_create_quiz(CITY)
        assert await qz.active_quiz_for_city(CITY) is None
        await qz.update_quiz(q["id"], enabled=1)
        assert await qz.active_quiz_for_city(CITY) is None  # нет вопросов
        await qz.create_question(q["id"], "В")
        assert (await qz.active_quiz_for_city(CITY))["id"] == q["id"]
        await qz.update_quiz(q["id"], enabled=0)
        assert await qz.active_quiz_for_city(CITY) is None

    run(go())


def test_purge_registers_quiz_attempts():
    assert "quiz_attempts" in {t for t, _, _ in db.USER_PURGE_TABLES}
