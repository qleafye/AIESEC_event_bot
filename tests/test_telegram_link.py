"""/link_vk даёт ссылку vk.me. Уже связанный и неготовый модуль ссылку не получают."""
import asyncio
from types import SimpleNamespace

from config import config
from database import db
from handlers.vk.telegram_link import link_vk_command


def _use_tmp_db(tmp_path):
    config.DB_PATH = str(tmp_path / "tg_link.db")


def _message(user_id, chat_type="private"):
    sent = []

    async def _answer(text, **_kwargs):
        sent.append(text)

    message = SimpleNamespace(
        from_user=SimpleNamespace(id=user_id),
        chat=SimpleNamespace(type=chat_type),
        answer=_answer,
    )
    return message, sent


def test_link_vk_command(tmp_path):
    _use_tmp_db(tmp_path)
    asyncio.run(db.init_db())

    async def _go():
        await db.set_setting("vk_enabled", "on")
        await db.set_setting("vk_community_id", "-555")
        async with db._connect() as conn:
            await conn.execute(
                "INSERT INTO users (telegram_id, full_name, status) VALUES (?, ?, ?)",
                (7, "Иван Иванов", "pending"),
            )
            await conn.commit()
        message, sent = _message(7)
        await link_vk_command(message)
        return sent

    sent = asyncio.run(_go())
    assert len(sent) == 1
    assert "https://vk.me/club555?ref=link_" in sent[0]
    assert "15 минут" in sent[0]
    assert "-555" not in sent[0]


def test_link_vk_already_linked(tmp_path):
    _use_tmp_db(tmp_path)
    asyncio.run(db.init_db())

    async def _go():
        async with db._connect() as conn:
            await conn.execute(
                "INSERT INTO users (telegram_id, vk_id, status) VALUES (?, ?, ?)",
                (7, 99, "approved"),
            )
            await conn.commit()
        message, sent = _message(7)
        await link_vk_command(message)
        return sent

    sent = asyncio.run(_go())
    assert "уже связан" in sent[0]
    assert "vk.me" not in sent[0]


def test_link_vk_no_registration(tmp_path):
    _use_tmp_db(tmp_path)
    asyncio.run(db.init_db())
    message, sent = _message(7)
    asyncio.run(link_vk_command(message))
    assert "регистрац" in sent[0]
    assert "vk.me" not in sent[0]


def test_link_vk_module_disabled(tmp_path):
    _use_tmp_db(tmp_path)
    asyncio.run(db.init_db())

    async def _go():
        async with db._connect() as conn:
            await conn.execute(
                "INSERT INTO users (telegram_id, status) VALUES (?, ?)",
                (7, "pending"),
            )
            await conn.commit()
        message, sent = _message(7)
        await link_vk_command(message)
        return sent

    sent = asyncio.run(_go())
    assert "не включено" in sent[0]
    assert "vk.me" not in sent[0]


def test_link_vk_stays_out_of_the_group(tmp_path):
    _use_tmp_db(tmp_path)
    asyncio.run(db.init_db())
    message, sent = _message(7, "supergroup")
    asyncio.run(link_vk_command(message))
    assert "личные сообщения" in sent[0]
    assert "vk.me" not in sent[0]


def test_router_is_before_registration():
    text = open("main.py", encoding="utf-8").read()
    link = text.index("dp.include_router(telegram_link.router)")
    registration = text.index("dp.include_router(registration.router)")
    assert link < registration
