"""`/uat` для приёмки регионального форума (СПб/Тюмень, 03.10): форумные состояния с шагом
города (одобрен с QR-пропуском, пришёл, ждёт, отклонён) и форумные роли с привязкой к городу
(волонтёр входа, дежурный SOS, менеджер форума).

pytest-asyncio недоступен — async через asyncio.run(), фейки по образцу
tests/test_uat_seed_260911.py."""
import asyncio

import pytest
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage

import domain.cities as cities
from config import config
from database import db
from services.infra.timeutil import msk_now
from tests._dbtpl import fast_init_db

TESTER_ID = 900940
SUPERADMIN_ID = 900941

_CITIES = [
    {"code": "msk", "label": "Москва", "tab_base": "", "enabled": 1, "sort_order": 0},
    {"code": "spb", "label": "Санкт-Петербург", "tab_base": " СПб", "enabled": 1, "sort_order": 1},
    {"code": "tyumen", "label": "Тюмень", "tab_base": " Тюмень", "enabled": 1, "sort_order": 2},
]


@pytest.fixture(autouse=True)
def _env(tmp_path):
    saved_cities = list(cities.CITIES)
    saved_admins = list(config.ADMIN_IDS)
    config.DB_PATH = str(tmp_path / "test_uat_seed_forum.db")
    fast_init_db()
    config.ADMIN_IDS = [SUPERADMIN_ID]
    cities.set_cities_for_test(_CITIES)
    asyncio.run(db.set_setting("uat_seed_enabled", "on"))
    asyncio.run(db.set_setting("uat_seed_testers", f"{TESTER_ID}; {SUPERADMIN_ID}"))
    asyncio.run(db.set_setting("event_season", "YL 26/2"))
    yield
    cities.set_cities_for_test(saved_cities)
    config.ADMIN_IDS = saved_admins


def _h():
    from handlers.access import uat_seed
    return uat_seed


class _FakeUser:
    def __init__(self, uid, username=None):
        self.id = uid
        self.username = username


class _FakeEditableMessage:
    def __init__(self):
        self.edits = []

    async def edit_text(self, text, parse_mode=None, reply_markup=None):
        self.edits.append((text, parse_mode, reply_markup))


class _FakeCallback:
    def __init__(self, data, user_id):
        self.data = data
        self.from_user = _FakeUser(user_id)
        self.message = _FakeEditableMessage()
        self.answer_calls = []

    async def answer(self, text=None, show_alert=False):
        self.answer_calls.append((text, show_alert))


def _state(user_id):
    return FSMContext(storage=MemoryStorage(), key=StorageKey(bot_id=1, chat_id=user_id, user_id=user_id))


def _cb(handler, data, user_id=TESTER_ID):
    callback = _FakeCallback(data, user_id)
    if handler.__name__ == "uat_execute":
        asyncio.run(handler(callback, _state(user_id)))
    else:
        asyncio.run(handler(callback))
    return callback


def _buttons(callback):
    kb = callback.message.edits[-1][2]
    return [(b.text, b.callback_data) for row in kb.inline_keyboard for b in row]


# ── Шаги пикеров ──────────────────────────────────────────────────────────────────────────

def test_forum_state_asks_city_with_buttons():
    uat = _h()
    callback = _cb(uat.uat_pick_state, "uat_st:fappr")
    text = callback.message.edits[0][0]
    assert "какого города" in text
    data = [d for _t, d in _buttons(callback)]
    assert "uat_stc:fappr:spb" in data and "uat_stc:fappr:tyumen" in data
    assert "uat_no" in data


def test_city_step_shows_roles_signed_with_city():
    uat = _h()
    callback = _cb(uat.uat_pick_state_city, "uat_stc:fappr:tyumen")
    buttons = _buttons(callback)
    assert ("🎗 Волонтёр входа (сканер) — Тюмень", "uat_role:fappr:vol:tyumen") in buttons
    assert ("🆘 Дежурный SOS — Тюмень", "uat_role:fappr:sos:tyumen") in buttons
    assert ("🎪 Менеджер форума — Тюмень", "uat_role:fappr:fmgr:tyumen") in buttons
    # коды capability на экран не попадают
    assert not any("checkin" in t or "moderate" in t for t, _d in buttons)


