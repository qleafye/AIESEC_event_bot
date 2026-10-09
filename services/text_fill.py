"""Подстановка в менеджерский шаблон со схлопыванием пустых плейсхолдеров.

Прод Юлида 17.09: `event_place_name` не заполнен, а шаблон «…увидимся {дата} в {город}.»
превращался в «…увидимся 30-31 октября в .». Пустое значение убирает плейсхолдер ВМЕСТЕ с
пробелом перед ним и предлогом-словом «в»/«во»/«in» (английский текст после `tr()` несёт те же
токены) — предложение остаётся целым: «…увидимся 30-31 октября.».

Как у `game_labels.fill_template` — `.replace`/regex по точному токену, не `.format()`:
посторонние `{`/`}` в тексте менеджера не роняют подстановку, неизвестный `{токен}` остаётся.
"""
from __future__ import annotations

import re

_PREPOSITIONS = r"(?:во|в|in)"


def fill_collapsing(template: str, **subs) -> str:
    """Подставляет `{ключ}` → значение; `None`/пустое значение схлопывает токен с предлогом."""
    collapsed = False
    for key, value in subs.items():
        token = "{" + key + "}"
        if token not in template:
            continue
        text = "" if value is None else str(value)
        if text.strip():
            template = template.replace(token, text)
            continue
        # Предлог — только отдельное слово (перед ним начало строки или пробел), регистр
        # любой: «В {город} ждём» тоже схлопнется.
        pattern = (
            r"(?:(?:^|\s+)" + _PREPOSITIONS + r"\s+|\s*)" + re.escape(token)
        )
        template = re.sub(pattern, "", template, flags=re.IGNORECASE)
        collapsed = True
    return template.strip() if collapsed else template


# Подстановка `{event}` — название мероприятия из «🎪 Название мероприятия» (`event_name`).
# Дефолты текстов делегату раньше говорили «Добро пожаловать на Юлид!» на любом событии; теперь
# говорят «на {event}», а пустое название заменяется нейтральным словом — фразы вида «на {event}»
# и «{event} в цифрах» читаются и с ним. Схлопывать токен, как `fill_collapsing`, здесь нельзя:
# «Добро пожаловать на!» — сломанное предложение.
EVENT_FALLBACK = {"ru": "мероприятие", "en": "the event"}


def event_label(name: str | None, lang: str = "ru") -> str:
    """Название мероприятия или нейтральное «мероприятие»/«the event» (язык делегата)."""
    text = (name or "").strip()
    return text or EVENT_FALLBACK["en" if lang == "en" else "ru"]


def fill_event(template, name: str | None, lang: str = "ru"):
    """`{event}` → название мероприятия; не-строку возвращает как есть."""
    if not isinstance(template, str) or "{event}" not in template:
        return template
    return template.replace("{event}", event_label(name, lang))


async def event_name() -> str | None:
    """Сохранённое `event_name` (ленивый импорт БД — модуль остаётся чистым для тестов)."""
    from database.db import get_setting

    return ((await get_setting("event_name")) or "").strip() or None
