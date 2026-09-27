"""Квик 27.09 — два бага резюме в Mini App, пойманные на проде.

A1. Обзор правки в режиме развилки (`reg_resume_mode=fork`): кнопки «Файл/Ссылка/Текстом/Нет
резюме» писали свой КОД в `resume_text`, сервер это принимал, джоба догрузки заливала слово
«mini» в облако как .txt — модератор видел мусор и отклонял делегата.

A2. Мастер новой анкеты: «Дальше» на шаге файла не ждала загрузки, анкета подавалась без
файла, а загрузка после подачи получала 403 — файл терялся.

Здесь — серверные сторожа: PATCH не принимает код развилки как резюме, submit не подаёт
анкету с выбранным «файлом» без файла, облако не получает код развилки как текст.
"""
from __future__ import annotations

import asyncio
import logging

import pytest

import reg_engine
from config import config
from database import db as bot_db
from settings_schema import SETTINGS_SCHEMA
from settings_synonyms import SETTINGS_SYNONYMS
from services.i18n_form_manual import _REGISTRY_TEXTS_EN

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

FORK_CODE_ERROR = SETTINGS_SCHEMA.get("reg_form_resume_fork_code_error_text", {}).get("default")
FILE_MISSING = SETTINGS_SCHEMA.get("reg_form_resume_file_missing_text", {}).get("default")


def _fork_mode():
    _set("reg_q_resume", "on")
    _set("reg_resume_mode", "fork")


# ── reg_engine.is_resume_fork_code ────────────────────────────────────────────────────────

@pytest.mark.parametrize("value", ["mini", " FILE ", "none", "link", "text", "Mini"])
def test_is_resume_fork_code_true_for_codes(value):
    assert reg_engine.is_resume_fork_code(value) is True


@pytest.mark.parametrize("value", [None, "", "мини", "minimal", "Работала в мини-проекте", 5, {"text": "mini"}])
def test_is_resume_fork_code_false_for_real_answers(value):
    assert reg_engine.is_resume_fork_code(value) is False


def test_fork_codes_cover_every_fork_button():
    codes = {code for code, _key, _icon in reg_engine._RESUME_FORK_OPTIONS}
    assert codes <= reg_engine.RESUME_FORK_CODES
    assert "none" in reg_engine.RESUME_FORK_CODES


# ── Реестр: два новых текста ──────────────────────────────────────────────────────────────

@pytest.mark.parametrize("key", ["reg_form_resume_fork_code_error_text", "reg_form_resume_file_missing_text"])
def test_new_registry_texts_exist_with_synonyms_and_english(key):
    entry = SETTINGS_SCHEMA[key]
    assert entry["type"] == "text"
    assert entry["group"] == "reg"
    assert entry["default"].strip()
    assert entry["label"].strip() and key not in entry["label"]
    assert entry["prompt"].strip()
    assert len(SETTINGS_SYNONYMS[key]) >= 2
    assert entry["default"] in _REGISTRY_TEXTS_EN


def test_new_registry_texts_sit_right_after_upload_error_text():
    keys = list(SETTINGS_SCHEMA)
    i = keys.index("reg_form_resume_upload_error_text")
    assert keys[i + 1:i + 3] == ["reg_form_resume_fork_code_error_text", "reg_form_resume_file_missing_text"]


# ── A1: PATCH не принимает код развилки как резюме ───────────────────────────────────────

@pytest.mark.parametrize("code", ["mini", "file", "link", "text", "none", " Mini "])
def test_patch_rejects_fork_code_as_resume_text(client, code):
    _fork_mode()
    resp = client.patch(
        "/app/api/reg/draft", headers=_hdr(DELEGATE_ID),
        json={"version": 0, "answers": {"resume_text": code}},
    )
    assert resp.status_code == 400, resp.text
    body = resp.json()
    assert body["reason"] == "invalid"
    assert body["errors"]["resume_text"] == FORK_CODE_ERROR
    row = _draft_row(DELEGATE_ID)
    assert row is None or not reg_engine.is_resume_fork_code((row["answers"] or {}).get("resume_text"))


