"""Квик 260911-2kb (W1 «Анкета не врёт») — пять УАТ-регрессов анкеты Mini App, найденных
разведкой по коду и подтверждённых на живом стенде YL26 в ночь 10-11.09.

Один файл на все пять пунктов — это одна и та же категория бага (приложение показывает
делегату не то, что реально лежит в базе), просто в пяти разных слоях: фронтовое состояние
мастера (1), контрол закрытого списка (2), спека шага (3), контракт колонок шага (4),
серверный расчёт дельты PATCH (5).

pytest-asyncio в этом окружении не установлен — каждый async-вызов гоняется через
`asyncio.run()`, харнесс HTTP — `tests/test_miniapp_routes.py`/`tests/test_miniapp_form.py`
(та же временная БД на запрос, `config.DB_PATH` в `tmp_path`).
"""
from __future__ import annotations

import asyncio
import re

import reg_engine

from tests.test_miniapp_frontend import (
    SCREENS_DIR,
    _HEX_OR_RGB_COLOR,
    _STRING_LITERAL,
    _js_without_comments,
)
from tests.test_miniapp_form import _seed_draft
from tests.test_miniapp_routes import (
    DELEGATE_ID,
    _cfg,
    _client,
    _hdr,
    _set,
    _standard_seed,
    _use_tmp_db,
)

FORM_SCREEN_JS = SCREENS_DIR / "form.js"
_CYRILLIC = re.compile(r"[А-Яа-яЁё]")


def _run(coro):
    return asyncio.run(coro)


def _ready(tmp_path, name="miniapp_form_regress_260911.db"):
    db_path = _use_tmp_db(tmp_path, name)
    _standard_seed()
    return db_path


# ══════════════════════════════════════════════════════════════════════════════════════════
# Пункт 1: ответ сервера пересобирает список шагов, а не только значения (goNext/adoptDraft)
# ══════════════════════════════════════════════════════════════════════════════════════════

def test_patch_response_drops_disabled_conditional_steps(tmp_path):
    """Выключение условия («не учусь») в ответе того же PATCH убирает course/university/
    specialty/study_field из `steps` — контракт, на который теперь опирается `adoptDraft`."""
    db_path = _ready(tmp_path)
    _set("edu_conditional", "on")
    version = _seed_draft(DELEGATE_ID, kind="new", event_city=None, patch={}, source="miniapp")
    client = _client(_cfg(db_path))
    resp = client.patch(
        "/app/api/reg/draft", headers=_hdr(DELEGATE_ID),
        json={"version": version, "answers": {"education_status": "Нет, завершил(а) обучение"},
              "step": "education_status"},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    step_keys = [s["key"] for s in body["steps"]]
    for disabled in ("course", "university", "specialty", "study_field"):
        assert disabled not in step_keys, body["steps"]
    # Пункт 1 (защита от цикла): res.step — «ещё не отвеченный» шаг, никогда сам только что
    # отвеченный education_status.
    assert body["step"] != "education_status"
    if body["step"] != reg_engine.STEP_DONE:
        assert body["step"] in step_keys


def test_form_screen_go_next_adopts_full_draft_not_step_increment():
    text = _js_without_comments(FORM_SCREEN_JS)
    assert "stepIndex += 1" not in text
    start = text.index("async function goNext(")
    end = text.index("async function goSkip(")
    body = text[start:end]
    assert "adoptDraft(res)" in body
    assert "applyServer" not in body


def test_form_screen_submit_changes_rebuilds_state_from_patch_response():
    text = _js_without_comments(FORM_SCREEN_JS)
    start = text.index("async function submitChanges(")
    end = text.index("function fieldRow(")
    body = text[start:end]
    patch_idx = body.index('method: "PATCH"')
    tail = body[patch_idx:]
    assert "buildFormState(" in tail


def test_form_screen_still_has_no_human_literals_or_innerhtml():
    text = _js_without_comments(FORM_SCREEN_JS)
    for m in _STRING_LITERAL.finditer(text):
        assert not _CYRILLIC.search(m.group(0)), m.group(0)
    assert "innerHTML" not in text
    assert not _HEX_OR_RGB_COLOR.search(text)
