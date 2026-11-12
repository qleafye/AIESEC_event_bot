"""Лента одна: новая карточка получает свой мессенджер, склейка и повторная подача его не меняют."""
import asyncio

from config import config
from database import db


def _use_tmp_db(tmp_path):
    config.DB_PATH = str(tmp_path / "active_messenger.db")


def test_telegram_user_default(tmp_path):
    _use_tmp_db(tmp_path)
    asyncio.run(db.init_db())

    async def _go():
        await db.add_user({"telegram_id": 100, "registration_date": "2026-11-12 12:00:00"})
        return await db.get_user(100)

    row = asyncio.run(_go())
    assert row["active_messenger"] == "telegram"


def test_vk_user_default(tmp_path):
    _use_tmp_db(tmp_path)
    asyncio.run(db.init_db())

    async def _go():
        await db.create_vk_user(55, {"full_name": "Иван Иванов"})
        return await db.get_vk_user(55)

    row = asyncio.run(_go())
    assert row["active_messenger"] == "vk"
    assert row["telegram_id"] is None


def test_link_preserves_messenger(tmp_path):
    _use_tmp_db(tmp_path)
    asyncio.run(db.init_db())

    async def _go():
        await db.add_user({
            "telegram_id": 100,
            "registration_date": "2026-11-12 12:00:00",
            "full_name": "Иван Иванов",
            "vk_username": "@nick",
        })
        await db.set_active_messenger(100, "vk")
        before = await db.get_user(100)
        await db.link_vk_account(100, 7)
        after = await db.get_user(100)
        return before, after

    before, after = asyncio.run(_go())
    assert before["active_messenger"] == "vk"
    assert after["active_messenger"] == "vk"
    assert after["vk_id"] == 7
    before["vk_id"] = 7
    assert after == before


def test_reregistration_keeps_messenger(tmp_path):
    _use_tmp_db(tmp_path)
    asyncio.run(db.init_db())

    async def _go():
        payload = {"telegram_id": 100, "registration_date": "2026-11-12 12:00:00", "full_name": "Было"}
        await db.add_user(payload)
        await db.set_active_messenger(100, "vk")
        payload["full_name"] = "Стало"
        payload["active_messenger"] = "telegram"
        await db.add_user(payload)
        return await db.get_user(100)

    row = asyncio.run(_go())
    assert row["full_name"] == "Стало"
    assert row["active_messenger"] == "vk"