def test_unknown_city_is_ignored():
    uat = _h()
    callback = _cb(uat.uat_pick_state_city, "uat_stc:fappr:paris")
    assert callback.message.edits == []


def test_forum_role_on_plain_state_asks_city():
    uat = _h()
    callback = _cb(uat.uat_pick_role, "uat_role:fresh:vol")
    data = [d for _t, d in _buttons(callback)]
    assert "uat_role:fresh:vol:tyumen" in data


def test_card_is_honest_about_qr_role_and_dropped_roles():
    uat = _h()
    asyncio.run(db.add_staff(TESTER_ID, "game_manager", TESTER_ID))
    callback = _cb(uat.uat_pick_role, "uat_role:farr:vol:spb")
    text, _pm, kb = callback.message.edits[0]
    assert "Санкт-Петербург" in text
    assert "QR-пропуск" in text and "отметка входа" in text
    assert "сканирует QR" in text
    assert "Снимутся ваши роли" in text and "Менеджер геймификации" in text
    assert kb.inline_keyboard[0][0].callback_data == "uat_go:farr:vol:spb"


@pytest.mark.parametrize("role_code", ["sos", "fmgr"])
def test_card_says_reg_manager_role_also_opens_receipts(role_code):
    """Дежурный SOS и менеджер форума — это роль «Менеджер регистраций» целиком: по умолчанию
    с модерацией чеков оплаты. Карточка говорит об этом прямо."""
    uat = _h()
    text = _cb(uat.uat_pick_role, f"uat_role:fappr:{role_code}:spb").message.edits[0][0]
    assert "даёт ещё" in text and "Модерация чеков" in text


def test_card_has_no_extra_note_when_receipts_removed_from_role():
    uat = _h()
    asyncio.run(db.set_setting("role_caps_reg_manager", "moderate_reg"))
    text = _cb(uat.uat_pick_role, "uat_role:fappr:sos:spb").message.edits[0][0]
    assert "даёт ещё" not in text


def test_card_warns_when_role_switched_off():
    uat = _h()
    asyncio.run(db.set_setting("role_volunteer_enabled", "off"))
    callback = _cb(uat.uat_pick_role, "uat_role:fresh:vol:tyumen")
    assert "выключена" in callback.message.edits[0][0]


def test_go_without_city_for_forum_state_changes_nothing():
    uat = _h()
    callback = _cb(uat.uat_execute, "uat_go:fappr:none")
    assert callback.message.edits == []
    assert callback.answer_calls and callback.answer_calls[0][1] is True
    assert asyncio.run(db.get_user(TESTER_ID)) is None


# ── Исполнение: состояния ──────────────────────────────────────────────────────────────────

def test_superadmin_seeds_approved_spb_keeps_admin_and_gets_qr_token():
    from handlers.access.admin_caps import ALL_CAPABILITIES, resolve_capabilities
    from services.checkin import checkin_denial

    uat = _h()
    _cb(uat.uat_execute, "uat_go:fappr:none:spb", user_id=SUPERADMIN_ID)

    user = asyncio.run(db.get_user(SUPERADMIN_ID))
    assert user["status"] == "approved"
    assert user["event_city"] == "spb"
    assert user["season"] == "YL 26/2"
    assert user["checkin_token"]
    assert asyncio.run(checkin_denial(user)) is None  # пропуск действительно действует
    assert asyncio.run(resolve_capabilities(SUPERADMIN_ID)) == set(ALL_CAPABILITIES)
    assert SUPERADMIN_ID in config.ADMIN_IDS


def test_arrived_tyumen_has_entry_mark_today():
    from services.checkin import ENTRY_POINT

    uat = _h()
    callback = _cb(uat.uat_execute, "uat_go:farr:none:tyumen")

    user = asyncio.run(db.get_user(TESTER_ID))
    assert user["status"] == "approved" and user["event_city"] == "tyumen"
    assert user["checkin_token"]
    marks = asyncio.run(db.list_checkins_for_user(TESTER_ID))
    assert [(m["point"], m["day"]) for m in marks] == [(ENTRY_POINT, msk_now().strftime("%Y-%m-%d"))]
    assert "Готово" in callback.message.edits[0][0]


