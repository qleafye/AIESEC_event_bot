"""Квик 27.09: сдача задания с картинкой в Mini App.

Раньше любая image/* ≤10 МБ уходила `sendPhoto`; Telegram отвечает 400 на HEIC/WebP и
прочие форматы, которые не умеет превращать в фото, сервер превращал это в 502
`telegram_unavailable`, а экран предлагал «попробуйте ещё раз» — бесконечно.

Сторожа:
- фото — только JPEG/PNG/GIF; HEIC/HEIF/WebP/TIFF сразу `sendDocument`;
- 400 на `sendPhoto` → тот же файл повторно `sendDocument`;
- окончательный 400 → HTTP 400 `file_rejected` с текстом реестра; недоступность → прежний 502;
- description Telegram попадает в лог без токена; лог загрузки — content_type/расширение/размер.
"""
from __future__ import annotations

import asyncio
import logging
from types import SimpleNamespace

import httpx
import pytest

from miniapp import telegram_api
from miniapp.routers.submissions import make_part_token
from core.settings_schema import SETTINGS_SCHEMA

from tests.test_miniapp_auth import TOKEN
from tests.test_miniapp_submissions import (  # noqa: F401 — фикстура client
    DELEGATE_ID,
    MB,
    SECRET,
    _upload,
    client,
)

REJECTED_DEFAULT = (
    "Этот файл не получилось отправить. Сделайте скриншот или сохраните как JPG — "
    "или сдайте задание в чате с ботом, прикрепив файлом."
)


class ScriptedBotApi:
    """Фейковый Bot API: по методу — заданный код и тело; по умолчанию успех как у Telegram."""

    def __init__(self):
        self.calls: list[str] = []
        self.responses: dict[str, tuple[int, dict]] = {}

    def handler(self, request: httpx.Request) -> httpx.Response:
        method = request.url.path.rsplit("/", 1)[-1]
        self.calls.append(method)
        if method in self.responses:
            status, body = self.responses[method]
            return httpx.Response(status, json=body)
        if method == "sendPhoto":
            result = {"message_id": 1, "photo": [{"file_id": "small"}, {"file_id": "AgACphotoBIG"}]}
        else:
            result = {"message_id": 2, "document": {"file_id": "BQACdocument"}}
        return httpx.Response(200, json={"ok": True, "result": result})


def _reject(description="Bad Request: IMAGE_PROCESS_FAILED"):
    return 400, {"ok": False, "error_code": 400, "description": description}


@pytest.fixture
def api(monkeypatch):
    fake = ScriptedBotApi()
    monkeypatch.setattr(
        telegram_api, "_make_client",
        lambda cfg, timeout: httpx.AsyncClient(transport=httpx.MockTransport(fake.handler)),
    )
    return fake


# ── Реестр ────────────────────────────────────────────────────────────────────────────────

def test_registry_key_exists_right_after_too_large():
    entry = SETTINGS_SCHEMA["miniapp_upload_file_rejected_text"]
    assert entry["type"] == "text" and entry["group"] == "miniapp"
    assert entry["default"] == REJECTED_DEFAULT
    keys = list(SETTINGS_SCHEMA)
    assert keys.index("miniapp_upload_file_rejected_text") == keys.index("miniapp_upload_too_large_text") + 1


def test_limits_carry_file_rejected_text(client, api):
    resp = client.get("/app/api/uploads/limits", headers=_hdr_delegate())
    assert resp.status_code == 200
    assert resp.json()["file_rejected_text"] == REJECTED_DEFAULT


def _hdr_delegate():
    from tests.test_miniapp_routes import _hdr
    return _hdr(DELEGATE_ID)


# ── Белый список фото ─────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("ctype,name", [
    ("image/heic", "IMG_0001.HEIC"),
    ("image/heif", "IMG_0002.heif"),
    ("image/webp", "shot.webp"),
    ("image/tiff", "scan.tiff"),
])
def test_non_photo_images_go_as_document_first(client, api, ctype, name):
    resp = _upload(client, DELEGATE_ID, b"img-bytes", name=name, ctype=ctype)
    assert resp.status_code == 200, resp.text
    assert api.calls == ["sendDocument"]
    body = resp.json()
    assert body["kind"] == "document"
    assert body["part_token"] == make_part_token(SECRET, DELEGATE_ID, "document", "BQACdocument")


@pytest.mark.parametrize("ctype,name", [
    ("image/jpeg", "pic.jpg"),
    ("image/png", "pic.png"),
    ("image/gif", "pic.gif"),
])
def test_photo_types_still_go_as_photo(client, api, ctype, name):
    resp = _upload(client, DELEGATE_ID, b"img-bytes", name=name, ctype=ctype)
    assert resp.status_code == 200, resp.text
    assert api.calls == ["sendPhoto"]
    assert resp.json()["kind"] == "photo"


