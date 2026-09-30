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
import atexit
import os
import shutil
import tempfile
import threading

_TEMPLATE_PATH: str | None = None


def _run_coro_sync(coro):
    """Запускает корутину синхронно вне зависимости от того, крутится ли уже event loop
    в текущем потоке.

    fast_init_db() заменяет как голые вызовы (`asyncio.run(init_db())` — нет своего луп'а),
    так и `await db.init_db()` внутри тестовых `async def go(): ...`, запущенных через
    СНАРУЖИ `asyncio.run(go())`. В первом случае можно звать `asyncio.run()` напрямую; во
    втором луп уже крутится, и `asyncio.run()` внутри него падает `RuntimeError: asyncio.run()
    cannot be called from a running event loop` -- строим шаблон в отдельном потоке со своим
    собственным лупом (`config` -- обычный модульный объект, не thread-local, `join()` ниже
    даёт нужный happens-before без дополнительной синхронизации)."""
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        asyncio.run(coro)
        return

    errors: list[BaseException] = []

    def _worker():
        loop = asyncio.new_event_loop()
        try:
            loop.run_until_complete(coro)
            # aiosqlite закрывает соединение через СВОЙ фоновый поток (call_soon_threadsafe
            # на этот же луп); без паузы луп иногда успевает закрыться в run_until_complete
            # раньше, чем этот поток дошлёт финальный результат -- безобидный, но шумный
            # "RuntimeError: Event loop is closed" в PytestUnhandledThreadExceptionWarning
            # (замечено на батче 260923). Один лишний цикл лупа даёт этому потоку время.
            loop.run_until_complete(asyncio.sleep(0.05))
        except BaseException as exc:  # noqa: BLE001 -- пробрасываем в вызывающий поток как есть
            errors.append(exc)
        finally:
            loop.close()

    t = threading.Thread(target=_worker)
    t.start()
    t.join()
    if errors:
        raise errors[0]


def _build_template() -> str:
    """Строит шаблонную БД один раз на процесс (у каждого воркера pytest-xdist — свой
    процесс и своя копия этого модуля, гонки между воркерами нет)."""
    global _TEMPLATE_PATH
    if _TEMPLATE_PATH is not None and os.path.exists(_TEMPLATE_PATH):
        return _TEMPLATE_PATH

    from config import config
    from database.db import init_db

    template_dir = tempfile.mkdtemp(prefix="gsd_dbtpl_")
    # Папка на процесс — без уборки каждый прогон оставлял по копии на воркер (к 30.09 во
    # временной папке ноутбука скопилось ~6500 таких, ~21 ГБ, диск кончился посреди прогона).
    atexit.register(shutil.rmtree, template_dir, True)
    template_path = os.path.join(template_dir, "template.db")
    original_db_path = config.DB_PATH
    try:
        config.DB_PATH = template_path
        _run_coro_sync(init_db())
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
