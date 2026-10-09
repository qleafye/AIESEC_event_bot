"""Склонение существительного после числа: 1 заявку, 2 заявки, 5 заявок; 0,5 балла.

Дробное число по-русски требует формы родительного падежа единственного числа — она совпадает
с формой «две»: «0,5 балла», «1,5 балла». `int(n)` молча срезал дробь («1,5» → «1 балл»), поэтому
число разбирается целиком: int, float или строка («+10», «−3», «2,5»). Неразбираемое — `many`.

`agree_points` — согласование слова валюты с числом прямо в готовом тексте: «1 баллов» → «1 балл»,
«22 баллов» → «22 балла», «1 points» → «1 point». Подстановка `{coins} баллов` в шаблоне не
знает числа заранее, а тексты менеджер переписывает сам — поэтому чинится результат, а не шаблон.
Трогаются только целые числа прямо перед словом (через пробел); дроби и чужие слова — нет.
"""
from __future__ import annotations

import re


def _number(n) -> float | None:
    if isinstance(n, bool):
        return float(n)
    if isinstance(n, (int, float)):
        return float(n)
    text = str(n).strip().replace("−", "-").replace(" ", "").replace(" ", "")
    text = text.lstrip("+").replace(",", ".")
    try:
        return float(text)
    except ValueError:
        return None


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
_RU_RE = re.compile(
    r"(?<![\d.,])([+\-−]?\d+)([  ])("
    + "|".join(sorted(_RU_WORD_TO_FORMS, key=len, reverse=True))
    + r")(?![а-яёА-ЯЁ\w])"
)
_EN_FORMS = {"point": ("point", "points"), "points": ("point", "points"),
             "coin": ("coin", "coins"), "coins": ("coin", "coins")}
_EN_RE = re.compile(r"(?<![\d.,])([+\-−]?\d+)([  ])(points|point|coins|coin)\b")


def _ru_sub(m: re.Match) -> str:
    one, few, many = _RU_WORD_TO_FORMS[m.group(3)]
    return f"{m.group(1)}{m.group(2)}{ru_plural(m.group(1), one, few, many)}"


def _en_sub(m: re.Match) -> str:
    one, many = _EN_FORMS[m.group(3)]
    word = one if abs(_number(m.group(1)) or 0) == 1 else many
    return f"{m.group(1)}{m.group(2)}{word}"


def agree_points(text):
    """«N баллов/монет/коинов» и «N points/coins» в согласии с числом; не-строку — как есть."""
    if not isinstance(text, str) or not text:
        return text
    return _EN_RE.sub(_en_sub, _RU_RE.sub(_ru_sub, text))


def points_word(n) -> str:
    """«балл» / «балла» / «баллов» для числа `n` — слово валюты в текстах менеджеру."""
    return ru_plural(n, "балл", "балла", "баллов")
