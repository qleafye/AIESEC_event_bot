"""Быстрая замена `init_db()` в тестах — шаблонная БД вместо повторного прогона схемы.

Зачем. `database.db.init_db()` — это ~39 `CREATE TABLE`, ~98 `_ensure_column`, идемпотентные
миграции по `PRAGMA user_version` и посев `lookup_entries` (~1-2 тыс. строк из офлайн-снапшота,
`seed_lookup_from_snapshot` вызывается дважды — "university"/"city"). На пустой БД это
~1.1-1.4с чистого времени (замерено 23.09 в этом воркере: `asyncio.run(init_db())` в холостом
цикле). 257 из ~390 файлов `tests/` заводят такую БД на КАЖДЫЙ тест — при ~7000 тестах это и
есть основная доля долгого прогона.

Идея: `init_db()` на пустой БД детерминирован — итог не зависит от времени вызова (кроме
`created_at` у посеянных строк `lookup_entries`, который никто в тестах не проверяет на
свежесть — см. коммит, добавивший этот модуль). Значит собранный один раз файл можно
скопировать (`shutil.copy`, ~3-5мс) вместо повторного прогона всей функции (~1.1-1.4с) —
итоговая схема, PRAGMA user_version и содержимое таблиц побайтово те же, что дал бы прямой
вызов `init_db()` на тот же путь.

НЕ использовать (и не переводили — см. коммит) в тестах, которые сами проверяют:
- миграцию старой схемы (легаси-таблица собрана вручную ДО init_db, чтобы проверить
  ALTER TABLE/бэкафилл) — tests/test_db_phase1.py, test_db_phase4.py, test_db_phase5.py,
  test_cities_phase71.py, test_db_hot_path_indexes.py, test_i18n_store_27.py,
  test_consent_raw_button_260907.py, test_consent_versioning_260822.py,
  test_manager_city_091.py, test_miniapp_outbox.py, test_season_import_073.py,
  test_game_archive_260818.py, test_city_offparity_phase72.py;
- аддитивность/идемпотентность самого init_db() при повторном вызове НА УЖЕ НАПОЛНЕННОЙ
  тестом БД (второй вызов должен не уронить и не стереть данные первого) — в таких тестах
  оставлен настоящий `init_db()` на месте второго (и последующих) вызовов, конвертирован
  только первый (реальная исходная подготовка).
"""
from __future__ import annotations

import asyncio
import os
import shutil
import tempfile

_TEMPLATE_PATH: str | None = None


def _build_template() -> str:
    """Строит шаблонную БД один раз на процесс (у каждого воркера pytest-xdist — свой
    процесс и своя копия этого модуля, гонки между воркерами нет)."""
    global _TEMPLATE_PATH
    if _TEMPLATE_PATH is not None and os.path.exists(_TEMPLATE_PATH):
        return _TEMPLATE_PATH

    from config import config
    from database.db import init_db

    template_dir = tempfile.mkdtemp(prefix="gsd_dbtpl_")
    template_path = os.path.join(template_dir, "template.db")
    original_db_path = config.DB_PATH
    try:
        config.DB_PATH = template_path
        asyncio.run(init_db())
    finally:
        config.DB_PATH = original_db_path

    _TEMPLATE_PATH = template_path
    return _TEMPLATE_PATH


def fast_init_db() -> None:
    """Копия готовой схемы (см. докстринг модуля) поверх текущего `config.DB_PATH` —
    замена `asyncio.run(init_db())`/`await db.init_db()` в тестах, которым нужна просто
    свежая пустая БД со всеми таблицами и миграциями, а не сам факт прогона SQL.

    Вызывающий обязан выставить `config.DB_PATH` ДО этого вызова — как и раньше перед
    `init_db()`. Синхронная (не корутина) — на месте `await db.init_db()` вызывается без
    `await`, на месте `asyncio.run(db.init_db())`/`_run(db.init_db())` — напрямую."""
    from config import config

    template_path = _build_template()
    shutil.copy(template_path, config.DB_PATH)