def test_big_jpeg_goes_as_document(client, api):
    resp = _upload(client, DELEGATE_ID, b"x" * (10 * MB + 1), name="big.jpg", ctype="image/jpeg")
    assert resp.status_code == 200, resp.text
    assert api.calls == ["sendDocument"]


# ── Фолбэк и отказ ────────────────────────────────────────────────────────────────────────

def test_rejected_photo_is_resent_as_document(client, api, caplog):
    api.responses["sendPhoto"] = _reject()
    with caplog.at_level(logging.INFO):
        resp = _upload(client, DELEGATE_ID, b"img-bytes", name="pic.jpg", ctype="image/jpeg")
    assert resp.status_code == 200, resp.text
    assert api.calls == ["sendPhoto", "sendDocument"]
    body = resp.json()
    assert body["kind"] == "document"
    assert body["content"] == "BQACdocument"
    assert body["part_token"] == make_part_token(SECRET, DELEGATE_ID, "document", "BQACdocument")
    assert any("IMAGE_PROCESS_FAILED" in r.getMessage() for r in caplog.records)
    assert all(TOKEN not in r.getMessage() for r in caplog.records)
    assert TOKEN not in resp.text


def test_rejected_photo_and_document_is_file_rejected(client, api):
    api.responses["sendPhoto"] = _reject()
    api.responses["sendDocument"] = _reject("Bad Request: file is corrupted")
    resp = _upload(client, DELEGATE_ID, b"img-bytes", name="pic.png", ctype="image/png")
    assert resp.status_code == 400, resp.text
    detail = resp.json()
    assert detail["reason"] == "file_rejected"
    assert detail["text"] == REJECTED_DEFAULT
    assert api.calls == ["sendPhoto", "sendDocument"]
    assert TOKEN not in resp.text


def test_rejected_document_is_file_rejected_without_retry(client, api):
    api.responses["sendDocument"] = _reject("Bad Request: file is empty")
    resp = _upload(client, DELEGATE_ID, b"%PDF-1.4", name="doc.pdf", ctype="application/pdf")
    assert resp.status_code == 400, resp.text
    detail = resp.json()
    assert detail["reason"] == "file_rejected"
    assert api.calls == ["sendDocument"]


def test_upstream_5xx_is_still_502(client, api):
    api.responses["sendPhoto"] = (502, {"ok": False})
    resp = _upload(client, DELEGATE_ID, b"img-bytes")
    assert resp.status_code == 502
    detail = resp.json()
    assert detail["reason"] == "telegram_unavailable"
    assert api.calls == ["sendPhoto"]  # 5xx не повторяем


# ── Лог загрузки ──────────────────────────────────────────────────────────────────────────

def test_upload_logs_content_type_ext_and_size_without_filename(client, api, caplog):
    with caplog.at_level(logging.INFO):
        resp = _upload(client, DELEGATE_ID, b"h" * 1234, name="Секретный_отчёт.HEIC", ctype="image/heic")
    assert resp.status_code == 200, resp.text
    lines = [r.getMessage() for r in caplog.records if r.name == "miniapp.routers.submissions"]
    hit = [m for m in lines if "image/heic" in m and ".heic" in m and "1234" in m]
    assert hit, lines
    assert all("Секретный_отчёт" not in m for m in lines)


# ── telegram_api ──────────────────────────────────────────────────────────────────────────

def test_telegram_api_error_carries_status_and_description_without_token(monkeypatch, caplog):
    fake = ScriptedBotApi()
    fake.responses["sendPhoto"] = _reject(f"Bad Request: wrong token {TOKEN} " + "x" * 400)
    monkeypatch.setattr(
        telegram_api, "_make_client",
        lambda cfg, timeout: httpx.AsyncClient(transport=httpx.MockTransport(fake.handler)),
    )
    cfg = SimpleNamespace(bot_token=TOKEN, proxy_url=None)
    with caplog.at_level(logging.WARNING):
        with pytest.raises(telegram_api.TelegramApiError) as info:
            asyncio.run(telegram_api.send_photo(cfg, 1, b"x", "a.jpg", "image/jpeg"))
    exc = info.value
    assert exc.status == 400
    assert exc.description and "wrong token" in exc.description
    assert TOKEN not in exc.description
    assert len(exc.description) <= 200
    assert TOKEN not in str(exc)
    assert any("wrong token" in r.getMessage() for r in caplog.records)
    assert all(TOKEN not in r.getMessage() for r in caplog.records)
