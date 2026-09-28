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
