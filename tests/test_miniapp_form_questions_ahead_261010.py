"""Приёмка 09.10 (Mini App): «Впереди ещё вопросов: 1» на шаге 9 из 12.

Окно вопросов мастера показывает два ближайших вопроса, а строка под ним подставляла в {n}
только те, что за окном (3 − 2 = 1). Делегат читает её как «сколько осталось до конца» —
и видел 1 при трёх оставшихся. Число теперь — все вопросы после текущего; строка, как и
раньше, появляется только когда за окном что-то есть.
"""
from __future__ import annotations

from domain.settings.schema import SETTINGS_SCHEMA

from tests.test_miniapp_frontend import _js_without_comments
from tests.test_miniapp_resume_fork_edit_js_260927 import FORM_SCREEN_JS, _between


def _window() -> str:
    return _between(_js_without_comments(FORM_SCREEN_JS), "function drawQuestionWindow(specs)", "const FORK_BACK_STEPS")


def test_number_is_all_questions_after_current():
    body = _window()
    assert 'replace("{n}", String(totalUpcoming))' in body
    assert 'replace("{n}", String(remaining))' not in body


def test_line_still_only_when_something_is_beyond_window():
    assert "remaining > 0" in _window()


def test_registry_prompt_says_until_the_end():
    assert "до конца анкеты" in SETTINGS_SCHEMA["reg_form_more_questions_text"]["prompt"]
