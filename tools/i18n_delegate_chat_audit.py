"""Квик 260917-en — аудит покрытия перевода делегатского ЧАТА бота (не анкеты — тот замер уже
даёт `tools/i18n_probe.py` над `services.i18n_sources.corpus()`).

Задача этого скрипта другая и дополняющая: не «сколько текста в корпусе», а «сколько мест
ОТПРАВКИ делегату всё ещё шлют голый русский литерал мимо `reg_i18n.say`/`tr_text`/`tr_kb`/
`tr_for`/`tr_fmt`». Эвристика по AST, не по regex — находит вызовы вида
`message.answer("...")`/`callback.answer("...")`/`callback.message.edit_text("...")`/
`bot.send_message(id, "...")` и т.п., где ПЕРВЫЙ строковый аргумент — ЛИТЕРАЛ (не переменная,
не вызов функции) с кириллицей внутри. Не идеально (не отличит уже-переведённую переменную от
непереведённой), но даёт воспроизводимое число для отчёта «было/стало» и не требует переписывать
на AST-уровне понимание семантики каждого хендлера.

Запуск:
    python tools/i18n_delegate_chat_audit.py
    python tools/i18n_delegate_chat_audit.py --files handlers/user_actions.py handlers/payment.py

Печатает таблицу (файл -> число непереведённых литералов на отправке) и построчный список
находок с номером строки — для ручного разбора, не для CI-гейта (порог не фиксируем: часть
находок — тексты менеджеров/дебаг-логи, не весь список подлежит переводу).
"""
from __future__ import annotations

import argparse
import ast
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# Делегатские хендлеры чата (аудит НЕ включает admin_*.py — та поверхность менеджерская и вне
# объёма Квик 260917-en). Список открытый — добавляйте новые швы делегатского чата сюда же.
DEFAULT_FILES = [
    "handlers/user_actions.py",
    "handlers/payment.py",
    "handlers/registration.py",
    "handlers/reg_flow.py",
    "handlers/reg_steps.py",
    "handlers/reg_consent.py",
    "handlers/reg_resume.py",
    "handlers/reg_resume_fork.py",
    "handlers/reg_handoff.py",
    "handlers/reg_lang.py",
    "handlers/reg_ambassador.py",
    "handlers/reg_extra_steps.py",
    "handlers/reg_types_composite.py",
    "handlers/reg_types_lookup.py",
    "handlers/reg_types_repeatable.py",
    "handlers/game_submit_counter.py",
    "services/application_effects.py",
]

# Методы отправки делегату, за которыми следим — первый АРГУМЕНТ метода (не self/message)
# несёт видимый делегату текст.
SEND_METHODS = {
    "answer", "edit_text", "answer_photo", "answer_document", "edit_caption",
}
# send_message/send_photo/... вызываются НА bot — текст обычно ВТОРОЙ позиционный аргумент
# (первый — chat_id).
BOT_SEND_METHODS = {"send_message", "send_photo", "send_document"}

# Обёртки, которые УЖЕ переводят — вызов ЭТИХ функций/методов не считается находкой, даже если
# несёт литерал (сам wrapper сделает перевод внутри).
TRANSLATING_CALLEES = {
    "say", "tr_text", "tr_for", "tr_fmt", "tr_kb",
}

_CYRILLIC = set("абвгдеёжзийклмнопрстуфхцчшщъыьэюяАБВГДЕЁЖЗИЙКЛМНОПРСТУФХЦЧШЩЪЫЬЭЮЯ")


def _has_cyrillic(s: str) -> bool:
    return any(ch in _CYRILLIC for ch in s)


def _callee_name(node: ast.expr) -> str | None:
    if isinstance(node, ast.Attribute):
        return node.attr
    if isinstance(node, ast.Name):
        return node.id
    return None


def _is_translating_call(node: ast.Call) -> bool:
    name = _callee_name(node.func)
    if name in TRANSLATING_CALLEES:
        return True
    # await reg_i18n.tr_for(...) / await reg_i18n.say(...) — уже покрыто по `name` выше
    # (attr на объекте reg_i18n тоже даёт .attr == "tr_for"/"say").
    return False


class _Finding(ast.NodeVisitor):
    def __init__(self, source_lines: list[str]):
        self.source_lines = source_lines
        self.hits: list[tuple[int, str]] = []

    def _check_literal_arg(self, node: ast.Call, arg_index: int) -> None:
        if len(node.args) <= arg_index:
            return
        arg = node.args[arg_index]
        # f-строки с кириллицей внутри JoinedStr тоже считаем (частый случай: f"{p}{prompt}"
        # обычно уже переменная — но литеральные куски f-строки типа f"Привет {name}" ловим).
        text = None
        if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
            text = arg.value
        elif isinstance(arg, ast.JoinedStr):
            text = "".join(
                v.value for v in arg.values if isinstance(v, ast.Constant) and isinstance(v.value, str)
            )
        if text and _has_cyrillic(text):
            flat = " ".join(text.split())
            self.hits.append((node.lineno, flat[:90]))

    def visit_Call(self, node: ast.Call) -> None:
        if _is_translating_call(node):
            # Не спускаемся внутрь — литералы-аргументы САМОЙ обёртки (say("текст")) уже
            # переводятся ею, это не находка.
            for child in ast.iter_child_nodes(node):
                self.generic_visit(child) if not isinstance(child, ast.Constant) else None
            return
        name = _callee_name(node.func)
        if name in SEND_METHODS:
            self._check_literal_arg(node, 0)
        elif name in BOT_SEND_METHODS:
            self._check_literal_arg(node, 1)
        self.generic_visit(node)


def audit_file(path: Path) -> list[tuple[int, str]]:
    source = path.read_text(encoding="utf-8")
    tree = ast.parse(source, filename=str(path))
    finder = _Finding(source.splitlines())
    finder.visit(tree)
    return finder.hits


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--files", nargs="*", default=None, help="файлы для аудита (по умолчанию — список делегатских хендлеров)")
    parser.add_argument("--quiet", action="store_true", help="только сводная таблица, без построчных находок")
    args = parser.parse_args(argv)

    files = args.files or DEFAULT_FILES
    total = 0
    print(f"{'файл':<45} {'находок':>8}")
    print("-" * 55)
    all_hits: dict[str, list[tuple[int, str]]] = {}
    for rel in files:
        path = ROOT / rel
        if not path.exists():
            print(f"{rel:<45} {'нет файла':>8}")
            continue
        hits = audit_file(path)
        all_hits[rel] = hits
        total += len(hits)
        print(f"{rel:<45} {len(hits):>8}")
    print("-" * 55)
    print(f"{'ИТОГО':<45} {total:>8}")

    if not args.quiet:
        for rel, hits in all_hits.items():
            if not hits:
                continue
            print(f"\n## {rel}")
            for lineno, text in hits:
                print(f"  {lineno:>5}: {text}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
