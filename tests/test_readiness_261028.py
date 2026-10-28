"""«✅ Готовность к форуму» — чек-лист ненастроенного в корне /admin и напоминание менеджерам.

Зачем: 23.10 дату закрытия регистрации Москвы никто не поставил — 57 лишних заявок; 14.10 форма
делегаций была подключена при выключенных делегациях — 11 автоотказов. Бот должен сам
показывать такие полувключённые состояния.

Покрывает: реестр пунктов и их проверки, отметку «🙈 Не нужно», счётчик по шапке города,
ряд в корне /admin, экран с кнопками, ежедневное напоминание и его регистрацию в планировщике.

pytest-asyncio в проекте нет — async через asyncio.run(); БД — tmp_path.
"""
import asyncio
from datetime import timedelta

import pytest

import domain.cities as cities
from config import config
from database import db
from database import ext_forms_db as ef
from database import shop_db
from handlers.access.admin_caps import role_caps_key, role_enabled_key
from services.forum import readiness as rd
from services.infra.timeutil import msk_now
from tests._dbtpl import fast_init_db

ADMIN_ID = 954401
MANAGER_ID = 954402
VOLUNTEER_ID = 954403
OTHER_ADMIN = 954404

TODAY = msk_now().date()


@pytest.fixture(autouse=True)
def _today_by_moscow():
    global TODAY
    TODAY = msk_now().date()


def _ready(tmp_path):
    config.DB_PATH = str(tmp_path / "test_readiness.db")
    fast_init_db()
    config.ADMIN_IDS = [ADMIN_ID]


def _set(key, value):
    asyncio.run(db.set_setting(key, value))


def _statuses(city):
    return {item.id: st for item, st in asyncio.run(rd.item_statuses(city))}


def _cities_on():
    _set("event_city_enabled", "on")


def _day(offset: int) -> str:
    return (TODAY + timedelta(days=offset)).strftime("%d.%m.%Y")


# ══ реестр и проверки ══════════════════════════════════════════════════════════════════════

def test_registry_is_one_declaration_with_fixed_order():
    from handlers.access.admin_caps import required_capability
    import handlers.admin  # noqa: F401 — грузит все швы на общем роутере
    ids = [item.id for item in rd.READINESS_ITEMS]
    assert ids == ["forum_date", "reg_close", "delegations", "scanner", "shop"]
    assert len(set(ids)) == len(ids)
    for item in rd.READINESS_ITEMS:
        assert item.scope in ("city", "global")
        assert required_capability(callback_data=item.fix_callback) is not None, item.id


def test_empty_db_cities_off_only_forum_date_is_todo(tmp_path):
    _ready(tmp_path)
    assert _statuses(None) == {
        "forum_date": "todo", "reg_close": "na", "delegations": "na", "scanner": "na", "shop": "na",
    }
    assert asyncio.run(rd.pending_count(None)) == 1


def test_forum_date_set_turns_ok(tmp_path):
    _ready(tmp_path)
    _set("forum_date", _day(10))
    assert _statuses(None)["forum_date"] == "ok"


def test_reg_close_per_city(tmp_path):
    _ready(tmp_path)
    _cities_on()
    _set("forum_date__city__msk", _day(10))
    st = _statuses("msk")
    assert st["forum_date"] == "ok"
    assert st["reg_close"] == "todo"
    _set("city_reg_close_date__city__msk", _day(5))
    assert _statuses("msk")["reg_close"] == "ok"
    assert _statuses("spb")["reg_close"] == "todo"  # у другого города своя дата


def test_delegations_half_on_form(tmp_path):
    _ready(tmp_path)

    async def _form(status="active"):
        fid = await ef.create_form(platform="yandex", external_id="x" * 24, title="Делегации")
        await ef.upsert_columns(fid, [("q1", "ФИО"), ("q2", "Университет")])
        if status != "active":
            await ef.set_form_status(fid, status)
        return fid

    fid = asyncio.run(_form())
    assert _statuses(None)["delegations"] == "todo"
    _set("delegation_form_id", str(fid))
    _set("delegation_armed_form_id", str(fid))
    assert _statuses(None)["delegations"] == "ok"


