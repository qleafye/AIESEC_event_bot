"""Phase 30 (30-02, A2-03) — сторож `services/lookup.py`: нормализация псевдонимов, ранжирование
поиска, идемпотентность посева, топ-8 чипов, очередь слияния.

pytest-asyncio в этом окружении не установлен (правило проекта) — каждый async-вызов идёт
через `asyncio.run()`. БД поднимается по образцу `_ready(tmp_path)`
(`tests/test_reg_engine_parity.py`/`tests/test_reg_resume_ttl_260820.py`): `config.DB_PATH`
в `tmp_path` + `asyncio.run(init_db())`.
"""
from __future__ import annotations

import asyncio

from config import config
from database.db import _connect, init_db
from services.lookup import (
    enqueue_merge,
    normalize_alias,
    pin_chip,
    pinned_chips,
    search_lookup,
    top_chips,
)


def _run(coro):
    return asyncio.run(coro)


def _ready(tmp_path, name="lookup_260912.db"):
    config.DB_PATH = str(tmp_path / name)
    _run(init_db())


async def _clear_lookup(kind):
    """Снапшоты `data/lookup/*.json` (задача 3) реальны и посеяны настоящим `init_db()` — без
    этой очистки тесты искали бы совпадения/коллизии со СЛУЧАЙНЫМИ именами реальных вузов/
    городов из снапшота (например «ИТМО»/«ЛЭТИ» есть в `config.UNIVERSITIES`-фолбэке).
    Каждый тест с СВОИМИ фикстурами `_insert_entry` очищает kind ПЕРЕД вставкой — детерминизм
    не должен зависеть от содержимого снапшота на диске."""
    async with _connect() as conn:
        await conn.execute("DELETE FROM lookup_entries WHERE kind = ?", (kind,))
        await conn.commit()


async def _insert_entry(kind, canonical, alias, *, source="test", pinned=0):
    async with _connect() as conn:
        await conn.execute(
            "INSERT OR IGNORE INTO lookup_entries "
            "(kind, canonical, alias, alias_norm, source, pinned, added_by, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, NULL, '2026-09-13 00:00:00')",
            (kind, canonical, alias, normalize_alias(alias), source, pinned),
        )
        await conn.commit()


async def _insert_user_answer(telegram_id, column, value, season=None):
    async with _connect() as conn:
        await conn.execute(
            "INSERT INTO users (telegram_id, full_name, registration_date) VALUES (?, 'Т', '2026-01-01 00:00:00')",
            (telegram_id,),
        )
        await conn.execute(f"UPDATE users SET {column} = ? WHERE telegram_id = ?", (value, telegram_id))
        if season is not None:
            await conn.execute("UPDATE users SET season = ? WHERE telegram_id = ?", (season, telegram_id))
        await conn.commit()


