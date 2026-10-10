"""Строка «Работа» в сводке анкеты — только если вопрос задавали."""
from domain.regform.engine import summary_fields


def _labels(answers):
    return dict(summary_fields(answers))


def test_work_row_absent_when_step_not_asked():
    assert "Работа" not in _labels({"full_name": "Аня", "city": "Москва"})


def test_work_row_shows_no_when_answered_no():
    assert _labels({"work_status": False})["Работа"] == "Нет"


def test_work_row_shows_yes_when_answered_yes():
    assert _labels({"work_status": True})["Работа"] == "Да"
