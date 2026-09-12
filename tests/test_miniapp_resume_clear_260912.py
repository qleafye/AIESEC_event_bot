"""Квик 260912-l53 (задача 1) — HTTP-контракт очистки шага анкеты Mini App: PATCH
`/app/api/reg/draft` с `clear: [step_key]` обязан обнулять ВЕСЬ набор колонок шага
(`reg_engine.columns_for_step`), а не одну основную, и делать это как у делегата с уже
существующей строкой `reg_drafts`, так и у одобренного делегата, у которого этой строки ещё
нет (снимок резюме лежит только в `users`).

Харнесс и стиль — `tests/test_miniapp_form_regress_260911.py`: та же временная БД на тест
(`tests.test_miniapp_routes._use_tmp_db`), `pytest-asyncio` в окружении не установлен —
async-хелперы (`_fill`/`_draft_row`/`_seed_draft`) уже гоняют себя через `asyncio.run()`
(`tests/test_miniapp_form.py`), здесь их вызываем как обычные синхронные функции.
"""
from __future__ import annotations

import pytest

import reg_engine

from tests.test_miniapp_form import _draft_row, _fill, _seed_draft
from tests.test_miniapp_routes import (
    DELEGATE_ID,
    REJECTED_ID,
    _cfg,
    _client,
    _hdr,
    _set,
    _standard_seed,
    _use_tmp_db,
)

RESUME_COLUMNS = reg_engine.columns_for_step("resume")


@pytest.fixture
def client(tmp_path):
    db_path = _use_tmp_db(tmp_path, "miniapp_resume_clear_260912.db")
    _standard_seed()
    return _client(_cfg(db_path))


def _resume_step(steps: list[dict]) -> dict:
    return next(s for s in steps if s["key"] == "resume")


# ══════════════════════════════════════════════════════════════════════════════════════════
# Черновик УЖЕ есть (REJECTED_ID, kind="new") — прямой случай
# ══════════════════════════════════════════════════════════════════════════════════════════

def test_clear_wipes_all_three_resume_columns_in_existing_draft(client):
    """`_seed_draft` пишет `patch` напрямую в `reg_drafts.answers` (тот же приём, что чат-бот
    для файлового ответа) — так резюме-файл оказывается в черновике без похода через PATCH
    роутера, у которого `resume_file_id`/`resume_file_name` вне allowlist `column_to_step`
    (они — колонки-компаньоны набора, а не самостоятельный ответ шага)."""
    _set("reg_q_resume", "on")
    _seed_draft(
        REJECTED_ID, kind="new", source="miniapp",
        patch={
            "resume_file_id": "AAAfileid123",
            "resume_file_name": "cv.pdf",
            "resume_text": "старый текст резюме до удаления",
            "age": 25,
        },
    )
    before = client.get("/app/api/reg/draft", headers=_hdr(REJECTED_ID)).json()

    resp = client.patch(
        "/app/api/reg/draft", headers=_hdr(REJECTED_ID),
        json={"version": before["version"], "answers": {}, "clear": ["resume"]},
    )
    assert resp.status_code == 200, resp.text

    row = _draft_row(REJECTED_ID)
    for col in RESUME_COLUMNS:
        assert row["answers"].get(col) is None, f"{col} не обнулилась"
    assert row["answers"]["age"] == 25  # соседний ответ цел

    resume_step = _resume_step(resp.json()["steps"])
    assert resume_step["value_source"] != "answer"
    assert not resume_step.get("display")
    assert all(v is None for v in resume_step["values"].values())


def test_clear_does_not_touch_neighbouring_answers(client):
    _set("reg_q_resume", "on")
    _seed_draft(
        REJECTED_ID, kind="new", source="miniapp",
        patch={"resume_text": "текст под удаление", "age": 30},
    )
    _fill(REJECTED_ID, full_name="Иванов Иван")
    before = client.get("/app/api/reg/draft", headers=_hdr(REJECTED_ID)).json()

    resp = client.patch(
        "/app/api/reg/draft", headers=_hdr(REJECTED_ID),
        json={"version": before["version"], "answers": {}, "clear": ["resume"]},
    )
    assert resp.status_code == 200, resp.text

    row = _draft_row(REJECTED_ID)
    assert row["answers"]["age"] == 30
    age_step = next(s for s in resp.json()["steps"] if s["key"] == "age")
    assert age_step["value"] == 30


