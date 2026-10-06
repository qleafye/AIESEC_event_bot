"""Общие подписи админки не называют конкретное событие: бот один на Юлид, РилТолк, СкиллАп."""
import settings_schema
from reg_presets import REG_PRESETS


def test_forum_stats_card_labels_are_neutral():
    for key in ("forum_stats_card_enabled", "forum_stats_card_caption_text", "forum_stats_card"):
        assert "Юлид" not in settings_schema.SETTINGS_SCHEMA[key]["label"]


def test_start_text_hint_example_is_neutral():
    assert "Юлид" not in settings_schema.SETTINGS_SCHEMA["start_text"]["prompt"]


def test_forum_preset_label_is_neutral():
    assert "Юлид" not in REG_PRESETS["forum"]["label"]


def test_photo_button_label_is_neutral():
    from handlers import admin_settings
    labels = [row[1] for row in admin_settings.PHOTO_FIELDS if row[0] == "forum_stats_card"]
    assert labels and all("Юлид" not in x for x in labels)
