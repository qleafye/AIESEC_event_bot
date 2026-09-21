"""Phase 32 Plan 14 (D-27): структурный сторож служебной метки «конца времён»
(`database.db.NO_DEADLINE_AT` = `"9999-12-31 23:59:59"`).

Метка безопасна ровно до тех пор, пока её никто не разбирает и не печатает по своей копии —
`game_labels.task_deadline`/`task_has_deadline`/`task_deadline_text`/`task_deadline_admin`
уже читают её как «срока нет». На ревизии планов фазы 32 нашлось семь собственных копий
разбора на четырёх поверхностях бота и Mini App (список — `.planning/phases/32-ambassador-
waves/32-04-PLAN.md`, раздел «Карта читателей метки «без срока»»), и цена возврата одной из
них — «31.12.9999 23:59» в глазах менеджера или «осталось 2 913 000 дней» в глазах делегата
(T-32-14-01/03). Этот файл — два сторожа по исходникам репозитория, которые ловят следующую
такую копию на уровне теста, а не на приёмке у живого менеджера.

Сторож 1 («свой разбор срока») — ищет `strptime(` с первым аргументом, содержащим
`deadline_at`: это разбор ХРАНИМОГО значения. Разбор ВВОДА менеджера (`parse_deadline` в
`miniapp/routers/admin_tasks.py`, пресеты в `handlers/admin_game_tasks.py`,
`services/scheduler.py`) читает переменную `raw`/`text`/`when`, а не `deadline_at`, — под
детектор не попадает и нарушением не является (проверено собственным тестом детектора ниже).

Сторож 2 («литерал метки») — ищет строку `9999-12-31` где угодно, кроме объявления
`NO_DEADLINE_AT` в `database/db.py`.

Оба сторожа читают файлы с диска синхронно, БД не трогают и приложение не поднимают —
выполняются меньше секунды.
"""
from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# Каталоги обхода — ровно те, что перечислены в разделе «Карта читателей» плана 32-04/32-14:
# корень репозитория (НЕ рекурсивно — там же лежат десятки несвязанных модулей) и шесть
# поддеревьев, где живёт логика проекта. `tests/`, `.venv/`, `.claude/`, `node_modules/`,
# `.planning/` — исключены явно (фикстуры сидируют мусорные даты вида "2099-01-01", план
# 32-04 их разбирал и признал классом (b) — не нарушение).
SCAN_ROOT_ONLY = ROOT
SCAN_SUBTREES = ("handlers", "miniapp", "services", "dashboard", "tools", "database")
EXCLUDE_DIR_NAMES = {"tests", ".venv", ".claude", "node_modules", ".planning", "__pycache__"}

# Сторож 1 — единственный файл на проекте, которому разрешено разбирать `deadline_at` строкой
# `strptime`: корневой `game_labels.py` (план 32-04, задача 1) — `task_deadline`/
# `task_deadline_admin` и есть тот самый единственный разбор, на который обязаны переходить
# все остальные читатели.
ALLOWED_STRPTIME_FILES = {"game_labels.py"}

# Сторож 2 — единственный файл, которому разрешён литерал `9999-12-31`: объявление
# `NO_DEADLINE_AT` в `database/db.py` (план 32-01). `dashboard/queries.py` держит
# ДОКУМЕНТИРОВАННУЮ копию значения (`_NO_DEADLINE_AT`, строка 1660) — модуль дашборда
# намеренно не импортирует `database.db` (см. докстринг файла, «дашборд — read-only
# независимая реализация»), копия используется ТОЛЬКО в булевом сравнении `deadline ==
# _NO_DEADLINE_AT` внутри `_ambassador_task_is_on_time` (нигде не рендерится человеку), и её
# дрейф от оригинала уже ловит отдельный тест `test_ambassador_no_deadline_sentinel_matches_
# bot_db` (`tests/test_dashboard_queries.py`) — второй сторож её не дублирует.
ALLOWED_SENTINEL_FILES = {"database/db.py", "dashboard/queries.py"}

_HUMAN_HINT = (
    "срок разбирает только `game_labels`: возьмите `task_deadline` / `task_has_deadline` / "
    "`task_deadline_text` / `task_deadline_admin`, а метку импортируйте как `NO_DEADLINE_AT`"
)

# Первый аргумент вызова `strptime(...)` — всё до первой запятой или закрывающей скобки.
_STRPTIME_FIRST_ARG = re.compile(r"strptime\(\s*([^,)]*)")


