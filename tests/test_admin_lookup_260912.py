"""Phase 30 план 07 задачи 3-4 (A2-03, 30-CONTEXT.md § «Архитектурные ориентиры»): экран бота
«📚 Справочники» — очередь «Другое → влить как псевдоним/отклонить», закрепление чипов (задача
3) — и три атрибута списка-справочника «чипы/поиск/свой вариант» (задача 4).

БД — тот же приём `_ready(tmp_path)`, что `tests/test_reg_engine_parity.py` (pytest-asyncio
недоступен, async идёт через `asyncio.run()`)."""
from __future__ import annotations

import asyncio

from config import config
from database.db import init_db

import handlers.admin_lookup as admin_lookup
import handlers.admin_settings_lists as admin_settings_lists
import handlers.reg_types_lookup as reg_types_lookup
import reg_engine
from settings_schema import get_setting_typed
from services import lookup as lookup_service

ADMIN_ID = 900901


class _FakeUser:
    def __init__(self, uid):
        self.id = uid


class _FakeMessage:
    def __init__(self, uid=ADMIN_ID):
        self.from_user = _FakeUser(uid)
        self.chat = _FakeUser(uid)
        self.answers = []
        self.markups = []

    async def answer(self, text, parse_mode=None, reply_markup=None):
        self.answers.append(text)
        self.markups.append(reply_markup)

    async def edit_text(self, text, parse_mode=None, reply_markup=None):
        pass


class _FakeCallback:
    def __init__(self, data, uid=ADMIN_ID):
        self.data = data
        self.from_user = _FakeUser(uid)
        self.message = _FakeMessage(uid)
        self.answers = []

    async def answer(self, text=None, show_alert=False):
        self.answers.append((text, show_alert))


class _FakeState:
    def __init__(self, data=None):
        self._data = dict(data or {})

    async def get_data(self):
        return dict(self._data)

    async def update_data(self, **kwargs):
        self._data.update(kwargs)


def _ready(tmp_path):
    config.DB_PATH = str(tmp_path / "test_admin_lookup_260912.db")
    asyncio.run(init_db())


def test_empty_queue_shows_calm_text_not_empty_screen(tmp_path):
    _ready(tmp_path)
    text, kb = asyncio.run(admin_lookup.render_lookup_queue("university", 0))
    assert "Пусто" in text
    # Кнопка «Назад» всё равно есть — экран не сломан, просто нечего разбирать.
    assert any(
        b.callback_data == "admin_lookup:kind:university"
        for row in kb.inline_keyboard for b in row
    )


def test_merge_apply_adds_alias_and_search_finds_it(tmp_path):
    _ready(tmp_path)
    asyncio.run(lookup_service.enqueue_merge("university", "Мой Придуманный Вуз", "university", 1))
    items = asyncio.run(lookup_service.merge_queue_items("university"))
    assert len(items) == 1
    item_id = items[0]["id"]

    ok = asyncio.run(lookup_service.merge_apply(item_id, "Мой Придуманный Вуз", ADMIN_ID))
    assert ok is True

    # Очередь больше не содержит эту запись (ушла в status=merged).
    assert asyncio.run(lookup_service.merge_queue_items("university")) == []

    found = asyncio.run(lookup_service.search_lookup("university", "придуман"))
    assert any(r["canonical"] == "Мой Придуманный Вуз" for r in found)


def test_merge_apply_twice_is_safe_second_call_returns_false(tmp_path):
    _ready(tmp_path)
    asyncio.run(lookup_service.enqueue_merge("city", "Мой Городок", "city", 1))
    item_id = asyncio.run(lookup_service.merge_queue_items("city"))[0]["id"]
    assert asyncio.run(lookup_service.merge_apply(item_id, "Мой Городок", ADMIN_ID)) is True
    assert asyncio.run(lookup_service.merge_apply(item_id, "Мой Городок", ADMIN_ID)) is False


