"""/find по id и пересылке, чужой город без карточки."""
import asyncio
from types import SimpleNamespace

import pytest

import domain.cities as cities
from config import config
from database import db
from handlers.access.admin_caps import required_capability
from handlers.applications import admin_find
from handlers.states import FindPerson
from tests._dbtpl import fast_init_db

CHIEF = 330501
MANAGER = 330502
DELEGATE = 330510
STARTED = 330511

_CITIES = [
    {"code": "msk", "label": "Москва", "tab_base": "", "enabled": 1, "sort_order": 0},
    {"code": "spb", "label": "Санкт-Петербург", "tab_base": "СПб", "enabled": 1, "sort_order": 1},
]


def _run(coro):
    return asyncio.run(coro)


def _ready(tmp_path):
    config.DB_PATH = str(tmp_path / "test_find_card_33.db")
    fast_init_db()
    config.ADMIN_IDS = [CHIEF]


@pytest.fixture
def city_registry():
    saved = cities.all_cities()
    cities.set_cities_for_test([dict(c) for c in _CITIES])
    yield
    cities.set_cities_for_test(saved)


class _State:
    def __init__(self):
        self.value = None

    async def clear(self):
        self.value = None

    async def set_state(self, value):
        self.value = value


class _Msg:
    def __init__(self, text=None, uid=None):
        self.text = text
        self.from_user = SimpleNamespace(id=uid) if uid is not None else None
        self.answers = []
        self.forward_origin = None

    async def answer(self, text, parse_mode=None, reply_markup=None):
        self.answers.append((text, parse_mode, reply_markup))


def _cbs(kb):
    if kb is None:
        return []
    return [b.callback_data for row in kb.inline_keyboard for b in row]


async def _seed_user(tid, *, username="finduser", city="msk", status="approved", email="ivan@example.com"):
    await db.add_user({
        "telegram_id": tid,
        "username": username,
        "full_name": "Иван Иванов",
        "email": email,
        "registration_date": "2026-01-01",
        "event_city": city,
    })
    async with db._connect() as conn:
        await conn.execute("UPDATE users SET status = ? WHERE telegram_id = ?", (status, tid))
        await conn.commit()


def test_find_by_numeric_id(tmp_path):
    from handlers import admin

    _ready(tmp_path)

    async def go():
        await _seed_user(DELEGATE, username="finduser")
        by_name = _Msg("/find @finduser", CHIEF)
        by_id = _Msg(f"/find {DELEGATE}", CHIEF)
        await admin.cmd_find_user(by_name)
        await admin.cmd_find_user(by_id)
        return by_name.answers[0], by_id.answers[0]

    name_ans, id_ans = _run(go())
    assert f"<code>{DELEGATE}</code>" in name_ans[0]
    assert f"<code>{DELEGATE}</code>" in id_ans[0]
    assert _cbs(name_ans[2]) == _cbs(id_ans[2])


def test_find_by_forward(tmp_path):
    _ready(tmp_path)

    async def go():
        await _seed_user(DELEGATE)
        msg = _Msg(uid=CHIEF)
        msg.forward_origin = SimpleNamespace(sender_user=SimpleNamespace(id=DELEGATE))
        await admin_find.find_person_message(msg, _State())
        return msg.answers[0]

    text, _mode, _kb = _run(go())
    assert f"<code>{DELEGATE}</code>" in text


def test_find_bare_command_asks(tmp_path):
    from handlers import admin

    _ready(tmp_path)
    msg = _Msg("/find", CHIEF)
    state = _State()
    _run(admin.cmd_find_user(msg, state))
    text = msg.answers[0][0]
    assert "не найден" not in text
    assert "@username" in text and "id" in text and "переслан" in text
    assert state.value == FindPerson.waiting


def test_find_name_is_not_search(tmp_path):
    _ready(tmp_path)

    async def go():
        await _seed_user(DELEGATE, username="ivan")
        msg = _Msg("Иван", CHIEF)
        await admin_find.find_person_message(msg, _State())
        return msg.answers[0]

    text, _mode, kb = _run(go())
    assert "Не понял" in text and "не имя" in text
    assert kb is None
    assert str(DELEGATE) not in text


def test_find_hidden_forward_explains(tmp_path):
    _ready(tmp_path)
    msg = _Msg(uid=CHIEF)
    msg.forward_origin = SimpleNamespace(sender_user=None)
    _run(admin_find.find_person_message(msg, _State()))
    text = msg.answers[0][0]
    assert "forward_origin" not in text
    assert "@username" in text and "id" in text


def test_find_started_only_by_id(tmp_path):
    from handlers import admin

    _ready(tmp_path)

    async def go():
        await db.mark_reg_started(STARTED, "onlystart")
        msg = _Msg(f"/find {STARTED}", CHIEF)
        await admin.cmd_find_user(msg)
        return msg.answers[0]

    text, _mode, kb = _run(go())
    assert "анкету не подавал" in text
    assert f"roles_addfor:{STARTED}" in _cbs(kb)


def test_find_other_city_hides_card(tmp_path, city_registry):
    from handlers import admin

    _ready(tmp_path)

    async def go():
        await db.set_setting("event_city_enabled", "on")
        await db.add_staff(MANAGER, "reg_manager", CHIEF)
        await db.set_staff_city(MANAGER, "spb")
        await _seed_user(DELEGATE, city="msk", email="secret@example.com")
        msg = _Msg(f"/find {DELEGATE}", MANAGER)
        await admin.cmd_find_user(msg)
        return msg.answers[0][0]

    text = _run(go())
    assert "другого города" in text
    assert "Email" not in text
    assert "Одобрена" not in text
    assert "citymv_start" not in text


def test_find_bound_manager_no_move_button(tmp_path, city_registry):
    from handlers import admin

    _ready(tmp_path)

    async def go():
        await db.set_setting("event_city_enabled", "on")
        await db.add_staff(MANAGER, "reg_manager", CHIEF)
        await db.set_staff_city(MANAGER, "spb")
        await _seed_user(DELEGATE, city="spb")
        msg = _Msg(f"/find {DELEGATE}", MANAGER)
        await admin.cmd_find_user(msg)
        return msg.answers[0]

    text, _mode, kb = _run(go())
    assert f"<code>{DELEGATE}</code>" in text
    assert "citymv_start" not in " ".join(_cbs(kb))


def test_find_chief_sees_move_button(tmp_path, city_registry):
    from handlers import admin

    _ready(tmp_path)

    async def go():
        await db.set_setting("event_city_enabled", "on")
        await _seed_user(DELEGATE, city="msk")
        msg = _Msg(f"/find {DELEGATE}", CHIEF)
        await admin.cmd_find_user(msg)
        return msg.answers[0]

    _text, _mode, kb = _run(go())
    assert f"citymv_start:{DELEGATE}" in _cbs(kb)


def test_find_state_capability():
    assert required_capability(raw_state="FindPerson:waiting") == "moderate_reg"
