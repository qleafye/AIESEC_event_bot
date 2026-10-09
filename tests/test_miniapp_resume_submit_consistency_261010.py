"""Ревью 10.10 (анкета приложения): резюме на подаче приводится к правде.

1. Заявка без файла и без текста резюме проходила, если тип не «файл» (не выбран или «текст» с
   пустым текстом). Теперь при включённом шаге «Резюме» в режимах `fork`/`file_or_text` это
   `resume_file_missing`. Ветки `none`/`link`/`mini` гард не трогает.
2. Загрузили файл, потом выбрали «Текстом» — файл оставался: в листе «Текстом», в сводке
   «прикреплено файлом», в карточке файл. Смена типа на «текст» снимает файл (и PATCH, и подача).
3. В `text_only` гард выходил раньше смены типа: старый `resume_type="file"` плюс текст уходил с
   «Файл» без файла. Тип переписывается и там.
"""
from __future__ import annotations

import pytest

from database import db as bot_db

from tests.test_miniapp_form import (  # noqa: F401 — фикстуры подтягиваются по имени
    bot_api,
    client,
    db_path,
    _draft_row,
    _fill,
    _run,
    _seed_draft,
)
from tests.test_miniapp_routes import DELEGATE_ID, UNREGISTERED_ID, _hdr, _set

BASE = {"full_name": "Иван Иванов", "age": 22}


def _mode(mode: str, resume_on: str = "on"):
    _set("reg_q_resume", resume_on)
    _set("reg_resume_mode", mode)


def _submit(client, telegram_id):
    return client.post("/app/api/reg/draft/submit", headers=_hdr(telegram_id))


# ── 1. Пустое резюме ─────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("mode", ["fork", "file_or_text"])
@pytest.mark.parametrize("extra", [{}, {"resume_type": "text"}, {"resume_type": "text", "resume_text": "  "}])
def test_no_file_no_text_is_refused(client, bot_api, mode, extra):
    _mode(mode)
    _seed_draft(UNREGISTERED_ID, kind="new", patch={**BASE, **extra})
    resp = _submit(client, UNREGISTERED_ID)
    assert resp.status_code == 400, resp.text
    assert resp.json()["reason"] == "resume_file_missing"


@pytest.mark.parametrize("resume_type", ["link", "mini", "none"])
def test_other_branches_are_not_touched(client, bot_api, resume_type):
    _mode("fork")
    _seed_draft(UNREGISTERED_ID, kind="new", patch={**BASE, "resume_type": resume_type})
    resp = _submit(client, UNREGISTERED_ID)
    assert resp.json().get("reason") != "resume_file_missing", resp.text


def test_resume_step_off_is_not_refused(client, bot_api):
    _mode("fork", resume_on="off")
    _seed_draft(UNREGISTERED_ID, kind="new", patch=BASE)
    assert _submit(client, UNREGISTERED_ID).status_code == 200


def test_text_only_without_text_is_not_this_guard(client, bot_api):
    _mode("text_only")
    _seed_draft(UNREGISTERED_ID, kind="new", patch=BASE)
    assert _submit(client, UNREGISTERED_ID).json().get("reason") != "resume_file_missing"


def test_file_or_text_with_file_and_no_type_passes(client, bot_api):
    _mode("file_or_text")
    _seed_draft(UNREGISTERED_ID, kind="new", patch={**BASE, "resume_file_id": "BQACx", "resume_file_name": "cv.pdf"})
    assert _submit(client, UNREGISTERED_ID).status_code == 200


def test_edit_of_unrelated_field_is_not_refused(client, bot_api):
    _mode("fork")
    _fill(DELEGATE_ID, resume_type=None, resume_text=None, resume_file_id=None)
    _seed_draft(DELEGATE_ID, kind="edit", patch={"phone": "+79997776655"})
    assert _submit(client, DELEGATE_ID).status_code == 200


# ── 2. «Текстом» после файла снимает файл ────────────────────────────────────────────────

def test_patch_text_branch_clears_uploaded_file(client):
    _mode("fork")
    _seed_draft(UNREGISTERED_ID, kind="new", patch={**BASE, "resume_type": "file",
                                                   "resume_file_id": "BQACx", "resume_file_name": "cv.pdf"},
                source="miniapp")
    version = _draft_row(UNREGISTERED_ID)["version"]
    resp = client.patch(
        "/app/api/reg/draft", headers=_hdr(UNREGISTERED_ID),
        json={"version": version, "answers": {"resume_type": "text"}},
    )
    assert resp.status_code == 200, resp.text
    answers = _draft_row(UNREGISTERED_ID)["answers"]
    assert answers["resume_type"] == "text"
    assert answers.get("resume_file_id") is None and answers.get("resume_file_name") is None


def test_submit_text_type_drops_file(client, bot_api):
    _mode("fork")
    _seed_draft(UNREGISTERED_ID, kind="new", patch={**BASE, "resume_type": "text", "resume_text": "мой опыт",
                                                   "resume_file_id": "BQACx", "resume_file_name": "cv.pdf"})
    resp = _submit(client, UNREGISTERED_ID)
    assert resp.status_code == 200, resp.text
    user = _run(bot_db.get_user(UNREGISTERED_ID))
    assert user["resume_type"] == "text"
    assert user["resume_text"] == "мой опыт"
    assert not user["resume_file_id"]


def test_edit_text_type_drops_old_file_in_users(client, bot_api):
    _mode("fork")
    _fill(DELEGATE_ID, resume_type="file", resume_file_id="BQACold")
    _seed_draft(DELEGATE_ID, kind="edit", patch={"resume_type": "text", "resume_text": "мой опыт"})
    resp = _submit(client, DELEGATE_ID)
    assert resp.status_code == 200, resp.text
    user = _run(bot_db.get_user(DELEGATE_ID))
    assert user["resume_type"] == "text"
    assert not user["resume_file_id"]


# ── 3. text_only тоже переписывает тип ───────────────────────────────────────────────────

def test_text_only_file_type_with_text_becomes_text(client, bot_api):
    _mode("text_only")
    _seed_draft(UNREGISTERED_ID, kind="new", patch={**BASE, "resume_type": "file", "resume_text": "мой опыт"})
    resp = _submit(client, UNREGISTERED_ID)
    assert resp.status_code == 200, resp.text
    assert _run(bot_db.get_user(UNREGISTERED_ID))["resume_type"] == "text"
