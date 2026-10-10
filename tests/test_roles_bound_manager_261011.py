"""«👥 Роли и доступы» (приёмка 10.10): менеджер, привязанный к городу, не правит роли и их права
(они одни на все города) и не выдаёт непривязанную роль; человек, которого бот знает только по
пересылке, подписан именем, а не «id N»."""
import asyncio
from types import SimpleNamespace

from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage

import domain.cities as cities
from database import db
from handlers.access import admin_roles
from handlers.access.admin_caps import role_caps_key, role_enabled_key
from services.access.person_label import person_label
from tests.test_admin_sections_ia20 import FakeCallback, _enable_cities
from tests.test_roles_phase8 import ADMIN_ID, MANAGER_ID, _roles_ready

NEWCOMER_ID = 900261101


def _bound_manager(tmp_path, bound=True) -> str:
    _roles_ready(tmp_path)
    _enable_cities()
    code = cities.city_codes()[1]
    asyncio.run(db.add_staff(MANAGER_ID, "reg_manager", ADMIN_ID))
    if bound:
        assert asyncio.run(db.set_staff_city(MANAGER_ID, code))
    return code


def test_bound_manager_cannot_toggle_role_or_its_caps(tmp_path):
    _bound_manager(tmp_path)
    before_enabled = asyncio.run(db.get_setting(role_enabled_key("reg_manager")))
    before_caps = asyncio.run(db.get_setting(role_caps_key("reg_manager")))

    cb = FakeCallback("roles_toggle:reg_manager", user_id=MANAGER_ID)
    asyncio.run(admin_roles.toggle_role_enabled(cb))
    assert cb.answers == [(admin_roles.ROLES_COMMON_DENIED_TEXT, True)]

    cb = FakeCallback("roles_cap:reg_manager:settings", user_id=MANAGER_ID)
    asyncio.run(admin_roles.toggle_role_cap(cb))
    assert cb.answers == [(admin_roles.ROLES_COMMON_DENIED_TEXT, True)]

    assert asyncio.run(db.get_setting(role_enabled_key("reg_manager"))) == before_enabled
    assert asyncio.run(db.get_setting(role_caps_key("reg_manager"))) == before_caps


def test_unbound_manager_and_superadmin_still_toggle_roles(tmp_path):
    _bound_manager(tmp_path, bound=False)
    for uid in (MANAGER_ID, ADMIN_ID):
        cb = FakeCallback("roles_toggle:game_manager", user_id=uid)
        asyncio.run(admin_roles.toggle_role_enabled(cb))
        assert cb.answers and cb.answers[0][0] != admin_roles.ROLES_COMMON_DENIED_TEXT


def test_bound_manager_grants_role_with_own_city(tmp_path):
    """`add_staff` писал город NULL — «все города»: привязанный выдавал второму аккаунту доступ
    ко всем городам. Новый человек получает город выдающего."""
    code = _bound_manager(tmp_path)
    cb = FakeCallback(f"roles_addrole:{NEWCOMER_ID}:game_manager", user_id=MANAGER_ID)
    asyncio.run(admin_roles.roles_assign(cb, bot=None))
    assert asyncio.run(db.get_staff_city(NEWCOMER_ID)) == code
    assert cb.answers[0][1] is True and "как у вас" in cb.answers[0][0]


def test_bound_manager_grant_writes_city_in_the_same_row(tmp_path):
    """Fail-closed: город лежит в самой строке роли, а не ставится вторым вызовом после неё."""
    code = _bound_manager(tmp_path)
    asyncio.run(admin_roles.roles_assign(
        FakeCallback(f"roles_addrole:{NEWCOMER_ID}:game_manager", user_id=MANAGER_ID), bot=None,
    ))
    rows = [r for r in asyncio.run(db.list_staff()) if r["telegram_id"] == NEWCOMER_ID]
    assert [r["city"] for r in rows] == [code]


def test_bound_manager_cannot_grant_to_all_cities_person(tmp_path):
    """У человека уже есть роль на все города (city NULL) — новая роль от городского менеджера
    действовала бы во всех городах. Выдаёт только суперадмин или менеджер без города."""
    _bound_manager(tmp_path)
    asyncio.run(db.add_staff(NEWCOMER_ID, "game_manager", ADMIN_ID))
    cb = FakeCallback(f"roles_addrole:{NEWCOMER_ID}:reg_manager", user_id=MANAGER_ID)
    asyncio.run(admin_roles.roles_assign(cb, bot=None))
    assert cb.answers == [(admin_roles.ROLES_ALL_CITIES_TARGET_TEXT, True)]
    assert sorted(asyncio.run(db.get_staff_roles(NEWCOMER_ID))) == ["game_manager"]


