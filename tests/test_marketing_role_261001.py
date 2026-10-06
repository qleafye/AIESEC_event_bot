"""Роль «📣 Маркетинг (метки)» (`marketing_manager`, право `source_links`).

Запрос РилТолка: маркетологу нужно ставить свои метки на ссылки и смотреть статистику по ним,
полную админку давать не хотим. Фиксирует:
- роль выдаётся с экрана «👥 Роли и доступы» и даёт ровно `source_links`;
- держатель видит в /admin только «📊 Данные» с двумя строками про метки;
- экран «🔗 Ссылки с метками»: метка -> число анкет, без имён и контактов; мастер новой ссылки;
- `/create_link` и «📈 Источники» ему открыты (кортеж «любое из» в ADMIN_CAPS), а заявки,
  рассылки, настройки, выгрузки — нет; прежние держатели moderate_reg/stats доступа не теряют;
- в Mini App роль ничего не открывает.
"""
import asyncio
import sqlite3
from types import SimpleNamespace

from aiogram.dispatcher.event.bases import UNHANDLED

from config import config
from database import db
from tests._dbtpl import fast_init_db
from tests.test_roles_phase8 import (
    FakeBot,
    _fresh_state,
    dispatch_callback,
    dispatch_message,
)

ADMIN_ID = 900801
MKT_ID = 901101
REG_ID = 901102
STATS_ID = 901103
ROLE = "marketing_manager"


class _LinkBot(FakeBot):
    """`me()` — для фильтра Command у aiogram, `get_me()` — для самой ссылки."""

    async def me(self):
        return SimpleNamespace(id=1, username="rt_test_bot")

    async def get_me(self):
        return await self.me()


def _ready(tmp_path):
    config.DB_PATH = str(tmp_path / "test_marketing_role.db")
    fast_init_db()
    config.ADMIN_IDS = [ADMIN_ID]


def _add_users(rows):
    conn = sqlite3.connect(config.DB_PATH)
    conn.executemany(
        "INSERT INTO users (telegram_id, full_name, email, phone, source) VALUES (?, ?, ?, ?, ?)",
        rows,
    )
    conn.commit()
    conn.close()


def _command_passes(text, uid):
    """Фильтр Command у aiogram требует настоящий `Message` (isinstance), поэтому команды
    через `dispatch_message` не доходят. Решение о доступе принимает CapabilityMiddleware —
    гоняем через неё сам текст команды: (пропущено ли до хендлера, ответы человеку)."""
    from handlers.admin_caps import CapabilityMiddleware
    from tests.test_roles_phase8 import FakeMessage, FakeUser

    called = []
    event = FakeMessage(text=text, user_id=uid, chat_id=uid)

    async def _handler(_event, _data):
        called.append(True)

    data = {"event_from_user": FakeUser(uid), "raw_state": None}
    asyncio.run(CapabilityMiddleware()(_handler, event, data))
    return bool(called), event.answers


def _run_command(handler, text, uid):
    from tests.test_roles_phase8 import FakeMessage

    message = FakeMessage(text=text, user_id=uid, chat_id=uid)
    asyncio.run(handler(message, _LinkBot()))
    return message.answers[-1][0]


def _flat(kb):
    return [b.callback_data for row in kb.inline_keyboard for b in row]


# ── модель прав ─────────────────────────────────────────────────────────────────────────

def test_holds_understands_any_of_tuple():
    from handlers.admin_caps import _holds, ANY_CAPABILITY

    assert _holds({"stats"}, ("stats", "source_links"))
    assert _holds({"source_links"}, ("stats", "source_links"))
    assert not _holds({"moderate_reg"}, ("stats", "source_links"))
    assert not _holds(set(), ("stats", "source_links"))
    assert _holds({"source_links"}, ANY_CAPABILITY)
    assert _holds({"stats"}, "stats") and not _holds({"stats"}, "settings")


