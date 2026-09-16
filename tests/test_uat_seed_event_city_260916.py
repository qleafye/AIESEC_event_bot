"""Квик 260916 (UAT-SEED-05/06): `/uat` засеивал делегата без `event_city`.

Стенд: тестер 1129356315 после «Заявка отправлена — ждёт проверки» имел `event_city` пусто —
недостижимое для реальной анкеты состояние (city fork не пропускает дальше без выбора, см.
`handlers/registration.py::_start_registration_flow`). Из-за пустого `event_city` тестер попал
под общие (не городские) тексты/настройки — при одобрении получил общий `approve_text` вместо
текста своего города, и приёмка сочла это багом одобрения, хотя баг — в сеялке.

Второй сюжет того же квика: засеянные заглушки-ответы («Тестовый университет (приёмка)»)
попадали в топ-8 подсказок справочника ВУЗов Mini App (`services/lookup.py::top_chips`) —
до первой настоящей заявки сезона такая строка легко становится первой подсказкой.

pytest-asyncio недоступен — async через asyncio.run(), по образцу
tests/test_uat_seed_260911.py / tests/test_cities_registry_260818.py."""
import asyncio
from datetime import datetime

from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.fsm.storage.base import StorageKey

import cities
from config import config
from database import db

TESTER_ID = 900930


def _ready(tmp_path, name="test_uat_seed_event_city.db"):
    config.DB_PATH = str(tmp_path / name)
    asyncio.run(db.init_db())
    config.ADMIN_IDS = [900999]


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _open_gate(testers="900930"):
    asyncio.run(db.set_setting("uat_seed_enabled", "on"))
    asyncio.run(db.set_setting("uat_seed_testers", testers))


def _import_handlers():
    from handlers import uat_seed
    return uat_seed


def _snapshot_cities():
    """Восстановитель кэша `cities.CITIES` — по образцу
    tests/test_cities_registry_260818.py::_snapshot_cities, чтобы не протекать в другие
    файлы одной pytest-сессии."""
    saved = list(cities.CITIES)

    def _restore():
        cities.set_cities_for_test(saved)

    return _restore


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
    def __init__(self, data, user_id, username=None):
        self.data = data
        self.from_user = _FakeUser(user_id, username)
        self.message = _FakeEditableMessage()
        self.answer_calls = []

    async def answer(self, text=None, show_alert=False):
        self.answer_calls.append((text, show_alert))


def _fresh_state(user_id):
    storage = MemoryStorage()
    key = StorageKey(bot_id=1, chat_id=user_id, user_id=user_id)
    return FSMContext(storage=storage, key=key)


def _go(uat_seed, state_code, role_code, user_id=TESTER_ID, username=None):
    callback = _FakeCallback(f"uat_go:{state_code}:{role_code}", user_id, username)
    state = _fresh_state(user_id)
    asyncio.run(uat_seed.uat_execute(callback, state))
    return callback


# ── event_city: модуль выключен (дефолт) — как у реального делегата в этом режиме ──────────

def test_pending_leaves_event_city_null_when_cities_module_off(tmp_path):
    _ready(tmp_path)
    _open_gate()
    uat_seed = _import_handlers()

    _go(uat_seed, "pending", "none")

    user = asyncio.run(db.get_user(TESTER_ID))
    assert user["event_city"] is None
    assert user["city"] == uat_seed._SEED_ANSWERS["city"]  # «Москва» не тронута


def test_draft_leaves_event_city_null_when_cities_module_off(tmp_path):
    _ready(tmp_path)
    _open_gate()
    uat_seed = _import_handlers()

    _go(uat_seed, "draft", "none")

    draft = asyncio.run(db.get_reg_draft(TESTER_ID))
    assert draft["event_city"] is None


# ── event_city: модуль включён — сеялка обязана поставить код города ───────────────────────