def test_patch_rejects_fork_code_in_dropzone_wrapper(client):
    _fork_mode()
    resp = client.patch(
        "/app/api/reg/draft", headers=_hdr(DELEGATE_ID),
        json={"version": 0, "answers": {"resume_text": {"text": "mini"}}},
    )
    assert resp.status_code == 400, resp.text
    assert resp.json()["errors"]["resume_text"] == FORK_CODE_ERROR


def test_patch_accepts_real_text_that_merely_mentions_mini(client):
    _fork_mode()
    resp = client.patch(
        "/app/api/reg/draft", headers=_hdr(DELEGATE_ID),
        json={"version": 0, "answers": {"resume_text": "Работала в мини-проекте"}},
    )
    assert resp.status_code == 200, resp.text
    assert _draft_row(DELEGATE_ID)["answers"]["resume_text"] == "Работала в мини-проекте"


def test_edit_link_branch_round_trip_reaches_users(client, bot_api):
    """Круговой путь правки: выбор «Ссылка» -> появляется шаг ссылки -> ссылка -> подача ->
    в `users` лежат resume_type=link и сама ссылка, а в resume_text нет кода кнопки."""
    _fork_mode()
    first = client.patch(
        "/app/api/reg/draft", headers=_hdr(DELEGATE_ID),
        json={"version": 0, "answers": {"resume_type": "link"}, "step": None},
    )
    assert first.status_code == 200, first.text
    body = first.json()
    assert "resume_link" in {s["key"] for s in body["steps"]}

    second = client.patch(
        "/app/api/reg/draft", headers=_hdr(DELEGATE_ID),
        json={"version": body["version"], "answers": {"resume_link": "https://hh.ru/resume/1"}},
    )
    assert second.status_code == 200, second.text

    resp = client.post("/app/api/reg/draft/submit", headers=_hdr(DELEGATE_ID))
    assert resp.status_code == 200, resp.text
    user = _run(bot_db.get_user(DELEGATE_ID))
    assert user["resume_type"] == "link"
    assert user["resume_link"] == "https://hh.ru/resume/1"
    assert not reg_engine.is_resume_fork_code(user.get("resume_text"))


# ── A2: submit не подаёт анкету с «файлом» без файла ─────────────────────────────────────

def _new_file_draft(**extra):
    patch = {"full_name": "Иван Иванов", "age": 22, "resume_type": "file"}
    patch.update(extra)
    _seed_draft(UNREGISTERED_ID, kind="new", patch=patch)


def test_submit_new_with_file_branch_but_no_file_is_refused_before_claim(client, bot_api):
    _fork_mode()
    _new_file_draft()
    resp = client.post("/app/api/reg/draft/submit", headers=_hdr(UNREGISTERED_ID))
    assert resp.status_code == 400, resp.text
    assert resp.json() == {"reason": "resume_file_missing", "text": FILE_MISSING}
    assert _run(bot_db.get_user(UNREGISTERED_ID)) is None
    # черновик не залочен: claim после отказа находит его
    claimed = _run(bot_db.claim_reg_draft(UNREGISTERED_ID))
    assert claimed is not None


def test_submit_new_after_file_arrives_succeeds(client, bot_api):
    _fork_mode()
    _new_file_draft()
    refused = client.post("/app/api/reg/draft/submit", headers=_hdr(UNREGISTERED_ID))
    assert refused.status_code == 400
    _run(bot_db.upsert_reg_draft(
        UNREGISTERED_ID, kind="new", participant_type="full", event_city=None, step=None,
        patch={"resume_file_id": "BQACresume", "resume_file_name": "cv.pdf"}, source="app",
    ))
    resp = client.post("/app/api/reg/draft/submit", headers=_hdr(UNREGISTERED_ID))
    assert resp.status_code == 200, resp.text
    assert _run(bot_db.get_user(UNREGISTERED_ID)) is not None