def test_role_grants_exactly_source_links(tmp_path):
    _ready(tmp_path)
    from handlers.admin_caps import resolve_capabilities

    asyncio.run(db.add_staff(MKT_ID, ROLE, ADMIN_ID))
    assert asyncio.run(resolve_capabilities(MKT_ID)) == {"source_links"}
    asyncio.run(db.set_setting("role_marketing_manager_enabled", "off"))
    assert asyncio.run(resolve_capabilities(MKT_ID)) == set()


def test_role_is_assigned_and_removed_from_roles_screen(tmp_path):
    _ready(tmp_path)
    result, event = dispatch_message(str(MKT_ID), ADMIN_ID, raw_state="StaffAdd:waiting_for_person")
    assert result is not UNHANDLED
    picker = _flat(event.answers[-1][2])
    assert f"roles_addrole:{MKT_ID}:{ROLE}" in picker

    result, _ = dispatch_callback(f"roles_addrole:{MKT_ID}:{ROLE}", ADMIN_ID)
    assert result is not UNHANDLED
    assert ROLE in asyncio.run(db.get_staff_roles(MKT_ID))

    from handlers import admin_roles
    text = asyncio.run(admin_roles.render_roles_text())
    assert "📣 Маркетинг (метки)" in text and "🔗 Ссылки с метками" in text
    kb = _flat(asyncio.run(admin_roles.build_roles_keyboard(ADMIN_ID)))
    assert f"roles_toggle:{ROLE}" in kb and f"roles_caps:{ROLE}" in kb
    assert f"roles_del:{MKT_ID}:{ROLE}" in kb

    result, _ = dispatch_callback(f"roles_del_ok:{MKT_ID}:{ROLE}", ADMIN_ID)
    assert result is not UNHANDLED
    assert ROLE not in asyncio.run(db.get_staff_roles(MKT_ID))


def test_role_caps_checkbox_screen_lists_the_new_right(tmp_path):
    _ready(tmp_path)
    result, event = dispatch_callback(f"roles_caps:{ROLE}", ADMIN_ID)
    assert result is not UNHANDLED
    texts = [b.text for row in event.message.markup.inline_keyboard for b in row]
    assert "✅ 🔗 Ссылки с метками" in texts


def test_settings_guide_describes_role_without_codes():
    from handlers.admin_roles import SETTINGS_GUIDE_KEYS, SETTINGS_GUIDE_SECTIONS, _render_settings_guide

    assert {"role_caps_marketing_manager", "role_marketing_manager_enabled"} <= set(SETTINGS_GUIDE_KEYS)
    text = "\n".join(_render_settings_guide(SETTINGS_GUIDE_SECTIONS, {k: None for k in SETTINGS_GUIDE_KEYS}))
    assert "Маркетинг (метки)" in text
    assert ROLE not in text


# ── что видит держатель ─────────────────────────────────────────────────────────────────

def test_holder_sees_only_link_rows():
    from handlers.admin_core import _visible_menu_rows
    from handlers.admin_sections import visible_rows, visible_sections

    caps = {"source_links"}
    assert visible_sections(caps, False) == [("data", "📊 Данные")]
    assert [r[1] for r in visible_rows("data", caps, False)] == ["admin_source_stats", "admin_source_links"]
    assert _visible_menu_rows(caps) == [("📈 Источники", "admin_source_stats"),
                                        ("🔗 Ссылки с метками", "admin_source_links")]


def _admin_help(uid):
    from handlers import admin as admin_mod
    from tests.test_roles_phase8 import FakeMessage

    message = FakeMessage(text="/admin", user_id=uid, chat_id=uid)
    asyncio.run(admin_mod.cmd_admin_help(message, _fresh_state(uid)))
    return message.answers[-1]