def test_pending_sets_event_city_to_first_enabled_city_when_module_on(tmp_path):
    _ready(tmp_path)
    _open_gate()
    asyncio.run(db.set_setting("event_city_enabled", "on"))
    restore = _snapshot_cities()
    try:
        cities.set_cities_for_test([
            {"code": "msk", "label": "Москва", "tab_base": "", "enabled": 1, "sort_order": 0},
            {"code": "spb", "label": "Санкт-Петербург", "tab_base": " СПб", "enabled": 1, "sort_order": 1},
        ])
        uat_seed = _import_handlers()

        _go(uat_seed, "pending", "none")

        user = asyncio.run(db.get_user(TESTER_ID))
    finally:
        restore()
    assert user["event_city"] == "msk"
    assert user["city"] == "Москва"  # согласован с event_city, а не жёстко «Москва»


def test_pending_skips_disabled_default_city_for_first_enabled(tmp_path):
    """`default_city_code()` называет город из `config.EVENT_CITY_DEFAULT`/первый в списке —
    но если ИМЕННО он выключен, сеялка обязана взять первый ВКЛЮЧЁННЫЙ, не мёртвый дефолт."""
    _ready(tmp_path)
    _open_gate()
    asyncio.run(db.set_setting("event_city_enabled", "on"))
    restore = _snapshot_cities()
    try:
        cities.set_cities_for_test([
            {"code": "msk", "label": "Москва", "tab_base": "", "enabled": 0, "sort_order": 0},
            {"code": "spb", "label": "Санкт-Петербург", "tab_base": " СПб", "enabled": 1, "sort_order": 1},
        ])
        uat_seed = _import_handlers()

        _go(uat_seed, "pending", "none")

        user = asyncio.run(db.get_user(TESTER_ID))
    finally:
        restore()
    assert user["event_city"] == "spb"
    assert user["city"] == "Санкт-Петербург"


def test_draft_sets_event_city_when_module_on(tmp_path):
    _ready(tmp_path)
    _open_gate()
    asyncio.run(db.set_setting("event_city_enabled", "on"))
    restore = _snapshot_cities()
    try:
        cities.set_cities_for_test([
            {"code": "msk", "label": "Москва", "tab_base": "", "enabled": 1, "sort_order": 0},
        ])
        uat_seed = _import_handlers()

        _go(uat_seed, "draft", "none")

        draft = asyncio.run(db.get_reg_draft(TESTER_ID))
    finally:
        restore()
    assert draft["event_city"] == "msk"


def test_short_track_also_sets_event_city_when_module_on(tmp_path):
    _ready(tmp_path)
    _open_gate()
    asyncio.run(db.set_setting("event_city_enabled", "on"))
    restore = _snapshot_cities()
    try:
        cities.set_cities_for_test([
            {"code": "msk", "label": "Москва", "tab_base": "", "enabled": 1, "sort_order": 0},
        ])
        uat_seed = _import_handlers()

        _go(uat_seed, "short", "none")

        user = asyncio.run(db.get_user(TESTER_ID))
    finally:
        restore()
    assert user["event_city"] == "msk"
    assert user["participant_type"] == "short"


# ── справочник: засеянные заглушки не должны лезть в топ-8 подсказок ───────────────────────

def test_seeded_university_stub_excluded_from_top_chips(tmp_path):
    _ready(tmp_path)
    _open_gate()
    asyncio.run(db.set_setting("event_season", "TEST-SEASON"))
    uat_seed = _import_handlers()

    _go(uat_seed, "pending", "none")

    from services.lookup import top_chips
    chips = asyncio.run(top_chips("university", None))
    assert uat_seed._SEED_ANSWERS["university"] not in chips


def test_real_university_answer_still_ranked(tmp_path):
    """Регресс: маркер исключает ТОЛЬКО подписанные «(приёмка)» строки, обычный ответ
    делегата по-прежнему считается и попадает в топ."""
    _ready(tmp_path)
    asyncio.run(db.set_setting("event_season", "TEST-SEASON"))

    asyncio.run(db.add_user({
        "telegram_id": 1,
        "full_name": "Настоящий Делегат",
        "registration_date": _now(),
        "university": "Высшая школа экономики",
        "season": "TEST-SEASON",
    }))

    from services.lookup import top_chips
    chips = asyncio.run(top_chips("university", None))
    assert "Высшая школа экономики" in chips
