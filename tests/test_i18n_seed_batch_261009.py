"""Пакетные засевы переводов на старте бота (`database.db.seed_manual_translations`,
`database.db.enqueue_untranslated`) — одно соединение и одна транзакция вместо коммита на строку.

Проверяется: (а) итог в таблицах тот же, что у прежнего построчного прохода
(`get_translation` + `upsert_translation` / `enqueue_translation`), включая ручные правки
менеджера и повтор строки во втором проходе; (б) сбой посреди прохода откатывает весь проход.
Все вызовы — через `asyncio.run`, `config.DB_PATH` смотрит в `tmp_path`.
"""
import asyncio

import pytest

from config import config
from database import db
from services.i18n import src_hash
from tests._dbtpl import fast_init_db

ORIGIN_A = "seed_a"
ORIGIN_B = "seed_b"
DICT_A = {"Привет": "Hello", "Пока": "Bye", "Да": "Yes", "Нет": "No"}
# «Да» повторяется во втором проходе — его строка уже ручная с чужим origin, пропуск.
DICT_B = {"Да": "Yeah", "Спасибо": "Thanks"}


def _db_ready(tmp_path, name):
    config.DB_PATH = str(tmp_path / name)
    fast_init_db()


async def _edge_rows():
    # Правка менеджера — не трогать; наш прошлый засев и машинный перевод — перезаписать.
    await db.upsert_translation("en", src_hash("Привет"), "Привет", "Hi (manager)", manual=1, origin_key="admin_edit")
    await db.upsert_translation("en", src_hash("Пока"), "Пока", "old seed", manual=1, origin_key=ORIGIN_A)
    await db.upsert_translation("en", src_hash("Нет"), "Нет", "machine", manual=0)


async def _row_by_row(passes):
    """Прежний построчный проход (`i18n_*_manual.seed` до пакетного варианта)."""
    results = []
    for translations, origin in passes:
        applied = skipped = 0
        for ru, en in translations.items():
            h = src_hash(ru)
            existing = await db.get_translation("en", h)
            if existing and existing.get("manual") and existing.get("origin_key") != origin:
                skipped += 1
                continue
            await db.upsert_translation("en", h, ru, en, manual=1, origin_key=origin)
            applied += 1
        results.append({"applied": applied, "skipped_manager_edit": skipped})
    return results


def _translations():
    async def go():
        rows, _ = await db.list_translations("en", limit=1000)
        return sorted((r["src_hash"], r["src_text"], r["text"], r["manual"], r["origin_key"]) for r in rows)
    return asyncio.run(go())


def test_seed_manual_translations_matches_row_by_row(tmp_path):
    passes = [(DICT_A, ORIGIN_A), (DICT_B, ORIGIN_B)]

    _db_ready(tmp_path, "old.db")
    asyncio.run(_edge_rows())
    old_result = asyncio.run(_row_by_row(passes))
    old_rows = _translations()

    _db_ready(tmp_path, "new.db")
    asyncio.run(_edge_rows())
    new_result = asyncio.run(db.seed_manual_translations("en", passes, src_hash))
    new_rows = _translations()

    assert new_result == old_result
    assert new_result == [{"applied": 3, "skipped_manager_edit": 1}, {"applied": 1, "skipped_manager_edit": 1}]
    assert new_rows == old_rows


def test_seed_manual_translations_failure_rolls_back_whole_call(tmp_path):
    _db_ready(tmp_path, "rollback.db")

    def hash_then_fail(text):
        if text == "Спасибо":
            raise RuntimeError("сбой посреди засева")
        return src_hash(text)

    with pytest.raises(RuntimeError):
        asyncio.run(db.seed_manual_translations("en", [(DICT_A, ORIGIN_A), (DICT_B, ORIGIN_B)], hash_then_fail))

    assert _translations() == []  # первый проход тоже откатился


def _queue():
    async def go():
        return await db.list_pending_translations("en", limit=1000)
    return [(r["id"], r["src_hash"], r["src_text"], r["origin_key"]) for r in asyncio.run(go())]


def test_enqueue_untranslated_matches_row_by_row(tmp_path):
    items = [("k1", "Раз"), ("k2", "Два"), ("k3", "Три"), ("k4", "Раз"), ("k5", "Четыре")]

    async def prepare():
        await db.upsert_translation("en", src_hash("Два"), "Два", "Two", manual=0)       # машинный — пропуск
        await db.upsert_translation("en", src_hash("Три"), "Три", "3", manual=1)         # ручной — пропуск
        await db.enqueue_translation("en", src_hash("Четыре"), "Четыре", origin_key="k0")  # уже в очереди

    async def row_by_row():
        queued = 0
        for origin_key, text in items:
            h = src_hash(text)
            if await db.get_translation("en", h):
                continue
            if await db.enqueue_translation("en", h, text, origin_key=origin_key) is not None:
                queued += 1
        return queued

    _db_ready(tmp_path, "old_q.db")
    asyncio.run(prepare())
    old_queued = asyncio.run(row_by_row())
    old_queue = _queue()

    _db_ready(tmp_path, "new_q.db")
    asyncio.run(prepare())
    new_queued = asyncio.run(db.enqueue_untranslated("en", [(k, src_hash(t), t) for k, t in items]))
    new_queue = _queue()

    assert new_queued == old_queued == 1  # только «Раз», повтор «Раз» и «Четыре» не плодят строк
    assert new_queue == old_queue


def test_enqueue_untranslated_failure_rolls_back_whole_call(tmp_path):
    _db_ready(tmp_path, "rollback_q.db")
    items = [("k1", src_hash("Раз"), "Раз"), ("k2", src_hash("Два"), "Два"), ("k3", src_hash("Три"), object())]

    with pytest.raises(Exception):
        asyncio.run(db.enqueue_untranslated("en", items))

    assert _queue() == []
