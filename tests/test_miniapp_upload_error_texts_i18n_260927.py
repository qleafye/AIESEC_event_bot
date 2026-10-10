"""Тексты сбоя загрузки в Mini App — из реестра, а не литералами JS.

Экран сдачи задания («Не удалось загрузить «…» — попробуйте ещё раз.») и загрузка обложки
задания («Обложка должна быть картинкой…») показывали русский литерал из JS — делегат с
английским интерфейсом видел его по-русски. Теперь оба текста — ключи реестра группы
`miniapp` рядом с соседями `miniapp_upload_*`, отдаются тем же GET /uploads/limits, что уже
несёт `too_large_text`/`file_rejected_text`, и имеют ручной английский.
"""
from __future__ import annotations

import re

from services.i18n_miniapp_manual import MANUAL_EN
from domain.settings.schema import SETTINGS_SCHEMA

from tests.test_miniapp_frontend import SCREENS_DIR, _js_without_comments
from tests.test_miniapp_submissions import DELEGATE_ID, client  # noqa: F401 — фикстура client

FAILED_KEY = "miniapp_upload_failed_text"
COVER_KEY = "miniapp_upload_cover_not_image_text"
FAILED_DEFAULT = "Не удалось загрузить «{name}» — попробуйте ещё раз."
COVER_DEFAULT = "Обложка должна быть картинкой — файл другого типа не подойдёт."


# ── Реестр ────────────────────────────────────────────────────────────────────────────────

def test_keys_are_miniapp_texts_with_human_labels():
    for key, default in ((FAILED_KEY, FAILED_DEFAULT), (COVER_KEY, COVER_DEFAULT)):
        entry = SETTINGS_SCHEMA[key]
        assert entry["type"] == "text" and entry["group"] == "miniapp", key
        assert entry["default"] == default, key
        assert entry["label"].strip() and "miniapp_" not in entry["label"], key
        assert entry["prompt"].strip(), key


def test_keys_sit_next_to_upload_neighbours():
    keys = list(SETTINGS_SCHEMA)
    i = keys.index("miniapp_upload_file_rejected_text")
    assert keys[i + 1:i + 3] == [FAILED_KEY, COVER_KEY]


def test_failed_text_prompt_names_the_file_placeholder():
    # Менеджер правит текст сам — подсказка обязана сказать, что {name} заменится на имя файла.
    assert "{name}" in SETTINGS_SCHEMA[FAILED_KEY]["prompt"]


def test_manual_english_exists_and_keeps_placeholders():
    for key in (FAILED_KEY, COVER_KEY):
        ru = SETTINGS_SCHEMA[key]["default"]
        assert ru in MANUAL_EN, key
        en = MANUAL_EN[ru]
        assert not re.search(r"[а-яё]", en.lower()), key
        assert re.findall(r"\{\w+\}", en) == re.findall(r"\{\w+\}", ru), key


# ── GET /uploads/limits ───────────────────────────────────────────────────────────────────

def test_limits_carry_both_texts(client):  # noqa: F811
    from tests.test_miniapp_routes import _hdr
    resp = client.get("/app/api/uploads/limits", headers=_hdr(DELEGATE_ID))
    assert resp.status_code == 200
    body = resp.json()
    assert body["upload_failed_text"] == FAILED_DEFAULT
    assert body["cover_not_image_text"] == COVER_DEFAULT


# ── Экраны: литералов больше нет ──────────────────────────────────────────────────────────

def test_submit_screen_has_no_retry_literal():
    text = _js_without_comments(SCREENS_DIR / "submit.js")
    assert "Не удалось загрузить" not in text
    assert "limits.upload_failed_text" in text


def test_task_edit_cover_texts_come_from_limits():
    text = _js_without_comments(SCREENS_DIR / "task_edit.js")
    assert "Обложка должна быть картинкой" not in text
    body = text[text.index("async function uploadCover("):]
    body = body[:body.index("function deadlinePicker(")]
    assert body.count("limits.cover_not_image_text") == 2
