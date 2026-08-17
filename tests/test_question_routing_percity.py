"""Phase 09.2 Plan 02 (CITY-06, CONTEXT D) — question / new-application notify routing by
delegate city.

pytest-asyncio is unavailable in this env — every async helper is driven via asyncio.run()
and config.DB_PATH points at a tmp_path file, same convention as tests/test_roles_phase8.py /
tests/test_manager_city_091.py.

Task 1: `handlers/admin_caps.py::capability_holders`/`notify_by_capability` grow an optional
    `city` kwarg -- addressing narrowing with a mandatory "never drop the message" fallback.
Task 2: `handlers/user_actions.py::process_question` resolves the delegate's city once and
    passes it through.
Task 3: `handlers/registration.py::finalize_registration` passes `data.get("event_city")`
    through the same primitive for the new-application notification.
"""
import asyncio
import inspect
import re
from pathlib import Path

from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage

from config import config
from database import db

ADMIN_ID = 920201
MSK_MANAGER_ID = 920202
SPB_MANAGER_ID = 920203
UNBOUND_MANAGER_ID = 920204
DELEGATE_ID = 920205


def _roles_ready(tmp_path):
    config.DB_PATH = str(tmp_path / "test_question_routing_percity.db")
    asyncio.run(db.init_db())
    config.ADMIN_IDS = [ADMIN_ID]


class FakeBot:
    def __init__(self):
        self.sent = []

    async def send_message(self, chat_id, text, parse_mode=None, reply_markup=None):
        self.sent.append((chat_id, text))


class FakeUser:
    def __init__(self, uid, username=None):
        self.id = uid
        self.username = username


class FakeChat:
    def __init__(self, cid):
        self.id = cid


class FakeMessage:
    """Minimal stand-in for the aiogram Message `process_question` receives."""

    def __init__(self, text=None, user_id=None, username=None):
        self.text = text
        self.from_user = FakeUser(user_id, username=username)
        self.chat = FakeChat(user_id)
        self.answers = []

    async def answer(self, text, parse_mode=None, reply_markup=None):
        self.answers.append((text, parse_mode, reply_markup))


def _fresh_state(user_id):
    storage = MemoryStorage()
    key = StorageKey(bot_id=1, chat_id=user_id, user_id=user_id)
    return FSMContext(storage=storage, key=key)


def _enable_cities():
    asyncio.run(db.set_setting("event_city_enabled", "on"))


def _add_delegate(telegram_id, event_city):
    asyncio.run(db.add_user({
        "telegram_id": telegram_id,
        "event_city": event_city,
        "registration_date": "2026-08-17 00:00:00",
    }))


def _count_questions():
    import sqlite3
    conn = sqlite3.connect(config.DB_PATH)
    try:
        return conn.execute("SELECT COUNT(*) FROM delegate_questions").fetchone()[0]
    finally:
        conn.close()


# ── Task 1: capability_holders/notify_by_capability city kwarg ─────────────────────────────

def test_capability_holders_without_city_kwarg_is_byte_identical_to_before(tmp_path):
    """Regression: no `city` argument at all -> same as calling it pre-09.2."""
    from handlers import admin_caps

    _roles_ready(tmp_path)
    asyncio.run(db.add_staff(MSK_MANAGER_ID, "reg_manager", ADMIN_ID))

    holders = asyncio.run(admin_caps.capability_holders("moderate_reg"))
    assert holders == [ADMIN_ID, MSK_MANAGER_ID]


def test_capability_holders_city_none_applies_no_filter(tmp_path):
    from handlers import admin_caps

    _roles_ready(tmp_path)
    _enable_cities()
    asyncio.run(db.add_staff(MSK_MANAGER_ID, "reg_manager", ADMIN_ID))
    asyncio.run(db.set_staff_city(MSK_MANAGER_ID, "msk"))

    holders = asyncio.run(admin_caps.capability_holders("moderate_reg", city=None))
    assert holders == [ADMIN_ID, MSK_MANAGER_ID]


