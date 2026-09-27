"""Обзор правки: «Другой способ» у строки «Резюме» после выбора ветки развилки.

Раньше `resumeEditBranch` сбрасывался только обновлением экрана или «Отменить изменения»:
делегат, промахнувшийся «Файл» вместо «Текстом», мог вернуться к кнопкам развилки, только
потеряв ВСЕ несохранённые правки. Теперь у подменённой строки есть кнопка (текст — реестр
`reg_form_resume_other_way_text`, отдаётся черновиком как `resume_other_way_text`), которая
возвращает кнопки развилки ТОЛЬКО этой строке: промис загрузки файла снимается (как у «Назад»
мастера), доехавшая старая загрузка список не перерисовывает, остальные правки не трогаются.

Структурные сторожа по `screens/form.js` без комментариев — тем же приёмом, что
`tests/test_miniapp_resume_upload_js_review_260927.py`.
"""
from __future__ import annotations

from services.i18n_form_manual import _REGISTRY_TEXTS_EN
from settings_schema import SETTINGS_SCHEMA
from settings_synonyms import SETTINGS_SYNONYMS

from tests.test_miniapp_frontend import _js_without_comments
from tests.test_miniapp_resume_fork_edit_js_260927 import _between, _screen_text

KEY = "reg_form_resume_other_way_text"
ROOT_FORM_PY = __import__("pathlib").Path(__file__).resolve().parent.parent / "miniapp" / "routers" / "form.py"


def _overview() -> str:
    return _between(_screen_text(), "async function renderOverview(", "async function renderWizard(")


def _back_fn() -> str:
    return _between(_overview(), "function backToResumeFork()", "function resumeEditSpec(spec)")


# ── Реестр и доставка ─────────────────────────────────────────────────────────────────────

def test_registry_key_is_human_reg_text_with_synonyms_and_english():
    entry = SETTINGS_SCHEMA[KEY]
    assert entry["type"] == "text" and entry["group"] == "reg"
    assert entry["default"].strip() and entry["prompt"].strip()
    assert entry["label"].strip() and "reg_form" not in entry["label"]
    assert len(SETTINGS_SYNONYMS[KEY]) >= 2
    assert entry["default"] in _REGISTRY_TEXTS_EN


def test_key_sits_with_resume_neighbours():
    keys = list(SETTINGS_SCHEMA)
    assert keys.index(KEY) == keys.index("reg_form_resume_file_missing_text") + 1


def test_draft_payload_carries_text():
    src = ROOT_FORM_PY.read_text(encoding="utf-8")
    assert f'"resume_other_way_text": await i18n.tr_setting("{KEY}", lang, tr_map)' in src


# ── Экран ─────────────────────────────────────────────────────────────────────────────────

def test_back_resets_only_resume_branch():
    body = _back_fn()
    assert "resumeEditBranch = null" in body
    assert "drawList()" in body
    # Правки остальных строк живут в state — пересобирать его нельзя.
    assert "buildFormState" not in body
    assert "state = " not in body
    assert 'api("/reg/draft"' not in body


def test_back_drops_pending_upload_like_wizard_go_back():
    body = _back_fn()
    assert "pendingUpload = null" in body
    assert "syncSubmitButtons()" in body
    assert "resumeUploadSeq += 1" in body


def test_back_ignored_while_busy():
    assert "if (busy) return;" in _back_fn()


def test_back_reopens_fork_buttons():
    assert "openers[" in _back_fn()


def test_stale_upload_does_not_redraw_after_back():
    row = _between(_overview(), "function fieldRow(spec)", "function drawList()")
    upload = row[row.index("uploadResume("):]
    on_done = upload[upload.index("onDone:"):upload.index("pendingUpload = upload")]
    assert "resumeUploadSeq" in on_done


def test_button_shown_only_on_substituted_resume_row():
    row = _between(_overview(), "function fieldRow(spec)", "function drawList()")
    btn = row[row.index("const otherWayBtn"):]
    btn = btn[:btn.index(";\n")]
    assert "spec.__resumeForkFile || spec.__resumeForkText" in btn
    assert "d.resume_other_way_text" in btn
    assert "backToResumeFork" in btn
    assert "otherWayBtn" in row[row.index("return h(\"div\", {}, row"):]


def test_screen_has_no_literal_other_way_text():
    text = _js_without_comments(ROOT_FORM_PY.parent.parent / "static" / "js" / "screens" / "form.js")
    assert "Другой способ" not in text