async def _set_setting(key, value):
    async with _connect() as conn:
        await conn.execute(
            "INSERT INTO bot_settings (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )
        await conn.commit()


# ── normalize_alias — чистая функция, без БД ────────────────────────────────────────────────

def test_normalize_casefold_makes_case_variants_equal():
    assert normalize_alias("СПбГАСУ") == normalize_alias("спбгасу")


def test_normalize_yo_to_e():
    assert normalize_alias("ёлка") == normalize_alias("елка")


def test_normalize_strips_parenthetical_tail():
    assert normalize_alias("СПбГУТ (бывш. ЛЭИС)") == normalize_alias("СПбГУТ")


def test_normalize_strips_im_prefix():
    normalized = normalize_alias("Университет имени Бонча-Бруевича")
    assert "имени" not in normalized


def test_normalize_strips_quotes_and_punctuation():
    assert normalize_alias('«СПбГЭТУ»,') == normalize_alias("СПбГЭТУ")


def test_normalize_collapses_internal_whitespace():
    assert normalize_alias("СПб   ГАСУ") == normalize_alias("СПб ГАСУ")


def test_normalize_none_and_blank_are_empty_string():
    assert normalize_alias(None) == ""
    assert normalize_alias("   ") == ""


# ── search_lookup — ранжирование и экранирование ────────────────────────────────────────────

def test_search_finds_university_by_old_abbreviation(tmp_path):
    _ready(tmp_path)
    _run(_clear_lookup("university"))
    _run(_insert_entry("university", "СПбГЭТУ", "СПбГЭТУ"))
    _run(_insert_entry("university", "СПбГЭТУ", "ЛЭТИ"))

    result = _run(search_lookup("university", "лэти"))
    assert any(r["canonical"] == "СПбГЭТУ" for r in result)


def test_search_ranks_exact_above_prefix_above_substring(tmp_path):
    _ready(tmp_path)
    _run(_clear_lookup("university"))
    _run(_insert_entry("university", "Точное совпадение", "вшэ"))
    _run(_insert_entry("university", "Только префикс", "вшэ-питер"))
    _run(_insert_entry("university", "Только подстрока", "невская вшэ школа"))

    result = _run(search_lookup("university", "вшэ", limit=10))
    canonicals = [r["canonical"] for r in result]
    assert canonicals.index("Точное совпадение") < canonicals.index("Только префикс")
    assert canonicals.index("Только префикс") < canonicals.index("Только подстрока")


def test_search_dedups_by_canonical(tmp_path):
    _ready(tmp_path)
    _run(_clear_lookup("university"))
    _run(_insert_entry("university", "ИТМО", "ИТМО"))
    _run(_insert_entry("university", "ИТМО", "итмо университет"))

    result = _run(search_lookup("university", "итмо"))
    assert len([r for r in result if r["canonical"] == "ИТМО"]) == 1


def test_search_q_percent_returns_not_all_and_not_more_than_limit(tmp_path):
    _ready(tmp_path)
    _run(_clear_lookup("university"))
    for i in range(20):
        _run(_insert_entry("university", f"ВУЗ {i}", f"вуз {i}"))

    result = _run(search_lookup("university", "%", limit=5))
    assert len(result) <= 5
    assert len(result) < 20


def test_search_underscore_and_quote_do_not_crash_or_match_everything(tmp_path):
    _ready(tmp_path)
    _run(_clear_lookup("university"))
    for i in range(10):
        _run(_insert_entry("university", f"ВУЗ {i}", f"вуз {i}"))

    result = _run(search_lookup("university", "_"))
    assert len(result) == 0

    result2 = _run(search_lookup("university", "'; DROP TABLE lookup_entries; --"))
    assert result2 == []

    # Таблица не пострадала от инъекции — параметризация выше отработала как надо.
    result3 = _run(search_lookup("university", "вуз 1"))
    assert len(result3) >= 1


def test_search_limit_respected(tmp_path):
    _ready(tmp_path)
    _run(_clear_lookup("university"))
    for i in range(10):
        _run(_insert_entry("university", f"Общий {i}", f"общий {i}"))

    result = _run(search_lookup("university", "общий", limit=3))
    assert len(result) == 3


def test_search_empty_query_returns_empty_list(tmp_path):
    _ready(tmp_path)
    _run(_clear_lookup("university"))
    _run(_insert_entry("university", "ИТМО", "ИТМО"))
    assert _run(search_lookup("university", "")) == []
    assert _run(search_lookup("university", "   ")) == []


def test_search_unknown_kind_returns_empty_when_no_entries(tmp_path):
    _ready(tmp_path)
    _run(_clear_lookup("university"))
    _run(_clear_lookup("city"))
    _run(_insert_entry("university", "ИТМО", "ИТМО"))
    assert _run(search_lookup("city", "итмо")) == []


# ── идемпотентный посев (задача 1, T-30-… — таблица не растёт на повторном init_db) ─────────

def test_repeated_init_db_does_not_grow_lookup_entries(tmp_path):
    _ready(tmp_path)
    _run(_clear_lookup("university"))
    _run(_insert_entry("university", "Тестовый ВУЗ вне снапшота", "тестовый вуз"))

    async def _count():
        async with _connect() as conn:
            cursor = await conn.execute(
                "SELECT COUNT(*) FROM lookup_entries WHERE kind = 'university'"
            )
            row = await cursor.fetchone()
            return row[0]

    before = _run(_count())
    _run(init_db())
    after = _run(_count())
    assert before == after == 1


# ── top_chips — закреплённые первыми, частота сезона следом ─────────────────────────────────

def test_top_chips_pinned_first_then_frequency(tmp_path):
    _ready(tmp_path)
    _run(_clear_lookup("university"))
    _run(_insert_entry("university", "Закреплённый", "закреплённый", pinned=1))
    _run(_insert_entry("university", "Частый", "частый"))
    _run(_set_setting("event_season", "YL 26/2"))
    _run(_insert_user_answer(1, "university", "Частый", season="YL 26/2"))
    _run(_insert_user_answer(2, "university", "Частый", season="YL 26/2"))

    chips = _run(top_chips("university", None, limit=8))
    assert chips[0] == "Закреплённый"
    assert "Частый" in chips


def test_top_chips_empty_season_only_pinned(tmp_path):
    _ready(tmp_path)
    _run(_clear_lookup("university"))
    _run(_insert_entry("university", "Закреплённый", "закреплённый", pinned=1))

    chips = _run(top_chips("university", None, limit=8))
    assert chips == ["Закреплённый"]


def test_top_chips_no_entries_returns_empty_list(tmp_path):
    _ready(tmp_path)
    _run(_clear_lookup("city"))
    assert _run(top_chips("city", None, limit=8)) == []


def test_top_chips_excludes_placeholders_and_folds_aliases(tmp_path):
    _ready(tmp_path)
    _run(_clear_lookup("university"))
    _run(_insert_entry("university", "НИУ ВШЭ", "НИУ ВШЭ"))
    _run(_insert_entry("university", "НИУ ВШЭ", "ВШЭ"))
    _run(_set_setting("event_season", "YL 26/2"))

    _run(_insert_user_answer(1, "university", "-", season="YL 26/2"))
    _run(_insert_user_answer(2, "university", "Пропустить", season="YL 26/2"))
    _run(_insert_user_answer(3, "university", "ВШЭ", season="YL 26/2"))
    _run(_insert_user_answer(4, "university", "ВШЭ", season="YL 26/2"))
    _run(_insert_user_answer(5, "university", "ВШЭ", season="YL 26/2"))
    _run(_insert_user_answer(6, "university", "НИУ ВШЭ", season="YL 26/2"))
    _run(_insert_user_answer(7, "university", "НИУ ВШЭ", season="YL 26/2"))
    _run(_insert_user_answer(8, "university", "МГУ", season="YL 26/2"))

    chips = _run(top_chips("university", None, limit=8))
    assert chips == ["НИУ ВШЭ", "МГУ"]
    assert "-" not in chips
    assert "Пропустить" not in chips
    assert "ВШЭ" not in chips


def test_top_chips_pinned_stays_first_after_folding(tmp_path):
    _ready(tmp_path)
    _run(_clear_lookup("university"))
    _run(_insert_entry("university", "Закреплённый", "закреплённый", pinned=1))
    _run(_insert_entry("university", "НИУ ВШЭ", "НИУ ВШЭ"))
    _run(_insert_entry("university", "НИУ ВШЭ", "ВШЭ"))
    _run(_set_setting("event_season", "YL 26/2"))

    _run(_insert_user_answer(1, "university", "ВШЭ", season="YL 26/2"))
    _run(_insert_user_answer(2, "university", "ВШЭ", season="YL 26/2"))
    _run(_insert_user_answer(3, "university", "НИУ ВШЭ", season="YL 26/2"))

    chips = _run(top_chips("university", None, limit=8))
    assert chips[0] == "Закреплённый"
    assert "НИУ ВШЭ" in chips
    assert "ВШЭ" not in chips


# ── enqueue_merge — очередь слияния «Другое» ────────────────────────────────────────────────

def test_enqueue_merge_writes_row(tmp_path):
    _ready(tmp_path)
    _run(enqueue_merge("university", "Мой странный ВУЗ", "university", 12345))

    async def _rows():
        async with _connect() as conn:
            cursor = await conn.execute("SELECT raw_text, status FROM lookup_merge_queue")
            return await cursor.fetchall()

    rows = _run(_rows())
    assert len(rows) == 1
    assert rows[0][0] == "Мой странный ВУЗ"
    assert rows[0][1] == "new"


def test_enqueue_merge_dedups_by_kind_raw_norm_status_new(tmp_path):
    _ready(tmp_path)
    _run(enqueue_merge("university", "Мой ВУЗ", "university", 1))
    _run(enqueue_merge("university", "мой вуз", "university", 2))

    async def _count():
        async with _connect() as conn:
            cursor = await conn.execute("SELECT COUNT(*) FROM lookup_merge_queue")
            row = await cursor.fetchone()
            return row[0]

    assert _run(_count()) == 1


def test_enqueue_merge_blank_text_is_noop(tmp_path):
    _ready(tmp_path)
    _run(enqueue_merge("university", "   ", "university", 1))

    async def _count():
        async with _connect() as conn:
            cursor = await conn.execute("SELECT COUNT(*) FROM lookup_merge_queue")
            row = await cursor.fetchone()
            return row[0]

    assert _run(_count()) == 0


# ── pin_chip / pinned_chips ──────────────────────────────────────────────────────────────────

def test_pin_chip_and_pinned_chips_roundtrip(tmp_path):
    _ready(tmp_path)
    _run(_clear_lookup("city"))
    _run(_insert_entry("city", "Москва", "москва"))
    _run(_insert_entry("city", "Санкт-Петербург", "спб"))

    assert _run(pinned_chips("city")) == []
    _run(pin_chip("city", "Москва", True))
    assert _run(pinned_chips("city")) == ["Москва"]
    _run(pin_chip("city", "Москва", False))
    assert _run(pinned_chips("city")) == []


def test_pin_chip_affects_all_aliases_of_canonical(tmp_path):
    _ready(tmp_path)
    _run(_clear_lookup("university"))
    _run(_insert_entry("university", "СПбГЭТУ", "СПбГЭТУ"))
    _run(_insert_entry("university", "СПбГЭТУ", "ЛЭТИ"))
    _run(pin_chip("university", "СПбГЭТУ", True))

    async def _pinned_count():
        async with _connect() as conn:
            cursor = await conn.execute(
                "SELECT COUNT(*) FROM lookup_entries WHERE canonical = ? AND pinned = 1",
                ("СПбГЭТУ",),
            )
            row = await cursor.fetchone()
            return row[0]

    assert _run(_pinned_count()) == 2
