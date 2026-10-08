"""Исправления по ревью фазы: фильтры рассылки, вопросы без вариантов и пр."""
from __future__ import annotations

import cities
from database import db, quiz_db as qz, session_enroll_db as se
from tests._enroll38 import CITY, add_user, ready, run, seed_delegates, seed_msk_program


def test_null_city_delegate_in_enroll_and_quiz_filters(tmp_path):
    ready(tmp_path)

    async def go():
        await seed_msk_program()
        d = await seed_delegates()
        await add_user(301, city=None)
        scope = cities.city_scope(CITY)
        exclude = list(scope[1]) if scope else []
        for spec in (
            [{"field": "session_enroll", "value": db.SESSION_ENROLL_NONE, "city": CITY,
              "exclude": exclude}],
            [{"field": "quiz", "value": db.QUIZ_NOT_PASSED, "city": CITY, "exclude": exclude}],
        ):
            got = await db.count_and_list_filtered(spec)
            assert 301 in got and d["cur1"] in got

    run(go())


def test_refresh_city_filter_spec_recomputes_exclude_for_enroll_and_quiz():
    spec = [{"field": "quiz", "value": "x", "city": CITY, "exclude": ["old"]},
            {"field": "session_enroll", "value": "none", "city": "zz-unknown", "exclude": []}]
    assert cities.refresh_city_filter_spec(spec) is None
    out = cities.refresh_city_filter_spec(spec[:1])
    assert out[0]["exclude"] == list(cities.city_scope(CITY)[1])


# ── Вопрос без вариантов ─────────────────────────────────────────────────────────────────────

def test_question_without_options_blocks_enable_and_activity(tmp_path):
    from handlers import admin_quiz as aq
    from tests.test_admin_enroll_38 import FakeCallback

    ready(tmp_path)

    async def go():
        quiz = await qz.get_or_create_quiz(CITY)
        qid = await qz.create_question(quiz["id"], "Пустой вопрос")
        cb = FakeCallback("prog_qzsw:msk")
        await aq.prog_qzsw(cb)
        assert (await qz.get_quiz(quiz["id"]))["enabled"] == 0
        assert "Пустой вопрос" in str(cb.answers if hasattr(cb, "answers") else vars(cb))
        await qz.update_quiz(quiz["id"], enabled=1)
        assert await qz.active_quiz_for_city(CITY) is None
        await qz.create_option(qid, "Да")
        assert await qz.active_quiz_for_city(CITY) is not None

    run(go())


def test_parse_csv_rejects_question_without_options():
    from services.quiz_import import parse_csv

    parsed = parse_csv("Вопрос;Вариант;Компетенция;Баллы\nЕсть вопрос;;;\n".encode("utf-8"), {}, 5)
    assert parsed.errors


def test_current_question_skips_question_without_options(tmp_path):
    from services import quiz as svc

    ready(tmp_path)

    async def go():
        quiz = await qz.get_or_create_quiz(CITY)
        await qz.create_question(quiz["id"], "Пустой")
        q2 = await qz.create_question(quiz["id"], "Нормальный")
        await qz.create_option(q2, "Да")
        attempt_id = await qz.create_attempt(1, quiz["id"], quiz["content_version"])
        cur = await svc.current_question(await qz.get_attempt(attempt_id))
        assert cur[0]["id"] == q2

    run(go())