def test_admin_command_for_holder_lists_only_create_link(tmp_path):
    _ready(tmp_path)
    asyncio.run(db.add_staff(MKT_ID, ROLE, ADMIN_ID))
    assert _command_passes("/admin", MKT_ID)[0]
    text, _pm, kb = _admin_help(MKT_ID)
    assert "/create_link" in text
    for cmd in ("/stats", "/export", "/broadcast", "/find", "/coins", "/settings_guide"):
        assert cmd not in text, cmd
    assert _flat(kb) == ["admin_sec:data"]


def test_admin_command_for_superadmin_keeps_every_command(tmp_path):
    _ready(tmp_path)
    text = _admin_help(ADMIN_ID)[0]
    for cmd in ("/stats ", "/stats_monthly", "/create_link", "/export", "/broadcast", "/find",
                "/coins", "/scheduled", "/refresh_allowlist", "/settings_guide"):
        assert cmd in text, cmd


def test_links_screen_shows_tags_and_counts_without_people(tmp_path):
    _ready(tmp_path)
    asyncio.run(db.add_staff(MKT_ID, ROLE, ADMIN_ID))
    _add_users([
        (1, "Иван Секретов", "ivan@example.com", "+79990000001", "vk_poster"),
        (2, "Мария Тайная", "maria@example.com", "+79990000002", "vk_poster"),
        (3, "Пётр Скрытый", "petr@example.com", "+79990000003", "ВК"),
    ])
    result, event = dispatch_callback("admin_source_links", MKT_ID)
    assert result is not UNHANDLED
    text = event.message.text
    assert "🔗 <b>Ссылки с метками</b>" in text
    assert "• vk_poster — 2" in text
    assert "• ВК — 1" in text
    for secret in ("Иван", "Мария", "Пётр", "@example.com", "+7999"):
        assert secret not in text
    assert _flat(event.message.markup) == ["srclink_new", "admin_sec:data"]


def test_new_link_wizard_explains_errors_and_returns_link(tmp_path):
    _ready(tmp_path)
    asyncio.run(db.add_staff(MKT_ID, ROLE, ADMIN_ID))
    state = _fresh_state(MKT_ID)
    result, event = dispatch_callback("srclink_new", MKT_ID, state=state)
    assert result is not UNHANDLED
    assert "vk_poster" in event.message.text
    raw_state = asyncio.run(state.get_state())
    assert raw_state == "SourceLinkCreate:waiting_for_tag"

    bot = _LinkBot()
    result, event = dispatch_message("афиша вк", MKT_ID, raw_state=raw_state, state=state, bot=bot)
    assert result is not UNHANDLED
    reply = event.answers[-1][0]
    assert "латинские буквы" in reply and "<code>vk_poster</code>" in reply
    assert asyncio.run(state.get_state()) == raw_state  # ошибка не выкидывает из мастера

    result, event = dispatch_message("<vk_poster>", MKT_ID, raw_state=raw_state, state=state, bot=bot)
    assert result is not UNHANDLED
    reply, _pm, kb = event.answers[-1]
    assert "<code>https://t.me/rt_test_bot?start=src_vk_poster</code>" in reply
    assert asyncio.run(state.get_state()) is None
    assert _flat(kb) == ["srclink_new", "admin_source_links"]


def test_new_link_wizard_cancel_returns_to_screen(tmp_path):
    _ready(tmp_path)
    asyncio.run(db.add_staff(MKT_ID, ROLE, ADMIN_ID))
    state = _fresh_state(MKT_ID)
    dispatch_callback("srclink_new", MKT_ID, state=state)
    result, event = dispatch_callback("srclink_cancel", MKT_ID, state=state)
    assert result is not UNHANDLED
    assert asyncio.run(state.get_state()) is None
    assert "Ссылки с метками" in event.message.text


def test_holder_can_use_create_link_and_source_stats(tmp_path):
    _ready(tmp_path)
    asyncio.run(db.add_staff(MKT_ID, ROLE, ADMIN_ID))
    from handlers.admin import cmd_create_link

    assert _command_passes("/create_link tg_channel", MKT_ID) == (True, [])
    assert "start=src_tg_channel" in _run_command(cmd_create_link, "/create_link tg_channel", MKT_ID)

    result, event = dispatch_callback("admin_source_stats", MKT_ID)
    assert result is not UNHANDLED
    assert "Источники регистраций" in event.message.text


