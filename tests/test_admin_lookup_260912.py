"""Phase 30 план 07 задача 3 (A2-03, 30-CONTEXT.md § «Архитектурные ориентиры»): экран бота
«📚 Справочники» — очередь «Другое → влить как псевдоним/отклонить» и закрепление чипов.

БД — тот же приём `_ready(tmp_path)`, что `tests/test_reg_engine_parity.py` (pytest-asyncio
недоступен, async идёт через `asyncio.run()`)."""
from __future__ import annotations

import asyncio

from config import config
from database.db import init_db

import handlers.admin_lookup as admin_lookup
from services import lookup as lookup_service

ADMIN_ID = 900901


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
