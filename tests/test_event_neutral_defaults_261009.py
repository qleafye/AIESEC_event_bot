"""Нейтральные дефолты 09.10: заводские тексты больше не называют конкретное событие.

Дефолты реестра говорили «Добро пожаловать на Юлид!», «вот твой Юлид в цифрах», «Завтра форум!»
— и это видел делегат конференции и СкиллАпа, у которых менеджер текст не переписывал. Теперь
название события подставляется из «🎪 Название мероприятия» (`{event}`), а «форум» в дефолтах
делегату заменён на «мероприятие»/«площадку». Сохранённые менеджером значения не трогаются —
меняется только `default`.

pytest-asyncio в окружении нет — async через `asyncio.run()`, БД — `tests/_dbtpl.fast_init_db`.
"""
from __future__ import annotations

import asyncio
import re

from config import config
from database import db
from services.text_fill import EVENT_FALLBACK, event_label, event_name, fill_event
from settings_schema import SETTINGS_SCHEMA
from tests._dbtpl import fast_init_db

ADMIN_ID = 261009001
UID = 261009010

_BRAND_RE = re.compile(r"Юлид|ЮЛид|YouLead|РилТолк|RusCo|СкиллАп\s*\d", re.IGNORECASE)

# Где «форум» в дефолте оставлен сознательно: модуль живёт только на форумах или текст
# ссылается на подпись экрана/тип события, а не называет событие делегату.
_FORUM_WORD_ALLOWED = {
    # перенос неявившихся с регионального форума на форум другого города — только Юлид-формат
    "regional_noshow_offer_text",
    # шпаргалка волонтёра ссылается на раздел «✅ Отметки на форуме» — так называется кнопка
    "checkin_volunteer_guide_text",
    # менеджеру: где включить — «🎪 Форум: функции» (подпись экрана)
    "onsite_off_text",
    # подтверждения смены типа события / пресета — перечисляют типы («для конференции»)
    "miniapp_settings_confirm_event_type_text",
    "skillup_preset_confirm_text",
}


def _ready(tmp_path, name="test_event_neutral_defaults_261009.db"):
    config.DB_PATH = str(tmp_path / name)
    fast_init_db()
    config.ADMIN_IDS = [ADMIN_ID]


def _run(coro):
    return asyncio.run(coro)


def _default_texts(entry) -> list[str]:
    out = []
    for value in (entry.get("default"), entry.get("default_free")):
        if isinstance(value, str):
            out.append(value)
        elif isinstance(value, list):
            out.extend(v for v in value if isinstance(v, str))
    return out


# ── реестр ─────────────────────────────────────────────────────────────────────────────────

def test_no_registry_default_names_a_specific_event():
    bad = {
        key: text for key, entry in SETTINGS_SCHEMA.items()
        for text in _default_texts(entry) if _BRAND_RE.search(text)
        and key not in ("skillup_preset_confirm_text",)  # название пресета, а не события
    }
    assert not bad, f"дефолт называет конкретное событие: {sorted(bad)}"


def test_forum_word_left_only_where_module_is_forum_only():
    leaked = sorted(
        key for key, entry in SETTINGS_SCHEMA.items()
        if entry.get("type") == "text" and key not in _FORUM_WORD_ALLOWED
        and any(re.search(r"форум", t, re.IGNORECASE) for t in _default_texts(entry))
    )
    assert not leaked, f"«форум» в дефолте текста: {leaked}"


def test_event_placeholder_defaults_and_hints():
    from settings_placeholders import PLACEHOLDER_LABELS, expected_placeholders

    assert PLACEHOLDER_LABELS["event"] == "название мероприятия"
    for key in ("forum_welcome_text", "regional_noshow_offer_text",
                "forum_stats_card_caption_text", "delegation_welcome_text"):
        assert "{event}" in SETTINGS_SCHEMA[key]["default"], key
        assert "{event}" in SETTINGS_SCHEMA[key]["prompt"], key
        assert expected_placeholders(key)["event"] == "название мероприятия", key


