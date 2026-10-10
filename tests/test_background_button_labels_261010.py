"""Подписи кнопок фоновых сообщений делегатам — из настроек (экран «📋 Заявки»): «Не пришёл»,
перенос в другой город, «🔕 Не присылать сегодня» / «🔔 Присылать всё»."""
from __future__ import annotations

import asyncio

from config import config
from database import db
from services import checkin_not_arrived as cna
from services import regional_noshow_move as rnm
import services.scheduler as sched
from domain.settings.ui_text_fields import BACKGROUND_BUTTON_FIELD_ORDER, UI_TEXT_SCHEMA
from tests._dbtpl import fast_init_db


def _ready(tmp_path):
    config.DB_PATH = str(tmp_path / "bg_buttons.db")
    fast_init_db()


def _texts(kb):
    return [b.text for row in kb.inline_keyboard for b in row]


def test_keys_on_apps_screen_defaults_match_old_literals():
    from handlers.admin_settings import _settings_group_keys

    apps = _settings_group_keys("apps")
    for key in BACKGROUND_BUTTON_FIELD_ORDER:
        assert key in apps
    assert UI_TEXT_SCHEMA["broadcast_mute_button_text"]["default"] == sched.MUTE_BUTTON_TEXT
    assert UI_TEXT_SCHEMA["broadcast_unmute_button_text"]["default"] == sched.UNMUTE_BUTTON_TEXT
    assert _texts(rnm.offer_keyboard()) == ["✅ Перенести заявку: {target_city}", "Нет, спасибо"]


def test_not_arrived_buttons_follow_settings(tmp_path):
    _ready(tmp_path)
    asyncio.run(db.set_setting("checkin_not_arrived_coming_button_text", "🚕 В пути"))
    asyncio.run(db.set_setting("checkin_not_arrived_cant_button_text", "  "))  # пусто -> по умолчанию
    labels = asyncio.run(cna.button_labels())
    assert _texts(cna._response_kb("2026-10-03", labels=labels)) == [
        "🚕 В пути", "😔 Не смогу прийти", "📍 Я на месте"]


def test_move_offer_buttons_follow_settings(tmp_path):
    _ready(tmp_path)
    asyncio.run(db.set_setting("regional_noshow_accept_button_text", "Еду в {target_city}"))
    labels = asyncio.run(rnm.offer_button_labels())
    kb = rnm._localized_offer_keyboard("ru", {}, "Москва", labels)
    assert _texts(kb) == ["Еду в Москва", "Нет, спасибо"]


def test_mute_buttons_follow_settings(tmp_path):
    _ready(tmp_path)
    asyncio.run(db.set_setting("broadcast_mute_button_text", "🔕 Тише сегодня"))
    asyncio.run(db.set_setting("broadcast_unmute_button_text", "🔔 Всё снова"))
    assert asyncio.run(sched.mute_button(1)).text == "🔕 Тише сегодня"
    assert asyncio.run(sched.unmute_button(1)).text == "🔔 Всё снова"
    langs = asyncio.run(sched.load_recipient_langs())
    assert langs.mute_button(1).text == "🔕 Тише сегодня"
