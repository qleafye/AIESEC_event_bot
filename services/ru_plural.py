"""Склонение существительного после числа: 1 заявку, 2 заявки, 5 заявок; 0,5 балла.

Дробное число по-русски требует формы родительного падежа единственного числа — она совпадает
с формой «две»: «0,5 балла», «1,5 балла». `int(n)` молча срезал дробь («1,5» → «1 балл»), поэтому
число разбирается целиком: int, float или строка («+10», «−3», «2,5»). Неразбираемое, nan и
бесконечность — `many`.

`agree_placeholder` — согласование слова валюты с числом В ШАБЛОНЕ, до подстановки: в «{coins}
баллов» слово сразу за плейсхолдером получает форму под значение `coins` («1 балл», «22 балла»,
«1 point»). Трогается только это одно слово; остальной текст и подставляемые значения (название
задания, имя) не меняются. Свободной правки готового текста нет сознательно: «1 021 баллов»,
«до 2 баллов», «1–3 баллов», даты и номера там неотличимы от числа перед словом.
"""
from __future__ import annotations

import math
import re


def _number(n) -> float | None:
    if isinstance(n, bool):
        return float(n)
    if isinstance(n, (int, float)):
        value = float(n)
    else:
        text = str(n).strip().replace("−", "-").replace(" ", "").replace(" ", "")
        text = text.lstrip("+").replace(",", ".")
        try:
            value = float(text)
        except ValueError:
            return None
    return value if math.isfinite(value) else None


def ru_plural(n, one: str, few: str, many: str) -> str:
    value = _number(n)
    if value is None:
        return many
    value = abs(value)
    if value != int(value):
        return few
    k = int(value)
    if k % 10 == 1 and k % 100 != 11:
        return one
    if k % 10 in (2, 3, 4) and k % 100 not in (12, 13, 14):
        return few
    return many


# Слова валюты, которые бот согласует с числом: формы «один / два / пять».
_RU_FORMS: tuple[tuple[str, str, str], ...] = (
    ("балл", "балла", "баллов"),
    ("монета", "монеты", "монет"),
    ("коин", "коина", "коинов"),
)
_RU_WORD_TO_FORMS = {w: forms for forms in _RU_FORMS for w in forms}
_EN_WORD_TO_FORMS = {"point": ("point", "points"), "points": ("point", "points"),
                     "coin": ("coin", "coins"), "coins": ("coin", "coins")}
_WORDS = "|".join(sorted([*_RU_WORD_TO_FORMS, *_EN_WORD_TO_FORMS], key=len, reverse=True))


def _form(word: str, value) -> str:
    if word in _RU_WORD_TO_FORMS:
        return ru_plural(value, *_RU_WORD_TO_FORMS[word])
    one, many = _EN_WORD_TO_FORMS[word]
    number = _number(value)
    return one if number is not None and abs(number) == 1 else many


def agree_placeholder(template, name: str, value):
    """В шаблоне «{name} баллов» слово сразу за плейсхолдером — в форме под `value`.
    Нечисловое `value` и не-строковый шаблон — как есть."""
    if not isinstance(template, str) or _number(value) is None:
        return template
    token = "{" + name + "}"
    if token not in template:
        return template
    pattern = re.compile(re.escape(token) + r"([  ])(" + _WORDS + r")(?!\w)")
    return pattern.sub(lambda m: token + m.group(1) + _form(m.group(2), value), template)


def points_word(n) -> str:
    """«балл» / «балла» / «баллов» для числа `n` — слово валюты в текстах менеджеру."""
    return ru_plural(n, "балл", "балла", "баллов")
