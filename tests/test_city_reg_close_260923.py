"""Квик 260923-p37 (CITY-REG-CLOSE): закрытие регистрации на город по дате + закрытие дыры
«старая deep-link `?start=city_spb` регистрирует на уже прошедший город».

pytest-asyncio недоступен в этом окружении — каждый async-тест ведётся через asyncio.run(),
config.DB_PATH указывает на файл в tmp_path (тот же приём, что и во всех соседних city-тестах).
Фейки (`_FakeMessage`/`_FakeCallback`/`FakeCommand`) скопированы БАЙТ-В-БАЙТ из
tests/test_city_flow_phase71.py — этот файл сам объясняет, почему копия, а не импорт.
"""
import asyncio
from datetime import datetime

from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage

import cities
import reg_engine
from config import config
from database import db
from handlers import registration as reg
from handlers import reg_flow
# Ловушка цикла admin <-> admin_settings (см. tests/test_delegate_texts_registry_260819.py):
# handlers.admin импортируется ПЕРВЫМ, admin_settings в одиночку не импортируется.
from handlers import admin as _admin_mod  # noqa: F401
from handlers import admin_settings
from settings_schema import SETTINGS_SCHEMA
from settings_validation import validate_setting_value


def _use_tmp_db(tmp_path, name="test_city_reg_close_260923.db"):
    config.DB_PATH = str(tmp_path / name)


def _new_state(uid: int) -> FSMContext:
    return FSMContext(storage=MemoryStorage(), key=StorageKey(bot_id=1, chat_id=uid, user_id=uid))


class _FakeUser:
    def __init__(self, uid, username=None):
        self.id = uid
        self.username = username


class _FakeChat:
    def __init__(self, cid):
        self.id = cid


class _FakeMessage:
    def __init__(self, uid, username=None):
        self.from_user = _FakeUser(uid, username)
        self.chat = _FakeChat(uid)
        self.texts = []
        self.markups = []

    async def answer(self, text=None, *a, **k):
        self.texts.append(text)
        self.markups.append(k.get("reply_markup"))
        return None

    async def answer_photo(self, *a, **k):
        self.texts.append("<photo>")
        self.markups.append(k.get("reply_markup"))
        return None

    async def edit_reply_markup(self, reply_markup=None):
        return None

    def model_copy(self, update=None):
        new = _FakeMessage(self.from_user.id, self.from_user.username)
        if update and "from_user" in update:
            new.from_user = update["from_user"]
        return new


class _FakeCallback:
    def __init__(self, data, user_id, username=None):
        self.data = data
        self.from_user = _FakeUser(user_id, username)
        self.message = _FakeMessage(0)

    async def answer(self, text=None, show_alert=False):
        return None


class FakeCommand:
    def __init__(self, args=None):
        self.args = args


def _callback_datas(markup):
    rows = getattr(markup, "inline_keyboard", None)
    if not rows:
        return []
    return [btn.callback_data for row in rows for btn in row]


def _close_city(code: str, date_str: str = "01.10.2026"):
    """Composite per-city override — ТОЛЬКО через cities.per_city_key (T-092-01), не литералом."""
    key = cities.per_city_key("city_reg_close_date", code)
    assert key is not None
    return db.set_setting(key, date_str)


def _disable_city(code: str):
    return db.set_setting(f"city_enabled__{code}", "off")


# ═══ Задача 1: is_city_registration_open / open_cities / city_gate ══════════════════════════

def test_is_city_registration_open_true_without_date(tmp_path):
    _use_tmp_db(tmp_path)

    async def go():
        await db.init_db()
        return await cities.is_city_registration_open("spb")

    assert asyncio.run(go()) is True


def test_is_city_registration_open_true_before_close_date(tmp_path, monkeypatch):
    _use_tmp_db(tmp_path)
    monkeypatch.setattr(cities, "msk_now", lambda: datetime(2026, 9, 30, 23, 59))

    async def go():
        await db.init_db()
        await db.set_setting("event_city_enabled", "on")
        await _close_city("spb", "01.10.2026")
        return await cities.is_city_registration_open("spb")

    assert asyncio.run(go()) is True


