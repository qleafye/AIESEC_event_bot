"""Phase 26.1 Plan 01 (SD-01, T-26.1-01-01): реестр событий супердашборда.

Разбор bootstrap-переменной `DASHBOARD_EVENTS` в список источников (код события -> путь
к его БД). Модуль stdlib-only (`dataclasses`, `logging`, `os`, `re`) — не импортирует ни
`dashboard.config` (тот сам импортирует этот модуль, обратный импорт дал бы цикл), ни
модулей бота (aiogram/database/...), см. `tests/test_dashboard_readonly.py`.

Fail-soft ПО ЗАПИСИ (RESEARCH Q2, CONTEXT `<research_followups>` п.2): опечатка в одной
записи не должна ронять процесс дашборда целиком — она пропускается с `logger.warning`,
остальные события разбираются дальше.
"""
from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass

logger = logging.getLogger(__name__)

# Латиница в нижнем регистре, цифры, дефис/подчёркивание — максимум 32 символа. Тот же
# смысл, что у slug-кода в остальном проекте (метки кампаний, коды городов).
_CODE_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,31}$")


@dataclass(frozen=True)
class EventSource:
    """`db_path` — уже абсолютный путь (`os.path.abspath` применяется при разборе, не при
    использовании) — так подменённый/опечатанный путь виден в логе старта, а не открывается
    молча относительно текущей рабочей директории процесса (T-26.1-01-01)."""
    code: str
    db_path: str


def _split_records(raw: str) -> list[str]:
    """Разделители записи — «;» И перевод строки (ловушка «мобильный Enter = отправка»,
    CLAUDE.md — тот же приём, что и в `dashboard.access._parse_caps_list`)."""
    records: list[str] = []
    for line in raw.splitlines():
        for segment in line.split(";"):
            segment = segment.strip()
            if segment:
                records.append(segment)
    return records


def parse_events(raw: str) -> tuple[EventSource, ...]:
    """Формат `код=путь;код=путь` (разделитель внутри записи — первый «=», `maxsplit`-стиль
    через `str.partition`: в пути «=» не встречается, но так надёжнее). Пустая строка ->
    `()`. Каждая битая запись пропускается МОЛЧА для процесса (без исключения наружу) и
    ГРОМКО для лога (`logger.warning`, с объяснением, что именно не так и что делать) —
    пустая запись, запись без «=», с пустым путём, с кодом не по маске, с уже занятым кодом.
    Принятая запись логируется `logger.info` с кодом и итоговым абсолютным путём."""
    events: list[EventSource] = []
    seen_codes: set[str] = set()
    for record in _split_records(raw or ""):
        if "=" not in record:
            logger.warning(
                "DASHBOARD_EVENTS: запись %r пропущена — нет «=» между кодом события и "
                "путём к БД (формат: код=путь_к_базе)", record,
            )
            continue
        code, _, path = record.partition("=")
        code = code.strip()
        path = path.strip()
        if not path:
            logger.warning(
                "DASHBOARD_EVENTS: у записи с кодом %r пустой путь к БД — запись пропущена",
                code,
            )
            continue
        if not _CODE_RE.match(code):
            logger.warning(
                "DASHBOARD_EVENTS: код события %r не подходит под формат (нужна латиница "
                "в нижнем регистре, цифры, дефис/подчёркивание) — запись пропущена", code,
            )
            continue
        if code in seen_codes:
            logger.warning(
                "DASHBOARD_EVENTS: код события %r уже занят предыдущей записью — повторная "
                "запись пропущена", code,
            )
            continue
        abs_path = os.path.abspath(path)
        events.append(EventSource(code=code, db_path=abs_path))
        seen_codes.add(code)
        logger.info("DASHBOARD_EVENTS: событие %r -> %s", code, abs_path)
    return tuple(events)


def multi_mode(events) -> bool:
    """SD-08: одиночный или пустой реестр — обычный дашборд события, без изменений в
    поведении. Режим сравнения включается только при ДВУХ и более источниках."""
    return len(events) >= 2
