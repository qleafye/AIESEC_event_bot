"""Живой прогон Mini App на стенде (16.09) — экран «Проверь перед отправкой»
(`screens/form.js::drawReview`):

1. Счётчик «N из M заполнено, K необязательных пропущено» считал пропущенный необязательный
   шаг заполненным — «Пропустить» пишет плейсхолдер «-» (тот же приём, что в боте), а не
   пустую строку, а общий `stepAnswered` (v != null && v !== "") этот плейсхолдер не отличает
   от настоящего ответа.
2. Тот же пропущенный шаг рисовался строкой с пустым значением в группе вместо свёрнутого
   «Пропущено необязательное: …» — прямое следствие пункта 1 (шаг не попадал в `skipped`).
3. ФИО (`full_name`) отсутствовало в таблице `REVIEW_GROUPS` и падало в дефолтную «Форум»
   вместо «О тебе».

Тесты структурные (regex по исходнику JS) — тот же приём, что `test_reg_form_v2_uat_260915.py`
и соседи: node-раннер для одной функции внутри модуля с side-effect-иерархией (замыкания
`render()`) был бы куда дороже одного grep по нужному куску.
"""
from tests.test_miniapp_frontend import SCREENS_DIR, _js_without_comments

FORM_SCREEN_JS = SCREENS_DIR / "form.js"


def _text() -> str:
    return _js_without_comments(FORM_SCREEN_JS)


def test_review_groups_places_full_name_in_about():
    """full_name обязан явно лежать в REVIEW_GROUPS -> "about" (не проваливаться в дефолтный
    "event" через reviewGroupOf)."""
    text = _text()
    table = text[text.index("const REVIEW_GROUPS = {"):text.index("function reviewGroupOf(")]
    assert '"about"' in table
    row = table[table.index("full_name:"):table.index("full_name:") + 40]
    assert '"about"' in row, row


def test_review_answered_helper_excludes_dash_placeholder():
    """Новый хелпер обзора не считает «-» ответом (сервер тоже — `reg_engine.form_spec::
    has_answer`, `v not in (None, "", "-")`), в отличие от общего `stepAnswered` (его контракт
    зафиксирован `test_form_screen_stepanswered_contract_unchanged` и не должен меняться —
    «-» там означает настоящий сохранённый ответ, например «аллергий нет»)."""
    text = _text()
    start = text.index("function reviewAnswered(")
    end = text.index("\n}", start)
    body = text[start:end]
    assert "spec.columns" in body
    assert 'v !== "-"' in body


def test_draw_review_uses_review_answered_not_step_answered_for_skip_split():
    """`drawReview` обязан звать СВОЙ хелпер (`reviewAnswered`), а не общий `stepAnswered` —
    иначе плейсхолдер «-» снова будет считаться заполненным ответом на обзоре."""
    text = _text()
    start = text.index("function drawReview() {")
    end = text.index("\n    }", start)
    body = text[start:end]
    assert "reviewAnswered(spec, state)" in body
    assert "stepAnswered(spec, state)" not in body


def test_step_answered_contract_still_treats_dash_as_answered():
    """Сторож встречной границы: общий `stepAnswered` (мастер/обзор точечной правки) НЕ
    получил регресс — «-» там по-прежнему «отвечено» (дословный контракт из
    test_form_screen_stepanswered_contract_unchanged)."""
    text = _text()
    start = text.index("function stepAnswered(")
    end = text.index("\n}", start)
    body = text[start:end]
    assert 'v !== "-"' not in body
    assert 'v != null && v !== ""' in body
