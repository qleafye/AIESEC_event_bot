"""Сканер Mini App перечитывает список городов из таблицы `cities` (кэш с TTL), а не живёт
холодным списком из `.env`: город, заведённый в боте после старта процесса Mini App, виден в
выборе города сканера, и его сессии не превращаются в «другой город»."""
from __future__ import annotations

import time

import domain.cities as cities
from database import db as bot_db
from tests.test_miniapp_checkin_260924 import BASE, _grant_checkin_to_game_manager, _run, client_with
from tests.test_miniapp_routes import GAME_MANAGER_ID, _hdr, _set


def test_scanner_sees_city_added_in_table_after_start(tmp_path):
    client = client_with(tmp_path)
    _grant_checkin_to_game_manager()
    saved = cities.all_cities()
    try:
        cities.set_cities_for_test([
            {"code": "msk", "label": "Москва", "tab_base": "", "enabled": 1, "sort_order": 0},
        ])
        cities._cities_loaded_at = 0.0  # кэш процесса Mini App «старый»
        for code, label, tab, order in (("msk", "Москва", "", 0), ("spb", "Санкт-Петербург", "СПб", 1),
                                        ("tyumen", "Тюмень", "Тюмень", 2), ("kzn", "Казань", "Казань", 3)):
            _run(bot_db.insert_city(code, label, tab, order))
        _set("event_city_enabled", "on")

        body = client.get(f"{BASE}/points", headers=_hdr(GAME_MANAGER_ID)).json()
        assert [c["code"] for c in body["cities"]] == ["msk", "spb", "tyumen", "kzn"]
        picked = client.get(f"{BASE}/points?city=kzn", headers=_hdr(GAME_MANAGER_ID)).json()
        assert picked["city"] == "kzn"
    finally:
        cities.set_cities_for_test(saved)
        cities._cities_loaded_at = time.monotonic()
