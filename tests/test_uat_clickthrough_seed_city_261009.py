"""Приёмка 09.10: сеялка `/uat` писала в `users.city` (город, где ЖИВЁТ делегат) подпись
города ФОРУМА вместе с датой — «Санкт-Петербург, 3 октября». Подсказки вопроса «Выбери свой
город» (`services.lookup.top_chips`) честно показывают частые ответы, и у тестеров первыми
кнопками шли подписи городов форума с датами.

Хелперы — из tests/test_uat_seed_event_city_260916.py."""
import asyncio

import cities
from database import db
from tests.test_uat_seed_event_city_260916 import (
    TESTER_ID,
    _go,
    _import_handlers,
    _open_gate,
    _ready,
    _snapshot_cities,
)

_FORUM_CITIES = [
    {"code": "spb", "label": "Санкт-Петербург, 3 октября", "tab_base": " СПб", "enabled": 1,
     "sort_order": 0},
    {"code": "msk", "label": "Москва, 30-31 октября", "tab_base": "", "enabled": 1,
     "sort_order": 1},
]


def _seed(tmp_path, state_code):
    _ready(tmp_path)
    _open_gate()
    asyncio.run(db.set_setting("event_city_enabled", "on"))
    restore = _snapshot_cities()
    try:
        cities.set_cities_for_test(_FORUM_CITIES)
        _go(_import_handlers(), state_code, "none")
        user = asyncio.run(db.get_user(TESTER_ID))
        draft = asyncio.run(db.get_reg_draft(TESTER_ID))
    finally:
        restore()
    return user, draft


def test_pending_seeds_home_city_without_forum_date(tmp_path):
    user, _ = _seed(tmp_path, "pending")
    assert user["event_city"] == "spb"
    assert user["city"] == "Санкт-Петербург"


def test_draft_seeds_home_city_without_forum_date(tmp_path):
    _, draft = _seed(tmp_path, "draft")
    assert draft["event_city"] == "spb"
    assert draft["answers"]["city"] == "Санкт-Петербург"


def test_label_override_with_date_is_stripped_too(tmp_path):
    """Подпись часто задают настройкой `city_label__{code}` в админке — она тоже с датой."""
    _ready(tmp_path)
    _open_gate()
    asyncio.run(db.set_setting("event_city_enabled", "on"))
    asyncio.run(db.set_setting("city_label__msk", "Москва, 30-31 октября"))
    restore = _snapshot_cities()
    try:
        cities.set_cities_for_test([
            {"code": "msk", "label": "Москва", "tab_base": "", "enabled": 1, "sort_order": 0},
        ])
        _go(_import_handlers(), "pending", "none")
        user = asyncio.run(db.get_user(TESTER_ID))
    finally:
        restore()
    assert user["city"] == "Москва"
