"""Общий сервис поиска человека (services/person_search.py) + починка roles_add_person
для тех, кто нажал /start, но анкету не подал (users_row_only_on_submit, reg_started).

pytest-asyncio недоступен в этом окружении (см. tests/test_db_phase5.py) — каждый async
вызов идёт через asyncio.run(), config.DB_PATH указывает на файл в tmp_path.
"""
import asyncio
import json

from aiogram.dispatcher.event.bases import UNHANDLED

from config import config
from database import db
from services import person_search


def _db_ready(tmp_path):
    config.DB_PATH = str(tmp_path / "test_person_search.db")
    asyncio.run(db.init_db())
    config.ADMIN_IDS = [900801]


# ── parse_query: разбор ввода ──────────────────────────────────────────────────────────────

def test_parse_query_username_forms():
    assert person_search.parse_query("@ivan_ivanov") == ("username", "@ivan_ivanov")
    assert person_search.parse_query("t.me/ivan_ivanov") == ("username", "ivan_ivanov")
    assert person_search.parse_query("https://t.me/ivan_ivanov") == ("username", "ivan_ivanov")
    assert person_search.parse_query("telegram.me/ivan_ivanov") == ("username", "ivan_ivanov")
    # голый юзернейм без «@» — те же правила именования Telegram (5-32, латиница/цифры/_)
    assert person_search.parse_query("ivan_ivanov") == ("username", "ivan_ivanov")


def test_parse_query_numeric_id():
    assert person_search.parse_query("900802") == ("id", 900802)
    assert person_search.parse_query("  900802  ") == ("id", 900802)


def test_parse_query_name_fallback():
    kind, value = person_search.parse_query("Иван Иванов")
    assert kind == "name"
    assert value == "Иван Иванов"
    # пустой ввод -> ("name", "") — вызывающий обязан вернуть «не найдено» без запроса
    assert person_search.parse_query("") == ("name", "")
    assert person_search.parse_query("   ") == ("name", "")


# ── поиск по username / id / ФИО среди users ───────────────────────────────────────────────

def test_search_by_username_finds_users_row(tmp_path):
    _db_ready(tmp_path)
    asyncio.run(db.add_user({
        "telegram_id": 900802, "username": "@ivan_ivanov", "full_name": "Иван Иванов",
        "university": "СПбГУ", "event_city": "spb", "registration_date": "2026-01-01",
    }))

    results = asyncio.run(person_search.search_people("@ivan_ivanov"))

    assert len(results) == 1
    assert results[0] == {
        "user_id": 900802, "full_name": "Иван Иванов", "username": "@ivan_ivanov",
        "city": "spb", "university": "СПбГУ", "status": "approved", "source": "users",
    }


def test_search_by_id_finds_users_row(tmp_path):
    _db_ready(tmp_path)
    asyncio.run(db.add_user({
        "telegram_id": 900802, "username": "@ivan_ivanov", "full_name": "Иван Иванов",
        "registration_date": "2026-01-01",
    }))

    results = asyncio.run(person_search.search_people("900802"))

    assert len(results) == 1
    assert results[0]["user_id"] == 900802
    assert results[0]["source"] == "users"


def test_search_by_name_finds_users_row(tmp_path):
    _db_ready(tmp_path)
    asyncio.run(db.add_user({
        "telegram_id": 900802, "username": "@ivan_ivanov", "full_name": "Иван Иванов",
        "registration_date": "2026-01-01",
    }))

    results = asyncio.run(person_search.search_people("иванов"))

    assert len(results) == 1
    assert results[0]["user_id"] == 900802


def test_search_no_match_returns_empty(tmp_path):
    _db_ready(tmp_path)
    assert asyncio.run(person_search.search_people("@nobody_here")) == []
    assert asyncio.run(person_search.search_people("123456789")) == []
    assert asyncio.run(person_search.search_people("Несуществующий Человек")) == []


# ── тёзки: одинаковое ФИО, разные города — различимы в выдаче ─────────────────────────────

def test_namesakes_distinguished_by_city(tmp_path):
    _db_ready(tmp_path)
    asyncio.run(db.add_user({
        "telegram_id": 900802, "username": "@ivan_msk", "full_name": "Иван Иванов",
        "university": "МГУ", "event_city": "msk", "registration_date": "2026-01-01",
    }))
    asyncio.run(db.add_user({
        "telegram_id": 900803, "username": "@ivan_spb", "full_name": "Иван Иванов",
        "university": "СПбГУ", "event_city": "spb", "registration_date": "2026-01-01",
    }))

    results = asyncio.run(person_search.search_people("Иванов"))

    assert len(results) == 2
    by_city = {r["city"]: r for r in results}
    assert by_city["msk"]["user_id"] == 900802
    assert by_city["msk"]["university"] == "МГУ"
    assert by_city["spb"]["user_id"] == 900803
    assert by_city["spb"]["university"] == "СПбГУ"