def test_arrived_twice_leaves_single_mark():
    uat = _h()
    _cb(uat.uat_execute, "uat_go:farr:none:spb")
    _cb(uat.uat_execute, "uat_go:farr:none:spb")
    assert len(asyncio.run(db.list_checkins_for_user(TESTER_ID))) == 1


@pytest.mark.parametrize("state_code,status", [("fpend", "pending"), ("frej", "rejected")])
def test_pending_and_rejected_in_city_have_no_qr(state_code, status):
    uat = _h()
    _cb(uat.uat_execute, f"uat_go:{state_code}:none:tyumen")
    user = asyncio.run(db.get_user(TESTER_ID))
    assert user["status"] == status
    assert user["event_city"] == "tyumen"
    assert not user["checkin_token"]
    assert asyncio.run(db.list_checkins_for_user(TESTER_ID)) == []


def test_forum_city_is_set_even_with_cities_module_off():
    uat = _h()
    assert asyncio.run(cities.cities_module_on()) is False
    _cb(uat.uat_execute, "uat_go:fappr:none:spb")
    assert asyncio.run(db.get_user(TESTER_ID))["event_city"] == "spb"


# ── Исполнение: роли ───────────────────────────────────────────────────────────────────────

def test_volunteer_tyumen_gets_checkin_and_city():
    from handlers.access.admin_caps import resolve_capabilities

    uat = _h()
    _cb(uat.uat_execute, "uat_go:fresh:vol:tyumen")

    assert asyncio.run(db.get_staff_roles(TESTER_ID)) == ["volunteer"]
    assert asyncio.run(db.get_staff_city(TESTER_ID)) == "tyumen"
    assert asyncio.run(resolve_capabilities(TESTER_ID)) == {"checkin"}


def test_sos_duty_spb_receives_spb_sos_only():
    from handlers.access.admin_caps import capability_holders

    uat = _h()
    _cb(uat.uat_execute, "uat_go:fappr:sos:spb")

    assert asyncio.run(db.get_staff_city(TESTER_ID)) == "spb"
    asyncio.run(db.set_setting("event_city_enabled", "on"))
    assert TESTER_ID in asyncio.run(capability_holders("moderate_reg", city="spb"))
    assert TESTER_ID not in asyncio.run(capability_holders("moderate_reg", city="tyumen"))


def test_forum_manager_gets_forum_caps_bound_to_city():
    from handlers.access.admin_caps import resolve_capabilities

    uat = _h()
    _cb(uat.uat_execute, "uat_go:fresh:fmgr:tyumen")

    assert set(asyncio.run(db.get_staff_roles(TESTER_ID))) == {"reg_manager", "reg_volunteer"}
    assert asyncio.run(db.get_staff_city(TESTER_ID)) == "tyumen"
    caps = asyncio.run(resolve_capabilities(TESTER_ID))
    assert {"moderate_reg", "checkin", "checkin_approve"} <= caps


def test_switch_from_city_role_to_plain_manager_drops_city_binding():
    uat = _h()
    _cb(uat.uat_execute, "uat_go:fresh:sos:spb")
    _cb(uat.uat_execute, "uat_go:fresh:reg")

    assert asyncio.run(db.get_staff_roles(TESTER_ID)) == ["reg_manager"]
    assert asyncio.run(db.get_staff_city(TESTER_ID)) is None


def test_switch_between_city_roles_replaces_roles_and_city():
    uat = _h()
    _cb(uat.uat_execute, "uat_go:fresh:fmgr:spb")
    _cb(uat.uat_execute, "uat_go:fresh:vol:tyumen")

    assert asyncio.run(db.get_staff_roles(TESTER_ID)) == ["volunteer"]
    assert asyncio.run(db.get_staff_city(TESTER_ID)) == "tyumen"


def test_role_prompt_has_no_service_words():
    """Приёмка 01.10: экран роли не говорит служебного «.env»."""
    from handlers.access import uat_seed

    assert ".env" not in uat_seed._ROLE_PROMPT
    assert "админ" in uat_seed._ROLE_PROMPT.lower()
