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
