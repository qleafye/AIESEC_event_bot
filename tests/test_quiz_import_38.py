"""Тест компетенций — импорт контента из CSV."""
from __future__ import annotations

import pytest

from database import quiz_db as qz, session_enroll_db as se
from services import quiz_import as qi
from tests._enroll38 import CITY, ready, run

COMPS = {"Лидерство": 1, "Командность": 2}

BASIC = (
    "вопрос;вариант;компетенция;баллы\n"
    "Q1;a1;Лидерство;5\nQ1;a2;Лидерство;3\nQ1;a3;Лидерство;0\n"
    "Q2;b1;Командность;5\nQ2;b2;Командность;2\nQ2;b3;Командность;0\n"
)


def test_template_bytes(tmp_path):
    ready(tmp_path)

    async def go():
        raw = await qi.build_template(CITY)
        assert raw.startswith(b"\xef\xbb\xbf")
        text = raw.decode("utf-8-sig")
        assert text.splitlines()[0] == "вопрос;вариант;компетенция;баллы"
        assert "Лидерство" in text
        await se.create_competency(CITY, "Стратегия")
        assert "Стратегия" in (await qi.build_template(CITY)).decode("utf-8-sig")

    run(go())


def test_parse_basic():
    parsed = qi.parse_csv(BASIC.encode("utf-8-sig"), COMPS, 5)
    assert not parsed.errors and not parsed.unknown_competencies
    assert len(parsed.questions) == 2 and parsed.option_count == 6
    assert parsed.questions[0]["options"][1] == {"text": "a2", "points": {1: 3}}


def test_parse_multi_competency_option():
    raw = "вопрос;вариант;компетенция;баллы\nQ;a;Лидерство;4\nQ;a;Командность;2\nQ;b;Лидерство;1\n"
    parsed = qi.parse_csv(raw.encode(), COMPS, 5)
    opts = parsed.questions[0]["options"]
    assert [o["text"] for o in opts] == ["a", "b"]
    assert opts[0]["points"] == {1: 4, 2: 2}


def test_parse_empty_question_continues():
    raw = "вопрос;вариант;компетенция;баллы\nQ;a;Лидерство;4\n;b;Лидерство;1\n"
    parsed = qi.parse_csv(raw.encode(), COMPS, 5)
    assert len(parsed.questions) == 1 and parsed.option_count == 2


@pytest.mark.parametrize("delim", [";", ",", "\t"])
@pytest.mark.parametrize("enc", ["utf-8-sig", "cp1251"])
def test_parse_delimiters_and_encodings(delim, enc):
    text = BASIC.replace(";", delim)
    parsed = qi.parse_csv(text.encode(enc), COMPS, 5)
    assert not parsed.errors
    assert len(parsed.questions) == 2 and parsed.option_count == 6
    assert parsed.questions[1]["options"][0]["points"] == {2: 5}


def test_parse_errors():
    raw = "вопрос;вариант;компетенция;баллы\nQ;a;Лидерство;abc\nQ;b;Лидерство;9\n"
    parsed = qi.parse_csv(raw.encode(), COMPS, 5)
    assert len(parsed.errors) == 2
    assert parsed.errors[0] == "Строка 2: баллы должны быть числом от 0 до 5, а там «abc»."
    assert parsed.errors[1].startswith("Строка 3:")


def test_unknown_competencies():
    raw = "вопрос;вариант;компетенция;баллы\nQ;a;Лидерства;4\nQ;a;  лидерство ;2\n"
    parsed = qi.parse_csv(raw.encode(), COMPS, 5)
    assert parsed.unknown_competencies == ["Лидерства"]
    assert parsed.questions[0]["options"][0]["points"] == {1: 2}


def test_formula_cells_as_text():
    raw = '=HYPERLINK("http://x");=cmd|calc;Лидерство;1\n'
    parsed = qi.parse_csv(raw.encode(), COMPS, 5)
    assert parsed.questions[0]["text"].startswith("=HYPERLINK")
    assert parsed.questions[0]["options"][0]["text"] == "=cmd|calc"


def test_preview_text():
    parsed = qi.parse_csv((BASIC + "Q3;c1;Ужас;1\n").encode(), COMPS, 5)
    text = qi.preview_text(parsed, current_questions=4, current_options=9, open_attempts=2)
    assert "3 вопроса, 7 вариантов" in text
    assert "Неизвестные компетенции: Ужас" in text
    assert "Будет заменено: 4 вопроса, 9 вариантов" in text
    assert "Незавершённых попыток: 2 — начнутся заново" in text
    bad = qi.parse_csv("Q;a;Лидерство;zzz\n".encode(), COMPS, 5)
    assert "ошибки" in qi.preview_text(bad, current_questions=0, current_options=0, open_attempts=0)


def test_apply_bumps_version(tmp_path):
    ready(tmp_path)

    async def go():
        comp = await se.create_competency(CITY, "Лидерство")
        quiz = await qz.get_or_create_quiz(CITY)
        parsed = qi.parse_csv(BASIC.encode(), {"Лидерство": comp}, 5)
        version = await qi.apply(quiz["id"], parsed)
        assert version == quiz["content_version"] + 1
        assert await qz.count_questions(quiz["id"]) == 2
        opts = await qz.list_options_for_quiz(quiz["id"])
        first = next(iter(opts.values()))
        assert first[0]["points"] == {comp: 5}
        bad = qi.parse_csv("Q;a;Лидерство;zzz\n".encode(), {"Лидерство": comp}, 5)
        with pytest.raises(ValueError):
            await qi.apply(quiz["id"], bad)
        with pytest.raises(ValueError):
            await qi.apply(quiz["id"], qi.ParsedQuiz())
        assert (await qz.get_quiz(quiz["id"]))["content_version"] == version

    run(go())