def test_is_city_registration_open_false_exactly_at_midnight_of_close_date(tmp_path, monkeypatch):
    _use_tmp_db(tmp_path)
    monkeypatch.setattr(cities, "msk_now", lambda: datetime(2026, 10, 1, 0, 0))

    async def go():
        await db.init_db()
        await db.set_setting("event_city_enabled", "on")
        await _close_city("spb", "01.10.2026")
        return await cities.is_city_registration_open("spb")

    assert asyncio.run(go()) is False


def test_is_city_registration_open_false_after_close_date(tmp_path, monkeypatch):
    _use_tmp_db(tmp_path)
    monkeypatch.setattr(cities, "msk_now", lambda: datetime(2026, 10, 5, 12, 0))

    async def go():
        await db.init_db()
        await db.set_setting("event_city_enabled", "on")
        await _close_city("spb", "01.10.2026")
        return await cities.is_city_registration_open("spb")

    assert asyncio.run(go()) is False


def test_is_city_registration_open_false_when_disabled_regardless_of_date(tmp_path, monkeypatch):
    _use_tmp_db(tmp_path)
    # "сейчас" далеко ДО любой возможной даты закрытия — единственная причина закрытия здесь
    # обязана быть is_city_enabled, не дата.
    monkeypatch.setattr(cities, "msk_now", lambda: datetime(2020, 1, 1, 0, 0))

    async def go():
        await db.init_db()
        await _disable_city("spb")
        return await cities.is_city_registration_open("spb")

    assert asyncio.run(go()) is False


def test_enabled_cities_still_contains_date_closed_city(tmp_path, monkeypatch):
    _use_tmp_db(tmp_path)
    monkeypatch.setattr(cities, "msk_now", lambda: datetime(2026, 10, 5, 12, 0))

    async def go():
        await db.init_db()
        await db.set_setting("event_city_enabled", "on")
        await _close_city("spb", "01.10.2026")
        return [c["code"] for c in await cities.enabled_cities()]

    assert "spb" in asyncio.run(go())


def test_open_cities_excludes_only_the_date_closed_city(tmp_path, monkeypatch):
    _use_tmp_db(tmp_path)
    monkeypatch.setattr(cities, "msk_now", lambda: datetime(2026, 10, 5, 12, 0))

    async def go():
        await db.init_db()
        await db.set_setting("event_city_enabled", "on")
        await _close_city("spb", "01.10.2026")
        return [c["code"] for c in await cities.open_cities()]

    assert asyncio.run(go()) == ["msk", "tyumen"]


def test_city_gate_module_off_always_go(tmp_path):
    _use_tmp_db(tmp_path)

    async def go():
        await db.init_db()
        return await reg_engine.city_gate("spb")

    assert asyncio.run(go()) == ("go", "spb")


def test_city_gate_event_city_open_go(tmp_path):
    _use_tmp_db(tmp_path)

    async def go():
        await db.init_db()
        await db.set_setting("event_city_enabled", "on")
        return await reg_engine.city_gate("spb")

    assert asyncio.run(go()) == ("go", "spb")


def test_city_gate_event_city_closed(tmp_path, monkeypatch):
    _use_tmp_db(tmp_path)
    monkeypatch.setattr(cities, "msk_now", lambda: datetime(2026, 10, 5, 12, 0))

    async def go():
        await db.init_db()
        await db.set_setting("event_city_enabled", "on")
        await _close_city("spb", "01.10.2026")
        return await reg_engine.city_gate("spb")

    assert asyncio.run(go()) == ("closed", "spb")


def test_city_gate_none_zero_open_all_closed(tmp_path, monkeypatch):
    _use_tmp_db(tmp_path)
    monkeypatch.setattr(cities, "msk_now", lambda: datetime(2026, 10, 5, 12, 0))

    async def go():
        await db.init_db()
        await db.set_setting("event_city_enabled", "on")
        for code in ("msk", "spb", "tyumen"):
            await _close_city(code, "01.10.2026")
        return await reg_engine.city_gate(None)

    assert asyncio.run(go()) == ("all_closed", None)