def test_holder_is_denied_everything_else(tmp_path):
    _ready(tmp_path)
    asyncio.run(db.add_staff(MKT_ID, ROLE, ADMIN_ID))
    for cb in ("admin_applications", "admin_app_list", "admin_broadcast", "admin_settings",
               "admin_stats", "admin_export_csv", "admin_roles", "admin_questions"):
        result, event = dispatch_callback(cb, MKT_ID)
        assert result is None, cb  # известный сотрудник: тост «Недостаточно прав», событие съедено
        assert event.answers == [("Недостаточно прав", True)], cb
    for cmd in ("/stats", "/export", "/broadcast", "/find @someone", "/settings_guide", "/coins"):
        assert _command_passes(cmd, MKT_ID) == (False, [("Недостаточно прав.", None, None)]), cmd


def test_existing_holders_keep_access(tmp_path):
    _ready(tmp_path)
    asyncio.run(db.add_staff(REG_ID, "reg_manager", ADMIN_ID))
    asyncio.run(db.add_staff(STATS_ID, "stats_manager", ADMIN_ID))
    assert _command_passes("/create_link vk_poster", REG_ID) == (True, [])
    result, event = dispatch_callback("admin_source_stats", STATS_ID)
    assert result is not UNHANDLED and "Источники регистраций" in event.message.text

    # Новый экран — только у держателей source_links (и суперадмина), остальным строка не мешает.
    result, event = dispatch_callback("admin_source_links", STATS_ID)
    assert event.answers == [("Недостаточно прав", True)]
    result, event = dispatch_callback("admin_source_links", ADMIN_ID)
    assert result is not UNHANDLED and "Ссылки с метками" in event.message.text
    # stats_manager по-прежнему не делает ссылки, reg_manager не видит «Источники».
    assert _command_passes("/create_link x", STATS_ID) == (False, [("Недостаточно прав.", None, None)])
    _r, event = dispatch_callback("admin_source_stats", REG_ID)
    assert event.answers == [("Недостаточно прав", True)]


# ── Mini App и дашборд ──────────────────────────────────────────────────────────────────

def test_miniapp_drops_bot_only_right(tmp_path):
    from dashboard import access as dash_access
    from dashboard import db as dash_db
    from miniapp.deps import BOT_ONLY_CAPS

    _ready(tmp_path)
    asyncio.run(db.add_staff(MKT_ID, ROLE, ADMIN_ID))
    with dash_db.read_conn(config.DB_PATH) as conn:
        caps = dash_access.resolve_capabilities(conn, MKT_ID, (ADMIN_ID,))
        assert caps == {"source_links"}
        assert frozenset(caps) - BOT_ONLY_CAPS == frozenset()  # в Mini App — обычный делегат
        assert dash_access.has_stats(conn, MKT_ID, (ADMIN_ID,)) is False


def test_miniapp_principal_of_holder_is_not_staff(tmp_path):
    import pytest
    from fastapi import HTTPException

    from miniapp import deps

    _ready(tmp_path)
    asyncio.run(db.add_staff(MKT_ID, ROLE, ADMIN_ID))
    request = SimpleNamespace(
        app=SimpleNamespace(state=SimpleNamespace(cfg=SimpleNamespace(
            db_path=config.DB_PATH, admin_ids=(ADMIN_ID,), bot_token="x"))),
        scope={"session": {}}, session={"telegram_id": MKT_ID}, method="GET", headers={},
        state=SimpleNamespace(),
    )
    with pytest.raises(HTTPException) as exc:  # cookie-вход дашборда — только сотрудникам
        asyncio.run(deps.principal(request, None))
    assert exc.value.status_code == 403
