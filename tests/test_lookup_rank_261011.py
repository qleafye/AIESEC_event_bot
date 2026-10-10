"""11.10.2026 — порядок подсказок вуза внутри яруса «префикс»/«подстрока».

Приёмка: на «политех» Московский политехнический университет не попадал в пятёрку — ярус
подстроки шёл по алфавиту и обрезался `LIMIT`, частые в сезоне вузы вытесняли редкие. Теперь
внутри яруса: закреплённые → частые ответы сезона (свёртка псевдонимов как у `top_chips`) →
совпадение по самой канонике выше старого псевдонима → короче → по алфавиту.
"""
from __future__ import annotations

import asyncio

from config import config
from database.db import _connect
from services.registration import lookup
from services.registration.lookup import normalize_alias, search_lookup
from tests._dbtpl import fast_init_db

MOSPOLY = "Московский политехнический университет"
SEASON = "YL 26/2"


def _ready(tmp_path):
    config.DB_PATH = str(tmp_path / "lookup_rank_261011.db")
    fast_init_db()
    lookup.invalidate_rank_cache()


async def _answers(university, n, season=SEASON, start=1000):
    async with _connect() as conn:
        await conn.execute(
            "INSERT OR REPLACE INTO bot_settings (key, value) VALUES ('event_season', ?)", (SEASON,)
        )
        for i in range(n):
            await conn.execute(
                "INSERT INTO users (telegram_id, university, season) VALUES (?, ?, ?)",
                (start + i, university, season),
            )
        await conn.commit()
    lookup.invalidate_rank_cache()


async def _entry(canonical, alias, pinned=0):
    async with _connect() as conn:
        await conn.execute(
            "INSERT INTO lookup_entries (kind, canonical, alias, alias_norm, source, pinned, created_at) "
            "VALUES ('university', ?, ?, ?, 'test', ?, '2026-10-11 12:00:00')",
            (canonical, alias, normalize_alias(alias), pinned),
        )
        await conn.commit()
    lookup.invalidate_rank_cache()


def _names(result):
    return [r["canonical"] for r in result]


def test_frequent_mospolytech_in_top_five_for_politeh(tmp_path):
    _ready(tmp_path)
    before = _names(asyncio.run(search_lookup("university", "политех", limit=5)))
    assert MOSPOLY not in before  # без частоты — алфавит, Москва за пятёркой
    asyncio.run(_answers(MOSPOLY, 7))
    after = _names(asyncio.run(search_lookup("university", "политех", limit=5)))
    assert MOSPOLY in after


def test_frequency_counts_old_alias_answers_under_canonical(tmp_path):
    """Ответы старым названием («МАМИ») считаются за канонику — та же свёртка, что у чипов."""
    _ready(tmp_path)
    asyncio.run(_answers("МАМИ", 4))
    assert MOSPOLY in _names(asyncio.run(search_lookup("university", "политех", limit=5)))


def test_other_season_answers_do_not_lift(tmp_path):
    _ready(tmp_path)
    asyncio.run(_answers(MOSPOLY, 7, season="YL 26/1"))
    assert MOSPOLY not in _names(asyncio.run(search_lookup("university", "политех", limit=5)))


def test_pinned_above_frequent_within_tier(tmp_path):
    _ready(tmp_path)
    asyncio.run(_entry("Ааа тестовый политех", "Ааа тестовый политех", pinned=1))
    asyncio.run(_entry("Яяя тестовый политех", "Яяя тестовый политех", pinned=1))
    asyncio.run(_answers(MOSPOLY, 7))
    names = _names(asyncio.run(search_lookup("university", "тестовый политех", limit=5)))
    assert names[:2] == ["Ааа тестовый политех", "Яяя тестовый политех"]


def test_canonical_match_above_old_alias(tmp_path):
    """При равной частоте вуз, найденный по своему названию, выше найденного по старому
    псевдониму — даже если псевдоним короче."""
    _ready(tmp_path)
    asyncio.run(_entry("Зюзинский институт тестов", "Зюзинский институт тестов"))
    asyncio.run(_entry("Абвгд университет", "Зюзинский инст"))  # старое имя другого вуза
    names = _names(asyncio.run(search_lookup("university", "зюзинский", limit=5)))
    assert names.index("Зюзинский институт тестов") < names.index("Абвгд университет")


def test_exact_tier_still_first(tmp_path):
    """Точное совпадение («Политех» — псевдоним из снапшота) по-прежнему выше частого вуза из
    яруса подстроки."""
    _ready(tmp_path)
    asyncio.run(_answers(MOSPOLY, 20))
    result = asyncio.run(search_lookup("university", "Политех", limit=5))
    assert normalize_alias(result[0]["alias"]) == "политех"
    assert result[0]["canonical"] != MOSPOLY


def _first_prefix_canonicals(q, n):
    async def go():
        async with _connect() as conn:
            cursor = await conn.execute(
                "SELECT canonical FROM lookup_entries WHERE kind = 'university' AND alias_norm LIKE ? LIMIT ?",
                (normalize_alias(q) + "%", n),
            )
            return [row[0] for row in await cursor.fetchall()]
    return asyncio.run(go())


def test_short_query_lifts_frequent_beyond_scan_cap(tmp_path, monkeypatch):
    """Короткий запрос: под префикс строк больше потолка скана, частый вуз дальше по rowid —
    всё равно в пятёрке. Потолок уменьшен, чтобы снапшот справочника точно его перекрыл."""
    _ready(tmp_path)
    monkeypatch.setattr(lookup, "_TIER_SCAN_CAP", 20)
    assert MOSPOLY not in _first_prefix_canonicals("мо", 20)  # иначе тест ничего не проверяет
    asyncio.run(_answers(MOSPOLY, 7))
    assert MOSPOLY in _names(asyncio.run(search_lookup("university", "мо", limit=5)))


def test_short_query_lifts_pinned(tmp_path, monkeypatch):
    _ready(tmp_path)
    monkeypatch.setattr(lookup, "_TIER_SCAN_CAP", 20)

    async def pin():
        await lookup.pin_chip("university", MOSPOLY, True)
    asyncio.run(pin())
    assert _names(asyncio.run(search_lookup("university", "мо", limit=5)))[0] == MOSPOLY


def test_rank_inputs_cached_until_invalidated(tmp_path):
    """Частота кэшируется: новые ответы видны после TTL или сброса; закрепление сбрасывает сразу."""
    _ready(tmp_path)
    asyncio.run(search_lookup("university", "политех", limit=5))  # прогрев пустой частотой
    async def raw_answers():
        async with _connect() as conn:
            for i in range(7):
                await conn.execute(
                    "INSERT INTO users (telegram_id, university, season) VALUES (?, ?, ?)",
                    (5000 + i, MOSPOLY, SEASON),
                )
            await conn.execute(
                "INSERT OR REPLACE INTO bot_settings (key, value) VALUES ('event_season', ?)", (SEASON,)
            )
            await conn.commit()
    asyncio.run(raw_answers())
    assert MOSPOLY not in _names(asyncio.run(search_lookup("university", "политех", limit=5)))
    lookup.invalidate_rank_cache()
    assert MOSPOLY in _names(asyncio.run(search_lookup("university", "политех", limit=5)))