def test_capability_holders_module_off_ignores_city_kwarg(tmp_path):
    from handlers import admin_caps

    _roles_ready(tmp_path)
    # event_city_enabled left at its default ("off").
    asyncio.run(db.add_staff(MSK_MANAGER_ID, "reg_manager", ADMIN_ID))
    asyncio.run(db.set_staff_city(MSK_MANAGER_ID, "msk"))

    holders = asyncio.run(admin_caps.capability_holders("moderate_reg", city="spb"))
    assert holders == [ADMIN_ID, MSK_MANAGER_ID]


def test_capability_holders_filters_by_bound_city(tmp_path):
    from handlers import admin_caps

    _roles_ready(tmp_path)
    _enable_cities()
    asyncio.run(db.add_staff(MSK_MANAGER_ID, "reg_manager", ADMIN_ID))
    asyncio.run(db.set_staff_city(MSK_MANAGER_ID, "msk"))
    asyncio.run(db.add_staff(SPB_MANAGER_ID, "reg_manager", ADMIN_ID))
    asyncio.run(db.set_staff_city(SPB_MANAGER_ID, "spb"))
    asyncio.run(db.add_staff(UNBOUND_MANAGER_ID, "reg_manager", ADMIN_ID))

    holders = asyncio.run(admin_caps.capability_holders("moderate_reg", city="spb"))

    assert ADMIN_ID in holders            # D-12: superadmin always
    assert UNBOUND_MANAGER_ID in holders  # no binding = all cities
    assert SPB_MANAGER_ID in holders      # bound to the requested city
    assert MSK_MANAGER_ID not in holders  # bound to a different city


def test_capability_holders_normalizes_both_sides(tmp_path):
    """Garbage/legacy binding label and a garbage requested city both collapse through
    normalize_city to the same default code, so they still match."""
    from handlers import admin_caps

    _roles_ready(tmp_path)
    _enable_cities()
    asyncio.run(db.add_staff(MSK_MANAGER_ID, "reg_manager", ADMIN_ID))
    asyncio.run(db.set_staff_city(MSK_MANAGER_ID, "not-a-real-code"))

    holders = asyncio.run(admin_caps.capability_holders("moderate_reg", city="also-garbage"))
    # Both sides normalize to the default city code -> they match.
    assert MSK_MANAGER_ID in holders


def test_capability_holders_city_filter_empties_falls_back_to_unfiltered(tmp_path):
    """T-092-04: every holder is bound to a DIFFERENT city than requested -> the message must
    still reach somebody, not vanish."""
    from handlers import admin_caps

    _roles_ready(tmp_path)
    _enable_cities()
    asyncio.run(db.add_staff(MSK_MANAGER_ID, "reg_manager", ADMIN_ID))
    asyncio.run(db.set_staff_city(MSK_MANAGER_ID, "msk"))
    config.ADMIN_IDS = []  # remove the D-12 always-kept safety net for this test

    holders = asyncio.run(admin_caps.capability_holders("moderate_reg", city="spb"))
    assert holders == [MSK_MANAGER_ID]  # fell back to the unfiltered list, not []


def test_notify_by_capability_city_kwarg_returns_same_sent_count_shape(tmp_path):
    from handlers import admin_caps

    _roles_ready(tmp_path)
    _enable_cities()
    asyncio.run(db.add_staff(SPB_MANAGER_ID, "reg_manager", ADMIN_ID))
    asyncio.run(db.set_staff_city(SPB_MANAGER_ID, "spb"))
    asyncio.run(db.add_staff(MSK_MANAGER_ID, "reg_manager", ADMIN_ID))
    asyncio.run(db.set_staff_city(MSK_MANAGER_ID, "msk"))

    bot = FakeBot()
    sent = asyncio.run(
        admin_caps.notify_by_capability(bot, "moderate_reg", "hi", city="spb")
    )
    recipients = [chat_id for chat_id, _t in bot.sent]

    assert ADMIN_ID in recipients
    assert SPB_MANAGER_ID in recipients
    assert MSK_MANAGER_ID not in recipients
    assert sent == len(recipients)


