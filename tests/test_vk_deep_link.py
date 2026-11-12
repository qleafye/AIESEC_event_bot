"""Ссылка vk.me пишет номер ВК на карточку Telegram и не открывает вторую анкету."""
import asyncio
import logging
from datetime import timedelta

from config import config
from database import db
from handlers.vk.vk_bot import handle_vk_message
from services.vk.vk_fsm import get_vk_fsm_state


def _use_tmp_db(tmp_path):
    config.DB_PATH = str(tmp_path / "vk_deep.db")


def _capture(monkeypatch):
    sent = []

    async def _send(vk_id, text, keyboard_json=None):
        sent.append((vk_id, text, keyboard_json))

    async def _start(_vk_id):
        raise AssertionError("вторая анкета")

    monkeypatch.setattr("handlers.vk.vk_bot.send_vk_message", _send)
    monkeypatch.setattr("handlers.vk.vk_registration.send_vk_message", _send)
    monkeypatch.setattr("handlers.vk.vk_registration.start_vk_registration", _start)
    return sent


async def _insert(**columns):
    keys = ", ".join(columns)
    marks = ", ".join("?" for _ in columns)
    async with db._connect() as conn:
        await conn.execute(
            f"INSERT INTO users ({keys}) VALUES ({marks})",
            tuple(columns.values()),
        )
        await conn.commit()


def test_deep_link_success(monkeypatch, tmp_path, caplog):
    _use_tmp_db(tmp_path)
    asyncio.run(db.init_db())
    sent = _capture(monkeypatch)

    async def _go():
        await _insert(telegram_id=100, full_name="Иван Иванов", status="pending", vk_username="@nick")
        before = await db.get_user(100)
        secret = await db.create_link_secret(100)
        with caplog.at_level(logging.INFO):
            await handle_vk_message(7, "/start", ref=f"link_{secret}")
        after = await db.get_user(100)
        return before, after, secret, await get_vk_fsm_state(7)

    before, after, secret, state = asyncio.run(_go())
    assert after["vk_id"] == 7
    before["vk_id"] = 7
    assert after == before
    assert state is None
    assert "связаны" in sent[-1][1]
    assert "уже есть" in sent[-1][1]
    assert secret not in caplog.text
    assert "@nick" not in caplog.text


def test_deep_link_expired(monkeypatch, tmp_path):
    _use_tmp_db(tmp_path)
    asyncio.run(db.init_db())
    sent = _capture(monkeypatch)
    clock = {"now": __import__("datetime").datetime(2026, 11, 12, 12, 0, 0)}
    monkeypatch.setattr(db, "msk_now", lambda: clock["now"])

    async def _go():
        await _insert(telegram_id=100, status="pending")
        secret = await db.create_link_secret(100)
        clock["now"] = clock["now"] + timedelta(minutes=15, seconds=1)
        await handle_vk_message(7, "", ref=f"link_{secret}")
        return await db.get_user(100)

    row = asyncio.run(_go())
    assert row["vk_id"] is None
    assert "истекла" in sent[-1][1]


def test_deep_link_existing_form(monkeypatch, tmp_path):
    _use_tmp_db(tmp_path)
    asyncio.run(db.init_db())
    sent = _capture(monkeypatch)

    async def _go():
        await _insert(telegram_id=100, status="pending", full_name="Из Telegram")
        await _insert(vk_id=7, telegram_id=None, status="pending", full_name="Из ВК")
        secret = await db.create_link_secret(100)
        await handle_vk_message(7, "", ref=f"link_{secret}")
        return await db.get_user(100), await db.get_vk_user(7)

    tg, vk = asyncio.run(_go())
    assert tg["vk_id"] is None
    assert vk["telegram_id"] is None
    assert vk["full_name"] == "Из ВК"
    assert "уже есть анкета" in sent[-1][1]
    assert "не склеиваются" in sent[-1][1]


def test_deep_link_duplicate(monkeypatch, tmp_path):
    _use_tmp_db(tmp_path)
    asyncio.run(db.init_db())
    sent = _capture(monkeypatch)

    async def _go():
        await _insert(telegram_id=100, vk_id=99, status="approved")
        secret = await db.create_link_secret(100)
        await handle_vk_message(7, "", ref=f"link_{secret}")
        return await db.get_user(100), await db.get_vk_user(7)

    tg, vk = asyncio.run(_go())
    assert tg["vk_id"] == 99
    assert vk is None
    assert "уже связан" in sent[-1][1]


def test_typed_secret_is_not_a_link(monkeypatch, tmp_path):
    _use_tmp_db(tmp_path)
    asyncio.run(db.init_db())
    sent = _capture(monkeypatch)

    async def _go():
        await _insert(telegram_id=100, status="pending")
        secret = await db.create_link_secret(100)
        await handle_vk_message(7, f"link_{secret}")
        return await db.get_user(100), await db.consume_link_secret(secret)

    row, leftover = asyncio.run(_go())
    assert row["vk_id"] is None
    assert leftover == 100
    assert "Напишите /start" in sent[-1][1]


def test_open_anketa_does_not_start_a_second_one(monkeypatch, tmp_path):
    _use_tmp_db(tmp_path)
    asyncio.run(db.init_db())
    sent = _capture(monkeypatch)

    async def _go():
        await _insert(telegram_id=100, status=None, full_name="Черновик")
        secret = await db.create_link_secret(100)
        await handle_vk_message(7, "", ref=f"link_{secret}")
        return await db.get_user(100)

    row = asyncio.run(_go())
    assert row["vk_id"] == 7
    assert row["full_name"] == "Черновик"
    assert row["status"] is None
    assert "Допишите" in sent[-1][1]
