"""Сторож раскладки репозитория: в корне лежат только точка входа и загрузка конфига.

Общие модули (без aiogram, для бота, Mini App и дашборда) живут в пакете `core/` — см.
`core/README.md`. Раньше они копились в корне россыпью, и корень стал нечитаемым."""
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# Единственные *.py, которым разрешено лежать в корне:
# main.py — точка входа (CMD в Dockerfile), config.py — загрузка .env (им пользуются рецепты
# эксплуатации вида `from config import settings`).
ALLOWED_ROOT_MODULES = {"main.py", "config.py"}


def test_no_new_python_modules_in_repo_root():
    extra = sorted(p.name for p in ROOT.glob("*.py") if p.name not in ALLOWED_ROOT_MODULES)
    assert not extra, (
        f"В корне репозитория появились новые модули: {extra}. Корень держим чистым — "
        "там только main.py и config.py. Общий модуль без aiogram кладите в пакет core/ "
        "(импорт: `from core import имя_модуля`), код бота — в handlers/ или services/, "
        "разовый скрипт — в tools/ или scripts/."
    )


def test_core_package_init_is_empty():
    init = ROOT / "core" / "__init__.py"
    assert init.exists(), "core/__init__.py пропал — без него `from core import …` не работает"
    assert init.read_text(encoding="utf-8").strip() == "", (
        "core/__init__.py должен оставаться пустым: образ дашборда копирует из core/ только "
        "несколько модулей, и любой импорт здесь уронит его на старте (см. core/README.md)."
    )