def test_delegations_paused_form_is_na(tmp_path):
    _ready(tmp_path)

    async def _form():
        fid = await ef.create_form(platform="yandex", external_id="y" * 24, title="Старая")
        await ef.upsert_columns(fid, [("q2", "Университет")])
        await ef.set_form_status(fid, "paused")

    asyncio.run(_form())
    assert _statuses(None)["delegations"] == "na"


def test_scanner_needs_a_staff_holder(tmp_path):
    _ready(tmp_path)
    _set("checkin_qr_enabled", "on")
    assert _statuses(None)["scanner"] == "todo"  # суперадмин не считается
    _set(role_enabled_key("volunteer"), "on")
    asyncio.run(db.add_staff(VOLUNTEER_ID, "volunteer", ADMIN_ID))
    assert _statuses(None)["scanner"] == "ok"


def test_shop_open_without_items(tmp_path):
    _ready(tmp_path)
    asyncio.run(shop_db.set_open(True))
    assert _statuses(None)["shop"] == "todo"
    asyncio.run(shop_db.add_item("Худи", 100, 5, 1))
    assert _statuses(None)["shop"] == "ok"


def test_skip_and_unskip(tmp_path):
    _ready(tmp_path)
    before = asyncio.run(rd.pending_count(None))
    assert asyncio.run(rd.set_skipped("forum_date", None, True)) is True
    assert _statuses(None)["forum_date"] == "skipped"
    assert asyncio.run(rd.pending_count(None)) == before - 1
    asyncio.run(rd.set_skipped("forum_date", None, False))
    assert _statuses(None)["forum_date"] == "todo"


def test_skip_unknown_item_writes_nothing(tmp_path):
    _ready(tmp_path)
    assert asyncio.run(rd.set_skipped("nope", None, True)) is False
    assert asyncio.run(db.get_setting("ready_skip__nope___")) is None


def test_global_skip_shared_by_cities(tmp_path):
    _ready(tmp_path)
    _cities_on()
    asyncio.run(shop_db.set_open(True))
    asyncio.run(rd.set_skipped("shop", "msk", True))
    assert _statuses("spb")["shop"] == "skipped"


def test_broken_check_is_na(tmp_path, monkeypatch):
    _ready(tmp_path)

    async def boom(city):
        raise RuntimeError("x")

    broken = rd.ReadyItem("forum_date", "t", "s", "city", boom, "settings_edit:forum_date")
    monkeypatch.setattr(rd, "READINESS_ITEMS", (broken,))
    assert asyncio.run(rd.item_statuses(None))[0][1] == "na"


def test_pending_count_for_admin_all_cities(tmp_path):
    _ready(tmp_path)
    _cities_on()
    asyncio.run(shop_db.set_open(True))  # общий пункт — один раз
    assert asyncio.run(cities.set_admin_city(ADMIN_ID, cities.ALL_CITIES))
    n_cities = len(asyncio.run(cities.enabled_cities()))
    # у каждого города нет даты форума и даты закрытия: 2 пункта города
    assert asyncio.run(rd.pending_count_for_admin(ADMIN_ID)) == 2 * n_cities + 1
    assert asyncio.run(cities.set_admin_city(ADMIN_ID, "msk"))
    assert asyncio.run(rd.pending_count_for_admin(ADMIN_ID)) == 3


def test_pending_count_for_admin_fail_soft(tmp_path, monkeypatch):
    _ready(tmp_path)

    async def boom(city, scope=None):
        raise RuntimeError("x")

    monkeypatch.setattr(rd, "pending_count", boom)
    assert asyncio.run(rd.pending_count_for_admin(ADMIN_ID)) == 0