def test_city_gate_none_one_open_go_with_that_code(tmp_path, monkeypatch):
    _use_tmp_db(tmp_path)
    monkeypatch.setattr(cities, "msk_now", lambda: datetime(2026, 10, 5, 12, 0))

    async def go():
        await db.init_db()
        await db.set_setting("event_city_enabled", "on")
        await _close_city("spb", "01.10.2026")
        await _close_city("tyumen", "01.10.2026")
        return await reg_engine.city_gate(None)

    assert asyncio.run(go()) == ("go", "msk")


def test_city_gate_none_two_or_more_open_fork(tmp_path):
    _use_tmp_db(tmp_path)

    async def go():
        await db.init_db()
        await db.set_setting("event_city_enabled", "on")
        return await reg_engine.city_gate(None)

    assert asyncio.run(go()) == ("fork", None)


def test_should_show_city_fork_false_with_one_open_of_three_enabled(tmp_path, monkeypatch):
    _use_tmp_db(tmp_path)
    monkeypatch.setattr(cities, "msk_now", lambda: datetime(2026, 10, 5, 12, 0))

    async def go():
        await db.init_db()
        await db.set_setting("event_city_enabled", "on")
        await _close_city("spb", "01.10.2026")
        await _close_city("tyumen", "01.10.2026")
        return await reg_engine.should_show_city_fork(None, False)

    assert asyncio.run(go()) is False


def test_city_fork_options_excludes_date_closed_city(tmp_path, monkeypatch):
    _use_tmp_db(tmp_path)
    monkeypatch.setattr(cities, "msk_now", lambda: datetime(2026, 10, 5, 12, 0))

    async def go():
        await db.init_db()
        await db.set_setting("event_city_enabled", "on")
        await _close_city("spb", "01.10.2026")
        opts = await reg_engine.city_fork_options()
        return [o["code"] for o in opts]

    assert asyncio.run(go()) == ["msk", "tyumen"]


def test_validate_city_choice_rejects_date_closed_city(tmp_path, monkeypatch):
    _use_tmp_db(tmp_path)
    monkeypatch.setattr(cities, "msk_now", lambda: datetime(2026, 10, 5, 12, 0))

    async def go():
        await db.init_db()
        await db.set_setting("event_city_enabled", "on")
        await _close_city("spb", "01.10.2026")
        return await reg_engine.validate_city_choice("spb")

    assert asyncio.run(go()) == (None, reg_engine.CITY_CLOSED_TEXT)


def test_validate_setting_value_date_only_normalizes_leading_zero():
    value, err = validate_setting_value("city_reg_close_date", "1.10.2026")
    assert (value, err) == ("01.10.2026", None)


def test_validate_setting_value_date_only_rejects_garbage_with_example():
    value, err = validate_setting_value("city_reg_close_date", "не дата")
    assert value is None
    assert "ДД.ММ.ГГГГ" in err
    assert "15.10.2026" in err


def test_registry_keys_right_after_city_fork_text_and_per_city():
    order = admin_settings._REG_FIELD_ORDER
    idx = order.index("city_fork_text")
    assert order[idx + 1: idx + 4] == [
        "city_reg_close_date", "city_reg_closed_text", "city_reg_all_closed_text",
    ]
    for key in ("city_reg_close_date", "city_reg_closed_text", "city_reg_all_closed_text"):
        assert SETTINGS_SCHEMA[key]["per_city"] is True


def test_reg_group_screen_shows_new_key_labels(tmp_path):
    _use_tmp_db(tmp_path)

    async def go():
        await db.init_db()
        return await admin_settings.render_settings_group_text("reg")

    text = asyncio.run(go())
    assert SETTINGS_SCHEMA["city_reg_close_date"]["label"] in text
    assert SETTINGS_SCHEMA["city_reg_closed_text"]["label"] in text
    assert SETTINGS_SCHEMA["city_reg_all_closed_text"]["label"] in text


# ═══ Задача 2: экран «город закрыт» + врезки в бот/Mini App ═════════════════════════════════

