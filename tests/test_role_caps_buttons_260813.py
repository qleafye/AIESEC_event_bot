"""Quick 260813: права роли выдаются чекбоксами, а не вводом кодов текстом.

До этого «✏️ Права роли» вела в generic settings_edit и просила НАБРАТЬ «moderate_reg;settings»
руками — прямое нарушение главного правила проекта (CLAUDE.md: бот для людей, не для прогеров).
Хранение осталось прежним (role_caps_<role>, type:"list"), поэтому тесты проверяют и запись.

Конвенции — как в tests/test_sheets_main_tab_pin_260813.py: plain def test_*, asyncio.run(go()),
tmp_path под БД, дублей-фейков ровно столько, сколько нужно.
"""
import asyncio

from config import config
from database import db
from database.db import set_setting
from settings_schema import get_setting_typed
from handlers import admin as admin_mod
from handlers import admin_caps
from handlers.admin_caps import ALL_CAPABILITIES, role_caps_key

ROLE = "reg_manager"


class _FakeUser:
    def __init__(self, uid):
        self.id = uid


class _FakeMessage:
    def __init__(self):
        self.text = None
        self.markup = None

    async def edit_text(self, text, parse_mode=None, reply_markup=None):
        self.text = text
        self.markup = reply_markup


class _FakeCallback:
    def __init__(self, data):
        self.data = data
        self.from_user = _FakeUser(900813)
        self.message = _FakeMessage()
        self.answers = []

    async def answer(self, text=None, show_alert=False):
        self.answers.append((text, show_alert))


def _ready(tmp_path):
    config.DB_PATH = str(tmp_path / "test_forum.db")
    asyncio.run(db.init_db())


def _labels(markup):
    return [b.text for row in markup.inline_keyboard for b in row]


def _callbacks(markup):
    return [b.callback_data for row in markup.inline_keyboard for b in row]


def test_caps_screen_shows_a_checkbox_per_capability(tmp_path):
    _ready(tmp_path)
    callback = _FakeCallback(f"roles_caps:{ROLE}")

    async def go():
        await set_setting(role_caps_key(ROLE), "moderate_reg")
        await admin_mod.show_role_caps(callback)

    asyncio.run(go())

    labels = _labels(callback.message.markup)
    # Одна кнопка на право + «назад»; отмеченное — с галкой, остальные — с пустым квадратом.
    assert len(labels) == len(ALL_CAPABILITIES) + 1
    assert any(l.startswith("✅") and "Модерация заявок" in l for l in labels)
    assert any(l.startswith("☐") and "Рассылки" in l for l in labels)
    # Кодов capability человеку не показываем — это и была исходная проблема.
    assert not any(cap in " ".join(labels) for cap in ALL_CAPABILITIES)


def test_toggle_adds_and_removes_capability(tmp_path):
    _ready(tmp_path)

    async def go():
        await set_setting(role_caps_key(ROLE), "moderate_reg")
        await admin_mod.toggle_role_cap(_FakeCallback(f"roles_cap:{ROLE}:broadcast"))
        after_add = await get_setting_typed(role_caps_key(ROLE))
        await admin_mod.toggle_role_cap(_FakeCallback(f"roles_cap:{ROLE}:moderate_reg"))
        after_remove = await get_setting_typed(role_caps_key(ROLE))
        return after_add, after_remove

    after_add, after_remove = asyncio.run(go())
    assert set(after_add) == {"moderate_reg", "broadcast"}
    assert after_remove == ["broadcast"]


def test_toggle_keeps_declaration_order(tmp_path):
    """Порядок прав фиксирован ALL_CAPABILITIES, а не порядком нажатий — иначе строка прав в
    списке ролей прыгает при каждой перерисовке."""
    _ready(tmp_path)

    async def go():
        await set_setting(role_caps_key(ROLE), "stats")
        await admin_mod.toggle_role_cap(_FakeCallback(f"roles_cap:{ROLE}:moderate_reg"))
        return await get_setting_typed(role_caps_key(ROLE))

    assert asyncio.run(go()) == ["moderate_reg", "stats"]


def test_unchecking_everything_really_means_no_rights(tmp_path):
    """Пустая строка в role_caps_* вернула бы ПРАВА ПО УМОЛЧАНИЮ (_parse_setting отдаёт default
    на falsy raw) — то есть снятие последней галки молча вернуло бы роли доступ. Сентинел это
    ломает: он не входит в ALL_CAPABILITIES, значит прав ноль."""
    _ready(tmp_path)

    async def go():
        await set_setting(role_caps_key(ROLE), "moderate_reg")
        await admin_mod.toggle_role_cap(_FakeCallback(f"roles_cap:{ROLE}:moderate_reg"))
        raw = await get_setting_typed(role_caps_key(ROLE))
        return raw, admin_mod._known_caps(raw)

    raw, known = asyncio.run(go())
    assert known == []
    assert not set(raw) & set(ALL_CAPABILITIES)


def test_caps_screen_survives_garbage_from_the_old_text_input(tmp_path):
    """В базе может лежать что угодно, что менеджер набрал руками до этого фикса."""
    _ready(tmp_path)
    callback = _FakeCallback(f"roles_caps:{ROLE}")

    async def go():
        await set_setting(role_caps_key(ROLE), "moderate_reg\nмодерация чеков\n;;")
        await admin_mod.show_role_caps(callback)

    asyncio.run(go())
    labels = _labels(callback.message.markup)
    assert sum(1 for l in labels if l.startswith("✅")) == 1
    assert "модерация чеков" not in callback.message.text


def test_unknown_role_and_capability_are_rejected(tmp_path):
    _ready(tmp_path)
    bad_role = _FakeCallback("roles_caps:nope")
    bad_cap = _FakeCallback(f"roles_cap:{ROLE}:take_over_the_world")

    async def go():
        await admin_mod.show_role_caps(bad_role)
        await admin_mod.toggle_role_cap(bad_cap)

    asyncio.run(go())
    assert bad_role.message.text is None
    assert bad_cap.message.text is None
    assert bad_role.answers[0][1] is True  # show_alert, не тишина
    assert bad_cap.answers[0][1] is True


def test_roles_screen_points_at_the_button_screen_not_the_text_editor(tmp_path):
    _ready(tmp_path)

    async def go():
        return await admin_mod.build_roles_keyboard()

    callbacks = _callbacks(asyncio.run(go()))
    assert f"roles_caps:{ROLE}" in callbacks
    assert not any(c.startswith("settings_edit:role_caps_") for c in callbacks)


def test_new_callbacks_are_capability_mapped():
    caps = admin_caps.ADMIN_CAPS
    assert caps["roles_caps:*"] == caps["admin_roles"]
    assert caps["roles_cap:*"] == caps["admin_roles"]
