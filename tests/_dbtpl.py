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
import sqlite3
import tempfile
import threading

_TEMPLATE_PATH: str | None = None

# Копии шаблона, разложенные fast_init_db() по tmp_path тестов. Каждая ~2.6 МБ, а tmp_path
# pytest не чистит до конца сессии — полный прогон оставлял ~25 ГБ. remove_stale_copies()
# (зовётся из conftest после каждого теста) стирает все, кроме текущей config.DB_PATH:
# часть тестов рассчитывает на базу, оставленную предыдущим тестом того же файла (см.
# conftest.py), поэтому последняя живёт до следующей подмены.
_COPIES: list[str] = []

# Итоги засевов replay_seed(): ключ → {таблица: (колонки, строки)}.
_SEEDED: dict[str, dict] = {}


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
    _track_copy(config.DB_PATH)


def _track_copy(db_path) -> None:
    path = os.path.abspath(str(db_path))
    if path not in _COPIES:
        _COPIES.append(path)


def replay_seed(key: str, seed) -> None:
    """Применяет к текущей `config.DB_PATH` то же, что сделал бы `seed()`, но сам `seed()`
    прогоняется один раз на процесс.

    Для тестов, где каждый тест сеет один и тот же большой набор через прод-функции
    (например, переопределения всех per_city-ключей по всем городам — ~970 `set_setting`,
    каждый со своим коммитом: ~60 с на тест на нагруженной машине). Первый вызов прогоняет
    `seed()` на свежей копии шаблона и запоминает строки, которых в шаблоне не было; этот и
    следующие вызовы вставляют их в текущую базу через `INSERT OR REPLACE` — на том же месте
    теста, где раньше стоял `seed()`, так что порядок шагов теста не меняется.

    Годится только для засева, который ДОБАВЛЯЕТ/ПЕРЕЗАПИСЫВАЕТ строки по первичному ключу и
    не зависит от того, что тест успел положить в базу до него (типичный случай —
    `set_setting`). Засев, который удаляет строки или пишет в таблицы с автоинкрементом, здесь
    не поддержан — падает с AssertionError, а не молча меняет смысл теста."""
    from config import config

    delta = _SEEDED.get(key)
    if delta is None:
        template_path = _build_template()
        seeded = os.path.join(os.path.dirname(template_path), f"seeded_{len(_SEEDED)}.db")
        shutil.copy(template_path, seeded)
        target = config.DB_PATH
        try:
            config.DB_PATH = seeded
            seed()
        finally:
            config.DB_PATH = target
        conn = sqlite3.connect(seeded)
        try:
            # База в WAL: без чекпойнта часть засева осталась бы в -wal.
            conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            conn.execute("ATTACH DATABASE ? AS tpl", (template_path,))
            delta = {}
            tables = [r[0] for r in conn.execute(
                "SELECT name FROM main.sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
            )]
            for name in tables:
                removed = conn.execute(
                    f'SELECT COUNT(*) FROM (SELECT * FROM tpl."{name}" EXCEPT SELECT * FROM main."{name}")'
                ).fetchone()[0]
                assert not removed, f"replay_seed({key!r}): засев удаляет/меняет строки шаблона в {name}"
                cur = conn.execute(
                    f'SELECT * FROM main."{name}" EXCEPT SELECT * FROM tpl."{name}"'
                )
                rows = cur.fetchall()
                if rows:
                    cols = [d[0] for d in cur.description]
                    delta[name] = (cols, rows)
        finally:
            conn.close()
        os.remove(seeded)
        _SEEDED[key] = delta

    conn = sqlite3.connect(str(config.DB_PATH))
    try:
        for name, (cols, rows) in delta.items():
            col_sql = ", ".join(f'"{c}"' for c in cols)
            marks = ", ".join("?" for _ in cols)
            conn.executemany(f'INSERT OR REPLACE INTO "{name}" ({col_sql}) VALUES ({marks})', rows)
        conn.commit()
    finally:
        conn.close()


def remove_stale_copies() -> None:
    """Стирает копии шаблона, на которые уже не смотрит config.DB_PATH (вместе с -wal/-shm/
    -journal). Файл, который ещё держит незакрытое соединение (Windows не даёт его удалить),
    остаётся в списке и стирается при следующем вызове."""
    from config import config

    current = os.path.abspath(str(config.DB_PATH))
    keep: list[str] = []
    for path in _COPIES:
        if path == current:
            keep.append(path)
            continue
        try:
            for suffix in ("-wal", "-shm", "-journal"):
                if os.path.exists(path + suffix):
                    os.remove(path + suffix)
            if os.path.exists(path):
                os.remove(path)
        except OSError:
            keep.append(path)
    _COPIES[:] = keep