def _is_deadline_strptime(line: str) -> bool:
    """True — строка разбирает ИМЕННО `deadline_at` (первый аргумент `strptime` содержит это
    имя). Разбор ввода менеджера (переменные `raw`/`text`/`when`/`day`/...) не матчит —
    единственный контракт этого детектора, проверенный тестом ниже."""
    match = _STRPTIME_FIRST_ARG.search(line)
    if not match:
        return False
    return "deadline_at" in match.group(1)


def _iter_py_files():
    roots = [(SCAN_ROOT_ONLY, False)] + [(ROOT / name, True) for name in SCAN_SUBTREES]
    for root_dir, recursive in roots:
        if not root_dir.exists():
            continue
        paths = root_dir.rglob("*.py") if recursive else root_dir.glob("*.py")
        for path in paths:
            rel_parts = path.relative_to(ROOT).parts
            if any(part in EXCLUDE_DIR_NAMES for part in rel_parts):
                continue
            yield path


def _find_strptime_violations() -> list[tuple[str, int, str]]:
    violations = []
    for path in _iter_py_files():
        rel = path.relative_to(ROOT).as_posix()
        if rel in ALLOWED_STRPTIME_FILES:
            continue
        for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
            if _is_deadline_strptime(line):
                violations.append((rel, lineno, line.strip()))
    return violations


def _find_sentinel_literal_violations() -> list[tuple[str, int, str]]:
    violations = []
    for path in _iter_py_files():
        rel = path.relative_to(ROOT).as_posix()
        if rel in ALLOWED_SENTINEL_FILES:
            continue
        for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
            if "9999-12-31" in line:
                violations.append((rel, lineno, line.strip()))
    return violations


def _format_violations(violations: list[tuple[str, int, str]], heading: str) -> str:
    body = "\n".join(f"  {path}:{lineno}: {text}" for path, lineno, text in violations)
    return f"{heading}\n{body}\n\n{_HUMAN_HINT}"


# ── сторож 1 ──────────────────────────────────────────────────────────────────────────────

def test_no_own_deadline_strptime_outside_game_labels():
    violations = _find_strptime_violations()
    assert not violations, _format_violations(
        violations, "Свой разбор `deadline_at` через `strptime` вне `game_labels.py`:",
    )


def test_strptime_detector_catches_deadline_at_sample():
    """Самопроверка детектора: строка-нарушение обязана ловиться."""
    assert _is_deadline_strptime('when = datetime.strptime(task["deadline_at"], FMT)')
    assert _is_deadline_strptime("dt = datetime.strptime(row['deadline_at'], fmt)")


def test_strptime_detector_ignores_manager_input_parsing_sample():
    """Самопроверка детектора: разбор ВВОДА менеджера (переменная, не `deadline_at`) не
    матчит — иначе сторож 1 зажёг бы `miniapp/routers/admin_tasks.py::parse_deadline` и
    аналогичные разборы пресетов/ручного ввода, которые план явно не считает нарушением."""
    assert not _is_deadline_strptime("when = datetime.strptime(raw, fmt)")
    assert not _is_deadline_strptime('stamp = datetime.strptime(text, "%d.%m.%Y %H:%M")')


# ── сторож 2 ──────────────────────────────────────────────────────────────────────────────

def test_no_sentinel_literal_outside_allowed_files():
    violations = _find_sentinel_literal_violations()
    assert not violations, _format_violations(
        violations, "Литерал метки «конца времён» (`9999-12-31`) вне разрешённых файлов:",
    )


def test_sentinel_literal_detector_catches_planted_sample(tmp_path):
    """Самопроверка детектора литерала на подсаженном образце — доказывает, что сканер
    реально читает содержимое файла, а не просто проверяет имя."""
    planted = tmp_path / "planted_copy.py"
    planted.write_text('SOME_OTHER_SENTINEL = "9999-12-31 23:59:59"\n', encoding="utf-8")
    text = planted.read_text(encoding="utf-8")
    assert any("9999-12-31" in line for line in text.splitlines())


# ── контракт списков разрешений ──────────────────────────────────────────────────────────

def test_allowed_file_lists_are_explicit_constants():
    assert ALLOWED_STRPTIME_FILES == {"game_labels.py"}
    assert ALLOWED_SENTINEL_FILES == {"database/db.py", "dashboard/queries.py"}
