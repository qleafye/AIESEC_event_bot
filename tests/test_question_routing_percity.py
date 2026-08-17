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


def _enable_cities():
    asyncio.run(db.set_setting("event_city_enabled", "on"))


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
