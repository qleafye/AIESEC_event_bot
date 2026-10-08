"""Разовый перенос старых баллов из Google-таблицы в монеты бота (источник `transfer`).

Таблица — та, что гейм-менеджеры вели руками до бота: колонка с @ником и колонки с баллами
по заданиям (на Юлиде — вкладка «Гейма чат»: «Ник | Задание 1 (#rolemodel) | …»). Один ник
может встречаться в нескольких строках — баллы складываются. Числа ищем во всех колонках,
кроме колонки ника; заголовок колонки становится частью причины.

Сопоставление — по @нику: сначала анкета (`users.username`), потом ник из чата делегатов
(`chat_usernames`, если человек сменил ник после анкеты). Кого не нашли — показываем списком,
монеты им не пишутся. Перенос повторяемый: у кого перенос уже был, второй раз не начисляется.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from database import chat_coins_db
from database.db import get_user_by_username

_NICK_RE = re.compile(r"^[@＠]?([A-Za-z0-9_]{4,32})$")
_NICK_HEADERS = ("ник", "nick", "username", "юзернейм", "telegram", "телеграм", "тг")
_REASON_MAX = 500


def normalize_nick(raw: str | None) -> str | None:
    m = _NICK_RE.match(str(raw or "").strip())
    return m.group(1) if m else None


def _number(raw: str | None) -> int | None:
    value = str(raw or "").strip().replace(",", ".").replace(" ", "").replace(" ", "")
    if not value:
        return None
    try:
        number = float(value)
    except ValueError:
        return None
    if number != number or number <= 0:  # NaN, ноль и минусы не переносим
        return None
    return int(round(number))


def _column_label(header: str, index: int) -> str:
    """«Задание 1 (#rolemodel)» -> «#rolemodel»; пустой заголовок -> «колонка 3»."""
    header = " ".join(str(header or "").split())
    tag = re.search(r"#\w+", header)
    if tag:
        return tag.group(0)
    return header or f"колонка {index + 1}"


def find_nick_column(values: list[list[str]]) -> int | None:
    """Колонка с никами: по заголовку («Ник», «username», …), иначе — та, где больше всего
    ячеек похожи на @ник."""
    if not values:
        return None
    header = [str(h).strip().lower() for h in values[0]]
    for i, h in enumerate(header):
        first = h.split()[0].strip(":@()") if h.split() else ""
        if first in _NICK_HEADERS:
            return i
    best, best_count = None, 0
    width = max(len(r) for r in values)
    for i in range(width):
        count = sum(1 for row in values[1:] if i < len(row) and str(row[i]).strip().startswith(("@", "＠"))
                    and normalize_nick(row[i]))
        if count > best_count:
            best, best_count = i, count
    return best


@dataclass
class Person:
    nick: str
    total: int = 0
    parts: dict[str, int] = field(default_factory=dict)

    def reason(self, tab_title: str) -> str:
        tab_title = " ".join(str(tab_title).split())
        detail = ", ".join(f"{label} {points}" for label, points in self.parts.items())
        text = f"перенос из таблицы «{tab_title}»: {detail}" if detail else f"перенос из таблицы «{tab_title}»"
        return text[:_REASON_MAX]


@dataclass
class Parsed:
    people: list[Person]
    bad_rows: int  # строки с баллами, но без понятного ника


def parse_values(values: list[list[str]]) -> Parsed | None:
    """None — не нашли колонку ника."""
    nick_col = find_nick_column(values)
    if nick_col is None:
        return None
    header = [str(h) for h in values[0]] if values else []
    people: dict[str, Person] = {}
    bad_rows = 0
    for row in values[1:]:
        cells = [str(c) for c in row]
        points = [
            (i, n) for i, c in enumerate(cells)
            if i != nick_col and (n := _number(c)) is not None
        ]
        nick = normalize_nick(cells[nick_col]) if nick_col < len(cells) else None
        if nick is None:
            if points:
                bad_rows += 1
            continue
        if not points:
            continue
        key = nick.lower()
        person = people.setdefault(key, Person(nick=nick))
        for i, n in points:
            label = _column_label(header[i] if i < len(header) else "", i)
            person.parts[label] = person.parts.get(label, 0) + n
            person.total += n
    return Parsed(people=list(people.values()), bad_rows=bad_rows)


@dataclass
class Plan:
    matched: list[tuple[Person, int]]       # (человек, telegram_id) — начислим
    already: list[tuple[Person, int]]       # перенос уже был
    unknown: list[Person]                   # нет в боте

    @property
    def total(self) -> int:
        return sum(p.total for p, _ in self.matched)


async def build_plan(parsed: Parsed) -> Plan:
    found: dict[str, int] = {}
    missing: list[str] = []
    for person in parsed.people:
        user = await get_user_by_username(person.nick)
        if user:
            found[person.nick.lower()] = int(user["telegram_id"])
        else:
            missing.append(person.nick.lower())
    found.update(await chat_coins_db.chat_username_ids(missing))
    done = await chat_coins_db.transferred_user_ids()

    matched, already, unknown = [], [], []
    seen_ids: dict[int, Person] = {}
    for person in parsed.people:
        tid = found.get(person.nick.lower())
        if tid is None:
            unknown.append(person)
        elif tid in done:
            already.append((person, tid))
        elif tid in seen_ids:
            # Два ника в таблице — один человек (сменил ник): складываем.
            other = seen_ids[tid]
            other.total += person.total
            for label, points in person.parts.items():
                other.parts[label] = other.parts.get(label, 0) + points
        else:
            seen_ids[tid] = person
            matched.append((person, tid))
    return Plan(matched=matched, already=already, unknown=unknown)


async def apply_plan(plan: Plan, tab_title: str, changed_by: int) -> list[tuple[Person, int]]:
    entries = [(tid, person.total, person.reason(tab_title)) for person, tid in plan.matched]
    done = set(await chat_coins_db.record_transfer(entries, changed_by))
    return [(person, tid) for person, tid in plan.matched if tid in done]
