"""Сторож раскладки кода: «снаружи слои, внутри домены».

Ярусы и что каждому можно импортировать:

- корень репозитория — только `main.py` (точка входа) и `config.py` (загрузка .env);
- `shared/` — только стандартная библиотека: дашборд копирует папку целиком одним `COPY`,
  в его образе нет ни aiogram, ни кода бота;
- `domain/` — логика без aiogram и без `handlers`: её импортируют Mini App и дашборд, где
  роутеров нет, а `handlers/__init__` при импорте любого `handlers.x` грузит роутеры бота;
- `services/`, `handlers/` — aiogram допустим.

Проверяются ВСЕ импорты файла, включая ленивые внутри функций: ленивый импорт тоже
выполнится в процессе Mini App, как только до него дойдёт код.
"""
import ast
import sys

from tests._paths import REPO_ROOT

# Единственные *.py, которым разрешено лежать в корне.
ALLOWED_ROOT_MODULES = {"main.py", "config.py"}

# Нарушения, которые пока терпим: (файл относительно корня, импортируемый модуль).
# Список только сокращается — новое нарушение валит тест.
DOMAIN_ALLOWLIST: set[tuple[str, str]] = set()

FORBIDDEN_IN_DOMAIN = ("aiogram", "handlers")


def _imports(path):
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                yield alias.name
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            yield node.module


def _py_files(package):
    return sorted((REPO_ROOT / package).rglob("*.py"))


def _top(module):
    return module.split(".")[0]


def test_repo_root_has_only_entry_points():
    extra = sorted(p.name for p in REPO_ROOT.glob("*.py") if p.name not in ALLOWED_ROOT_MODULES)
    assert not extra, (
        f"В корне репозитория появились модули: {extra}. В корне только main.py и config.py. "
        "Модуль на чистой stdlib — в shared/, доменная логика без aiogram — в domain/<домен>/, "
        "код бота — в services/<домен>/ или handlers/<домен>/, разовый скрипт — в tools/."
    )


def test_shared_imports_only_stdlib():
    bad = []
    for path in _py_files("shared"):
        for module in _imports(path):
            if _top(module) not in sys.stdlib_module_names and _top(module) != "shared":
                bad.append(f"{path.relative_to(REPO_ROOT).as_posix()}: {module}")
    assert not bad, (
        "shared/ импортирует только стандартную библиотеку (его копирует образ дашборда, "
        f"где нет зависимостей бота): {bad}"
    )


def test_domain_imports_no_aiogram_and_no_handlers():
    bad = []
    for path in _py_files("domain"):
        rel = path.relative_to(REPO_ROOT).as_posix()
        for module in _imports(path):
            if _top(module) in FORBIDDEN_IN_DOMAIN and (rel, module) not in DOMAIN_ALLOWLIST:
                bad.append(f"{rel}: {module}")
    assert not bad, (
        "domain/ не импортирует aiogram и handlers — его грузят Mini App и дашборд без "
        f"роутеров бота. Код с aiogram кладите в services/<домен>/ или handlers/<домен>/: {bad}"
    )


def test_domain_allowlist_has_no_stale_entries():
    present = set()
    for path in _py_files("domain"):
        rel = path.relative_to(REPO_ROOT).as_posix()
        present.update((rel, m) for m in _imports(path))
    stale = sorted(DOMAIN_ALLOWLIST - present)
    assert not stale, f"Нарушение исправлено — уберите его из DOMAIN_ALLOWLIST: {stale}"


def test_new_packages_are_regular_packages():
    missing = sorted(
        d.relative_to(REPO_ROOT).as_posix()
        for top in ("shared", "domain")
        if (REPO_ROOT / top).is_dir()
        for d in [REPO_ROOT / top, *(p for p in (REPO_ROOT / top).rglob("*") if p.is_dir() and p.name != "__pycache__")]
        if not (d / "__init__.py").exists()
    )
    assert not missing, f"Пакетам нужен __init__.py: {missing}"
