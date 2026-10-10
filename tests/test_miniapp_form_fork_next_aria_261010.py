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


def test_fork_next_button_disabled_until_choice():
    """Приёмка 10.10: «Дальше» на выборе города без выбора давала «Некорректный выбор.».
    Кнопка неактивна, пока ничего не выбрано, и оживает по тапу на вариант."""
    body = _between(_js_without_comments(FORM_SCREEN_JS), "function drawFork(item)", "function drawPre(")
    button = body[body.index('h("button", {'):]
    button = button[:button.index("}, icon(")]
    assert "disabled: busy || !chosen" in button
    assert "nextBtn = h(\"button\"" in body
    pick = body[body.index("field(h, {"):]
    pick = pick[:pick.index("const errorZone")]
    assert "syncNext()" in pick
    assert "nextBtn.disabled = busy || !chosen" in body