def test_notify_by_capability_recipient_order_keeps_superadmins_first(tmp_path):
    from handlers import admin_caps

    _roles_ready(tmp_path)
    _enable_cities()
    asyncio.run(db.add_staff(SPB_MANAGER_ID, "reg_manager", ADMIN_ID))
    asyncio.run(db.set_staff_city(SPB_MANAGER_ID, "spb"))

    holders = asyncio.run(admin_caps.capability_holders("moderate_reg", city="spb"))
    assert holders[0] == ADMIN_ID


# ── Task 2: delegate question -> process_question resolves + passes the delegate's city ────

def test_process_question_module_off_recipients_match_capability_holders(tmp_path):
    from handlers import admin_caps
    from handlers import user_actions as ua_mod

    _roles_ready(tmp_path)
    # event_city_enabled left off.
    asyncio.run(db.add_staff(MSK_MANAGER_ID, "reg_manager", ADMIN_ID))
    _add_delegate(DELEGATE_ID, "spb")

    bot = FakeBot()
    state = _fresh_state(DELEGATE_ID)
    message = FakeMessage(text="Когда дедлайн?", user_id=DELEGATE_ID)
    asyncio.run(ua_mod.process_question(message, state, bot))

    recipients = [chat_id for chat_id, _t in bot.sent]
    expected = asyncio.run(admin_caps.capability_holders("moderate_reg"))
    assert recipients == expected


def test_process_question_routes_to_delegate_city_manager(tmp_path):
    from handlers import user_actions as ua_mod

    _roles_ready(tmp_path)
    _enable_cities()
    asyncio.run(db.add_staff(SPB_MANAGER_ID, "reg_manager", ADMIN_ID))
    asyncio.run(db.set_staff_city(SPB_MANAGER_ID, "spb"))
    asyncio.run(db.add_staff(MSK_MANAGER_ID, "reg_manager", ADMIN_ID))
    asyncio.run(db.set_staff_city(MSK_MANAGER_ID, "msk"))
    asyncio.run(db.add_staff(UNBOUND_MANAGER_ID, "reg_manager", ADMIN_ID))
    _add_delegate(DELEGATE_ID, "spb")

    bot = FakeBot()
    state = _fresh_state(DELEGATE_ID)
    message = FakeMessage(text="Когда дедлайн?", user_id=DELEGATE_ID)
    asyncio.run(ua_mod.process_question(message, state, bot))

    recipients = [chat_id for chat_id, _t in bot.sent]
    assert ADMIN_ID in recipients
    assert SPB_MANAGER_ID in recipients
    assert UNBOUND_MANAGER_ID in recipients
    assert MSK_MANAGER_ID not in recipients


def test_process_question_no_event_city_normalizes_to_default(tmp_path):
    from handlers import user_actions as ua_mod

    _roles_ready(tmp_path)
    _enable_cities()
    asyncio.run(db.add_staff(MSK_MANAGER_ID, "reg_manager", ADMIN_ID))
    asyncio.run(db.set_staff_city(MSK_MANAGER_ID, "msk"))  # msk is the configured default
    _add_delegate(DELEGATE_ID, None)

    bot = FakeBot()
    state = _fresh_state(DELEGATE_ID)
    message = FakeMessage(text="Когда дедлайн?", user_id=DELEGATE_ID)
    asyncio.run(ua_mod.process_question(message, state, bot))

    recipients = [chat_id for chat_id, _t in bot.sent]
    assert MSK_MANAGER_ID in recipients  # NULL event_city -> default city -> msk manager sees it


