"""Тексты экранов «Информация о форуме»/«Контакты» и кнопки «🪙 Баланс» — из настроек, дефолт
дословно прежний текст (его же находит ручной английский слой)."""
from __future__ import annotations

import asyncio

from config import config
from database import db
from handlers.i18n import reg_i18n
from domain.settings.ui_text_fields import (
    GAME_SCREEN_FIELD_ORDER, INFO_SCREEN_FIELD_ORDER, UI_TEXT_SCHEMA, ui_text, ui_tr,
)
from tests._dbtpl import fast_init_db


def _ready(tmp_path):
    config.DB_PATH = str(tmp_path / "info_labels.db")
    fast_init_db()


def test_keys_on_bot_screens():
    from handlers.settings.admin_settings import _settings_group_keys
    from domain.settings.amb_fields import AMB_FIELD_ORDER

    event = _settings_group_keys("event")
    game = _settings_group_keys("game")
    assert all(k in event for k in INFO_SCREEN_FIELD_ORDER)
    assert all(k in game for k in GAME_SCREEN_FIELD_ORDER)
    assert "wave_rating_button_text" in AMB_FIELD_ORDER


def test_defaults_still_translated_by_manual_layer():
    en = lambda s: reg_i18n.tr_text(s, "en", {})  # noqa: E731
    for key in ("info_date_label_text", "info_venue_title_text", "balance_history_button_text"):
        default = UI_TEXT_SCHEMA[key]["default"]
        assert en(default) != default, key


def test_custom_value_escaped_and_empty_falls_back(tmp_path):
    _ready(tmp_path)
    asyncio.run(db.set_setting("info_venue_title_text", "Где <мы>"))
    asyncio.run(db.set_setting("info_date_label_text", ""))
    same = lambda s: s  # noqa: E731
    assert asyncio.run(ui_tr("info_venue_title_text", same)) == "Где &lt;мы&gt;"
    assert asyncio.run(ui_text("info_date_label_text")) == "Дата"
    assert asyncio.run(ui_text("info_date_lead_text")) == ""  # пусто -> фраза по типу события
