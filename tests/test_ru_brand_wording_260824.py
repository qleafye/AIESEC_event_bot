"""Quick 260824: «ЮЛид и АЙСЕК пишем только на русском».

Сторож против регресса: проверяет не файлы целиком (в путях к SVG латиница законна),
а именно значения человеко-видимых словарей подписей — подписи пресетов оформления
и подписи/подсказки реестра настроек (включая зеркало в reg_labels).

Коды (id пресетов `bluebook`/`youlead`, ключи реестра `reg_q_aiesec_role` и т.п.) —
не человеко-видимые строки, их не трогаем и в этом сторожe не проверяем.
"""
from __future__ import annotations

import re

from handlers.admin_miniapp_theme import _PRESET_BLURBS, _PRESET_LABELS
import reg_labels
from settings_schema import SETTINGS_SCHEMA

LATIN_BRAND_SUBSTRINGS = ("aiesec", "youlead", "bluebook")

# Примеры-URL (t.me/aiesec_ru и т.п.) — не человеко-видимая надпись бренда, а
# служебная ссылка; латиница там обязана остаться рабочей ссылкой.
_URL_RE = re.compile(r"https?://\S+")


def _contains_latin_brand(text: str) -> bool:
    lowered = _URL_RE.sub("", text.lower())
    return any(sub in lowered for sub in LATIN_BRAND_SUBSTRINGS)


def test_preset_labels_are_cyrillic():
    assert set(_PRESET_LABELS.keys()) == {"bluebook", "youlead"}
    assert set(_PRESET_BLURBS.keys()) == {"bluebook", "youlead"}

    offenders = [
        key for key, value in {**_PRESET_LABELS, **_PRESET_BLURBS}.items()
        if _contains_latin_brand(value)
    ]
    assert not offenders, f"латиница бренда в подписях/описаниях пресетов: {offenders}"


def test_preset_label_has_no_nested_parens():
    offenders = [key for key, value in _PRESET_LABELS.items() if "(" in value]
    assert not offenders, f"скобки в подписи пресета дадут вложенные скобки: {offenders}"


def test_registry_labels_and_prompts_are_cyrillic():
    offenders = []

    for key, entry in SETTINGS_SCHEMA.items():
        label = entry.get("label")
        if label is not None and _contains_latin_brand(label):
            offenders.append(f"SETTINGS_SCHEMA[{key!r}].label")
        prompt = entry.get("prompt")
        if prompt is not None and _contains_latin_brand(prompt):
            offenders.append(f"SETTINGS_SCHEMA[{key!r}].prompt")

    for key, label in reg_labels.REG_LABELS.items():
        if label is not None and _contains_latin_brand(label):
            offenders.append(f"REG_LABELS[{key!r}]")

    assert not offenders, f"латиница бренда в реестре/подписях анкеты: {offenders}"


def test_aiesec_role_label_matches_registry():
    assert (
        reg_labels.REG_LABELS["reg_q_aiesec_role"]
        == SETTINGS_SCHEMA["reg_q_aiesec_role"]["label"]
    )