def test_city_fork_kb_excludes_date_closed_city(tmp_path, monkeypatch):
    _use_tmp_db(tmp_path)
    monkeypatch.setattr(cities, "msk_now", lambda: datetime(2026, 10, 5, 12, 0))
    uid = 937001

    async def go():
        await db.init_db()
        await db.set_setting("event_city_enabled", "on")
        await _close_city("spb", "01.10.2026")
        msg = _FakeMessage(uid, "u")
        await reg.cmd_start(msg, _new_state(uid), bot=object(), command=None)
        return msg

    msg = asyncio.run(go())
    all_codes = [c for m in msg.markups for c in _callback_datas(m)]
    assert all_codes == ["city_pick:msk", "city_pick:tyumen"]


def test_deeplink_to_date_closed_city_shows_closed_screen_with_open_city_buttons(tmp_path, monkeypatch):
    _use_tmp_db(tmp_path)
    monkeypatch.setattr(cities, "msk_now", lambda: datetime(2026, 10, 5, 12, 0))
    uid = 937002
    started = []
    orig = reg._start_registration_flow

    async def spy(*a, **k):
        started.append(k)
        return await orig(*a, **k)

    monkeypatch.setattr(reg, "_start_registration_flow", spy)

    async def go():
        await db.init_db()
        await db.set_setting("event_city_enabled", "on")
        await _close_city("spb", "01.10.2026")
        msg = _FakeMessage(uid, "u")
        await reg.cmd_start(msg, _new_state(uid), bot=object(), command=FakeCommand("city_spb"))
        return msg

    msg = asyncio.run(go())
    assert not started
    closed_text = next((t for t in msg.texts if t and "закрыта" in t), None)
    assert closed_text is not None
    assert "Санкт-Петербург" in closed_text
    assert "Можно зарегистрироваться на другой форум" in closed_text
    last_markup = msg.markups[-1]
    assert _callback_datas(last_markup) == ["city_pick:msk", "city_pick:tyumen"]


def test_deeplink_to_disabled_city_shows_closed_screen(tmp_path):
    _use_tmp_db(tmp_path)
    uid = 937012
    started = []

    async def go():
        await db.init_db()
        await db.set_setting("event_city_enabled", "on")
        orig = reg._start_registration_flow

        async def spy(*a, **k):
            started.append(k)
            return await orig(*a, **k)
        reg._start_registration_flow = spy
        try:
            await _disable_city("spb")
            msg = _FakeMessage(uid, "u")
            await reg.cmd_start(msg, _new_state(uid), bot=object(), command=FakeCommand("city_spb"))
            return msg
        finally:
            reg._start_registration_flow = orig

    msg = asyncio.run(go())
    assert not started
    closed_text = next((t for t in msg.texts if t and "закрыта" in t), None)
    assert closed_text is not None


def test_deeplink_when_all_cities_closed_shows_all_closed_text_no_buttons(tmp_path, monkeypatch):
    _use_tmp_db(tmp_path)
    monkeypatch.setattr(cities, "msk_now", lambda: datetime(2026, 10, 5, 12, 0))
    uid = 937003
    started = []
    orig = reg._start_registration_flow

    async def spy(*a, **k):
        started.append(k)
        return await orig(*a, **k)

    monkeypatch.setattr(reg, "_start_registration_flow", spy)

    async def go():
        await db.init_db()
        await db.set_setting("event_city_enabled", "on")
        for code in ("msk", "spb", "tyumen"):
            await _close_city(code, "01.10.2026")
        msg = _FakeMessage(uid, "u")
        await reg.cmd_start(msg, _new_state(uid), bot=object(), command=FakeCommand("city_spb"))
        return msg

    msg = asyncio.run(go())
    assert not started
    assert "Регистрация закрыта." in msg.texts
    idx = msg.texts.index("Регистрация закрыта.")
    assert msg.markups[idx] is None


