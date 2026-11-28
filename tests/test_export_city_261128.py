"""Команда /export отдаёт тот же город, что кнопка выгрузки, и не обещает всю базу."""
import asyncio
import csv
import io

from config import config
from database import db
from handlers.comms import admin_broadcasts
import domain.cities as cities
from tests._dbtpl import fast_init_db


ADMIN_ID = 26112804


def _init(tmp_path):
    config.DB_PATH = str(tmp_path / "export_city.db")
    fast_init_db()
    config.ADMIN_IDS = [ADMIN_ID]


def _seed(telegram_id, event_city):
    asyncio.run(db.add_user({
        "telegram_id": telegram_id,
        "full_name": f"User {telegram_id}",
        "registration_date": "2026-11-28 12:00:00",
        "event_city": event_city,
    }))


class FakeUser:
    def __init__(self, uid):
        self.id = uid


class FakeMessage:
    def __init__(self, uid):
        self.from_user = FakeUser(uid)
        self.documents = []

    async def answer_document(self, document, caption=None):
        self.documents.append((document, caption))


def _ids(document):
    text = document.data.decode("utf-8-sig")
    body = list(csv.reader(io.StringIO(text), delimiter=";"))[1:]
    return {row[0] for row in body}


def test_export_command_one_city_does_not_promise_the_whole_base(tmp_path):
    _init(tmp_path)
    asyncio.run(db.set_setting("event_city_enabled", "on"))
    asyncio.run(db.set_setting("city_label__spb", "СПб <город>"))
    _seed(1, "msk")
    _seed(2, "spb")
    _seed(3, "spb")
    asyncio.run(cities.set_admin_city(ADMIN_ID, "spb"))
    message = FakeMessage(ADMIN_ID)

    asyncio.run(admin_broadcasts.cmd_export(message))

    document, caption = message.documents[0]
    assert document.filename == "users_spb.csv"
    assert _ids(document) == {"2", "3"}
    assert caption == "База данных пользователей — СПб &lt;город&gt;"


def test_export_command_all_cities_keeps_everyone_and_names_the_mode(tmp_path):
    _init(tmp_path)
    asyncio.run(db.set_setting("event_city_enabled", "on"))
    _seed(1, "msk")
    _seed(2, "spb")
    asyncio.run(cities.set_admin_city(ADMIN_ID, cities.ALL_CITIES))
    message = FakeMessage(ADMIN_ID)

    asyncio.run(admin_broadcasts.cmd_export(message))

    document, caption = message.documents[0]
    assert document.filename == "users.csv"
    assert _ids(document) == {"1", "2"}
    assert caption == "База данных пользователей — 🌍 Все города"


def test_export_command_module_off_is_the_whole_base(tmp_path):
    _init(tmp_path)
    _seed(1, "msk")
    _seed(2, "spb")
    message = FakeMessage(ADMIN_ID)

    asyncio.run(admin_broadcasts.cmd_export(message))

    document, caption = message.documents[0]
    assert document.filename == "users.csv"
    assert _ids(document) == {"1", "2"}
    assert caption == "База данных пользователей"
