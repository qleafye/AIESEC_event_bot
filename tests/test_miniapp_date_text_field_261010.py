"""Приёмка 09.10 (Mini App): поле даты — нативное `type="date"` при подсказке «ДД.ММ.ГГГГ».

Вопрос, подсказка и ошибка формата говорят о ручном вводе «ДД.ММ.ГГГГ», а в нативный пикер
такое не впечатать (playwright: «Malformed value»). Выбран один способ, согласованный с чатом и
со всеми текстами: текстовое поле с цифровой клавиатурой, точки поле ставит само.

Node-подпроцесс с фейковым DOM из `tests/test_miniapp_form_controls_js_260911.py`.
"""
from __future__ import annotations

import json
import shutil
import subprocess

import pytest

import reg_engine

from tests.test_miniapp_form_controls_js_260911 import _FAKE_DOM_PRELUDE
from tests.test_miniapp_frontend import FORM_JS

_SCRIPT = _FAKE_DOM_PRELUDE + """
const m = await import(%(url)s);
const out = {};
out.mask = ["1", "15", "150", "1503", "15032", "15032007", "150320071", "15.03.2007", "ab15c03"].map(m.maskDateInput);

const calls = [];
const wrap = m.field(h, { key: "birth_date", type: "date", label: "Дата рождения" }, null, (v, o) => calls.push([v, o || null]));
const input = wrap._nodes.control;
out.type = input.getAttribute("type");
out.inputmode = input.getAttribute("inputmode");
input.value = "15032007";
input.dispatch("input", {});
out.typed = input.value;
out.lastCall = calls[calls.length - 1];

out.storedRu = m.field(h, { key: "birth_date", type: "date", label: "Д" }, "15.03.2007", () => {})._nodes.control.value;
out.storedIso = m.field(h, { key: "birth_date", type: "date", label: "Д" }, "2007-03-15", () => {})._nodes.control.value;
// Ревью 10.10: правка в середине и Backspace по точке.
const e = (prev, raw, caret, type) => m.maskDateEdit(prev, raw, caret, type);
out.midInsert = e("15.03.2007", "15.093.2007", 5, "insertText");      // вставили 9 после «0» месяца
out.midDelete = e("15.03.2007", "15.0.2007", 4, "deleteContentBackward"); // стёрли «3»
out.dotBackspace = e("15.03", "1503", 2, "deleteContentBackward");   // Backspace сразу после точки
out.typeEnd = e("15.0", "15.03", 5, "insertText");
out.typeThird = e("15", "150", 3, "insertText");
console.log(JSON.stringify(out));
"""


@pytest.fixture(scope="module")
def result(tmp_path_factory):
    node = shutil.which("node")
    if not node:
        pytest.skip("node не найден в PATH")
    script = tmp_path_factory.mktemp("date") / "run.mjs"
    script.write_text(_SCRIPT % {"url": json.dumps(FORM_JS.as_uri())}, encoding="utf-8")
    res = subprocess.run([node, str(script)], capture_output=True, text=True, encoding="utf-8", timeout=60)
    assert res.returncode == 0, res.stderr
    return json.loads(res.stdout.strip().splitlines()[-1])


def test_field_is_text_with_numeric_keyboard(result):
    assert result["type"] == "text"
    assert result["inputmode"] == "numeric"


def test_dots_are_inserted_while_typing(result):
    assert result["mask"] == [
        "1", "15", "15.0", "15.03", "15.03.2", "15.03.2007", "15.03.2007", "15.03.2007", "15.03",
    ]
    assert result["typed"] == "15.03.2007"


def test_typing_does_not_commit_step(result):
    assert result["lastCall"] == ["15.03.2007", None]


def test_stored_value_shown_in_chat_format(result):
    assert result["storedRu"] == "15.03.2007"
    assert result["storedIso"] == "15.03.2007"


def test_masked_value_passes_server_validator():
    assert reg_engine.validate_answer("birth_date", "15.03.2007") == ("15.03.2007", None)


def test_middle_insert_keeps_caret_after_typed_digit(result):
    # Цифры: 1 5 0 9 3 2 0 0 7 -> лимит 8 срезает хвост, каретка — сразу после «9».
    assert result["midInsert"] == {"value": "15.09.3200", "caret": 5}


def test_middle_delete_keeps_caret_in_place(result):
    assert result["midDelete"] == {"value": "15.02.007", "caret": 4}


def test_backspace_after_dot_removes_digit_before_it(result):
    assert result["dotBackspace"] == {"value": "10.3", "caret": 1}


def test_typing_at_end_keeps_caret_at_end(result):
    assert result["typeEnd"] == {"value": "15.03", "caret": 5}
    assert result["typeThird"] == {"value": "15.0", "caret": 4}