# ── city_scope: привязанный к городу менеджер видит только своих ──────────────────────────

def test_city_scope_restricts_username_lookup(tmp_path):
    _db_ready(tmp_path)
    asyncio.run(db.add_user({
        "telegram_id": 900802, "username": "@ivan_spb", "full_name": "Иван Иванов",
        "event_city": "spb", "registration_date": "2026-01-01",
    }))

    assert asyncio.run(
        person_search.search_people("@ivan_spb", city_scope=("msk", ()))
    ) == []
    own_city = asyncio.run(person_search.search_people("@ivan_spb", city_scope=("spb", ())))
    assert len(own_city) == 1
    assert own_city[0]["user_id"] == 900802


def test_city_scope_restricts_id_lookup(tmp_path):
    _db_ready(tmp_path)
    asyncio.run(db.add_user({
        "telegram_id": 900802, "username": "@ivan_spb", "full_name": "Иван Иванов",
        "event_city": "spb", "registration_date": "2026-01-01",
    }))

    assert asyncio.run(person_search.search_people("900802", city_scope=("msk", ()))) == []
    assert len(asyncio.run(person_search.search_people("900802", city_scope=("spb", ())))) == 1


def test_city_scope_restricts_name_lookup(tmp_path):
    _db_ready(tmp_path)
    asyncio.run(db.add_user({
        "telegram_id": 900802, "username": "@ivan_msk", "full_name": "Иван Иванов",
        "event_city": "msk", "registration_date": "2026-01-01",
    }))
    asyncio.run(db.add_user({
        "telegram_id": 900803, "username": "@ivan_spb", "full_name": "Иван Иванов",
        "event_city": "spb", "registration_date": "2026-01-01",
    }))

    scoped = asyncio.run(person_search.search_people("Иванов", city_scope=("msk", ())))
    assert [r["user_id"] for r in scoped] == [900802]


# ── фоллбэк на reg_started: человек нажал /start, анкету не подал ──────────────────────────

def test_username_fallback_to_reg_started(tmp_path):
    _db_ready(tmp_path)
    asyncio.run(db.mark_reg_started(900900, "started_only", event_city="msk"))

    results = asyncio.run(person_search.search_people("@started_only"))

    assert len(results) == 1
    assert results[0]["user_id"] == 900900
    assert results[0]["source"] == "reg_started"
    assert results[0]["username"] == "@started_only"
    assert results[0]["status"] is None


def test_id_fallback_to_reg_started(tmp_path):
    _db_ready(tmp_path)
    asyncio.run(db.mark_reg_started(900900, "started_only", event_city="msk"))

    results = asyncio.run(person_search.search_people("900900"))

    assert len(results) == 1
    assert results[0]["source"] == "reg_started"


def test_name_fallback_to_reg_started_reads_partial_data(tmp_path):
    _db_ready(tmp_path)
    asyncio.run(db.mark_reg_started(900900, "started_only", event_city="msk"))
    asyncio.run(db.set_reg_step(
        900900, "full_name",
        json.dumps({"full_name": "Пётр Петров", "university": "МГУ"}),
    ))

    results = asyncio.run(person_search.search_people("Петров"))

    assert len(results) == 1
    assert results[0]["user_id"] == 900900
    assert results[0]["full_name"] == "Пётр Петров"
    assert results[0]["university"] == "МГУ"
    assert results[0]["source"] == "reg_started"


def test_users_row_takes_priority_over_reg_started(tmp_path):
    """Правило users_row_only_on_submit: если человек уже подал анкету (сменил телефон/юзернейм
    после незавершённой попытки — тестовый суррогат тем же telegram_id), выдаётся только
    его users-строка, не дублируется reg_started-строкой."""
    _db_ready(tmp_path)
    asyncio.run(db.mark_reg_started(900900, "same_person", event_city="msk"))
    asyncio.run(db.add_user({
        "telegram_id": 900900, "username": "@same_person", "full_name": "Уже Подал",
        "event_city": "msk", "registration_date": "2026-01-01",
    }))

    results = asyncio.run(person_search.search_people("@same_person"))

    assert len(results) == 1
    assert results[0]["source"] == "users"
    assert results[0]["full_name"] == "Уже Подал"


