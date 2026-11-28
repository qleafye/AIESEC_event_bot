"""/coins не меняет баланс чужого города. Журнал приложения показывает только свой город."""
import asyncio

from config import config
from database import db
from domain.cities import ALL_CITIES, set_admin_city
from handlers import admin as admin_mod
from services.applications.personal_letter import OTHER_CITY
from tests._dbtpl import fast_init_db
from tests.test_miniapp_coins_admin import (
    BOUND_GAME_MANAGER,
    DELEGATE_ID,
    GAME_MANAGER_ID,
    SPB_DELEGATE,
    _give,
    _hdr,
    _set,
    _setup,
)


ADMIN_ID = 26112805
SPB_ID = 26112806
MSK_ID = 26112807


def _init(tmp_path):
    config.DB_PATH = str(tmp_path / "coins_city.db")
    fast_init_db()
    config.ADMIN_IDS = [ADMIN_ID]


def _user(telegram_id, username, event_city):
    asyncio.run(db.add_user({
        "telegram_id": telegram_id,
        "username": username,
        "full_name": username,
        "registration_date": "2026-11-28 12:00:00",
        "event_city": event_city,
    }))


class FakeUser:
    def __init__(self, uid):
        self.id = uid


class FakeBot:
    def __init__(self):
        self.sent = []

    async def send_message(self, chat_id, text, parse_mode=None, reply_markup=None):
        self.sent.append((chat_id, text))


class FakeMessage:
    def __init__(self, text, user_id=ADMIN_ID):
        self.text = text
        self.from_user = FakeUser(user_id)
        self.bot = FakeBot()
        self.answers_sent = []

    async def answer(self, text, parse_mode=None, reply_markup=None):
        self.answers_sent.append(text)


def _balance(telegram_id):
    return asyncio.run(db.get_balance(telegram_id))


def test_coins_command_refuses_other_city_with_the_letter_words(tmp_path):
    _init(tmp_path)
    asyncio.run(db.set_setting("event_city_enabled", "on"))
    _user(SPB_ID, "@spb_person", "spb")
    _user(MSK_ID, "@msk_person", "msk")
    asyncio.run(set_admin_city(ADMIN_ID, "spb"))

    foreign = FakeMessage("/coins @msk_person +5 за стенд")
    asyncio.run(admin_mod.cmd_coins(foreign, foreign.bot))

    assert foreign.answers_sent == [OTHER_CITY]
    assert "другого города" in OTHER_CITY
    assert _balance(MSK_ID) == 0
    assert foreign.bot.sent == []

    own = FakeMessage("/coins @spb_person +5 за стенд")
    asyncio.run(admin_mod.cmd_coins(own, own.bot))
    assert _balance(SPB_ID) == 5


def test_coins_command_all_cities_still_changes_the_balance(tmp_path):
    _init(tmp_path)
    asyncio.run(db.set_setting("event_city_enabled", "on"))
    _user(MSK_ID, "@msk_person", "msk")
    asyncio.run(set_admin_city(ADMIN_ID, ALL_CITIES))

    message = FakeMessage("/coins @msk_person +3 бонус")
    asyncio.run(admin_mod.cmd_coins(message, message.bot))

    assert _balance(MSK_ID) == 3
    assert OTHER_CITY not in "".join(message.answers_sent)


def test_journal_keeps_only_the_manager_city(tmp_path):
    client = _setup(tmp_path)
    _set("event_city_enabled", "on")
    assert _give(client, DELEGATE_ID, 4).status_code == 200
    assert _give(client, SPB_DELEGATE, 9).status_code == 200

    scoped = client.get("/app/api/admin/coins", headers=_hdr(BOUND_GAME_MANAGER)).json()
    assert scoped["total"] == 1
    assert [item["user_id"] for item in scoped["items"]] == [SPB_DELEGATE]

    everything = client.get("/app/api/admin/coins", headers=_hdr(GAME_MANAGER_ID)).json()
    assert everything["total"] == 2
    assert {item["user_id"] for item in everything["items"]} == {DELEGATE_ID, SPB_DELEGATE}
