"""Резюме не того формата (`.doc` — так прислали двое делегатов): отказ называет принимаемые
форматы («PDF, DOCX» — из одной константы `RESUME_ALLOWED_EXTS`) и объясняет, как пересохранить
файл. Текст — ключ реестра `reg_form_resume_wrong_type_text`, один и тот же в Mini App и в чате;
`{formats}` подставляется после перевода.

Отдельный файл, а не блок в `test_miniapp_upload_client_timing_261028.py`: фикстуры `client`
Mini App-анкеты (`tests/test_miniapp_form.py`) и сдачи (`tests/test_miniapp_submissions.py`)
называются одинаково и в одном модуле не уживаются.
"""
from __future__ import annotations

import asyncio

import domain.regform.engine as reg_engine
from database import db
from domain.settings.schema import SETTINGS_SCHEMA
from handlers.reg import reg_flow

from tests.test_miniapp_form import (  # noqa: F401 — фикстуры client, bot_api, db_path
    DELEGATE_ID,
    _draft_row,
    _seed_draft,
    _upload_resume,
    bot_api,
    client,
    db_path,
)
from tests.test_reg_resume_draft import (
    USER_ID,
    _FakeDocument,
    _FakeResumeMessage,
    _new_state,
    _texts,
    _use_tmp_db,
)

CHAT_UID = USER_ID + 2810


def test_allowed_exts_and_label():
    assert reg_engine.RESUME_ALLOWED_EXTS == (".pdf", ".docx")
    assert reg_engine.resume_formats_label() == "PDF, DOCX"
    assert reg_engine.is_allowed_resume("cv.doc") is False
    assert reg_engine.is_allowed_resume("CV.PDF") is True
    assert reg_engine.is_allowed_resume("cv.docx") is True


def test_registry_default_has_formats_placeholder():
    default = SETTINGS_SCHEMA["reg_form_resume_wrong_type_text"]["default"]
    assert "{formats}" in default and "Сохранить как" in default


def test_too_large_default_matches_resume_limit():
    limit_mb = reg_engine.RESUME_MAX_BYTES // (1024 * 1024)
    default = SETTINGS_SCHEMA["reg_form_resume_too_large_text"]["default"]
    assert f"{limit_mb} МБ" in default
    assert "20 МБ" not in default


def test_miniapp_doc_resume_rejected_with_formats(client, bot_api):
    _seed_draft(DELEGATE_ID, kind="edit")
    resp = _upload_resume(client, DELEGATE_ID, b"\xd0\xcf\x11\xe0 doc", name="cv.doc", ctype="application/msword")
    assert resp.status_code == 400
    body = resp.json()
    assert body["reason"] == "bad_type"
    assert "PDF, DOCX" in body["text"] and "Сохранить как" in body["text"]
    assert "{formats}" not in body["text"]
    assert bot_api.documents == []


def test_miniapp_manager_text_without_placeholder_goes_as_is(client, bot_api):
    asyncio.run(db.set_setting("reg_form_resume_wrong_type_text", "Только PDF, пожалуйста."))
    _seed_draft(DELEGATE_ID, kind="edit")
    resp = _upload_resume(client, DELEGATE_ID, b"doc", name="cv.doc", ctype="application/msword")
    assert resp.status_code == 400
    assert resp.json()["text"] == "Только PDF, пожалуйста."


def _chat_resume(file_name: str):
    async def go():
        await db.upsert_reg_draft(CHAT_UID, kind="new", participant_type="full", source="bot")
        state = _new_state(CHAT_UID)
        await state.update_data(participant_type="full", _draft_kind="new")
        msg = _FakeResumeMessage(CHAT_UID, "delegate", document=_FakeDocument(file_name=file_name))
        await reg_flow.process_resume(msg, state, bot=None)
        return msg, await state.get_data(), await db.get_reg_draft(CHAT_UID)

    return asyncio.run(go())


def test_chat_doc_resume_rejected_with_formats(tmp_path):
    _use_tmp_db(tmp_path, "resume_wrong_type_chat.db")
    msg, data, draft = _chat_resume("cv.doc")
    texts = _texts(msg)
    assert len(texts) == 1
    assert "PDF, DOCX" in texts[0] and "{formats}" not in texts[0]
    assert "resume_file_id" not in data
    assert not (draft or {}).get("answers", {}).get("resume_file_id")


def test_chat_manager_text_without_placeholder_goes_as_is(tmp_path):
    _use_tmp_db(tmp_path, "resume_wrong_type_chat_custom.db")
    asyncio.run(db.set_setting("reg_form_resume_wrong_type_text", "Нужен PDF."))
    msg, _, _ = _chat_resume("cv.doc")
    assert _texts(msg) == ["Нужен PDF."]