def test_include_started_false_hides_reg_started(tmp_path):
    _db_ready(tmp_path)
    asyncio.run(db.mark_reg_started(900900, "started_only", event_city="msk"))

    assert asyncio.run(
        person_search.search_people("@started_only", include_started=False)
    ) == []


def test_reg_started_out_of_city_scope_is_hidden(tmp_path):
    _db_ready(tmp_path)
    asyncio.run(db.mark_reg_started(900900, "started_only", event_city="spb"))

    assert asyncio.run(
        person_search.search_people("@started_only", city_scope=("msk", ()))
    ) == []
    found = asyncio.run(person_search.search_people("@started_only", city_scope=("spb", ())))
    assert len(found) == 1


# ── db.py: аксессоры reg_started по username/id ────────────────────────────────────────────

def test_get_reg_started_by_username_ltrim_and_nocase(tmp_path):
    _db_ready(tmp_path)
    asyncio.run(db.mark_reg_started(900900, "MixedCase", event_city="msk"))

    row = asyncio.run(db.get_reg_started_by_username("mixedcase"))
    assert row is not None
    assert row["telegram_id"] == 900900

    row2 = asyncio.run(db.get_reg_started_by_username("@MixedCase"))
    assert row2 is not None
    assert row2["telegram_id"] == 900900


def test_get_reg_started_by_username_placeholder_is_none(tmp_path):
    _db_ready(tmp_path)
    assert asyncio.run(db.get_reg_started_by_username("-")) is None
    assert asyncio.run(db.get_reg_started_by_username(None)) is None


def test_get_reg_started_by_id(tmp_path):
    _db_ready(tmp_path)
    asyncio.run(db.mark_reg_started(900900, "started_only", event_city="msk"))

    assert asyncio.run(db.get_reg_started_by_id(900900)) is not None
    assert asyncio.run(db.get_reg_started_by_id(1)) is None


# ── roles_add_person: находит человека из reg_started, явно об этом сообщает ───────────────

from handlers import admin as admin_mod  # noqa: E402 -- после db-фикстур, как в test_roles_phase8.py
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage


ADMIN_ID = 900801


class FakeUser:
    def __init__(self, uid, username=None, full_name=None):
        self.id = uid
        self.username = username
        self.full_name = full_name


class FakeChat:
    def __init__(self, cid):
        self.id = cid


class FakeMessage:
    def __init__(self, text=None, user_id=None, chat_id=None):
        self.text = text
        self.html_text = text
        self.from_user = FakeUser(user_id) if user_id is not None else None
        self.chat = FakeChat(chat_id if chat_id is not None else user_id)
        self.reply_to_message = None
        self.answers = []

    async def answer(self, text, parse_mode=None, reply_markup=None):
        self.answers.append((text, parse_mode, reply_markup))


class FakeBot:
    def __init__(self):
        self.sent = []

    async def send_message(self, chat_id, text, parse_mode=None, reply_markup=None):
        self.sent.append((chat_id, text))


def _fresh_state(user_id):
    storage = MemoryStorage()
    key = StorageKey(bot_id=1, chat_id=user_id, user_id=user_id)
    return FSMContext(storage=storage, key=key)


def dispatch_message(text, user_id, *, raw_state=None):
    bot = FakeBot()
    state = _fresh_state(user_id)
    event = FakeMessage(text=text, user_id=user_id, chat_id=user_id)
    kwargs = dict(
        event_from_user=FakeUser(user_id), bot=bot, raw_state=raw_state,
        state=state, event_update=None,
    )
    result = asyncio.run(admin_mod.router.propagate_event("message", event, **kwargs))
    return result, event


def test_roles_add_person_finds_reg_started_fallback(tmp_path):
    _db_ready(tmp_path)
    asyncio.run(db.mark_reg_started(900900, "started_only", event_city="msk"))

    result, event = dispatch_message(
        "@started_only", ADMIN_ID, raw_state="StaffAdd:waiting_for_person",
    )

    assert result is not UNHANDLED
    text, _parse_mode, markup = event.answers[-1]
    assert "не найден в базе бота" not in text
    assert "started_only" in text or "900900" in text
    assert "анкету пока не подавал" in text
    cds = [b.callback_data for row in markup.inline_keyboard for b in row]
    assert f"roles_addrole:900900:reg_manager" in cds


def test_roles_add_person_still_rejects_truly_unknown_username(tmp_path):
    _db_ready(tmp_path)

    result, event = dispatch_message(
        "@nobody_at_all", ADMIN_ID, raw_state="StaffAdd:waiting_for_person",
    )

    assert result is not UNHANDLED
    text, _parse_mode, _markup = event.answers[-1]
    assert "не найден в базе бота" in text
