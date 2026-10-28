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