def test_bare_start_when_all_cities_closed_shows_all_closed_text(tmp_path, monkeypatch):
    _use_tmp_db(tmp_path)
    monkeypatch.setattr(cities, "msk_now", lambda: datetime(2026, 10, 5, 12, 0))
    uid = 937004
    started = []
    orig = reg._start_registration_flow

    async def spy(*a, **k):
        started.append(k)
        return await orig(*a, **k)

    monkeypatch.setattr(reg, "_start_registration_flow", spy)

    async def go():
        await db.init_db()
        await db.set_setting("event_city_enabled", "on")
        for code in ("msk", "spb", "tyumen"):
            await _close_city(code, "01.10.2026")
        msg = _FakeMessage(uid, "u")
        await reg.cmd_start(msg, _new_state(uid), bot=object(), command=None)
        return msg

    msg = asyncio.run(go())
    assert not started
    assert "Регистрация закрыта." in msg.texts


def test_tap_open_city_from_closed_screen_preserves_referrer_and_starts_city(tmp_path, monkeypatch):
    _use_tmp_db(tmp_path)
    monkeypatch.setattr(cities, "msk_now", lambda: datetime(2026, 10, 5, 12, 0))
    uid = 937005
    referrer_id = 900001

    async def go():
        await db.init_db()
        await db.set_setting("event_city_enabled", "on")
        await _close_city("spb", "01.10.2026")
        state = _new_state(uid)
        msg = _FakeMessage(uid, "u")
        # Прямой вызов приватной функции — deep-link-токены взаимно исключающие (referrer_id
        # ИЛИ city_{code} в ОДНОМ /start, не оба сразу), поэтому атрибуция здесь моделирует
        # то, что cmd_start передал бы _city_fork_then_continue при deep-link на закрытый
        # город с уже известным (например, из reg_started) реферером.
        await reg._city_fork_then_continue(msg, state, "spb", referrer_id, None, None, None)
        cb = _FakeCallback("city_pick:msk", uid, "u")
        await reg_flow.city_pick(cb, state)
        return await state.get_data()

    data = asyncio.run(go())
    assert data.get("referrer_id") == referrer_id
    assert data.get("event_city") == "msk"


def test_city_pick_rejects_date_closed_city_flow_not_started(tmp_path, monkeypatch):
    _use_tmp_db(tmp_path)
    monkeypatch.setattr(cities, "msk_now", lambda: datetime(2026, 10, 5, 12, 0))
    uid = 937006
    started = []
    orig = reg._start_registration_flow

    async def spy(*a, **k):
        started.append(k)
        return await orig(*a, **k)

    monkeypatch.setattr(reg, "_start_registration_flow", spy)

    async def go():
        await db.init_db()
        await db.set_setting("event_city_enabled", "on")
        await _close_city("spb", "01.10.2026")
        state = _new_state(uid)
        cb = _FakeCallback("city_pick:spb", uid, "u")
        await reg_flow.city_pick(cb, state)

    asyncio.run(go())
    assert not started


def test_bare_start_single_open_city_no_fork_autofills_event_city(tmp_path, monkeypatch):
    _use_tmp_db(tmp_path)
    monkeypatch.setattr(cities, "msk_now", lambda: datetime(2026, 10, 5, 12, 0))
    uid = 937007
    captured = {}
    orig = reg._start_registration_flow

    async def spy(*a, **k):
        captured.update(k)
        return await orig(*a, **k)

    monkeypatch.setattr(reg, "_start_registration_flow", spy)

    async def go():
        await db.init_db()
        await db.set_setting("event_city_enabled", "on")
        await _close_city("spb", "01.10.2026")
        await _close_city("tyumen", "01.10.2026")
        msg = _FakeMessage(uid, "u")
        await reg.cmd_start(msg, _new_state(uid), bot=object(), command=None)
        return msg

    msg = asyncio.run(go())
    all_codes = [c for m in msg.markups for c in _callback_datas(m)]
    assert not any(c.startswith("city_pick:") for c in all_codes)
    assert captured.get("event_city") == "msk"


