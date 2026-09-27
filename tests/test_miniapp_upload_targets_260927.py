"""Ревью квика 27.09 (загрузка картинок в Mini App): три доводки после ревью.

1. Узкий белый список фото (JPEG/PNG/GIF) и повтор документом при 400 — только для СДАЧИ
   задания. Ассеты оформления (`target=settings_asset`) и обложка задания
   (`target=task_cover`) живут по прежнему правилу: любая image/* уходит `sendPhoto`, отказ
   Telegram документом не подменяется — иначе бот потом отдаёт `file_id` документа через
   `answer_photo`, Telegram отвечает 400, и делегаты молча видят заглушку.
2. `target` из query-параметра попадает в лог без переводов строк и не длиннее потолка —
   подделать строку лога нельзя.
3. `file_rejected` («сохраните как JPG») — только когда Telegram отказал именно файлу
   (IMAGE_PROCESS_FAILED, PHOTO_INVALID_DIMENSIONS, «file is …» и т.п.). Прочие 400 (например,
   слишком длинная подпись) — прежняя недоступность 502, описание — в лог.
"""
from __future__ import annotations

import logging

from tests.test_miniapp_frontend import SCREENS_DIR, _js_without_comments
from tests.test_miniapp_routes import ADMIN_ID, _hdr
from tests.test_miniapp_submissions import (  # noqa: F401 — фикстура client
    DELEGATE_ID,
    _upload,
    client,
)
from tests.test_miniapp_upload_photo_fallback_260927 import _reject, api  # noqa: F401 — фикстура api


# ── 1. Ассеты настроек и обложки — прежнее правило ────────────────────────────────────────

def test_settings_asset_webp_still_goes_as_photo(client, api):
    resp = _upload(client, ADMIN_ID, b"img", name="welcome.webp", ctype="image/webp", target="settings_asset")
    assert resp.status_code == 200, resp.text
    assert api.calls == ["sendPhoto"]
    assert resp.json()["kind"] == "photo"


def test_settings_asset_rejected_photo_is_not_resent_as_document(client, api):
    api.responses["sendPhoto"] = _reject()
    resp = _upload(client, ADMIN_ID, b"img", name="welcome.heic", ctype="image/heic", target="settings_asset")
    assert resp.status_code == 502, resp.text
    assert resp.json()["reason"] == "telegram_unavailable"
    assert api.calls == ["sendPhoto"]


def test_task_cover_webp_goes_as_photo(client, api):
    resp = _upload(client, ADMIN_ID, b"img", name="cover.webp", ctype="image/webp", target="task_cover")
    assert resp.status_code == 200, resp.text
    assert api.calls == ["sendPhoto"]
    assert resp.json()["kind"] == "photo"


def test_task_cover_rejected_photo_is_not_resent_as_document(client, api):
    api.responses["sendPhoto"] = _reject()
    resp = _upload(client, ADMIN_ID, b"img", name="cover.jpg", ctype="image/jpeg", target="task_cover")
    assert resp.status_code == 502, resp.text
    assert api.calls == ["sendPhoto"]


def test_task_submission_webp_still_goes_as_document(client, api):
    resp = _upload(client, DELEGATE_ID, b"img", name="shot.webp", ctype="image/webp")
    assert resp.status_code == 200, resp.text
    assert api.calls == ["sendDocument"]


def test_task_edit_cover_upload_names_its_target():
    text = _js_without_comments(SCREENS_DIR / "task_edit.js")
    assert 'api("/uploads?target=task_cover", {' in text


# ── 2. target в логе ──────────────────────────────────────────────────────────────────────

def test_logged_target_has_no_line_breaks_and_is_capped(client, api, caplog):
    evil = "x\r\nuploads: target=resume content_type=forged" + "y" * 500
    with caplog.at_level(logging.INFO):
        client.post(
            "/app/api/uploads", params={"target": evil},
            files={"file": ("pic.jpg", b"img", "image/jpeg")},
            headers=_hdr(DELEGATE_ID),
        )
    lines = [r.getMessage() for r in caplog.records if r.name == "miniapp.routers.submissions"]
    hit = [m for m in lines if m.startswith("uploads: target=")]
    assert hit, lines
    for message in hit:
        assert "\n" not in message and "\r" not in message
        assert "y" * 100 not in message


# ── 3. file_rejected — только отказ по файлу ──────────────────────────────────────────────

def test_caption_too_long_on_document_is_not_file_rejected(client, api, caplog):
    api.responses["sendDocument"] = _reject("Bad Request: message caption is too long")
    with caplog.at_level(logging.WARNING):
        resp = _upload(client, DELEGATE_ID, b"%PDF-1.4", name="doc.pdf", ctype="application/pdf")
    assert resp.status_code == 502, resp.text
    assert resp.json()["reason"] == "telegram_unavailable"
    assert any("caption is too long" in r.getMessage() for r in caplog.records)


def test_caption_too_long_on_photo_is_not_retried_as_document(client, api):
    api.responses["sendPhoto"] = _reject("Bad Request: message caption is too long")
    resp = _upload(client, DELEGATE_ID, b"img", name="pic.jpg", ctype="image/jpeg")
    assert resp.status_code == 502, resp.text
    assert api.calls == ["sendPhoto"]


def test_invalid_dimensions_photo_is_retried_as_document(client, api):
    api.responses["sendPhoto"] = _reject("Bad Request: PHOTO_INVALID_DIMENSIONS")
    resp = _upload(client, DELEGATE_ID, b"img", name="pano.jpg", ctype="image/jpeg")
    assert resp.status_code == 200, resp.text
    assert api.calls == ["sendPhoto", "sendDocument"]
    assert resp.json()["kind"] == "document"


def test_400_without_description_is_not_file_rejected(client, api):
    api.responses["sendDocument"] = (400, {"ok": False, "error_code": 400})
    resp = _upload(client, DELEGATE_ID, b"%PDF-1.4", name="doc.pdf", ctype="application/pdf")
    assert resp.status_code == 502, resp.text
