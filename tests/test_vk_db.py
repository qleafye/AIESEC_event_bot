"""Карточка ВК: телеграма нет, чужое поле не пишется, дефолт «одобрена» не срабатывает."""
import asyncio

from config import config
from database import db


def _use_tmp_db(tmp_path):
    config.DB_PATH = str(tmp_path / "vk_users.db")


def test_create_and_get_vk_user(tmp_path):
    _use_tmp_db(tmp_path)
    asyncio.run(db.init_db())

    async def _go():
        await db.create_vk_user(55, {"full_name": "Иван Иванов"})
        return await db.get_vk_user(55)

    row = asyncio.run(_go())
    assert row["vk_id"] == 55
    assert row["full_name"] == "Иван Иванов"
    assert row["telegram_id"] is None


def test_vk_user_is_not_approved_by_default(tmp_path):
    _use_tmp_db(tmp_path)
    asyncio.run(db.init_db())

    async def _go():
        await db.create_vk_user(56, {})
        return await db.get_vk_user(56)

    row = asyncio.run(_go())
    assert row["status"] is None
    assert row["telegram_id"] is None


def test_update_vk_user(tmp_path):
    _use_tmp_db(tmp_path)
    asyncio.run(db.init_db())

    async def _go():
        await db.create_vk_user(57, {"full_name": "Было Имя"})
        await db.update_vk_user(57, {"full_name": "Стало Имя", "age": 21})
        return await db.get_vk_user(57)

    row = asyncio.run(_go())
    assert row["full_name"] == "Стало Имя"
    assert row["age"] == 21


def test_get_nonexistent_vk_user(tmp_path):
    _use_tmp_db(tmp_path)
    asyncio.run(db.init_db())
    assert asyncio.run(db.get_vk_user(999)) is None


def test_unknown_column_is_rejected(tmp_path):
    _use_tmp_db(tmp_path)
    asyncio.run(db.init_db())

    async def _go():
        await db.create_vk_user(58, {"telegram_id": 1})

    try:
        asyncio.run(_go())
    except ValueError as exc:
        assert "не пишется" in str(exc)
    else:
        raise AssertionError("telegram_id must not be writable")
    assert asyncio.run(db.get_vk_user(58)) is None
