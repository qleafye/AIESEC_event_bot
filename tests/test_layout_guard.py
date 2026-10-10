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

# Ярусы ниже хендлеров не импортируют `handlers`: импорт любого `handlers.x` грузит все роутеры
# бота (`handlers/__init__`), а для Mini App и дашборда их нет. Сервисы пока ходят в хендлеры
# ленивыми импортами (права `admin_caps`, рендеры анкеты) — это долг, список только сокращается.
BELOW_HANDLERS = ("services", "keyboards", "database", "miniapp", "dashboard", "domain", "shared")
HANDLERS_IMPORT_ALLOWLIST: set[tuple[str, str]] = {
    ("keyboards/builders.py", "handlers.payment"),
    ("services/access/miniapp_access.py", "handlers.access.admin_caps"),
    ("services/applications/application_effects.py", "handlers.reg.reg_schema"),
    ("services/applications/decision_delivery.py", "handlers.reg.reg_schema"),
    ("services/applications/reject_rules_notify.py", "handlers.access.admin_caps"),
    ("services/chat/chat_coins.py", "handlers.access.admin_caps"),
    ("services/chat_tracking.py", "handlers.access.admin_caps"),
    ("services/checkin_broadcast.py", "handlers.i18n.reg_i18n"),
    ("services/checkin_volunteer_broadcast.py", "handlers.access.admin_caps"),
    ("services/cities/city_move.py", "handlers.reg.reg_schema"),
    ("services/cities/city_move.py", "handlers.registration"),
    ("services/comms/reminders.py", "handlers.access.admin_caps"),
    ("services/daily_digest.py", "handlers.access.admin_caps"),
    ("services/ext_forms/ext_forms_notify.py", "handlers.access.admin_caps"),
    ("services/forum/checkin_not_arrived.py", "handlers.i18n.reg_i18n"),
    ("services/forum/forum_stats_card.py", "handlers.i18n"),
    ("services/forum/forum_welcome.py", "handlers.i18n"),
    ("services/forum_day_report.py", "handlers.access.admin_caps"),
    ("services/forum_noshow_poll.py", "handlers.i18n"),
    ("services/game_digest.py", "handlers.access.admin_caps"),
    ("services/reg_digest.py", "handlers.access.admin_caps"),
    ("services/regional_noshow_move.py", "handlers.access.admin_caps"),
    ("services/regional_noshow_move.py", "handlers.i18n"),
    ("services/registration/reg_finalize.py", "handlers.reg.reg_schema"),
    ("services/registration/reg_finalize.py", "handlers.registration"),
    ("services/scheduler.py", "handlers.access.admin_caps"),
    ("services/scheduler.py", "handlers.i18n"),
    ("services/scheduler.py", "handlers.registration"),
    ("services/session_feedback.py", "handlers.i18n"),
    ("services/session_feedback.py", "handlers.i18n.reg_i18n"),
    ("services/settings/audit.py", "handlers.settings.admin_miniapp"),
    ("services/sheets/sheet_reconcile.py", "handlers.registration"),
    ("services/sheets/sheet_reconcile.py", "handlers.sheets.admin_sheets"),
    ("services/sos.py", "handlers.access.admin_caps"),
    ("services/sos.py", "handlers.i18n"),
    ("services/sos.py", "handlers.states"),
}


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


def _handlers_imports_below():
    found = set()
    for package in BELOW_HANDLERS:
        for path in _py_files(package):
            rel = path.relative_to(REPO_ROOT).as_posix()
            found.update((rel, m) for m in _imports(path) if _top(m) == "handlers")
    return found


def test_lower_layers_do_not_import_handlers():
    new = sorted(_handlers_imports_below() - HANDLERS_IMPORT_ALLOWLIST)
    assert not new, (
        "Новый импорт handlers из нижнего яруса. Общую логику вынесите в services/<домен>/ или "
        f"domain/<домен>/, а хендлер пусть импортирует её оттуда: {new}"
    )


def test_handlers_import_allowlist_has_no_stale_entries():
    stale = sorted(HANDLERS_IMPORT_ALLOWLIST - _handlers_imports_below())
    assert not stale, f"Импорт убран — вычеркните его из HANDLERS_IMPORT_ALLOWLIST: {stale}"


def test_file_relative_asset_dirs_exist():
    """Каталоги, вычисленные от `__file__` модуля, переживают перенос модуля в подпакет: после
    раскладки по доменам `parent.parent` указывал бы уже не на корень репозитория."""
    from handlers.settings.admin_miniapp_theme import PREVIEW_DIR
    from services.forum.checkin_training import _FONTS_DIR

    assert PREVIEW_DIR.is_dir(), PREVIEW_DIR
    assert _FONTS_DIR.is_dir(), _FONTS_DIR