def test_clear_and_answers_in_one_patch_do_not_conflict(client):
    _set("reg_q_resume", "on")
    _seed_draft(REJECTED_ID, kind="new", source="miniapp", patch={"resume_text": "текст под удаление"})
    before = client.get("/app/api/reg/draft", headers=_hdr(REJECTED_ID)).json()

    resp = client.patch(
        "/app/api/reg/draft", headers=_hdr(REJECTED_ID),
        json={"version": before["version"], "answers": {"age": "25"}, "clear": ["resume"]},
    )
    assert resp.status_code == 200, resp.text

    row = _draft_row(REJECTED_ID)
    assert row["answers"]["age"] == 25
    for col in RESUME_COLUMNS:
        assert row["answers"].get(col) is None


# ══════════════════════════════════════════════════════════════════════════════════════════
# Одобренный делегат БЕЗ строки reg_drafts — главный случай (пункт 5 objective), не краевой
# ══════════════════════════════════════════════════════════════════════════════════════════

def test_clear_creates_draft_row_for_approved_delegate_without_one(client):
    """`baseline = ctx["answers"] if ctx["draft"] else {}` — при пустом `baseline` сравнение
    `baseline.get(col) != val` для «не было ключа -> стало None» дало бы `False`, и очистка
    молча не персистировалась бы. Задача 1 кладёт обнулённые колонки в `delta` принудительно,
    после расчёта дельты — этот тест ловит именно регресс этого шага."""
    _set("reg_q_resume", "on")
    _fill(DELEGATE_ID, resume_file_id="BBBfileid456", resume_text="старое резюме делегата", age=22)
    assert _draft_row(DELEGATE_ID) is None  # строки reg_drafts действительно нет

    before = client.get("/app/api/reg/draft", headers=_hdr(DELEGATE_ID)).json()
    resp = client.patch(
        "/app/api/reg/draft", headers=_hdr(DELEGATE_ID),
        json={"version": before["version"], "answers": {}, "clear": ["resume"]},
    )
    assert resp.status_code == 200, resp.text

    row = _draft_row(DELEGATE_ID)
    assert row is not None  # PATCH создал строку
    for col in RESUME_COLUMNS:
        assert row["answers"].get(col) is None
    # Снимок остальных ответов из users не потерян (регресс W1 пункта 5).
    assert row["answers"]["age"] == 22

    resume_step = _resume_step(resp.json()["steps"])
    assert resume_step["value_source"] != "answer"


# ══════════════════════════════════════════════════════════════════════════════════════════
# Ошибки контракта
# ══════════════════════════════════════════════════════════════════════════════════════════

def test_clear_unknown_step_is_bad_field_and_writes_nothing(client):
    resp = client.patch(
        "/app/api/reg/draft", headers=_hdr(DELEGATE_ID),
        json={"version": 0, "answers": {}, "clear": ["не_шаг"]},
    )
    assert resp.status_code == 400
    assert resp.json() == {"reason": "bad_field", "field": "не_шаг"}
    assert _draft_row(DELEGATE_ID) is None


def test_empty_required_answer_still_validates_not_silently_cleared(client):
    """Пункт 2 objective: `null` в `answers` НЕ является общим механизмом очистки — для
    обязательного шага это по-прежнему «пустой ввод», который обязан ловить `validate_answer`,
    а не тихо проскакивать как удаление."""
    resp = client.patch(
        "/app/api/reg/draft", headers=_hdr(DELEGATE_ID),
        json={"version": 0, "answers": {"full_name": None}},
    )
    assert resp.status_code == 400
    body = resp.json()
    assert body["reason"] == "invalid"
    assert "full_name" in body["errors"]
    assert _draft_row(DELEGATE_ID) is None


# ══════════════════════════════════════════════════════════════════════════════════════════
# Сторож контракта: докстринг роутера описывает clear[]
# ══════════════════════════════════════════════════════════════════════════════════════════

def test_contract_docstring_describes_clear_field():
    import pathlib

    text = pathlib.Path("miniapp/routers/__init__.py").read_text(encoding="utf-8")
    assert "clear[step_key]" in text
