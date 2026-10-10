"""Ревью квика 27.09: серверные сторожа резюме в Mini App смотрят на режим и на то, что правили.

Код развилки («file»/«link»/«text»/«mini») отбивается как резюме только в режиме `fork` —
в `text_only`/`file_or_text` кнопок развилки нет, и «Выбери вариант кнопкой» там вводит в
тупик. «none» — не код кнопки, это обычный ответ (EN-делегат без резюме пишет «None»).

Гард «выбран файл, а файла нет» на подаче:
- работает, только если шаг «Резюме» включён и режим допускает файл (`fork`/`file_or_text`);
- в правке — только если в этой правке трогали резюме (правка телефона не блокируется
  старой потерей файла);
- явное удаление файла в черновике правки не откатывается к старому файлу в `users`;
- ссылка облака `resume_url` (у текстового резюме это .txt) файлом не считается.
"""
from __future__ import annotations

import pytest

import reg_engine
from database import db as bot_db
from domain.settings.schema import SETTINGS_SCHEMA

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

FORK_CODE_ERROR = SETTINGS_SCHEMA["reg_form_resume_fork_code_error_text"]["default"]
FILE_MISSING = SETTINGS_SCHEMA["reg_form_resume_file_missing_text"]["default"]


def _mode(mode: str, resume_on: str = "on"):
    _set("reg_q_resume", resume_on)
    _set("reg_resume_mode", mode)


def _submit(client, telegram_id):
    return client.post("/app/api/reg/draft/submit", headers=_hdr(telegram_id))


# ── Код развилки — только в режиме fork ───────────────────────────────────────────────────

def test_none_is_not_a_fork_code():
    assert "none" not in reg_engine.RESUME_FORK_CODES
    assert reg_engine.is_resume_fork_code("None") is False


@pytest.mark.parametrize("mode", ["text_only", "file_or_text"])
@pytest.mark.parametrize("value", ["mini", "file", "text", "link", "None"])
def test_patch_does_not_refuse_fork_code_outside_fork_mode(client, mode, value):
    _mode(mode)
    resp = client.patch(
        "/app/api/reg/draft", headers=_hdr(DELEGATE_ID),
        json={"version": 0, "answers": {"resume_text": value}},
    )
    if resp.status_code == 400:
        assert (resp.json().get("errors") or {}).get("resume_text") != FORK_CODE_ERROR, resp.text
    else:
        assert resp.status_code == 200, resp.text


def test_patch_still_refuses_fork_code_in_fork_mode(client):
    _mode("fork")
    resp = client.patch(
        "/app/api/reg/draft", headers=_hdr(DELEGATE_ID),
        json={"version": 0, "answers": {"resume_text": "mini"}},
    )
    assert resp.status_code == 400, resp.text
    assert resp.json()["errors"]["resume_text"] == FORK_CODE_ERROR


# ── Гард подачи: где он НЕ должен срабатывать ────────────────────────────────────────────

def test_edit_of_unrelated_field_passes_despite_lost_file(client, bot_api):
    _mode("fork")
    _fill(DELEGATE_ID, resume_type="file", resume_file_id=None, resume_url=None)
    _seed_draft(DELEGATE_ID, kind="edit", patch={"phone": "+79997776655"})
    resp = _submit(client, DELEGATE_ID)
    assert resp.status_code == 200, resp.text


def test_edit_in_text_only_mode_is_not_blocked_by_stale_file_type(client, bot_api):
    _mode("text_only")
    _fill(DELEGATE_ID, resume_type="file", resume_file_id=None, resume_url=None)
    _seed_draft(DELEGATE_ID, kind="edit", patch={"resume_type": "file", "phone": "+79997776655"})
    resp = _submit(client, DELEGATE_ID)
    assert resp.status_code == 200, resp.text


def test_new_with_resume_step_off_is_not_blocked(client, bot_api):
    _mode("fork", resume_on="off")
    _seed_draft(UNREGISTERED_ID, kind="new", patch={"full_name": "Иван Иванов", "age": 22, "resume_type": "file"})
    resp = _submit(client, UNREGISTERED_ID)
    assert resp.status_code == 200, resp.text


def test_new_in_text_only_mode_is_not_blocked(client, bot_api):
    _mode("text_only")
    _seed_draft(UNREGISTERED_ID, kind="new", patch={"full_name": "Иван Иванов", "age": 22, "resume_type": "file"})
    resp = _submit(client, UNREGISTERED_ID)
    assert resp.status_code == 200, resp.text


# ── Гард подачи: где он ОБЯЗАН срабатывать ───────────────────────────────────────────────

def test_edit_switch_to_file_with_only_text_resume_url_is_refused(client, bot_api):
    """Текстовое резюме: в users есть ссылка облака на .txt, файла нет. Делегат выбрал «Файл»
    и ничего не загрузил — ссылка облака файлом не считается."""
    _mode("fork")
    _fill(DELEGATE_ID, resume_type="text", resume_text="мой опыт",
          resume_url="https://cloud.example.org/s/TOK/download?files=cv.txt", resume_file_id=None)
    _seed_draft(DELEGATE_ID, kind="edit", patch={"resume_type": "file"})
    resp = _submit(client, DELEGATE_ID)
    assert resp.status_code == 400, resp.text
    assert resp.json() == {"reason": "resume_file_missing", "text": FILE_MISSING}
    assert _run(bot_db.claim_reg_draft(DELEGATE_ID)) is not None


def test_edit_removed_file_does_not_fall_back_to_users_file(client, bot_api):
    """Делегат удалил файл «×» (в черновике явное None) — старый файл в users не спасает."""
    _mode("fork")
    _fill(DELEGATE_ID, resume_type="file", resume_file_id="BQACold")
    _seed_draft(DELEGATE_ID, kind="edit", patch={
        "resume_file_id": None, "resume_file_name": None, "resume_text": None,
    })
    resp = _submit(client, DELEGATE_ID)
    assert resp.status_code == 400, resp.text
    assert resp.json()["reason"] == "resume_file_missing"


def test_edit_switch_to_file_with_old_file_in_users_passes(client, bot_api):
    _mode("fork")
    _fill(DELEGATE_ID, resume_type="file", resume_file_id="BQACold")
    _seed_draft(DELEGATE_ID, kind="edit", patch={"resume_type": "file"})
    resp = _submit(client, DELEGATE_ID)
    assert resp.status_code == 200, resp.text


def test_new_in_file_or_text_mode_with_file_branch_and_no_file_is_refused(client, bot_api):
    _mode("file_or_text")
    _seed_draft(UNREGISTERED_ID, kind="new", patch={"full_name": "Иван Иванов", "age": 22, "resume_type": "file"})
    resp = _submit(client, UNREGISTERED_ID)
    assert resp.status_code == 400, resp.text
    assert resp.json()["reason"] == "resume_file_missing"
