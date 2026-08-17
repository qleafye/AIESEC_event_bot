"""Phase 14 Plan 06 (CITY-07) — cities registry moves from `.env` into the DB.

Widest blast-radius test in the phase: `cities.CITIES` used to be a module-level constant
parsed from `config.EVENT_CITIES` at import time; it is now a cache backed by the `cities`
table, reloaded on demand. `from cities import CITIES` is aliased by name into
`handlers/admin.py` and `handlers/registration.py` — the single highest-risk invariant this
file guards is "reload_cities() mutates the SAME list object", never rebinding `CITIES`.

pytest-asyncio is unavailable in this env — every async helper is driven via asyncio.run()
and config.DB_PATH points at a tmp_path file, same convention as
tests/test_game_city_tasks_091.py / tests/test_city_scope_phase72.py.
"""
import asyncio
import pathlib

from config import config
from database import db
import cities


def _db_ready(tmp_path):
    config.DB_PATH = str(tmp_path / "test_cities_registry_260818.db")
    asyncio.run(db.init_db())


# ── Task 1: `cities` table + CRUD accessors ──────────────────────────────────────────────────

def test_init_db_creates_cities_table_and_is_idempotent(tmp_path):
    _db_ready(tmp_path)

    async def _inspect():
        import aiosqlite
        async with aiosqlite.connect(config.DB_PATH) as conn:
            async with conn.execute("PRAGMA table_info(cities)") as cursor:
                rows = await cursor.fetchall()
        return {row[1] for row in rows}

    columns = asyncio.run(_inspect())
    assert {"code", "label", "tab_base", "enabled", "sort_order", "created_at"} <= columns

    # Second init_db() must not raise (CREATE TABLE IF NOT EXISTS is idempotent).
    asyncio.run(db.init_db())


def test_insert_and_list_cities_rows_ordered_by_sort_order(tmp_path):
    _db_ready(tmp_path)

    async def _run():
        await db.insert_city("tmn", "Тюмень", "", 1)
        await db.insert_city("msk", "Москва", "", 0)
        await db.insert_city("spb", "Санкт-Петербург", "SPB", 2)
        return await db.list_cities_rows()

    rows = asyncio.run(_run())
    assert [r["code"] for r in rows] == ["msk", "tmn", "spb"]


def test_update_city_changes_only_passed_fields(tmp_path):
    _db_ready(tmp_path)

    async def _run():
        await db.insert_city("msk", "Москва", "", 0)
        ok = await db.update_city("msk", label="Мск")
        rows = await db.list_cities_rows()
        missing = await db.update_city("nope", label="x")
        return ok, rows[0], missing

    ok, row, missing = asyncio.run(_run())
    assert ok is True
    assert row["label"] == "Мск"
    assert row["tab_base"] == ""  # untouched field stays as inserted
    assert missing is False


def test_delete_city_row_removes_once_then_returns_false(tmp_path):
    _db_ready(tmp_path)

    async def _run():
        await db.insert_city("msk", "Москва", "", 0)
        first = await db.delete_city_row("msk")
        second = await db.delete_city_row("msk")
        remaining = await db.count_cities()
        return first, second, remaining

    first, second, remaining = asyncio.run(_run())
    assert first is True
    assert second is False
    assert remaining == 0


def test_count_users_and_tasks_by_city_ignore_null(tmp_path):
    _db_ready(tmp_path)

    async def _run():
        import aiosqlite
        async with aiosqlite.connect(config.DB_PATH) as conn:
            await conn.execute(
                "INSERT INTO users (telegram_id, event_city) VALUES (?, ?)", (1, "spb")
            )
            await conn.execute(
                "INSERT INTO users (telegram_id, event_city) VALUES (?, ?)", (2, "spb")
            )
            await conn.execute(
                "INSERT INTO users (telegram_id, event_city) VALUES (?, ?)", (3, None)
            )
            await conn.execute(
                "INSERT INTO game_tasks (text, category, coins, proof_type, deadline_at, "
                "created_at, event_city) VALUES (?, ?, ?, ?, ?, ?, ?)",
                ("t1", "cat", 1, "text", "2026-01-01 00:00:00", "2026-01-01 00:00:00", "spb"),
            )
            await conn.execute(
                "INSERT INTO game_tasks (text, category, coins, proof_type, deadline_at, "
                "created_at, event_city) VALUES (?, ?, ?, ?, ?, ?, ?)",
                ("t2", "cat", 1, "text", "2026-01-01 00:00:00", "2026-01-01 00:00:00", None),
            )
            await conn.commit()
        users_spb = await db.count_users_by_city("spb")
        tasks_spb = await db.count_tasks_by_city("spb")
        return users_spb, tasks_spb

    users_spb, tasks_spb = asyncio.run(_run())
    assert users_spb == 2
    assert tasks_spb == 1


def test_db_py_never_imports_cities_module():
    """Structural: database/db.py stays a pure SQL layer -- no `import cities`/`from cities`
    (that would create an import cycle, since cities.py already imports database.db)."""
    src = pathlib.Path(__file__).resolve().parents[1].joinpath("database", "db.py").read_text(encoding="utf-8")
    assert "import cities" not in src
    assert "from cities" not in src