def test_settings_keys_and_synonyms():
    from domain.settings.schema import SETTINGS_SCHEMA
    from domain.settings.synonyms import SETTINGS_SYNONYMS
    en = SETTINGS_SCHEMA["ready_remind_enabled"]
    assert (en["type"], en["group"], en["default"], en["options"]) == ("enum", "system", "on", ["on", "off"])
    days = SETTINGS_SCHEMA["ready_remind_days"]
    assert (days["type"], days["group"], days["default"], days["min"], days["max"]) == ("int", "system", 3, 1, 30)
    assert "Например: 3" in days["prompt"]
    for key in ("ready_remind_enabled", "ready_remind_days"):
        assert len(SETTINGS_SYNONYMS[key]) >= 2
    assert not any(k.startswith("ready_skip") for k in SETTINGS_SCHEMA)


# ══ корень /admin и экран ══════════════════════════════════════════════════════════════════

class FakeUser:
    def __init__(self, uid):
        self.id = uid


class FakeMessage:
    def __init__(self):
        self.text = None
        self.markup = None

    async def edit_text(self, text, parse_mode=None, reply_markup=None):
        self.text = text
        self.markup = reply_markup


class FakeCallback:
    def __init__(self, data, user_id=ADMIN_ID):
        self.data = data
        self.from_user = FakeUser(user_id)
        self.message = FakeMessage()
        self.answers = []

    async def answer(self, text=None, show_alert=False):
        self.answers.append((text, show_alert))


def _rows(kb):
    return [[(b.text, b.callback_data) for b in row] for row in kb.inline_keyboard]


def _flat(kb):
    return [b.callback_data for row in kb.inline_keyboard for b in row]


def _grant(uid, role, caps):
    _set(role_enabled_key(role), "on")
    _set(role_caps_key(role), caps)
    asyncio.run(db.add_staff(uid, role, ADMIN_ID))


def _press(handler, data, user_id=ADMIN_ID):
    cb = FakeCallback(data, user_id)
    asyncio.run(handler(cb))
    return cb


def test_root_first_row_is_readiness_for_superadmin(tmp_path):
    from handlers.settings.admin_core import build_admin_keyboard
    _ready(tmp_path)
    kb = asyncio.run(build_admin_keyboard(ADMIN_ID))
    assert _rows(kb)[0] == [("⚠️ Не настроено: 1 — готовность к форуму", "ready_list")]
    asyncio.run(rd.set_skipped("forum_date", None, True))
    kb = asyncio.run(build_admin_keyboard(ADMIN_ID))
    assert _rows(kb)[0] == [("✅ Готовность к форуму", "ready_list")]


def test_root_without_settings_right_has_no_readiness(tmp_path):
    from handlers.settings.admin_core import build_admin_keyboard
    _ready(tmp_path)
    _grant(MANAGER_ID, "reg_manager", "moderate_reg")
    assert "ready_list" not in _flat(asyncio.run(build_admin_keyboard(MANAGER_ID)))
    assert asyncio.run(build_admin_keyboard(OTHER_ADMIN)).inline_keyboard == []


def test_root_parity_with_cities_off(tmp_path):
    from handlers.settings.admin_core import admin_keyboard_for, build_admin_keyboard
    _ready(tmp_path)
    assert _rows(asyncio.run(admin_keyboard_for(ADMIN_ID))) == _rows(asyncio.run(build_admin_keyboard(ADMIN_ID)))


def test_root_with_cities_puts_readiness_into_header_row(tmp_path):
    from handlers.settings.admin_core import admin_keyboard_for
    _ready(tmp_path)
    _cities_on()
    asyncio.run(db.set_setting("amb_team_selection_enabled", "on"))  # худший случай: 9 разделов
    kb = asyncio.run(admin_keyboard_for(ADMIN_ID))
    assert [b.callback_data for b in kb.inline_keyboard[0]] == ["admin_city_switch", "ready_list"]
    assert len(kb.inline_keyboard) <= 10


