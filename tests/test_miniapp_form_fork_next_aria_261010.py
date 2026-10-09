"""Приёмка 09.10 (Mini App): кнопка «Дальше» на выборе города для скринридера звалась
«Выбери город мероприятия:» (`aria-label` = вопрос развилки) — снимок доступности не находил
кнопку «Дальше». Имя кнопки — её подпись `d.next_cta_text`, как на остальных экранах мастера.
"""
from __future__ import annotations

from tests.test_miniapp_frontend import _js_without_comments
from tests.test_miniapp_resume_fork_edit_js_260927 import FORM_SCREEN_JS, _between


def test_fork_next_button_aria_label_is_its_caption():
    body = _between(_js_without_comments(FORM_SCREEN_JS), "function drawFork(item)", "function drawPre(")
    button = body[body.index('h("button", {'):]
    button = button[:button.index("}, icon(")]
    assert '"aria-label": d.next_cta_text' in button
    assert "item.text" not in button
