"""Phase 15 (15-02, D-19): экран «⚙️ Настройки → 📊 Дашборд» — десять чекбоксов блоков.

pytest-asyncio недоступен в этом окружении (см. tests/test_db_phase5.py) — каждый async
хелпер гоняется через asyncio.run(), config.DB_PATH указывает на файл в tmp_path.

Task 1: реестр — десять ключей dashboard_block_* в SETTINGS_SCHEMA, вне SETTINGS_FIELDS,
дефолты читаются через get_setting_typed на пустой БД.

Task 2: экран handlers/admin_dashboard.py — рендер, тумблер по одному ключу, права в
ADMIN_CAPS, регресс «бот для людей» (сырой код ключа не попадает в текст/подписи кнопок).
"""
import asyncio

from config import config
from database import db
from settings_schema import SETTINGS_SCHEMA, get_setting_typed
from handlers import admin_settings
from handlers.admin_caps import ADMIN_CAPS, required_capability


ADMIN_ID = 900901


def _admin_ready(tmp_path):
    config.DB_PATH = str(tmp_path / "test_dashboard_settings_ui.db")
    asyncio.run(db.init_db())
    config.ADMIN_IDS = [ADMIN_ID]


DASHBOARD_KEYS_EXPECTED = [
    "dashboard_block_funnel",
    "dashboard_block_dynamics",
    "dashboard_block_universities",
    "dashboard_block_sources",
    "dashboard_block_courses",
    "dashboard_block_study_fields",
    "dashboard_block_dropout",
    "dashboard_block_utm",
    "dashboard_block_months",
    "dashboard_block_game",
]


# ── Task 1: реестр ───────────────────────────────────────────────────────────────────────

def test_exactly_ten_dashboard_block_keys():
    keys = [k for k in SETTINGS_SCHEMA if k.startswith("dashboard_block_")]
    assert sorted(keys) == sorted(DASHBOARD_KEYS_EXPECTED)


def test_dashboard_block_keys_shape():
    for key in DASHBOARD_KEYS_EXPECTED:
        entry = SETTINGS_SCHEMA[key]
        assert entry["type"] == "enum", key
        assert entry["options"] == ["on", "off"], key
        assert entry["prompt"] is None, key
        assert entry["group"] == "dashboard", key
        assert entry.get("label"), key


def test_dashboard_block_keys_not_in_settings_fields():
    field_keys = {k for k, _, _ in admin_settings.SETTINGS_FIELDS}
    for key in DASHBOARD_KEYS_EXPECTED:
        assert key not in field_keys, key


def test_dashboard_block_defaults_on_empty_db(tmp_path):
    _admin_ready(tmp_path)

    async def _read_all():
        return {key: await get_setting_typed(key) for key in DASHBOARD_KEYS_EXPECTED}

    values = asyncio.run(_read_all())
    for key, value in values.items():
        expected = "off" if key == "dashboard_block_game" else "on"
        assert value == expected, key


# ── Task 2: экран с чекбоксами ───────────────────────────────────────────────────────────

class FakeUser:
    def __init__(self, uid):
        self.id = uid


class FakeMessage:
    def __init__(self):
        self.text = None
        self.markup = None
        self.edit_calls = 0

    async def edit_text(self, text, parse_mode=None, reply_markup=None):
        self.text = text
        self.markup = reply_markup
        self.edit_calls += 1


class FakeCallback:
    def __init__(self, data, user_id=ADMIN_ID):
        self.data = data
        self.from_user = FakeUser(user_id)
        self.message = FakeMessage()
        self.answers = []

    async def answer(self, text=None, show_alert=False):
        self.answers.append((text, show_alert))


def _flat_callback_data(kb):
    return [btn.callback_data for row in kb.inline_keyboard for btn in row]


def test_dashboard_settings_screen_shows_eight_buttons_in_order(tmp_path):
    _admin_ready(tmp_path)
    from handlers import admin_dashboard

    kb = asyncio.run(admin_dashboard.build_dashboard_settings_keyboard())
    data = _flat_callback_data(kb)
    block_buttons = [d for d in data if d.startswith("dash_block:")]
    expected_order = [f"dash_block:{k[len('dashboard_block_'):]}" for k in admin_dashboard.DASHBOARD_BLOCKS]
    assert block_buttons == expected_order
    # Phase 20 (20-03): «Назад» ведёт в раздел-владелец экрана («📊 Данные»), а не в корень
    # настроек; цель считает section_of по SECTIONS.
    assert "admin_sec:data" in data


def test_dashboard_settings_open_screen_handler(tmp_path):
    _admin_ready(tmp_path)
    from handlers import admin_dashboard

    callback = FakeCallback("admin_dashboard_settings")
    asyncio.run(admin_dashboard.open_dashboard_settings(callback))
    assert callback.message.edit_calls == 1
    assert "dashboard_block_" not in callback.message.text


def test_dashboard_block_toggle_flips_only_its_own_key(tmp_path):
    _admin_ready(tmp_path)
    from handlers import admin_dashboard

    callback = FakeCallback("dash_block:funnel")
    asyncio.run(admin_dashboard.toggle_dashboard_block(callback))

    async def _read_all():
        return {key: await get_setting_typed(key) for key in DASHBOARD_KEYS_EXPECTED}

    values = asyncio.run(_read_all())
    assert values["dashboard_block_funnel"] == "off"
    for key in DASHBOARD_KEYS_EXPECTED:
        if key == "dashboard_block_funnel":
            continue
        expected = "off" if key == "dashboard_block_game" else "on"
        assert values[key] == expected, key
    assert callback.message.edit_calls == 1
    assert callback.answers, "тост не отправлен"


def test_dashboard_block_toggle_unknown_suffix_does_not_write(tmp_path):
    _admin_ready(tmp_path)
    from handlers import admin_dashboard

    callback = FakeCallback("dash_block:unknown_suffix")
    asyncio.run(admin_dashboard.toggle_dashboard_block(callback))

    assert callback.message.edit_calls == 0
    assert callback.answers and callback.answers[0][0] == "Неизвестная кнопка"

    async def _read_all():
        return {key: await get_setting_typed(key) for key in DASHBOARD_KEYS_EXPECTED}

    values = asyncio.run(_read_all())
    for key in DASHBOARD_KEYS_EXPECTED:
        expected = "off" if key == "dashboard_block_game" else "on"
        assert values[key] == expected, key


def test_dashboard_settings_text_and_labels_have_no_raw_keys(tmp_path):
    _admin_ready(tmp_path)
    from handlers import admin_dashboard

    text = asyncio.run(admin_dashboard.render_dashboard_settings_text())
    assert "dashboard_block_" not in text

    kb = asyncio.run(admin_dashboard.build_dashboard_settings_keyboard())
    for row in kb.inline_keyboard:
        for btn in row:
            assert "dashboard_block_" not in btn.text


def test_dashboard_settings_callbacks_registered_in_admin_caps():
    assert ADMIN_CAPS.get("admin_dashboard_settings") == "settings"
    assert ADMIN_CAPS.get("dash_block:*") == "settings"
    assert required_capability(callback_data="admin_dashboard_settings") == "settings"
    assert required_capability(callback_data="dash_block:funnel") == "settings"
