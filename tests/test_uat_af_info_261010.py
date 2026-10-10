"""Приёмка 10.10 (s2.menu.1): «ℹ️ Информация о форуме» отвечала «пока заполняется», хотя дата
форума задана, — экран требовал текстовую дату И место сразу. Теперь он показывает всё, что
известно, а «пока заполняется» — только когда не известно ничего.

pytest-asyncio в окружении нет — async через `asyncio.run()`, БД — `tests/_dbtpl.fast_init_db`.
"""
import asyncio

from aiogram.types import User

from config import config
from database import db
from handlers import user_actions
from tests._dbtpl import fast_init_db

UID = 900261041


def _ready(tmp_path, name="uat_info.db"):
    config.DB_PATH = str(tmp_path / name)
    fast_init_db()
    asyncio.run(db.add_user({
        "telegram_id": UID, "full_name": "Делегат", "registration_date": "2026-10-01 00:00:00",
    }))
    asyncio.run(db.set_user_status(UID, "approved"))


class _Msg:
    def __init__(self):
        self.from_user = User(id=UID, is_bot=False, first_name="Делегат")
        self.text = "ℹ️ Информация о форуме"
        self.sent = []

    async def answer(self, text, **kwargs):
        self.sent.append(text)


def _info():
    msg = _Msg()
    asyncio.run(user_actions.show_info_menu(msg))
    return msg.sent[-1]


def test_forum_start_date_shown_when_text_date_empty(tmp_path):
    _ready(tmp_path)
    asyncio.run(db.set_setting("forum_date", "30.10.2026"))
    text = _info()
    assert "пока заполняется" not in text
    assert "30.10.2026" in text


def test_partial_info_shown_without_place(tmp_path):
    _ready(tmp_path, "uat_info_partial.db")
    asyncio.run(db.set_setting("event_date", "30–31 октября"))
    text = _info()
    assert "30–31 октября" in text and "пока заполняется" not in text
    assert "Место" not in text  # чего нет — того и не показываем


def test_place_only(tmp_path):
    _ready(tmp_path, "uat_info_place.db")
    asyncio.run(db.set_setting("event_place_name", "Технопарк"))
    text = _info()
    assert "Технопарк" in text and "пока заполняется" not in text


def test_nothing_known_still_says_filling(tmp_path):
    _ready(tmp_path, "uat_info_empty.db")
    assert "пока заполняется" in _info()
