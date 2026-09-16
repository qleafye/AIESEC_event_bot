"""16.09.2026 — сторож дозаливки справочника городов (пробел: поиск «Екат» ничего не находил,
т.к. Екатеринбург и ещё 20+ крупных городов РФ отсутствовали в `cities_ru.json`, а
`seed_lookup_from_snapshot` не подхватывал бы дополнение снапшота на уже засеянной базе
стенда/прода — ранний выход на непустой таблице). Три вещи проверяем:

1. Обновлённый снапшот находится поиском на СВЕЖЕЙ базе (Екатеринбург/Екб).
2. Дозаливка добавляет недостающие записи на УЖЕ засеянной базе (симуляция прод-БД до
   обновления снапшота — города физически нет в таблице, хотя снапшот на диске уже новый).
3. Повторная дозаливка (второй «рестарт») ничего не добавляет, а ручные записи менеджера
   (pinned/added_by) дозаливка не трогает.

БД — тот же приём `_ready(tmp_path)`, что у соседних тестов справочника: pytest-asyncio
недоступен, async идёт через `asyncio.run()`.
"""
from __future__ import annotations

import asyncio

from config import config
from database.db import _connect, init_db
from services.lookup import normalize_alias, search_lookup


def _run(coro):
    return asyncio.run(coro)


def _ready(tmp_path, name="lookup_topup_260916.db"):
    config.DB_PATH = str(tmp_path / name)
    _run(init_db())


async def _count(kind):
    async with _connect() as conn:
        cursor = await conn.execute(
            "SELECT COUNT(*) FROM lookup_entries WHERE kind = ?", (kind,)
        )
        row = await cursor.fetchone()
        return row[0]


async def _delete_canonical(kind, canonical):
    """Симулирует «старую» прод-базу, засеянную ДО обновления снапшота — города физически нет
    ни одной строкой (ни каноники, ни алиасов), хотя файл на диске уже новый."""
    async with _connect() as conn:
        await conn.execute(
            "DELETE FROM lookup_entries WHERE kind = ? AND canonical = ?", (kind, canonical)
        )
        await conn.commit()


async def _mark_as_manager_entry(kind, canonical, admin_id=999):
    """Помечает уже посеянную строку как «внесённую менеджером» (`pin_chip`/`merge_apply` в
    проде пишут `pinned=1`/`added_by`) — дозаливка не должна это стереть или задублировать."""
    async with _connect() as conn:
        await conn.execute(
            "UPDATE lookup_entries SET pinned = 1, added_by = ? WHERE kind = ? AND canonical = ?",
            (admin_id, kind, canonical),
        )
        await conn.commit()


async def _manager_flags(kind, canonical):
    async with _connect() as conn:
        cursor = await conn.execute(
            "SELECT pinned, added_by, source FROM lookup_entries "
            "WHERE kind = ? AND canonical = ? ORDER BY alias",
            (kind, canonical),
        )
        return await cursor.fetchall()


# ── 1. Свежая база — обновлённый снапшот находится поиском ─────────────────────────────────

def test_search_finds_ekaterinburg_by_prefix_on_fresh_db(tmp_path):
    _ready(tmp_path)
    result = _run(search_lookup("city", "Екат"))
    assert any(r["canonical"] == "Екатеринбург" for r in result)


def test_search_finds_ekaterinburg_by_common_abbreviation(tmp_path):
    _ready(tmp_path)
    result = _run(search_lookup("city", "Екб"))
    assert any(r["canonical"] == "Екатеринбург" for r in result)


def test_search_finds_other_previously_missing_cities(tmp_path):
    _ready(tmp_path)
    for query, canonical in [
        ("Челябинск", "Челябинск"),
        ("Краснодар", "Краснодар"),
        ("Тюмень", "Тюмень"),
        ("Владивосток", "Владивосток"),
        ("Севастополь", "Севастополь"),
    ]:
        result = _run(search_lookup("city", query))
        assert any(r["canonical"] == canonical for r in result), f"не найден {canonical}"


# ── 2. Дозаливка на уже засеянной базе (старая база + новый снапшот на диске) ───────────────

def test_topup_adds_missing_city_on_already_seeded_db(tmp_path):
    _ready(tmp_path)
    # "Старая" база: Екатеринбурга ещё нет — как на проде до обновления снапшота.
    _run(_delete_canonical("city", "Екатеринбург"))
    assert _run(search_lookup("city", "Екатеринбург")) == []

    before = _run(_count("city"))
    _run(init_db())  # "рестарт бота" на уже засеянной (но неполной) базе
    after = _run(_count("city"))

    assert after > before
    result = _run(search_lookup("city", "Екб"))
    assert any(r["canonical"] == "Екатеринбург" for r in result)


def test_topup_is_idempotent_second_restart_adds_nothing(tmp_path):
    _ready(tmp_path)
    _run(_delete_canonical("city", "Екатеринбург"))

    _run(init_db())  # первый "рестарт" дозаливает недостающее
    after_first = _run(_count("city"))
    _run(init_db())  # второй "рестарт" — снапшот уже полностью на месте
    after_second = _run(_count("city"))

    assert after_first == after_second


# ── 3. Ручные записи менеджера не трогаются дозаливкой ──────────────────────────────────────

def test_topup_does_not_touch_manager_pinned_entry(tmp_path):
    _ready(tmp_path)
    _run(_mark_as_manager_entry("city", "Москва", admin_id=777))
    before = _run(_manager_flags("city", "Москва"))
    assert before, "запись «Москва» должна быть в снапшоте"
    assert all(pinned == 1 and added_by == 777 for pinned, added_by, _source in before)

    _run(init_db())  # "рестарт" — дозаливка не должна перезаписать pinned/added_by

    after = _run(_manager_flags("city", "Москва"))
    assert before == after


def test_topup_does_not_duplicate_manager_added_alias(tmp_path):
    """Менеджер влил кастомный псевдоним через «Другое → влить» (`services.lookup.merge_apply`
    в проде) под существующей каноникой — у него `source != "wikidata_curated"`. Дозаливка
    снапшота не должна тронуть эту строку ни повторной вставкой, ни изменением source."""
    _ready(tmp_path)
    custom_alias = "Мойгородвручную"
    async def _insert_manager_alias():
        async with _connect() as conn:
            await conn.execute(
                "INSERT OR IGNORE INTO lookup_entries "
                "(kind, canonical, alias, alias_norm, source, pinned, added_by, created_at) "
                "VALUES ('city', 'Москва', ?, ?, 'merge_queue', 0, 777, '2026-09-16 00:00:00')",
                (custom_alias, normalize_alias(custom_alias)),
            )
            await conn.commit()

    _run(_insert_manager_alias())
    before = _run(_count("city"))
    _run(init_db())
    after = _run(_count("city"))
    assert before == after

    async def _source_of_custom_alias():
        async with _connect() as conn:
            cursor = await conn.execute(
                "SELECT source FROM lookup_entries WHERE kind='city' AND alias_norm=?",
                (normalize_alias(custom_alias),),
            )
            return await cursor.fetchone()

    row = _run(_source_of_custom_alias())
    assert row is not None
    assert row[0] == "merge_queue"
