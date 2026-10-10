"""Приёмка 09.10 (Mini App): загрузка резюме без чата с ботом.

Файл резюме приложение кладёт отправкой документа в чат делегата. Делегат, который открыл
приложение по ссылке и ни разу не нажимал /start (или заблокировал бота), получал от Telegram
«chat not found» / «bot was blocked by the user» — а приложение отвечало общим 502 и «попробуй
ещё раз», хотя повтор не поможет никогда.

Теперь такой отказ — 409 `no_chat` с текстом реестра: открыть чат с ботом и нажать /start или
написать о себе текстом (кнопка «Написать текстом» стоит в той же дропзоне). Обрыв связи
по-прежнему 502 «попробуй ещё раз».
"""
from __future__ import annotations

import httpx
import pytest

from services.i18n.i18n_form_manual import _REGISTRY_TEXTS_EN
from domain.settings.schema import SETTINGS_SCHEMA
from domain.settings.synonyms import SETTINGS_SYNONYMS

from tests.test_miniapp_form import (  # noqa: F401 — фикстуры подтягиваются по имени
    bot_api,
    client,
    db_path,
    _draft_row,
    _seed_draft,
)
from tests.test_miniapp_routes import UNREGISTERED_ID, _hdr

KEY = "reg_form_resume_no_chat_text"


def _upload(client):
    return client.post(
        "/app/api/uploads?target=resume",
        files={"file": ("resume.pdf", b"%PDF-1.4 fake", "application/pdf")}, headers=_hdr(UNREGISTERED_ID),
    )


def _telegram_says(bot_api, status: int, description: str):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, json={"ok": False, "error_code": status, "description": description})
    bot_api.handler = handler


@pytest.mark.parametrize("status,description", [
    (400, "Bad Request: chat not found"),
    (403, "Forbidden: bot was blocked by the user"),
    (403, "Forbidden: bot can't initiate conversation with a user"),
    (403, "Forbidden: user is deactivated"),
])
def test_no_chat_answers_with_human_text(client, bot_api, status, description):
    _seed_draft(UNREGISTERED_ID, kind="new")
    _telegram_says(bot_api, status, description)
    resp = _upload(client)
    assert resp.status_code == 409, resp.text
    assert resp.json() == {"reason": "no_chat", "text": SETTINGS_SCHEMA[KEY]["default"]}
    assert not (_draft_row(UNREGISTERED_ID)["answers"] or {}).get("resume_file_id")


def test_network_failure_stays_retryable_502(client, bot_api):
    _seed_draft(UNREGISTERED_ID, kind="new")
    bot_api.fail = True
    resp = _upload(client)
    assert resp.status_code == 502, resp.text
    assert resp.json()["reason"] == "telegram_unavailable"


def test_registry_text_is_human_and_translated():
    entry = SETTINGS_SCHEMA[KEY]
    assert entry["type"] == "text" and entry["group"] == "reg"
    assert "/start" in entry["default"] and "текстом" in entry["default"]
    assert entry["label"].strip() and entry["prompt"].strip()
    assert len(SETTINGS_SYNONYMS[KEY]) >= 2
    assert entry["default"] in _REGISTRY_TEXTS_EN