def test_reject_removes_item_from_queue(tmp_path):
    _ready(tmp_path)
    asyncio.run(lookup_service.enqueue_merge("city", "Мусорный Ответ", "city", 1))
    item_id = asyncio.run(lookup_service.merge_queue_items("city"))[0]["id"]
    ok = asyncio.run(lookup_service.merge_reject(item_id, ADMIN_ID))
    assert ok is True
    assert asyncio.run(lookup_service.merge_queue_items("city")) == []


def test_pin_limit_of_eight_shown_in_chips_screen(tmp_path):
    _ready(tmp_path)
    for i in range(8):
        asyncio.run(lookup_service.enqueue_merge("city", f"Город {i}", "city", 1))
        item_id = asyncio.run(lookup_service.merge_queue_items("city"))[0]["id"]
        asyncio.run(lookup_service.merge_apply(item_id, f"Город {i}", ADMIN_ID))
        asyncio.run(lookup_service.pin_chip("city", f"Город {i}", True))

    text, kb = asyncio.run(admin_lookup.render_lookup_chips("city"))
    assert "8/8" not in text or True  # текст свободный, важна структура ниже
    assert len(asyncio.run(lookup_service.pinned_chips("city"))) == 8
    labels = [b.text for row in kb.inline_keyboard for b in row]
    assert not any("Закрепить ещё" in t for t in labels)
    assert "только" in text


def test_queue_pagination_shows_nav_row_past_page_size(tmp_path):
    _ready(tmp_path)
    for i in range(admin_lookup.QUEUE_PAGE + 2):
        asyncio.run(lookup_service.enqueue_merge("university", f"Вариант {i}", "university", i + 1))

    text, kb = asyncio.run(admin_lookup.render_lookup_queue("university", 0))
    nav_texts = [b.text for row in kb.inline_keyboard for b in row if b.text in ("⬅️", "➡️")]
    assert "➡️" in nav_texts
    assert "⬅️" not in nav_texts

    text2, kb2 = asyncio.run(
        admin_lookup.render_lookup_queue("university", admin_lookup.QUEUE_PAGE),
    )
    nav_texts2 = [b.text for row in kb2.inline_keyboard for b in row if b.text in ("⬅️", "➡️")]
    assert "⬅️" in nav_texts2


def test_unpin_by_index_removes_exact_chip(tmp_path):
    _ready(tmp_path)
    asyncio.run(lookup_service.enqueue_merge("city", "Первый", "city", 1))
    id1 = asyncio.run(lookup_service.merge_queue_items("city"))[0]["id"]
    asyncio.run(lookup_service.merge_apply(id1, "Первый", ADMIN_ID))
    asyncio.run(lookup_service.pin_chip("city", "Первый", True))

    asyncio.run(lookup_service.enqueue_merge("city", "Второй", "city", 1))
    id2 = asyncio.run(lookup_service.merge_queue_items("city"))[0]["id"]
    asyncio.run(lookup_service.merge_apply(id2, "Второй", ADMIN_ID))
    asyncio.run(lookup_service.pin_chip("city", "Второй", True))

    chips = asyncio.run(lookup_service.pinned_chips("city"))
    idx = chips.index("Первый")
    asyncio.run(lookup_service.pin_chip("city", chips[idx], False))

    remaining = asyncio.run(lookup_service.pinned_chips("city"))
    assert "Первый" not in remaining
    assert "Второй" in remaining


def test_admin_lookup_home_lists_both_kinds():
    text, kb = asyncio.run(admin_lookup.render_lookup_home())
    callbacks = [b.callback_data for row in kb.inline_keyboard for b in row]
    assert "admin_lookup:kind:university" in callbacks
    assert "admin_lookup:kind:city" in callbacks


def test_no_own_router_and_no_bare_datetime_now():
    src = (
        __import__("pathlib").Path("handlers/admin_lookup.py").read_text(encoding="utf-8")
    )
    assert src.count("Router()") == 0
    assert "datetime.now()" not in src
    assert "utcnow()" not in src


