"""Тест компетенций — жизненный цикл попытки, результат, пересдача, статистика."""
from __future__ import annotations

from database import quiz_db as qz, session_enroll_db as se
from services import quiz
from tests._enroll38 import CITY, ready, run


async def _seed(allow_retake=0):
    """Тест из 3 вопросов по 2 варианта; компетенция «Лидерство», уровни 0/60."""
    comp = await se.create_competency(CITY, "Лидерство")
    q = await qz.get_or_create_quiz(CITY)
    await qz.update_quiz(q["id"], enabled=1, allow_retake=allow_retake)
    oids = {}
    for n in range(1, 4):
        qid = await qz.create_question(q["id"], f"Вопрос {n}")
        hi = await qz.create_option(qid, "Высокий")
        lo = await qz.create_option(qid, "Низкий")
        await qz.set_option_points(hi, comp, 2)
        await qz.set_option_points(lo, comp, 0)
        oids[n] = (qid, hi, lo)
    await qz.create_level(q["id"], "Базовый", 0, "Начало пути")
    await qz.create_level(q["id"], "Продвинутый", 60, "Уверенно")
    return await qz.get_quiz(q["id"]), comp, oids


async def _finish(tid, quiz_row, oids, pick=1):
    attempt, _ = await quiz.start_or_resume(tid, quiz_row)
    for n in range(1, 4):
        qid, hi, lo = oids[n]
        res = await quiz.answer(tid, attempt["id"], qid, hi if pick else lo)
    return attempt, res


def test_start_new_and_resume(tmp_path):
    ready(tmp_path)

    async def go():
        quiz_row, _, oids = await _seed()
        attempt, status = await quiz.start_or_resume(7, quiz_row)
        assert status == "new"
        qid, hi, _ = oids[1]
        assert await quiz.answer(7, attempt["id"], qid, hi) == "ok"
        attempt, status = await quiz.start_or_resume(7, quiz_row)
        assert status == "resumed"
        question, options, n, total = await quiz.current_question(attempt)
        assert (question["id"], n, total, len(options)) == (oids[2][0], 2, 3, 2)

    run(go())


def test_version_restart(tmp_path):
    ready(tmp_path)

    async def go():
        quiz_row, _, oids = await _seed()
        attempt, _ = await quiz.start_or_resume(7, quiz_row)
        await quiz.answer(7, attempt["id"], oids[1][0], oids[1][1])
        await qz.bump_content_version(quiz_row["id"])
        quiz_row = await qz.get_quiz(quiz_row["id"])
        attempt2, status = await quiz.start_or_resume(7, quiz_row)
        assert status == "restarted"
        assert attempt2["id"] != attempt["id"] and attempt2["answers"] == {}
        assert attempt2["content_version"] == quiz_row["content_version"]
        assert await qz.get_attempt(attempt["id"]) is None

    run(go())


def test_answer_validation(tmp_path):
    ready(tmp_path)

    async def go():
        quiz_row, _, oids = await _seed()
        attempt, _ = await quiz.start_or_resume(7, quiz_row)
        aid = attempt["id"]
        assert await quiz.answer(8, aid, oids[1][0], oids[1][1]) == "not_owner"
        assert await quiz.answer(7, aid, oids[1][0], oids[2][1]) == "bad_option"
        assert await quiz.answer(7, aid, oids[2][0], oids[2][1]) == "stale"
        assert await quiz.answer(7, aid, oids[1][0], oids[1][1]) == "ok"
        assert await quiz.answer(7, aid, oids[1][0], oids[1][1]) == "stale"
        current = await quiz.current_question(await qz.get_attempt(aid))
        assert current[0]["id"] == oids[2][0]
        await _finish(7, quiz_row, oids)
        assert await quiz.answer(7, aid, oids[3][0], oids[3][1]) == "finished"

    run(go())


def test_last_answer_finishes(tmp_path):
    ready(tmp_path)

    async def go():
        quiz_row, comp, oids = await _seed()
        attempt, res = await _finish(7, quiz_row, oids)
        assert res == "done"
        done = await qz.get_attempt(attempt["id"])
        assert done["finished_at"]
        assert done["scores"][comp]["points"] == 6
        assert done["scores"][comp]["max"] == 6

    run(go())


def test_finished_no_retake(tmp_path):
    ready(tmp_path)

    async def go():
        quiz_row, _, oids = await _seed(allow_retake=0)
        await _finish(7, quiz_row, oids)
        attempt, status = await quiz.start_or_resume(7, quiz_row)
        assert status == "finished" and attempt["finished_at"]
        assert await quiz.retake(7, quiz_row) is None

        await qz.update_quiz(quiz_row["id"], allow_retake=1)
        quiz_row = await qz.get_quiz(quiz_row["id"])
        fresh = await quiz.retake(7, quiz_row)
        assert fresh["answers"] == {} and fresh["finished_at"] is None

    run(go())


def test_result_lines(tmp_path):
    ready(tmp_path)

    async def go():
        quiz_row, _, oids = await _seed()
        second = await se.create_competency(CITY, "Командность")
        assert await quiz.result_lines(7, quiz_row) is None
        await _finish(7, quiz_row, oids)
        lines = await quiz.result_lines(7, quiz_row)
        assert lines == [{"competency": "Лидерство", "level_name": "Продвинутый",
                          "level_description": "Уверенно"}]
        assert second  # компетенция без баллов в результат не попадает

        await _finish(8, quiz_row, oids, pick=0)
        low = await quiz.result_lines(8, quiz_row)
        assert low[0]["level_name"] == "Базовый"

    run(go())


def test_result_without_level(tmp_path):
    ready(tmp_path)

    async def go():
        quiz_row, _, oids = await _seed()
        for lv in await qz.list_levels(quiz_row["id"]):
            await qz.delete_level(lv["id"])
        await qz.create_level(quiz_row["id"], "Высокий", 90)
        await _finish(7, quiz_row, oids, pick=0)
        lines = await quiz.result_lines(7, quiz_row)
        assert lines[0]["level_name"] is None and lines[0]["level_description"] == ""

    run(go())


def test_stats(tmp_path):
    ready(tmp_path)

    async def go():
        quiz_row, _, oids = await _seed()
        await _finish(1, quiz_row, oids, pick=1)
        await _finish(2, quiz_row, oids, pick=0)
        await quiz.start_or_resume(3, quiz_row)
        got = await quiz.stats(quiz_row)
        assert (got["started"], got["finished"]) == (3, 2)
        assert got["by_competency"] == {"Лидерство": {"Продвинутый": 1, "Базовый": 1}}

    run(go())
