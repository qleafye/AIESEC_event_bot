"""Переводы SOS для делегата на английском (приёмка 01.10): кнопка геопозиции, шапка ответа."""
from aiogram.types import ReplyKeyboardMarkup

from handlers import reg_i18n
from services import i18n
from services.i18n_form_manual import FORM_DEFAULT_EN

EN_MAP = {i18n.src_hash(ru): en for ru, en in FORM_DEFAULT_EN.items()}


def test_location_button_is_translated_for_english_delegate():
    from handlers.sos import _collecting_kb

    kb = reg_i18n.tr_kb(_collecting_kb(), "en", EN_MAP)
    assert isinstance(kb, ReplyKeyboardMarkup)
    texts = [b.text for row in kb.keyboard for b in row]
    assert "📍 Send location" in texts
    assert "Done" in texts
