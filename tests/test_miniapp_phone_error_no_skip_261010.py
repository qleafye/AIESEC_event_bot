"""Приёмка 09.10 (Mini App): ошибка телефона отсылала к кнопке «Пропустить», которой в
приложении на шаге телефона нет (там «Поделиться номером из Телеграма»). Чат текст не меняет —
у него кнопка есть.
"""
from __future__ import annotations

import pytest

import i18n_ui_en
import reg_engine

from tests.test_miniapp_form import client, db_path  # noqa: F401 — фикстуры подтягиваются по имени
from tests.test_miniapp_routes import UNREGISTERED_ID, _hdr


@pytest.mark.parametrize("raw", ["", "abc"])
def test_app_error_does_not_mention_skip(raw):
    value, err = reg_engine.validate_answer("phone", raw, surface="app")
    assert value is None
    assert "Пропустить" not in err
    assert "+79161234567" in err


@pytest.mark.parametrize("raw", ["", "abc"])
def test_chat_error_unchanged(raw):
    _value, err = reg_engine.validate_answer("phone", raw)
    assert "«Пропустить»" in err


def test_app_accepts_valid_number():
    assert reg_engine.validate_answer("phone", "+7 999 123-45-67", surface="app") == ("+7 999 123-45-67", None)


def test_patch_in_app_uses_app_text(client):
    resp = client.patch(
        "/app/api/reg/draft", headers=_hdr(UNREGISTERED_ID),
        json={"version": 0, "answers": {"phone": "abc"}},
    )
    assert resp.status_code == 400, resp.text
    assert resp.json()["errors"]["phone"] == reg_engine.PHONE_ERROR_APP


def test_app_text_has_english():
    assert reg_engine.PHONE_ERROR_APP in i18n_ui_en.UI_EN