def test_ready_list_screen(tmp_path):
    from handlers.settings import admin_readiness as ar
    _ready(tmp_path)
    cb = _press(ar.ready_list, "ready_list")
    assert "Готовность к форуму" in cb.message.text
    assert "⚠️ Не настроено: 1" in cb.message.text
    assert "⚠️ 🗓 Не задана дата начала форума" in cb.message.text
    rows = _rows(cb.message.markup)
    assert [("🛠 Дата форума", "settings_edit:forum_date"), ("🙈 Не нужно", "ready_skip:forum_date")] in rows
    assert rows[-1] == [("← Назад", "admin_menu")]
    assert [("📅 За сколько дней напоминать", "settings_edit:ready_remind_days")] in rows
    assert cb.answers == [(None, False)]


def test_fix_button_hidden_without_right(tmp_path):
    from handlers.settings import admin_readiness as ar
    _ready(tmp_path)
    _grant(MANAGER_ID, "reg_manager", "settings")
    asyncio.run(shop_db.set_open(True))
    cb = _press(ar.ready_list, "ready_list", MANAGER_ID)
    rows = _rows(cb.message.markup)
    assert [("🙈 Не нужно", "ready_skip:shop")] in rows
    assert not any(cbd == "admin_shop" for row in rows for _t, cbd in row)


def test_skip_and_unskip_buttons(tmp_path):
    from handlers.settings import admin_readiness as ar
    _ready(tmp_path)
    cb = _press(ar.ready_skip, "ready_skip:forum_date")
    assert asyncio.run(db.get_setting("ready_skip__forum_date___")) == "1"
    assert "➖ 🗓 Не задана дата начала форума — не нужно" in cb.message.text
    assert [("↩️ Вернуть: Дата форума", "ready_unskip:forum_date")] in _rows(cb.message.markup)
    assert "Всё настроено 👍" in cb.message.text
    _press(ar.ready_unskip, "ready_unskip:forum_date")
    assert _statuses(None)["forum_date"] == "todo"


def test_skip_unknown_id_alerts(tmp_path):
    from handlers.settings import admin_readiness as ar
    _ready(tmp_path)
    cb = _press(ar.ready_skip, "ready_skip:nope")
    assert cb.answers == [("Пункт не найден.", True)]
    assert cb.message.text is None
    assert asyncio.run(db.get_setting("ready_skip__nope___")) is None


def test_all_cities_screen(tmp_path):
    from handlers.settings import admin_readiness as ar
    _ready(tmp_path)
    _cities_on()
    assert asyncio.run(cities.set_admin_city(ADMIN_ID, cities.ALL_CITIES))
    cb = _press(ar.ready_list, "ready_list")
    assert "выберите город" in cb.message.text
    flat = _flat(cb.message.markup)
    for c in asyncio.run(cities.enabled_cities()):
        assert f"ready_city:{c['code']}" in flat
    cb = _press(ar.ready_skip, "ready_skip:reg_close")
    assert cb.answers == [("Сначала выберите город.", True)]
    cb = _press(ar.ready_city, "ready_city:zzz")
    assert cb.answers == [("Город не найден.", True)]
    cb = _press(ar.ready_city, "ready_city:spb")
    assert asyncio.run(cities.admin_selected_city(ADMIN_ID)) == "spb"
    assert "⚠️ Не настроено: 2" in cb.message.text
    assert "spb" not in cb.message.text  # код города человеку не показываем


def test_remind_toggle(tmp_path):
    from handlers.settings import admin_readiness as ar
    _ready(tmp_path)
    cb = _press(ar.ready_remind_toggle, "ready_remind_toggle")
    assert asyncio.run(db.get_setting("ready_remind_enabled")) == "off"
    assert [("🔔 Напоминать за 3 дн.: выкл", "ready_remind_toggle")] in _rows(cb.message.markup)


def test_ready_callbacks_need_settings():
    from handlers.access.admin_caps import required_capability
    import handlers.settings.admin_readiness  # noqa: F401
    for data in ("ready_list", "ready_city:msk", "ready_skip:shop", "ready_unskip:shop", "ready_remind_toggle"):
        assert required_capability(callback_data=data) == "settings", data