def test_default_source_options_neutral():
    from reg_options import DEFAULT_SOURCE_OPTIONS

    assert "Соцсети Юлид" not in DEFAULT_SOURCE_OPTIONS
    assert "Соцсети мероприятия" in DEFAULT_SOURCE_OPTIONS


# ── подстановка {event} ───────────────────────────────────────────────────────────────────

def test_event_label_uses_name_or_neutral_word():
    assert event_label("СкиллАп 5") == "СкиллАп 5"
    assert event_label("  Юлид  ") == "Юлид"
    assert event_label(None) == EVENT_FALLBACK["ru"] == "мероприятие"
    assert event_label("   ") == "мероприятие"
    assert event_label(None, "en") == "the event"
    assert event_label("Юлид", "en") == "Юлид"


def test_fill_event_keeps_sentence_whole():
    assert fill_event("Добро пожаловать на {event}!", None) == "Добро пожаловать на мероприятие!"
    assert fill_event("Добро пожаловать на {event}!", "РилТолк") == "Добро пожаловать на РилТолк!"
    assert fill_event("Без токена", "Юлид") == "Без токена"
    assert fill_event(None, "Юлид") is None


def test_event_name_reads_setting(tmp_path):
    _ready(tmp_path)
    assert _run(event_name()) is None
    _run(db.set_setting("event_name", "  РилТолк  "))
    assert _run(event_name()) == "РилТолк"


# ── доставка: подстановка доходит до делегата ─────────────────────────────────────────────

class _FakeBot:
    def __init__(self):
        self.sent: list[tuple[int, str]] = []

    async def send_message(self, chat_id, text, **kwargs):
        self.sent.append((chat_id, text))


def _welcome_after_entry(tmp_path, event_title: str | None) -> str:
    import services.checkin as checkin_mod
    import services.forum_welcome as fw
    from services.checkin import ENTRY_POINT, record_arrival

    _ready(tmp_path)
    checkin_mod.clear_first_entry_listeners()
    try:
        _run(db.set_setting("forum_welcome_enabled", "on"))
        if event_title is not None:
            _run(db.set_setting("event_name", event_title))
        fw.register()
        _run(db.add_user({
            "telegram_id": UID, "full_name": "Тест Делегатов", "username": "t",
            "event_city": None, "registration_date": "2026-09-01 00:00:00", "status": "approved",
        }))
        bot = _FakeBot()
        _run(record_arrival(
            _run(db.get_user(UID)), ENTRY_POINT, source="miniapp",
            scanned_at="2026-10-30 09:15:00", bot=bot,
        ))
        assert len(bot.sent) == 1
        return bot.sent[0][1]
    finally:
        checkin_mod.clear_first_entry_listeners()


def test_forum_welcome_substitutes_event_name(tmp_path):
    text = _welcome_after_entry(tmp_path, "РилТолк & друзья")
    assert "Добро пожаловать на РилТолк &amp; друзья!" in text
    assert "{event}" not in text and "Юлид" not in text


def test_forum_welcome_without_event_name_reads_neutral(tmp_path):
    text = _welcome_after_entry(tmp_path, None)
    assert text.endswith("Добро пожаловать на мероприятие!")


def test_stats_card_caption_default_has_no_brand_after_fill():
    caption = SETTINGS_SCHEMA["forum_stats_card_caption_text"]["default"]
    filled = fill_event(caption, "СкиллАп 5")
    assert "СкиллАп 5 в цифрах" in filled
    assert "Юлид" not in fill_event(caption, None)


def test_preview_shows_event_name_or_neutral_word(tmp_path):
    """Превью текста в настройках подставляет {event} так же, как доставка делегату."""
    import settings_ops

    _ready(tmp_path)
    text = SETTINGS_SCHEMA["forum_welcome_text"]["default"]
    samples = _run(settings_ops.preview_samples())
    assert settings_ops.preview_text("forum_welcome_text", text, samples=samples).endswith(
        "Добро пожаловать на мероприятие!")
    _run(db.set_setting("event_name", "РилТолк"))
    samples = _run(settings_ops.preview_samples())
    assert settings_ops.preview_text("forum_welcome_text", text, samples=samples).endswith(
        "Добро пожаловать на РилТолк!")
