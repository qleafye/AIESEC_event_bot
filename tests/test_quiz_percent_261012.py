"""Процент по компетенции в результате теста: result_lines, строка результата, реестр, EN."""
from __future__ import annotations

import json

from database import db, quiz_db as qz
from domain.cities import per_city_key
from domain.settings.schema import SETTINGS_SCHEMA
from handlers.forum import quiz as h
from handlers.forum.quiz import _result_row
from services.forum import quiz as svc
from services.i18n.i18n_form_manual import FORM_DEFAULT_EN
from tests._enroll38 import CITY, ready, run
from tests._enroll38_chat import FakeCallback
from tests.test_quiz_attempts_38 import _finish, _seed
from tests.test_quiz_flow_38 import U, answer_all, seed_quiz


async def _set_scores(attempt_id, scores):
    async with db._connect() as conn:
        await conn.execute("UPDATE quiz_attempts SET scores_json=? WHERE id=?",
                           (json.dumps({str(k): v for k, v in scores.items()}), attempt_id))
        await conn.commit()


def test_percent_values(tmp_path):
    ready(tmp_path)

    async def go():
        quiz_row, _, oids = await _seed()
        await _finish(7, quiz_row, oids, pick=1)   # 6 из 6
        await _finish(8, quiz_row, oids, pick=0)   # 0 из 6
        assert (await svc.result_lines(7, quiz_row))[0]["percent"] == 100
        assert (await svc.result_lines(8, quiz_row))[0]["percent"] == 0
    run(go())


def test_percent_integer_division(tmp_path):
    ready(tmp_path)

    async def go():
        quiz_row, comp, oids = await _seed()
        attempt, _ = await svc.start_or_resume(9, quiz_row)
        picks = [oids[1][1], oids[2][1], oids[3][2]]  # 2+2+0 из 6 -> 66
        for n, o in zip((1, 2, 3), picks):
            await svc.answer(9, attempt["id"], oids[n][0], o)
        assert (await svc.result_lines(9, quiz_row))[0]["percent"] == 66
        # 3 из 4 -> 75
        await _set_scores(attempt["id"], {comp: {"points": 3, "max": 4}})
        assert (await svc.result_lines(9, quiz_row))[0]["percent"] == 75
    run(go())


def test_percent_none_for_old_attempt_and_zero_max(tmp_path):
    ready(tmp_path)

    async def go():
        quiz_row, comp, oids = await _seed()
        attempt, _ = await _finish(7, quiz_row, oids)
        levels = await qz.list_levels(quiz_row["id"])
        await _set_scores(attempt["id"], {comp: {"level_id": levels[0]["id"]}})
        line = (await svc.result_lines(7, quiz_row))[0]
        assert line["percent"] is None and line["level_name"] == levels[0]["name"]
        await _set_scores(attempt["id"], {comp: {"points": 0, "max": 0}})
        assert (await svc.result_lines(7, quiz_row))[0]["percent"] is None
    run(go())


def test_result_row_pure():
    t = "{competency}: {percent}% · {level}"
    assert _result_row(t, "Лидерство", "Продвинутый", 75) == "Лидерство: 75% · Продвинутый"
    assert _result_row(t, "Лидерство", "Продвинутый", None) == "Лидерство · Продвинутый"
    assert _result_row("{competency} — {level}", "A", "B", 50) == "A — B"
    assert _result_row("{competency} — {level} {x}", "A", "B", None) == "A — B {x}"


def test_screen_default_and_no_level(tmp_path):
    ready(tmp_path)

    async def go():
        _, oids = await seed_quiz()
        cb = await answer_all(oids, pick=1)
        text = cb.message.last[0]
        assert "Лидерство: 100% · Продвинутый" in text and "None" not in text
    run(go())


def test_screen_escapes_competency(tmp_path):
    ready(tmp_path)

    async def go():
        from database import session_enroll_db as se
        _, oids = await seed_quiz()
        comp = (await se.list_competencies(CITY))[0]
        async with db._connect() as conn:
            await conn.execute("UPDATE competencies SET name=? WHERE id=?", ("<b>Лидер</b>", comp["id"]))
            await conn.commit()
        cb = await answer_all(oids, pick=1)
        assert "&lt;b&gt;Лидер&lt;/b&gt;: 100%" in cb.message.last[0]
    run(go())


def test_screen_legacy_saved_text(tmp_path):
    ready(tmp_path)

    async def go():
        _, oids = await seed_quiz()
        await db.set_setting(per_city_key("quiz_result_line", CITY), "{competency} — {level}")
        cb = await answer_all(oids, pick=1)
        text = cb.message.last[0]
        assert "Лидерство — Продвинутый" in text and "%" not in text
    run(go())


def test_screen_percent_none_default_template(tmp_path):
    ready(tmp_path)

    async def go():
        quiz_row, oids = await seed_quiz()
        cb = await answer_all(oids, pick=1)
        attempt = await qz.get_last_finished_attempt(U, quiz_row["id"])
        levels = await qz.list_levels(quiz_row["id"])
        from database import session_enroll_db as se
        comp = (await se.list_competencies(CITY))[0]["id"]
        await _set_scores(attempt["id"], {comp: {"level_id": levels[1]["id"]}})
        res = FakeCallback("qz:res", U)
        await h.quiz_result(res)
        text = res.message.last[0]
        assert "Лидерство · Продвинутый" in text and "%" not in text and "None" not in text
    run(go())


def test_registry_and_en():
    spec = SETTINGS_SCHEMA["quiz_result_line"]
    assert {"competency", "level", "percent"} <= set(spec["placeholders"])
    assert "{percent}" in spec["default"]
    assert spec["default"] in FORM_DEFAULT_EN
