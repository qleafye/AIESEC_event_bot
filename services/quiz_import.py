"""Импорт контента теста компетенций из CSV: шаблон, разбор, предпросмотр, применение.

Только CSV (без новых зависимостей). Формат — одна строка на пару «вариант — компетенция»:
вопрос; вариант; компетенция; баллы. Пустая ячейка вопроса — продолжение прежнего вопроса,
тот же вариант в соседних строках с разными компетенциями — один вариант с несколькими
компетенциями. Ячейки — только данные: формулы не исполняются, ничего не вычисляется."""
from __future__ import annotations

import csv
import io
from dataclasses import dataclass, field

from database import quiz_db, session_enroll_db
from services.checkin import _sniff_dialect, decode_scan_export
from services.ru_plural import ru_plural

TEMPLATE_HEADERS = ("вопрос", "вариант", "компетенция", "баллы")
_DELIMITERS = (";", ",", "\t")
_DEFAULT_COMPETENCIES = ("Лидерство", "Командность")


@dataclass
class ParsedQuiz:
    questions: list[dict] = field(default_factory=list)
    unknown_competencies: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    @property
    def option_count(self) -> int:
        return sum(len(q["options"]) for q in self.questions)


async def build_template(city: str) -> bytes:
    """Файл-образец: заголовок и два вопроса-примера с компетенциями города."""
    names = [c["name"] for c in await session_enroll_db.list_competencies(city)]
    names = names or list(_DEFAULT_COMPETENCIES)
    first, second = names[0], names[1] if len(names) > 1 else names[0]
    buffer = io.StringIO()
    writer = csv.writer(buffer, delimiter=";", lineterminator="\r\n")
    writer.writerow(TEMPLATE_HEADERS)
    writer.writerow(["Как вы ведёте команду к цели?", "Беру ответственность на себя", first, 5])
    writer.writerow(["", "Жду указаний", first, 1])
    writer.writerow(["Что для вас важнее в работе?", "Договориться со всеми", second, 5])
    writer.writerow(["", "Сделать всё самому", second, 1])
    return buffer.getvalue().encode("utf-8-sig")


def _delimiter(text: str) -> str:
    sample = text[:4096]
    dialect = _sniff_dialect(sample)
    if dialect is not None and dialect.delimiter in _DELIMITERS:
        first = next(csv.reader([sample.splitlines()[0]], delimiter=dialect.delimiter), [])
        if len(first) >= 2:
            return dialect.delimiter
    head = sample.splitlines()[0] if sample.strip() else ""
    best = max(_DELIMITERS, key=head.count)
    return best if head.count(best) else ";"


def _norm(name: str) -> str:
    return " ".join(name.split()).casefold()


def parse_csv(raw: bytes, competency_ids_by_name: dict[str, int], points_max: int) -> ParsedQuiz:
    parsed = ParsedQuiz()
    text = decode_scan_export(raw).lstrip("﻿")
    if not text.strip():
        parsed.errors.append("Файл пустой: в нём нет ни одной строки.")
        return parsed
    known = {_norm(name): cid for name, cid in competency_ids_by_name.items()}
    rows = list(csv.reader(io.StringIO(text), delimiter=_delimiter(text)))

    current: dict | None = None
    last_option: dict | None = None
    first_data_seen = False
    for number, row in enumerate(rows, start=1):
        cells = [(c or "").strip() for c in row] + ["", "", "", ""]
        question, option, comp, pts = cells[:4]
        if not any(cells[:4]):
            continue
        if not first_data_seen:
            first_data_seen = True
            if tuple(c.casefold() for c in cells[:4]) == TEMPLATE_HEADERS:
                continue
        if question and (current is None or question != current["text"]):
            current = {"text": question, "options": []}
            parsed.questions.append(current)
            last_option = None
        elif current is None:
            parsed.errors.append(
                f"Строка {number}: не указан вопрос. Впишите текст вопроса в первую колонку."
            )
            continue
        if not option:
            parsed.errors.append(
                f"Строка {number}: не указан вариант ответа. Впишите его во вторую колонку."
            )
            continue
        if last_option is None or last_option["text"] != option:
            last_option = {"text": option, "points": {}}
            current["options"].append(last_option)
        if not comp and not pts:
            continue
        try:
            value = int(pts)
            if not 0 <= value <= points_max:
                raise ValueError
        except ValueError:
            parsed.errors.append(
                f"Строка {number}: баллы должны быть числом от 0 до {points_max}, "
                f"а там «{pts}»."
            )
            continue
        if not comp:
            parsed.errors.append(
                f"Строка {number}: у баллов нет компетенции. Впишите её в третью колонку."
            )
            continue
        cid = known.get(_norm(comp))
        if cid is None:
            if comp not in parsed.unknown_competencies:
                parsed.unknown_competencies.append(comp)
            continue
        last_option["points"][cid] = value
    for number, question in enumerate(parsed.questions, start=1):
        if not question["options"] and not parsed.errors:
            parsed.errors.append(
                f"Вопрос {number} «{question['text'][:60]}» без вариантов ответа. "
                "Добавьте хотя бы один вариант во вторую колонку."
            )
    if not parsed.questions and not parsed.errors:
        parsed.errors.append("В файле не нашлось ни одного вопроса.")
    return parsed


_plural = ru_plural  # общая функция склонения (services/ru_plural.py)


def _count(n: int, one: str, few: str, many: str) -> str:
    return f"{n} {_plural(n, one, few, many)}"


def preview_text(
    parsed: ParsedQuiz, *, current_questions: int, current_options: int, open_attempts: int,
) -> str:
    lines = []
    if parsed.errors:
        lines.append("В файле есть ошибки, ничего не заменено:")
        lines.extend(parsed.errors[:10])
        if len(parsed.errors) > 10:
            lines.append(f"…и ещё ошибок: {len(parsed.errors) - 10}")
        return "\n".join(lines)
    lines.append(
        "В файле: "
        f"{_count(len(parsed.questions), 'вопрос', 'вопроса', 'вопросов')}, "
        f"{_count(parsed.option_count, 'вариант', 'варианта', 'вариантов')}."
    )
    if parsed.unknown_competencies:
        lines.append(
            "Неизвестные компетенции: " + ", ".join(parsed.unknown_competencies)
            + ". Баллы по ним не сохранятся — сначала заведите их в списке компетенций."
        )
    lines.append(
        "Будет заменено: "
        f"{_count(current_questions, 'вопрос', 'вопроса', 'вопросов')}, "
        f"{_count(current_options, 'вариант', 'варианта', 'вариантов')}."
    )
    if open_attempts:
        lines.append(f"Незавершённых попыток: {open_attempts} — начнутся заново.")
    return "\n".join(lines)


async def apply(quiz_id: int, parsed: ParsedQuiz) -> int:
    """Заменяет контент теста одной транзакцией; возвращает новую версию контента."""
    if parsed.errors:
        raise ValueError("В файле есть ошибки — исправьте их и загрузите файл заново.")
    if not parsed.questions:
        raise ValueError("В файле нет ни одного вопроса — заменять нечем.")
    return await quiz_db.replace_content(quiz_id, parsed.questions)
