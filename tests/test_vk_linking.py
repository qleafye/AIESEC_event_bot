"""Одноразовый секрет склейки: 15 минут, второй раз пусто, протухший не открывает карточку."""
import asyncio
from datetime import datetime, timedelta

from config import config
from database import db


def _use_tmp_db(tmp_path):
    config.DB_PATH = str(tmp_path / "vk_link.db")


def test_create_link_secret(tmp_path):
    _use_tmp_db(tmp_path)
    asyncio.run(db.init_db())

    async def _go():
        secret = await db.create_link_secret(456)
        return secret, await db.consume_link_secret(secret)

    secret, telegram_id = asyncio.run(_go())
    assert telegram_id == 456
    assert "456" not in secret
    assert len(secret) >= 32


def test_secret_expires(tmp_path, monkeypatch):
    _use_tmp_db(tmp_path)
    asyncio.run(db.init_db())
    clock = {"now": datetime(2026, 11, 12, 12, 0, 0)}
    monkeypatch.setattr(db, "msk_now", lambda: clock["now"])

    async def _go():
        secret = await db.create_link_secret(456)
        clock["now"] = clock["now"] + timedelta(minutes=15, seconds=1)
        return await db.consume_link_secret(secret)

    assert asyncio.run(_go()) is None


def test_secret_one_time(tmp_path):
    _use_tmp_db(tmp_path)
    asyncio.run(db.init_db())

    async def _go():
        secret = await db.create_link_secret(456)
        first = await db.consume_link_secret(secret)
        second = await db.consume_link_secret(secret)
        return first, second

    assert asyncio.run(_go()) == (456, None)


def test_invalid_secret(tmp_path):
    _use_tmp_db(tmp_path)
    asyncio.run(db.init_db())
    assert asyncio.run(db.consume_link_secret("нет-такого")) is None
    assert asyncio.run(db.consume_link_secret("")) is None


def test_pending_links_are_purged_with_the_delegate():
    assert ("pending_links", "telegram_id", "vk_link") in db.USER_PURGE_TABLES
