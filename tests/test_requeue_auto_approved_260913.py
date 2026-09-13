"""Тесты инструмента возврата автоодобренных заявок в очередь модерации (инцидент 06.09.2026).

Стиль — по образцу tests/test_admin_rereg_260817.py: временная БД через `config.DB_PATH`,
async-логика внутри `asyncio.run(go())` (pytest-asyncio в этом окружении недоступен).
"""
import asyncio

import aiosqlite

from config import config
from database import db
import tools.requeue_auto_approved as tool

SEASON = "YL 26/2"
WINDOW_FROM = "2026-09-06 08:00:00"
WINDOW_TO = "2026-09-06 13:15:00"


def _use_tmp_db(tmp_path):
    config.DB_PATH = str(tmp_path / "test_forum.db")


async def _insert_user(**kwargs) -> None:
    defaults = {
        "telegram_id": None,
        "username": "u",
        "full_name": "Тестовый Тестов",
        "status": "approved",
        "season": SEASON,
        "approved_at": None,
        "registration_date": "2026-09-06 09:00:00",
    }
    defaults.update(kwargs)
    async with aiosqlite.connect(config.DB_PATH) as conn:
        await conn.execute(
            "INSERT INTO users (telegram_id, username, full_name, status, season, "
            "approved_at, registration_date) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                defaults["telegram_id"],
                defaults["username"],
                defaults["full_name"],
                defaults["status"],
                defaults["season"],
                defaults["approved_at"],
                defaults["registration_date"],
            ),
        )
        await conn.commit()


async def _insert_decision(telegram_id: int) -> None:
    async with aiosqlite.connect(config.DB_PATH) as conn:
        await conn.execute(
            "INSERT INTO application_decisions "
            "(telegram_id, decision, decided_by, decided_at, effects_due_at) "
            "VALUES (?, 'approve', 1, '2026-09-06 09:10:00', '2026-09-06 09:20:00')",
            (telegram_id,),
        )
        await conn.commit()


async def _seed(tmp_path) -> None:
    """3 подходящие + 3 неподходящие-ловушки, как описано в behavior плана."""
    await db.init_db()

    # Подходящие: approved, approved_at IS NULL, нет решения, дата внутри окна.
    await _insert_user(telegram_id=1001, username="ok1", registration_date="2026-09-06 08:30:00")
    await _insert_user(telegram_id=1002, username="ok2", registration_date="2026-09-06 10:00:00")
    await _insert_user(telegram_id=1003, username="ok3", registration_date="2026-09-06 13:00:00")

    # Ловушка (а): approved_at заполнен — обычное ручное одобрение, не трогаем.
    await _insert_user(
        telegram_id=2001, username="trap_a",
        approved_at="2026-09-06 09:30:00", registration_date="2026-09-06 09:30:00",
    )

    # Ловушка (б): есть строка в application_decisions — модератор уже видел заявку.
    await _insert_user(telegram_id=2002, username="trap_b", registration_date="2026-09-06 09:40:00")
    await _insert_decision(2002)

    # Ловушка (в): дата вне окна выборки.
    await _insert_user(telegram_id=2003, username="trap_c", registration_date="2026-09-06 20:00:00")


async def _status_of(telegram_id: int) -> str | None:
    async with aiosqlite.connect(config.DB_PATH) as conn:
        async with conn.execute(
            "SELECT status FROM users WHERE telegram_id = ?", (telegram_id,)
        ) as cursor:
            row = await cursor.fetchone()
    return row[0] if row else None


def test_select_rows_returns_exactly_three_expected(tmp_path):
    _use_tmp_db(tmp_path)

    async def go():
        await _seed(tmp_path)
        rows = await tool.select_rows(SEASON, WINDOW_FROM, WINDOW_TO)
        ids = {r["telegram_id"] for r in rows}
        assert ids == {1001, 1002, 1003}

    asyncio.run(go())


def test_dry_run_changes_nothing(tmp_path, monkeypatch):
    _use_tmp_db(tmp_path)
    calls = []

    async def fake_bulk(id_to_label):
        calls.append(id_to_label)
        return 1

    async def go():
        await _seed(tmp_path)

        # Патчим имя внутри services.sheets — main() импортирует его локально при вызове.
        import services.sheets as sheets_mod
        monkeypatch.setattr(sheets_mod, "bulk_update_status_in_sheet", fake_bulk)

        rc = await tool.main(SEASON, WINDOW_FROM, WINDOW_TO, False, None)
        assert rc == 0

        for tid in (1001, 1002, 1003, 2001, 2002, 2003):
            assert await _status_of(tid) == "approved"

        assert not (tmp_path / "backups").exists()
        assert calls == []

    asyncio.run(go())


def test_apply_changes_exactly_three_and_backs_up(tmp_path, monkeypatch):
    _use_tmp_db(tmp_path)

    async def fake_bulk(id_to_label):
        return 1

    async def go():
        await _seed(tmp_path)

        import services.sheets as sheets_mod
        monkeypatch.setattr(sheets_mod, "bulk_update_status_in_sheet", fake_bulk)

        rc = await tool.main(SEASON, WINDOW_FROM, WINDOW_TO, True, None)
        assert rc == 0

        assert await _status_of(1001) == "pending"
        assert await _status_of(1002) == "pending"
        assert await _status_of(1003) == "pending"

        # Ловушки не тронуты.
        assert await _status_of(2001) == "approved"
        assert await _status_of(2002) == "approved"
        assert await _status_of(2003) == "approved"

        backups_dir = tmp_path / "backups"
        assert backups_dir.exists()
        backup_files = list(backups_dir.glob("forum-*-requeue.db"))
        assert len(backup_files) == 1

    asyncio.run(go())


def test_apply_calls_sheets_once_with_expected_ids_and_sheets_failure_does_not_rollback(tmp_path, monkeypatch):
    _use_tmp_db(tmp_path)
    calls = []

    async def failing_bulk(id_to_label):
        calls.append(id_to_label)
        raise RuntimeError("Sheets недоступен")

    async def go():
        await _seed(tmp_path)

        import services.sheets as sheets_mod
        monkeypatch.setattr(sheets_mod, "bulk_update_status_in_sheet", failing_bulk)

        rc = await tool.main(SEASON, WINDOW_FROM, WINDOW_TO, True, None)
        # Скрипт не падает несмотря на исключение Sheets (fail-soft).
        assert rc == 0

        assert len(calls) == 1
        assert calls[0] == {"1001": "Новая", "1002": "Новая", "1003": "Новая"}

        # БД уже возвращена в очередь — ошибка листа НЕ откатывает статусы.
        assert await _status_of(1001) == "pending"
        assert await _status_of(1002) == "pending"
        assert await _status_of(1003) == "pending"

    asyncio.run(go())