def test_submit_new_with_file_id_passes_guard(client, bot_api):
    _fork_mode()
    _new_file_draft(resume_file_id="BQACresume", resume_file_name="cv.pdf")
    resp = client.post("/app/api/reg/draft/submit", headers=_hdr(UNREGISTERED_ID))
    assert resp.status_code == 200, resp.text


def test_submit_edit_with_file_in_users_passes_guard(client, bot_api):
    _fork_mode()
    _fill(DELEGATE_ID, resume_type="file", resume_url="https://cloud.example.org/s/x")
    _seed_draft(DELEGATE_ID, kind="edit", patch={"phone": "+79997776655"})
    resp = client.post("/app/api/reg/draft/submit", headers=_hdr(DELEGATE_ID))
    assert resp.status_code == 200, resp.text


# ── A1 облако: код развилки не уезжает в Nextcloud ───────────────────────────────────────

from tests.test_resume_retry_260907 import (  # noqa: E402
    _FakeBot,
    _configure_nextcloud,
    _insert_user,
    _ready,
    _ts,
)


def test_retry_skips_fork_code_and_marks_dead_but_uploads_real_text(tmp_path, monkeypatch, caplog):
    _ready(tmp_path)
    _configure_nextcloud(monkeypatch)
    _insert_user(telegram_id=201, full_name="Код", registration_date=_ts(40), resume_text="mini", resume_url=None)
    _insert_user(telegram_id=202, full_name="Текст", registration_date=_ts(30),
                 resume_text="мой настоящий опыт", resume_url=None)

    from services import reg_finalize
    from services import nextcloud as nextcloud_mod
    from services import sheets as sheets_mod

    uploaded = []

    async def _fake_upload_text_resume(text, filename):
        uploaded.append(text)
        return "https://cloud.example.org/s/TOK/download?path=%2F&files=y.txt"

    async def _fake_update_row_by_id(tab, tid, row):
        return True

    monkeypatch.setattr(nextcloud_mod, "upload_text_resume", _fake_upload_text_resume)
    monkeypatch.setattr(sheets_mod, "update_row_by_id", _fake_update_row_by_id)
    monkeypatch.setattr(reg_finalize, "_resume_retry_dead", set())

    with caplog.at_level(logging.WARNING):
        done = asyncio.run(reg_finalize.retry_pending_resume_uploads(_FakeBot()))
    assert done == 1
    assert uploaded == ["мой настоящий опыт"]
    assert 201 in reg_finalize._resume_retry_dead
    assert not (asyncio.run(bot_db.get_user(201)) or {}).get("resume_url")
    warn = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING and "201" in r.getMessage()]
    assert warn and all("mini" not in m for m in warn)


def _post_finalize_env(tmp_path, monkeypatch):
    from tests.test_reg_finalize import _offline, _patch_notify, _patch_sheet_calls
    config.DB_PATH = str(tmp_path / "post_finalize_fork.db")
    from tests._dbtpl import fast_init_db
    fast_init_db()
    _offline(monkeypatch)
    _patch_sheet_calls(monkeypatch)
    _patch_notify(monkeypatch)
    monkeypatch.setattr(config, "ADMIN_IDS", [])
    from services import nextcloud as nextcloud_mod
    uploaded = []

    async def _fake_upload_text_resume(text, filename):
        uploaded.append(text)
        return "https://cloud.example.org/s/TOK/download?path=%2F&files=y.txt"

    monkeypatch.setattr(nextcloud_mod, "upload_text_resume", _fake_upload_text_resume)
    return uploaded


@pytest.mark.parametrize("text,expected_upload", [("link", False), (" Mini ", False), ("Мой опыт в проектах", True)])
def test_post_finalize_skips_fork_code_text_resume(tmp_path, monkeypatch, text, expected_upload):
    uploaded = _post_finalize_env(tmp_path, monkeypatch)
    from services import reg_finalize as rf
    from tests.test_reg_finalize import FakeBot, UID, _seed_user

    async def go():
        await _seed_user(UID, status="pending")
        await rf.post_finalize(FakeBot(), UID, "new", resume_text=text)

    asyncio.run(go())
    assert bool(uploaded) is expected_upload