def test_process_question_still_creates_exactly_one_question_row(tmp_path):
    from handlers import user_actions as ua_mod

    _roles_ready(tmp_path)
    _enable_cities()
    asyncio.run(db.add_staff(SPB_MANAGER_ID, "reg_manager", ADMIN_ID))
    asyncio.run(db.set_staff_city(SPB_MANAGER_ID, "spb"))
    _add_delegate(DELEGATE_ID, "spb")

    bot = FakeBot()
    state = _fresh_state(DELEGATE_ID)
    message = FakeMessage(text="Когда дедлайн?", user_id=DELEGATE_ID)
    asyncio.run(ua_mod.process_question(message, state, bot))

    assert _count_questions() == 1


def test_process_question_sent_count_reply_branch_unchanged(tmp_path):
    """D-14/UX contract: sent_count > 0 still yields the "sent" confirmation text."""
    from handlers import user_actions as ua_mod

    _roles_ready(tmp_path)
    _enable_cities()
    _add_delegate(DELEGATE_ID, "spb")

    bot = FakeBot()
    state = _fresh_state(DELEGATE_ID)
    message = FakeMessage(text="Когда дедлайн?", user_id=DELEGATE_ID)
    asyncio.run(ua_mod.process_question(message, state, bot))

    assert message.answers
    assert "отправлен" in message.answers[0][0]


def test_process_question_source_has_city_kwarg_and_correct_order():
    from handlers import user_actions as ua_mod

    s = inspect.getsource(ua_mod.process_question)
    assert "city=" in s
    assert "normalize_city" in s
    assert s.index("create_question") < s.index("notify_by_capability")


# ── Task 3: new-application notification -> same filter ────────────────────────────────────

def test_finalize_registration_source_passes_event_city():
    from handlers import registration as reg_mod

    s = inspect.getsource(reg_mod.finalize_registration)
    assert "notify_by_capability" in s
    assert 'city=data.get("event_city")' in s


def test_both_fanout_sites_pass_city_kwarg():
    repo_root = Path(__file__).resolve().parent.parent
    reg_src = (repo_root / "handlers/registration.py").read_text(encoding="utf-8")
    ua_src = (repo_root / "handlers/user_actions.py").read_text(encoding="utf-8")
    combined = reg_src + "\n" + ua_src
    calls_with_city = len(re.findall(r"notify_by_capability\([^)]*city=", combined))
    assert calls_with_city == 2


def test_new_application_notification_routes_to_delegate_city_manager(tmp_path, monkeypatch):
    from handlers import registration as reg_mod

    _roles_ready(tmp_path)
    _enable_cities()
    asyncio.run(db.set_setting("pending_notify_mode", "instant"))  # REG-02: notify on pending too
    asyncio.run(db.add_staff(SPB_MANAGER_ID, "reg_manager", ADMIN_ID))
    asyncio.run(db.set_staff_city(SPB_MANAGER_ID, "spb"))
    asyncio.run(db.add_staff(MSK_MANAGER_ID, "reg_manager", ADMIN_ID))
    asyncio.run(db.set_staff_city(MSK_MANAGER_ID, "msk"))
    asyncio.run(db.add_staff(UNBOUND_MANAGER_ID, "reg_manager", ADMIN_ID))

    async def _fake_row(data):
        return []

    async def _fake_append(row):
        return None

    monkeypatch.setattr(reg_mod, "_sheet_dispatch", lambda *_a, **_kw: (_fake_row, _fake_append))
    monkeypatch.setattr(reg_mod, "_spawn", lambda coro: coro.close())

    bot = FakeBot()
    state = _fresh_state(DELEGATE_ID)
    asyncio.run(state.update_data(
        full_name="Тест Тестов",
        username="@test",
        event_city="spb",
        participant_type="full",
    ))
    message = FakeMessage(text="irrelevant", user_id=DELEGATE_ID)

    asyncio.run(reg_mod.finalize_registration(message, state, bot))

    recipients = [chat_id for chat_id, text in bot.sent if "Новая регистрация" in text]
    assert ADMIN_ID in recipients
    assert SPB_MANAGER_ID in recipients
    assert UNBOUND_MANAGER_ID in recipients
    assert MSK_MANAGER_ID not in recipients
