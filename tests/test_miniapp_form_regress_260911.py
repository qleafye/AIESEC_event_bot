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
import json
import re

import reg_engine
import settings_schema
import settings_synonyms
from database import db as bot_db

from tests.test_miniapp_frontend import (
    SCREENS_DIR,
    _HEX_OR_RGB_COLOR,
    _STRING_LITERAL,
    _js_without_comments,
)
from tests.test_miniapp_form import _draft_row, _fill, _seed_draft
from tests.test_miniapp_routes import (
    DELEGATE_ID,
    UNREGISTERED_ID,
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


# ══════════════════════════════════════════════════════════════════════════════════════════
# Пункт 3: кнопка «Пропустить» на всех необязательных шагах
# ══════════════════════════════════════════════════════════════════════════════════════════

def test_every_skip_allowed_step_gets_skip_label(tmp_path):
    _ready(tmp_path)

    async def go():
        return {
            step: (await reg_engine.step_spec(step)).get("skip_label")
            for step in reg_engine._SKIP_ALLOWED_STEPS
        }

    labels = _run(go())
    for step, label in labels.items():
        assert label, step
    mini_label = labels.pop("mini_portfolio")
    assert mini_label
    # Одиннадцать остальных делят один общий текст реестра (reg_form_skip_cta_text).
    assert len(set(labels.values())) == 1


def test_only_skip_allowed_steps_get_skip_label(tmp_path):
    _ready(tmp_path)

    async def go():
        out = {}
        for step_key, *_rest in reg_engine.REG_FLOW:
            out[step_key] = (await reg_engine.step_spec(step_key)).get("skip_label")
        return out

    specs = _run(go())
    for step_key, label in specs.items():
        if step_key in reg_engine._SKIP_ALLOWED_STEPS:
            assert label, step_key
        else:
            assert label is None, step_key


def test_skip_allowed_and_skip_text_errors_sets_match():
    """Сторож пары наборов: расхождение означало бы, что будущий skip-шаг получит кнопку
    «Пропустить», а `validate_answer` не примет буквальный «-» (делегат снова упрётся), либо
    наоборот — «-» проходит валидатор без кнопки, которая его отправляет."""
    assert set(reg_engine._SKIP_ALLOWED_STEPS) == set(reg_engine._SKIP_TEXT_ERRORS)


def test_skip_dash_validates_and_empty_still_errors():
    for step in reg_engine._SKIP_ALLOWED_STEPS:
        value, err = reg_engine.validate_answer(step, "-")
        assert (value, err) == ("-", None), step
        empty_value, empty_err = reg_engine.validate_answer(step, "")
        assert empty_value is None, step
        assert empty_err, step


def test_manager_can_change_skip_button_label_seen_by_delegate(tmp_path):
    _ready(tmp_path)
    _set("reg_form_skip_cta_text", "Скип")

    async def go():
        return await reg_engine.step_spec("comments")

    spec = _run(go())
    assert spec["skip_label"] == "Скип"


def test_skip_cta_text_registered_in_schema_and_synonyms():
    entry = settings_schema.SETTINGS_SCHEMA["reg_form_skip_cta_text"]
    assert entry["group"] == "reg"
    assert entry["default"] == "Пропустить"
    assert len(settings_synonyms.SETTINGS_SYNONYMS["reg_form_skip_cta_text"]) >= 2


# ══════════════════════════════════════════════════════════════════════════════════════════
# Пункт 4: шаг заполнен по НАБОРУ своих колонок, а не по одной
# ══════════════════════════════════════════════════════════════════════════════════════════

def test_step_spec_columns_match_columns_for_step_for_every_step(tmp_path):
    _ready(tmp_path)

    async def go():
        return {
            step_key: (await reg_engine.step_spec(step_key))["columns"]
            for step_key, *_rest in reg_engine.REG_FLOW
        }

    columns_by_step = _run(go())
    for step_key, columns in columns_by_step.items():
        expected = reg_engine.columns_for_step(step_key) or [reg_engine.STEP_TO_COLUMN.get(step_key, step_key)]
        assert columns == expected, step_key
    assert columns_by_step["resume"] == list(reg_engine._EXTRA_ANSWER_COLUMNS)
    assert len(columns_by_step["resume"]) == 3
    assert columns_by_step["specialty"] == ["specialty"]
    assert columns_by_step["vk"] == ["vk_username"]


def test_form_spec_resume_file_answered_shows_filename_not_raw_id(tmp_path):
    _ready(tmp_path)
    _set("reg_q_resume", "on")

    async def go():
        return await reg_engine.form_spec(
            {"resume_file_id": "RAWFILEID_SENTINEL_1", "resume_file_name": "resume.pdf"},
            participant_type="full",
        )

    spec_form = _run(go())
    resume_spec = next(s for s in spec_form["steps"] if s["key"] == "resume")
    assert resume_spec["value_source"] == "answer"
    assert resume_spec["display"] == "resume.pdf"
    assert resume_spec["value"] is None
    assert set(resume_spec["values"].keys()) == set(reg_engine.columns_for_step("resume"))
    assert spec_form["progress"]["done"] >= 1


def test_form_spec_legacy_resume_file_id_only_hides_raw_id_everywhere(tmp_path):
    """Легаси-строка: только resume_file_id, без имени файла — шаг отвечен, но показать
    нечего (нет человекочитаемой колонки-компаньона), сырой id не уходит НИ В ОДНОМ поле
    спеки, кроме самого `values` (там он законно лежит под своим именем колонки)."""
    _ready(tmp_path)
    _set("reg_q_resume", "on")
    sentinel = "RAWFILEID_SENTINEL_LEGACY_2"

    async def go():
        return await reg_engine.form_spec({"resume_file_id": sentinel}, participant_type="full")

    spec_form = _run(go())
    resume_spec = next(s for s in spec_form["steps"] if s["key"] == "resume")
    assert resume_spec["value_source"] == "answer"
    spec_without_values = {k: v for k, v in resume_spec.items() if k != "values"}
    assert sentinel not in json.dumps(spec_without_values)
    assert resume_spec.get("display") in (None, "")


def test_form_spec_resume_text_unchanged(tmp_path):
    _ready(tmp_path)
    _set("reg_q_resume", "on")

    async def go():
        return await reg_engine.form_spec({"resume_text": "Мой опыт в продажах"}, participant_type="full")

    spec_form = _run(go())
    resume_spec = next(s for s in spec_form["steps"] if s["key"] == "resume")
    assert resume_spec["value"] == "Мой опыт в продажах"
    assert resume_spec.get("display") in (None, "")


def test_answers_from_user_row_carries_resume_columns_prior_does_not():
    user_row = {
        "full_name": "Тест Тестов", "resume_file_id": "RAWFILEID_SENTINEL_3",
        "resume_file_name": "cv.pdf", "resume_text": None,
    }
    out = reg_engine.answers_from_user_row(user_row)
    assert out.get("resume_file_id") == "RAWFILEID_SENTINEL_3"
    assert out.get("resume_file_name") == "cv.pdf"
    prior = reg_engine.prior_answers_for(user_row)
    assert "resume" not in prior
    # Узкий UPDATE финала обязан знать каждую колонку снимка — иначе `no such column`.
    for col in out:
        assert col in reg_engine.answer_columns(), col


def test_http_get_draft_shows_resume_answered_for_edit_without_draft_row(tmp_path):
    """Одобренный делегат с резюме-файлом в `users` и БЕЗ строки `reg_drafts` — раньше шаг
    резюме читался «не заполнено» (`answers_from_user_row` резюме не знал вовсе). Теперь
    `value_source` становится "answer"; человекочитаемого имени файла у такого делегата в
    базе физически нет (`resume_file_name` не персистится в `users`, см. `DRAFT_ONLY_COLUMNS`,
    только `reg_drafts`/финал) — раскрытие сырого id при этом всё равно исключено."""
    db_path = _ready(tmp_path)
    _set("reg_q_resume", "on")
    sentinel = "RAWFILEID_SENTINEL_HTTP_4"
    _fill(DELEGATE_ID, resume_file_id=sentinel, participant_type="full")
    client = _client(_cfg(db_path))
    resp = client.get("/app/api/reg/draft", headers=_hdr(DELEGATE_ID))
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["exists"] is False
    resume_spec = next(s for s in body["steps"] if s["key"] == "resume")
    assert resume_spec["value_source"] == "answer"
    spec_without_values = {k: v for k, v in resume_spec.items() if k != "values"}
    assert sentinel not in json.dumps(spec_without_values)


def test_form_screen_uses_values_and_columns_without_resume_column_literals():
    text = _js_without_comments(FORM_SCREEN_JS)
    assert "s.values" in text
    assert "spec.columns" in text
    for literal in ('"resume_file_id"', '"resume_file_name"', '"resume_text"'):
        assert literal not in text, literal


# ══════════════════════════════════════════════════════════════════════════════════════════
# Пункт 5: первый PATCH из приложения не обнуляет уже поданную анкету
# ══════════════════════════════════════════════════════════════════════════════════════════

def test_bootstrap_patch_on_submitted_anketa_persists_snapshot_not_empty(tmp_path):
    """`uploadResume` бутстрапит черновик именно так — `{version: 0, answers: {}}`. Живой
    симптом: строка `reg_drafts` создавалась с `answers={}`, вся анкета «слетала»."""
    db_path = _ready(tmp_path)
    _fill(DELEGATE_ID, age=25, phone="+79001234567")
    client = _client(_cfg(db_path))
    resp = client.patch(
        "/app/api/reg/draft", headers=_hdr(DELEGATE_ID), json={"version": 0, "answers": {}},
    )
    assert resp.status_code == 200, resp.text
    row = _draft_row(DELEGATE_ID)
    assert row is not None
    assert row["answers"] != {}
    assert row["answers"].get("age") == 25
    assert row["answers"].get("phone") == "+79001234567"
    assert row["answers"].get("full_name")


def test_get_after_bootstrap_patch_shows_answer_not_prior_or_empty(tmp_path):
    db_path = _ready(tmp_path)
    _fill(DELEGATE_ID, age=25)
    client = _client(_cfg(db_path))
    resp = client.patch(
        "/app/api/reg/draft", headers=_hdr(DELEGATE_ID), json={"version": 0, "answers": {}},
    )
    assert resp.status_code == 200, resp.text
    body = client.get("/app/api/reg/draft", headers=_hdr(DELEGATE_ID)).json()
    age_spec = next(s for s in body["steps"] if s["key"] == "age")
    assert age_spec["value_source"] == "answer"
    assert age_spec["value"] == 25


def test_bootstrap_patch_drops_underscore_key_and_source_step_stays_off(tmp_path):
    db_path = _ready(tmp_path)
    _fill(DELEGATE_ID, source_from_tag=1)
    client = _client(_cfg(db_path))
    resp = client.patch(
        "/app/api/reg/draft", headers=_hdr(DELEGATE_ID), json={"version": 0, "answers": {}},
    )
    assert resp.status_code == 200, resp.text
    row = _draft_row(DELEGATE_ID)
    assert "_source_from_tag" not in row["answers"]
    step_keys = [s["key"] for s in resp.json()["steps"]]
    assert "source" not in step_keys


def test_new_delegate_patch_is_byte_for_byte_no_snapshot(tmp_path):
    """`kind == "new"` (делегат без поданной анкеты) — `ctx["answers"]` и так пуст, снимка
    заводить неоткуда: PATCH создаёт строку ровно с тем, что делегат ответил."""
    db_path = _ready(tmp_path)
    client = _client(_cfg(db_path))
    resp = client.patch(
        "/app/api/reg/draft", headers=_hdr(UNREGISTERED_ID),
        json={"version": 0, "answers": {"full_name": "Иван Иванов"}},
    )
    assert resp.status_code == 200, resp.text
    row = _draft_row(UNREGISTERED_ID)
    assert set(row["answers"].keys()) == {"full_name"}


def test_draft_step_contract_unaffected_by_baseline_change(tmp_path):
    db_path = _ready(tmp_path)
    client = _client(_cfg(db_path))
    resp = client.patch(
        "/app/api/reg/draft", headers=_hdr(DELEGATE_ID), json={"version": 0, "answers": {}},
    )
    assert resp.status_code == 200, resp.text
    # answers пуст, step не передан -> COALESCE оставляет прежнее/NULL.
    assert _draft_row(DELEGATE_ID)["step"] is None

    body2 = client.patch(
        "/app/api/reg/draft", headers=_hdr(DELEGATE_ID),
        json={"version": resp.json()["version"], "answers": {"age": "25"}, "step": "age"},
    ).json()
    step_keys = [s["key"] for s in body2["steps"]]
    if body2["step"] != reg_engine.STEP_DONE:
        assert body2["step"] in step_keys


def test_lww_field_versions_and_conflicts_not_broken_by_snapshot(tmp_path):
    db_path = _ready(tmp_path)
    _fill(DELEGATE_ID, age=25)
    client = _client(_cfg(db_path))
    resp = client.patch(
        "/app/api/reg/draft", headers=_hdr(DELEGATE_ID), json={"version": 0, "answers": {}},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["conflicts"] == []
    row = _draft_row(DELEGATE_ID)
    field_versions = row["meta"].get("field_versions", {})
    assert row["version"] == 1
    assert field_versions.get("age") == 1

    resp2 = client.patch(
        "/app/api/reg/draft", headers=_hdr(DELEGATE_ID),
        json={"version": row["version"], "answers": {"phone": "+79000000000"}},
    )
    assert resp2.status_code == 200, resp2.text
    assert resp2.json()["conflicts"] == []

    # Правка ТОЙ ЖЕ колонки из чата — GET из приложения обязан отдать значение чата
    # (пофилевый LWW не сломан снимком).
    _run(bot_db.upsert_reg_draft(
        DELEGATE_ID, kind="edit", participant_type="full", event_city=None,
        step=None, patch={"age": 30}, source="bot",
    ))
    body3 = client.get("/app/api/reg/draft", headers=_hdr(DELEGATE_ID)).json()
    age_spec = next(s for s in body3["steps"] if s["key"] == "age")
    assert age_spec["value"] == 30


def test_empty_bootstrap_patch_then_submit_writes_no_fake_history(tmp_path):
    """Пустая правка (создание черновика без изменений) не подделывает историю — submit сразу
    после такого PATCH не меняет `users` и не пишет `reg_answer_history`."""
    db_path = _ready(tmp_path)
    client = _client(_cfg(db_path))
    resp = client.patch(
        "/app/api/reg/draft", headers=_hdr(DELEGATE_ID), json={"version": 0, "answers": {}},
    )
    assert resp.status_code == 200, resp.text
    before = _run(bot_db.get_user(DELEGATE_ID))
    resp2 = client.post("/app/api/reg/draft/submit", headers=_hdr(DELEGATE_ID))
    assert resp2.status_code == 200, resp2.text
    after = _run(bot_db.get_user(DELEGATE_ID))
    assert before == after
    assert _run(bot_db.get_answer_history(DELEGATE_ID)) == []
