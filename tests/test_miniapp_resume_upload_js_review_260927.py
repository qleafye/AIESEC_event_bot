"""Ревью квика 27.09 — клиентская половина доводок загрузки резюме в Mini App.

1. Обзор правки: «Отправить изменения» ждёт загрузку файла в полёте (как «Дальше» мастера) —
   иначе submit захватывал черновик, загрузка получала 403, и новый файл терялся.
2. Мастер: промис загрузки не переживает ухода с шага резюме (Назад, смена ветки развилки) —
   «Дальше» на других шагах не выключается, а завершившаяся старая загрузка не двигает чужой
   шаг сама.
3. Обзор правки: у ветки «Файл» нет галки подтверждения — файл применяется сразу при выборе,
   галка писала бы в состояние сам объект файла.
4. Мастер: ответ сервера «файл не загружен» возвращает на шаг резюме, только если такой шаг
   есть в анкете, — иначе уходил на первый шаг и подача зацикливалась.

Структурные сторожа по `screens/form.js` без комментариев — тем же приёмом, что
`tests/test_miniapp_resume_fork_edit_js_260927.py`.
"""
from __future__ import annotations

from tests.test_miniapp_resume_fork_edit_js_260927 import _between, _screen_text


def _overview() -> str:
    text = _screen_text()
    return _between(text, "async function renderOverview(", "async function renderWizard(")


def _wizard() -> str:
    text = _screen_text()
    return text[text.index("async function renderWizard("):]


# ── 1. Обзор правки ждёт загрузку ────────────────────────────────────────────────────────

def test_overview_keeps_upload_promise():
    body = _overview()
    assert "let pendingUpload = null" in body
    row = _between(body, "function fieldRow(spec)", "function drawList()")
    call = row.index("uploadResume(")
    assert "pendingUpload = upload" in row[call:]


def test_overview_submit_waits_for_upload_before_submit():
    body = _overview()
    submit = _between(body, "async function submitChanges()", "async function pickResumeBranchInOverview(")
    wait = submit.index("await pendingUpload")
    assert wait < submit.index('api("/reg/draft/submit"')


def test_overview_submit_buttons_disabled_while_uploading():
    body = _overview()
    disabled = _between(body, "function submitDisabled()", "}")
    assert "pendingUpload" in disabled
    draw = _between(body, "function drawList()", "drawList();\n  }")
    assert "disabled: submitDisabled()" in draw
    assert draw.count("submitDisabled()") >= 2  # кнопка футера и MainButton


# ── 2. Мастер: загрузка не переживает ухода с шага ───────────────────────────────────────

def test_wizard_go_back_drops_pending_upload():
    go_back = _between(_wizard(), "function goBack()", "const showProgress")
    assert "pendingUpload = null" in go_back


def test_wizard_branch_pick_drops_pending_upload():
    pick = _between(_wizard(), "async function pickResumeBranch(code)", "function compositeErrorText(")
    assert "pendingUpload = null" in pick


def test_wizard_stale_upload_does_not_advance_foreign_step():
    draw = _between(_wizard(), "function drawStep()", "async function goNext()")
    upload = draw[draw.index("uploadResume("):]
    on_done = upload[upload.index("onDone:"):upload.index("pendingUpload = upload")]
    assert "mySeq === drawSeq" in on_done


def test_wizard_main_button_waits_for_upload_only_on_resume_step():
    body = _between(_wizard(), "function currentMainDisabled()", "function syncMainButton()")
    assert "pendingUpload" in body
    assert 'rawSpec.key === "resume"' in body


# ── 3. Нет галки у ветки «Файл» в обзоре ─────────────────────────────────────────────────

def test_overview_file_branch_has_no_confirm_button():
    body = _overview()
    spec_fn = _between(body, "function resumeEditSpec(spec)", "function fieldRow(spec)")
    assert "__resumeForkFile: true" in spec_fn
    row = _between(body, "function fieldRow(spec)", "function drawList()")
    confirm = row[row.index("const confirmBtn"):]
    assert "spec.__resumeForkFile" in confirm[:confirm.index("h(\"button\"")]


# ── 4. Нет шага «Резюме» — не прыгаем на первый шаг ──────────────────────────────────────

def test_submit_form_missing_file_checks_resume_step_exists():
    text = _screen_text()
    body = _between(text, "async function submitForm()", "drawCurrent();\n  }\n}")
    branch = body[body.index('"resume_file_missing"'):]
    branch = branch[:branch.index("} else if")]
    assert 'state.specs.some((s) => s.key === "resume")' in branch