# ══ напоминание ════════════════════════════════════════════════════════════════════════════

class FakeBot:
    def __init__(self, fail_for=()):
        self.sent = []
        self.fail_for = set(fail_for)

    async def send_message(self, chat_id, text, parse_mode=None, reply_markup=None, **kw):
        if chat_id in self.fail_for:
            raise RuntimeError("chat not found")
        self.sent.append((chat_id, text, reply_markup))


def _bot(monkeypatch, **kw):
    from services import scheduler as sched
    bot = FakeBot(**kw)
    monkeypatch.setattr(sched, "_bot", bot, raising=False)
    return bot


def _job():
    return asyncio.run(rd.readiness_remind_job())


def _shop_todo():
    asyncio.run(shop_db.set_open(True))


def test_reminder_sends_list_and_once_a_day(tmp_path, monkeypatch):
    _ready(tmp_path)
    bot = _bot(monkeypatch)
    _set("forum_date", _day(2))
    _shop_todo()
    assert _job() == 1
    uid, text, kb = bot.sent[0]
    assert uid == ADMIN_ID
    assert "До форума 2 дн." in text
    assert "Не настроено: 1" in text
    assert "• 🛍 Магазин открыт, а товаров в нём нет" in text
    assert _flat(kb) == ["ready_list"]
    assert asyncio.run(db.get_setting("ready_reminded___")) == TODAY.isoformat()
    assert _job() == 0  # второй раз за день — нет


@pytest.mark.parametrize("offset,expected", [(5, 0), (0, 1), (-1, 0)])
def test_reminder_window(tmp_path, monkeypatch, offset, expected):
    _ready(tmp_path)
    bot = _bot(monkeypatch)
    _set("forum_date", _day(offset))
    _shop_todo()
    assert _job() == expected
    if offset == 0:
        assert "До форума сегодня" in bot.sent[0][1]


def test_reminder_nothing_todo(tmp_path, monkeypatch):
    _ready(tmp_path)
    bot = _bot(monkeypatch)
    _set("forum_date", _day(1))
    _shop_todo()
    asyncio.run(rd.set_skipped("shop", None, True))
    assert _job() == 0
    assert bot.sent == []


def test_reminder_switched_off(tmp_path, monkeypatch):
    _ready(tmp_path)
    _bot(monkeypatch)
    _set("forum_date", _day(1))
    _shop_todo()
    _set("ready_remind_enabled", "off")
    assert _job() == 0


def test_reminder_per_city(tmp_path, monkeypatch):
    _ready(tmp_path)
    _cities_on()
    bot = _bot(monkeypatch)
    _set("forum_date__city__msk", _day(1))
    assert _job() == 1  # у msk не задана дата закрытия регистрации
    text, kb = bot.sent[0][1], bot.sent[0][2]
    assert asyncio.run(cities.city_label("msk")) in text
    assert "msk" not in text
    assert _flat(kb) == ["ready_city:msk"]
    assert asyncio.run(db.get_setting("ready_reminded__spb")) is None


def test_reminder_broken_chat_does_not_block_others(tmp_path, monkeypatch):
    _ready(tmp_path)
    config.ADMIN_IDS = [ADMIN_ID, OTHER_ADMIN]
    bot = _bot(monkeypatch, fail_for={ADMIN_ID})
    _set("forum_date", _day(1))
    _shop_todo()
    assert _job() == 1
    assert [s[0] for s in bot.sent] == [OTHER_ADMIN]


def test_reminder_no_forum_dates(tmp_path, monkeypatch):
    _ready(tmp_path)
    _cities_on()
    bot = _bot(monkeypatch)
    assert _job() == 0
    assert bot.sent == []


def test_init_scheduler_registers_readiness_job():
    import inspect
    from services import scheduler as sched
    src = inspect.getsource(sched.init_scheduler)
    assert "readiness_remind_job" in src
    assert "_READY_JOB_ID" in src and rd.JOB_ID == "readiness_remind"
    assert src.index("readiness_remind_job") < src.index("_scheduler.resume()")