def test_deeplink_before_close_date_starts_registration_on_that_city(tmp_path, monkeypatch):
    _use_tmp_db(tmp_path)
    monkeypatch.setattr(cities, "msk_now", lambda: datetime(2026, 9, 30, 23, 59))
    uid = 937008
    captured = {}
    orig = reg._start_registration_flow

    async def spy(*a, **k):
        captured.update(k)
        return await orig(*a, **k)

    monkeypatch.setattr(reg, "_start_registration_flow", spy)

    async def go():
        await db.init_db()
        await db.set_setting("event_city_enabled", "on")
        await _close_city("spb", "01.10.2026")
        msg = _FakeMessage(uid, "u")
        await reg.cmd_start(msg, _new_state(uid), bot=object(), command=FakeCommand("city_spb"))
        return msg

    asyncio.run(go())
    assert captured.get("event_city") == "spb"


def test_registered_delegate_of_closed_city_sees_normal_menu_and_own_texts(tmp_path, monkeypatch):
    _use_tmp_db(tmp_path)
    monkeypatch.setattr(cities, "msk_now", lambda: datetime(2026, 10, 5, 12, 0))
    uid = 937009

    async def go():
        await db.init_db()
        await db.set_setting("event_city_enabled", "on")
        await _close_city("spb", "01.10.2026")
        await db.add_user({
            "telegram_id": uid, "full_name": "Delegate", "registration_date": "2026-08-17 00:00:00",
            "event_city": "spb",
        })
        override_key = cities.per_city_key("start_text_registered", "spb")
        await db.set_setting(override_key, "СПб текст для вернувшихся")
        msg = _FakeMessage(uid, "u")
        await reg.cmd_start(msg, _new_state(uid), bot=object(), command=None)
        return msg

    msg = asyncio.run(go())
    assert not any(t and "закрыта" in t for t in msg.texts)
    assert "СПб текст для вернувшихся" in msg.texts


def test_registered_delegate_edit_deeplink_not_gated_by_closed_city(tmp_path, monkeypatch):
    _use_tmp_db(tmp_path)
    monkeypatch.setattr(cities, "msk_now", lambda: datetime(2026, 10, 5, 12, 0))
    uid = 937010
    captured = {}
    orig = reg._start_registration_flow

    async def spy(*a, **k):
        captured.update(k)
        return await orig(*a, **k)

    monkeypatch.setattr(reg, "_start_registration_flow", spy)

    async def go():
        await db.init_db()
        await db.set_setting("event_city_enabled", "on")
        await _close_city("spb", "01.10.2026")
        await db.add_user({
            "telegram_id": uid, "full_name": "Delegate", "registration_date": "2026-08-17 00:00:00",
            "event_city": "spb",
        })
        msg = _FakeMessage(uid, "u")
        await reg.cmd_start(msg, _new_state(uid), bot=object(), command=FakeCommand("edit"))
        return msg

    msg = asyncio.run(go())
    assert not any(t and "закрыта" in t for t in msg.texts)
    assert captured.get("event_city") == "spb"


def test_city_scope_unaffected_by_closing_city(tmp_path):
    _use_tmp_db(tmp_path)
    before = cities.city_scope("spb")

    async def close():
        await db.init_db()
        await _close_city("spb", "01.10.2026")

    asyncio.run(close())
    after = cities.city_scope("spb")
    assert before == after == ("spb", ())


def test_miniapp_registration_closed_before_and_after_close_date(tmp_path, monkeypatch):
    _use_tmp_db(tmp_path)

    async def setup():
        await db.init_db()
        await db.set_setting("event_city_enabled", "on")
        await _close_city("spb", "01.10.2026")

    asyncio.run(setup())

    from miniapp.routers import form as form_mod

    monkeypatch.setattr(cities, "msk_now", lambda: datetime(2026, 9, 30, 23, 59))
    assert asyncio.run(form_mod._registration_closed("spb")) is False

    monkeypatch.setattr(cities, "msk_now", lambda: datetime(2026, 10, 5, 12, 0))
    assert asyncio.run(form_mod._registration_closed("spb")) is True


def test_miniapp_registration_closed_false_when_module_off(tmp_path, monkeypatch):
    _use_tmp_db(tmp_path)
    monkeypatch.setattr(cities, "msk_now", lambda: datetime(2026, 10, 5, 12, 0))

    async def go():
        await db.init_db()
        from miniapp.routers import form as form_mod
        return await form_mod._registration_closed("spb")

    assert asyncio.run(go()) is False
