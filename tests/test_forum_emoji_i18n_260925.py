"""Тексты форума с ведущим эмодзи (checkin_*/sos_*/program_*) на английском.

Бот шлёт делегатские тексты через `handlers.i18n.reg_i18n.tr_text`, который снимает ведущий эмодзи
ДО поиска перевода: ручной словарь обязан держать ключ и без префикса, иначе EN-делегат
получает русский текст (подпись QR, «сигнал SOS отправлен»). Mini App переводит целиком через
`services.i18n.tr` — там нужен ключ С эмодзи. Проверяем обе дороги без БД: карта `src_hash ->
en` собирается из `FORM_DEFAULT_EN` так же, как её пишет `seed()`."""
from __future__ import annotations

import pytest

from handlers.i18n import reg_i18n
from services import i18n
from services.i18n_form_manual import FORM_DEFAULT_EN
from services.i18n_glossary import split_leading_symbols
from domain.settings.schema import SETTINGS_SCHEMA

_TR_MAP = {i18n.src_hash(ru): en for ru, en in FORM_DEFAULT_EN.items()}

# Ключ -> как текст уходит к человеку. «chat» — через `reg_i18n.tr_text` (снимает эмодзи),
# «miniapp» — через `i18n.tr_setting` (целиком). Новый ключ форума с ведущим эмодзи обязан
# попасть сюда — иначе падает `test_every_forum_emoji_key_is_classified`.
_FORUM_EMOJI_KEYS = {
    "checkin_qr_caption_text": "chat",
    "sos_sent_text": "chat",
    "checkin_undo_button_text": "miniapp",
    # F19: напоминание взявшему SOS — через перевод адресату (services/sos.py::_translated_for).
    "sos_claimed_remind_text": "chat",
    # F19: сигнал менеджерам «взяли, но не решили» — служебный, оргкомитету, не переводится.
    "sos_claimed_escalation_text": None,
    # Инструкция волонтёру (group "apps") — служебный текст, не переводится.
    "checkin_volunteer_guide_text": None,
    # Кнопки опроса «не пришли» делегату (подписи из реестра, 10.10) — перевод как у чата.
    "checkin_not_arrived_here_button_text": "chat",
    "checkin_not_arrived_coming_button_text": "chat",
    "checkin_not_arrived_cant_button_text": "chat",
}


def _forum_emoji_keys() -> set[str]:
    out = set()
    for key, spec in SETTINGS_SCHEMA.items():
        if not key.startswith(("checkin_", "sos_", "program_")):
            continue
        default = spec.get("default")
        if isinstance(default, str) and split_leading_symbols(default.strip())[0]:
            out.add(key)
    return out


def test_every_forum_emoji_key_is_classified():
    assert _forum_emoji_keys() == set(_FORUM_EMOJI_KEYS)


@pytest.mark.parametrize("key", sorted(k for k, v in _FORUM_EMOJI_KEYS.items() if v))
def test_forum_emoji_default_is_english_for_en_delegate(key):
    default = SETTINGS_SCHEMA[key]["default"].strip()
    expected = FORM_DEFAULT_EN[default]
    assert i18n.tr(default, "en", _TR_MAP) == expected
    if _FORUM_EMOJI_KEYS[key] == "chat":
        assert reg_i18n.tr_text(default, "en", _TR_MAP) == expected


def test_qr_caption_english_for_en_delegate():
    caption = SETTINGS_SCHEMA["checkin_qr_caption_text"]["default"]
    out = reg_i18n.tr_text(caption, "en", _TR_MAP)
    assert out.startswith("🎟 Your QR code for check-in")
    assert reg_i18n.tr_text(caption, "ru", _TR_MAP) is caption