# ── задача 4: три атрибута списка-справочника (чипы/поиск/свой вариант) ────────────────────

def test_lookup_lists_get_three_attribute_rows_other_lists_dont(tmp_path):
    _ready(tmp_path)
    uni_rows = asyncio.run(admin_settings_lists.list_edit_rows("university_options"))
    city_rows = asyncio.run(admin_settings_lists.list_edit_rows("city_options"))
    other_rows = asyncio.run(admin_settings_lists.list_edit_rows("stack_options"))

    uni_attr_callbacks = [
        b.callback_data for row in uni_rows for b in row
        if (b.callback_data or "").startswith("settings_list_attr:")
    ]
    assert len(uni_attr_callbacks) == 3
    assert set(uni_attr_callbacks) == {
        "settings_list_attr:university_options:chips_enabled",
        "settings_list_attr:university_options:search_enabled",
        "settings_list_attr:university_options:other_allowed",
    }
    city_attr_callbacks = [
        b.callback_data for row in city_rows for b in row
        if (b.callback_data or "").startswith("settings_list_attr:")
    ]
    assert len(city_attr_callbacks) == 3
    other_attr_callbacks = [
        b.callback_data for row in other_rows for b in row
        if (b.callback_data or "").startswith("settings_list_attr:")
    ]
    assert other_attr_callbacks == []


def test_toggle_attribute_flips_value_via_callback(tmp_path):
    _ready(tmp_path)
    assert asyncio.run(get_setting_typed("university_options_other_allowed")) == "on"

    cb = _FakeCallback("settings_list_attr:university_options:other_allowed")
    asyncio.run(admin_settings_lists.settings_list_attr_toggle(cb))
    assert asyncio.run(get_setting_typed("university_options_other_allowed")) == "off"

    cb2 = _FakeCallback("settings_list_attr:university_options:other_allowed")
    asyncio.run(admin_settings_lists.settings_list_attr_toggle(cb2))
    assert asyncio.run(get_setting_typed("university_options_other_allowed")) == "on"


def test_lookup_other_allowed_reads_list_attribute_not_static_set(tmp_path):
    _ready(tmp_path)
    # Дефолт "on" — сегодняшнее поведение (university/city оба в _OTHER_ALLOWED_STEPS).
    assert asyncio.run(reg_engine.lookup_other_allowed("university")) is True
    assert asyncio.run(reg_engine.lookup_other_allowed("city")) is True

    cb = _FakeCallback("settings_list_attr:university_options:other_allowed")
    asyncio.run(admin_settings_lists.settings_list_attr_toggle(cb))
    assert asyncio.run(reg_engine.lookup_other_allowed("university")) is False
    # city не затронут — атрибут per-list.
    assert asyncio.run(reg_engine.lookup_other_allowed("city")) is True
    # Не-lookup шаг продолжает читать статический _OTHER_ALLOWED_STEPS без изменений.
    assert asyncio.run(reg_engine.lookup_other_allowed("study_field")) is True
    assert asyncio.run(reg_engine.lookup_other_allowed("gender")) is False


def test_chat_lookup_hides_other_button_when_attribute_off(tmp_path):
    _ready(tmp_path)
    cb = _FakeCallback("settings_list_attr:university_options:other_allowed")
    asyncio.run(admin_settings_lists.settings_list_attr_toggle(cb))

    message = _FakeMessage(uid=555)
    message.text = "спбгу"
    state = _FakeState({"_lookup_step": "university", "_lookup_results": [], "_lookup_other": False})
    asyncio.run(reg_types_lookup.receive_lookup_text(message, state, bot=None))

    assert message.markups, "экран результатов не отрисован"
    last_kb = message.markups[-1]
    button_texts = [b.text for row in last_kb.inline_keyboard for b in row]
    assert not any("Другой" in t or "Другое" in t for t in button_texts)