def test_bound_manager_cannot_grant_to_other_city_person_but_can_to_own(tmp_path):
    code = _bound_manager(tmp_path)
    other = next(c for c in cities.city_codes() if c != code)
    asyncio.run(db.add_staff(NEWCOMER_ID, "game_manager", ADMIN_ID))
    asyncio.run(db.set_staff_city(NEWCOMER_ID, other))
    cb = FakeCallback(f"roles_addrole:{NEWCOMER_ID}:reg_manager", user_id=MANAGER_ID)
    asyncio.run(admin_roles.roles_assign(cb, bot=None))
    assert cb.answers and cb.answers[0][1] is True and "работает в городе" in cb.answers[0][0]
    assert sorted(asyncio.run(db.get_staff_roles(NEWCOMER_ID))) == ["game_manager"]

    asyncio.run(db.set_staff_city(NEWCOMER_ID, code))
    cb = FakeCallback(f"roles_addrole:{NEWCOMER_ID}:reg_manager", user_id=MANAGER_ID)
    asyncio.run(admin_roles.roles_assign(cb, bot=None))
    assert sorted(asyncio.run(db.get_staff_roles(NEWCOMER_ID))) == ["game_manager", "reg_manager"]
    assert {r["city"] for r in asyncio.run(db.list_staff()) if r["telegram_id"] == NEWCOMER_ID} == {code}


def test_unbound_manager_grant_stays_all_cities(tmp_path):
    _bound_manager(tmp_path, bound=False)
    cb = FakeCallback(f"roles_addrole:{NEWCOMER_ID}:game_manager", user_id=MANAGER_ID)
    asyncio.run(admin_roles.roles_assign(cb, bot=None))
    assert asyncio.run(db.get_staff_city(NEWCOMER_ID)) is None


class _ForwardMessage:
    def __init__(self, sender):
        self.text = None
        self.forward_origin = SimpleNamespace(sender_user=sender)
        self.from_user = SimpleNamespace(id=ADMIN_ID)
        self.answers = []

    async def answer(self, text=None, **kwargs):
        self.answers.append(text)


def _state(uid):
    return FSMContext(storage=MemoryStorage(), key=StorageKey(bot_id=1, chat_id=uid, user_id=uid))


def test_name_from_forward_is_kept_for_roles_list(tmp_path):
    """Менеджер без анкеты и /start был в списке ролей «id 1315109411»: имя из пересылки при
    выдаче роли не сохранялось. Теперь сохраняется и не стирается чисткой истории чата."""
    _roles_ready(tmp_path)
    assert asyncio.run(person_label(NEWCOMER_ID)) == f"id {NEWCOMER_ID}"
    sender = SimpleNamespace(id=NEWCOMER_ID, username="newbie", full_name="Новый Менеджер")
    msg = _ForwardMessage(sender)
    asyncio.run(admin_roles.roles_add_person(msg, _state(ADMIN_ID)))
    assert "Новый Менеджер (@newbie)" in msg.answers[-1]

    asyncio.run(db.add_staff(NEWCOMER_ID, "game_manager", ADMIN_ID))
    asyncio.run(db.prune_chat_history("2999-01-01 00:00:00"))
    assert asyncio.run(person_label(NEWCOMER_ID)) == "Новый Менеджер (@newbie)"


def test_role_keys_through_generic_settings_editor_are_common_and_denied(tmp_path):
    """Обходной путь: «🔎 Найти настройку» → общий редактор/список с ключом role_caps_* или
    role_*_enabled. Ключи ролей — общие, запись упирается в тот же гейт, что у остальных общих."""
    from handlers.settings import admin_settings_global as gscope

    _bound_manager(tmp_path)
    for key in ("role_caps_reg_manager", "role_reg_manager_enabled"):
        assert asyncio.run(gscope.common_write_denied(MANAGER_ID, key)) is True
    assert asyncio.run(gscope.common_write_denied(ADMIN_ID, "role_caps_reg_manager")) is False
