"""Поиск настройки по человеческому слову — общий для бота и Mini App.

Менеджер пишет «приветствие» или «оплата» и получает подходящие настройки. Раньше это
умел только экран «⚙️ Настройки» Mini App (`miniapp/static/js/form.js::searchFilter`), а в боте
с сотнями ключей менеджер листал разделы наугад.

Здесь одна точка правды на серверной стороне:

- `search_terms(key)` — слова, по которым настройку ищут (`settings_synonyms`); их же роутер
  Mini App отдаёт фронту полем `search_terms`, второго словаря нет.
- `search(candidates, query)` — то же сопоставление, что `form.js` (нижний регистр, ё=е, все
  слова запроса должны найтись, слово совпадает по началу слова кандидата, с 5 букв — ещё и с
  одной опечаткой), плюс ранжирование: попадание в подпись весит больше, чем в синоним, а
  синоним — больше, чем в подсказку. Фронт Mini App фильтрует на клиенте своей копией этого
  же правила (JS не может позвать Python); ранжирование нужно только боту — там выдача
  обрезана до нескольких кнопок, и порядок решает, увидит ли менеджер нужное.

Модуль корневой и без aiogram: его импортируют и бот, и веб-процесс Mini App.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from settings_synonyms import BOT_ONLY_SYNONYMS, SETTINGS_SYNONYMS

# Опечатка допускается только у слов от этой длины — короче слишком много ложных попаданий
# (то же значение, что SEARCH_FUZZY_MIN_LEN в form.js).
FUZZY_MIN_LEN = 5

_WORD_RE = re.compile(r"[^\W_]+", re.UNICODE)

# Веса полей: подпись настройки — это то, что менеджер видит на кнопке, она важнее всего;
# синоним — слово, которым настройку называют вслух; подсказка длинная и шумная.
_W_LABEL, _W_TERMS, _W_HELP = 3, 2, 1
# Бонус за слово, совпавшее целиком, а не началом («оплата» точнее «оплатившим»).
_W_EXACT = 1

_PER_CITY_SEP = "__city__"


def _base(key: str) -> str:
    return key.split(_PER_CITY_SEP)[0]


def search_terms(key: str, *, bot: bool = False) -> list[str]:
    """Слова-синонимы настройки (композитный ключ города сводится к базовому).

    `bot=True` — поиск бота: к веб-синонимам добавляются `BOT_ONLY_SYNONYMS` (ключи, которые
    правятся только в боте). Веб-поиску они не отдаются — правило `settings_synonyms.py`:
    карта веб-подсказок совпадает с `editable_keys()`."""
    base = _base(key)
    terms = SETTINGS_SYNONYMS.get(base)
    if not terms and bot:
        terms = BOT_ONLY_SYNONYMS.get(base)
    return list(terms or [])


def normalize(text) -> str:
    return ("" if text is None else str(text)).lower().replace("ё", "е")


def words(text) -> list[str]:
    return _WORD_RE.findall(normalize(text))


def _within_one_edit(a: str, b: str) -> bool:
    if a == b:
        return True
    la, lb = len(a), len(b)
    if abs(la - lb) > 1:
        return False
    i = 0
    n = min(la, lb)
    while i < n and a[i] == b[i]:
        i += 1
    if la == lb:
        return a[i + 1:] == b[i + 1:]
    if la > lb:
        return a[i + 1:] == b[i:]
    return a[i:] == b[i + 1:]


def match_word(q: str, word: str) -> int:
    """0 — мимо; 2 — слово совпало целиком; 1 — началом или с одной опечаткой."""
    if word == q:
        return 2
    if word.startswith(q):
        return 1
    if len(q) >= FUZZY_MIN_LEN:
        if _within_one_edit(q, word):
            return 1
        if len(word) > len(q) and _within_one_edit(q, word[: len(q)]):
            return 1
    return 0


@dataclass
class Candidate:
    """Что можно найти: ключ (или любой идентификатор строки), подпись, подсказка, синонимы."""
    key: str
    label: str
    help: str = ""
    terms: list[str] = field(default_factory=list)
    extra: dict = field(default_factory=dict)


def _field_score(q: str, field_words: list[str]) -> int:
    best = 0
    for w in field_words:
        m = match_word(q, w)
        if m > best:
            best = m
            if best == 2:
                break
    return best


def search(candidates: list[Candidate], query: str, limit: int | None = None) -> list[Candidate]:
    """Подходящие кандидаты по убыванию веса; при равенстве — в исходном порядке. Пустой
    запрос — пустой результат (боту нечего показывать «всё»)."""
    q_words = words(query)
    if not q_words:
        return []
    scored: list[tuple[int, int, Candidate]] = []
    for idx, cand in enumerate(candidates):
        label_w = words(cand.label)
        terms_w = words(" ".join(cand.terms))
        help_w = words(cand.help)
        total = 0
        for q in q_words:
            best = 0
            for weight, fw in ((_W_LABEL, label_w), (_W_TERMS, terms_w), (_W_HELP, help_w)):
                m = _field_score(q, fw)
                if m:
                    best = max(best, weight * 2 + (_W_EXACT if m == 2 else 0))
            if not best:
                break
            total += best
        else:
            scored.append((-total, idx, cand))
    scored.sort(key=lambda t: (t[0], t[1]))
    out = [c for _s, _i, c in scored]
    return out[:limit] if limit is not None else out
