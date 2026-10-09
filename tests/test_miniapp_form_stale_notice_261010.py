"""Приёмка 09.10 (Mini App): ошибка резюме «залипала» на экране.

«Файл резюме ещё не загрузился…» — плашка `say(…)` над всем экраном анкеты. Она оставалась и
после смены способа резюме на текст, и на экране «Заявка принята». Плашку снимают: экран
«Заявка принята», смена ветки развилки и принятый сервером ответ шага.

Структурные сторожа по `screens/form.js` без комментариев — тем же приёмом, что
`tests/test_miniapp_resume_other_way_js_260927.py`.
"""
from __future__ import annotations

from tests.test_miniapp_frontend import _js_without_comments
from tests.test_miniapp_resume_fork_edit_js_260927 import FORM_SCREEN_JS, _between


def _screen() -> str:
    return _js_without_comments(FORM_SCREEN_JS)


def test_complete_screen_clears_notice():
    body = _between(_screen(), "function renderComplete(res)", "function renderAmbassadorOffer(")
    assert 'say("")' in body


def test_resume_branch_switch_clears_notice():
    body = _between(_screen(), "async function pickResumeBranch(code)", "function compositeErrorText(")
    ok = body[body.index("busy = false;"):body.index("if (staysOnStep)")]
    assert 'say("")' in ok


def test_accepted_step_clears_notice():
    body = _between(_screen(), "async function goNext()", "async function goSkip()")
    ok = body[body.index("adoptDraft(res);"):body.index("} catch (err)")]
    assert 'say("")' in ok
