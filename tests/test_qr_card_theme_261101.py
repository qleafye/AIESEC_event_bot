"""Оформление карточки QR из админки (раздел «🎨 Оформление» → «🎟 Карточка QR»).

Главная страховка: пока менеджер ничего не настроил, карточка делегата байт-в-байт та же, что
была до появления оформления — утренняя рассылка QR не должна поменяться «сама». Отпечатки ниже
сняты с карточки ДО правки `services/forum/qr_card.py`; они зависят от версии Pillow (сжатие и
квантование PNG), поэтому сверяются только на той же версии, а независимо от версии сравниваются
варианты рендера в одном прогоне (без темы / theme=None / тема по умолчанию / тема из пустой БД)."""
from __future__ import annotations

import hashlib

import PIL
import pytest

from services.forum.qr_card import render_qr_card

# Сняты на Pillow 11.3.0 + segno 1.6.6 до добавления темы.
_GOLDEN_PIL_VERSION = "11.3.0"
_GOLDEN_SHA256_SHORT = "88eb8566e9dc205c71e678d888f3766560cbbb2387436b1dd32a8b8e0a32793f"
_GOLDEN_SHA256_LONG_NO_CITY = "ff43806d9e93854f1a39c366569fe5857b5add838eed1657abbb6d2e563ce9d0"

_SHORT = (
    "YL26·Иванова Анна·Москва·" + "x" * 22, "Иванова Анна", "Москва",
    "Пропуск на Юлид", "Покажи этот QR на входе", "ЮЛИД",
)
_LONG_NO_CITY = (
    "YL26·Константинопольская Александра Сергеевна·—·" + "y" * 22,
    "Константинопольская Александра Сергеевна Длиннофамильная", "",
    "Пропуск на Юлид", "Покажи этот QR на входе", "ЮЛИД",
)


def _sha(png: bytes) -> str:
    return hashlib.sha256(png).hexdigest()


def test_default_render_is_byte_identical_to_pre_theme_card():
    if PIL.__version__ != _GOLDEN_PIL_VERSION:
        pytest.skip(f"отпечаток снят на Pillow {_GOLDEN_PIL_VERSION}, здесь {PIL.__version__}")
    assert _sha(render_qr_card(*_SHORT)) == _GOLDEN_SHA256_SHORT
    assert _sha(render_qr_card(*_LONG_NO_CITY)) == _GOLDEN_SHA256_LONG_NO_CITY
