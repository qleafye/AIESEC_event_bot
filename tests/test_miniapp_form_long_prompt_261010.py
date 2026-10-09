"""Приёмка 09.10 (Mini App): длинный вопрос резюме — гигантский курсивный заголовок.

Вопрос резюме — абзац с нумерованным списком; в плите шага он шёл крупным курсивом на весь
экран телефона, а переносы строк схлопывались, и «1. 2. 3.» склеивались в одну строку.
Длинный вопрос (длиннее порога или с переносами) теперь идёт под плитой обычным текстом с
`white-space: pre-line`, в заголовке плиты — короткая подпись шага.
"""
from __future__ import annotations

from pathlib import Path

from tests.test_miniapp_frontend import _js_without_comments
from tests.test_miniapp_resume_fork_edit_js_260927 import FORM_SCREEN_JS, _between

APP_CSS = Path(__file__).resolve().parent.parent / "miniapp" / "static" / "app.css"


def _draw_step() -> str:
    return _between(_js_without_comments(FORM_SCREEN_JS), "function drawStep()", "function splitTemplate(")


def test_long_prompt_detected_by_length_and_newlines():
    body = _draw_step()
    assert "promptText.length > LONG_PROMPT_CHARS" in body
    assert 'promptText.includes("\\n")' in body


def test_long_prompt_title_is_short_label_and_text_goes_below():
    body = _draw_step()
    assert "longPrompt ? labelText(spec.label) : promptText" in body
    assert '"step-prompt-long"' in body
    nodes = body[body.index("const stepNodes = ["):]
    assert nodes.index("plate,") < nodes.index("longPromptNode,")


def test_long_prompt_keeps_line_breaks():
    css = APP_CSS.read_text(encoding="utf-8")
    rule = css[css.index(".step-prompt-long {"):]
    rule = rule[:rule.index("}")]
    assert "white-space: pre-line" in rule
