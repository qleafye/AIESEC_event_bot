"""Phase 40: цифры регистраций и отметки площадки в Mini App — только агрегаты.

Харнесс — `tests/test_miniapp_routes.py`. Боевую базу не открываем.
`GET /app/api/stats/game` остаётся статистикой геймы.
"""
from __future__ import annotations

import asyncio
import json

from database import db as bot_db
from domain.cities import city_scope

from tests.test_miniapp_routes import (
    ADMIN_ID,
    DELEGATE_ID,
    GAME_MANAGER_ID,
    _cfg,
    _client,
    _hdr,
    _seed,
    _set,
    _use_tmp_db,
)

STATS_CITY_ID = 900701  # stats_manager, привязан к spb
REG_PATH = "/app/api/stats/registrations"
GAME_PATH = "/app/api/stats/game"
SEASON = "YL26"

# Ключи, которых в агрегатах быть не должно (T-40-01): люди, супердашборд, код города.
FORBIDDEN_KEYS = {
    "username", "phone", "telegram_id", "full_name", "dashboard", "sheet_id",
    "code", "city", "email", "user_id", "first_name", "last_name",
}
_SECRET_TEXT = (
    "Секретная Фамилия", "leak_nick", "+79990001122", "Прошлый Делегат",
    str(910001), str(910010),
)
_CITY_CODES = ("msk", "spb", "tyumen")


def _run(coro):
    return asyncio.run(coro)


def _all_keys(value, acc=None) -> set:
    acc = set() if acc is None else acc
    if isinstance(value, dict):
        for key, item in value.items():
            acc.add(key)
            _all_keys(item, acc)
    elif isinstance(value, list):
        for item in value:
            _all_keys(item, acc)
    return acc


def _strings(value, acc=None) -> list:
    acc = [] if acc is None else acc
    if isinstance(value, str):
        acc.append(value)
    elif isinstance(value, dict):
        for item in value.values():
            _strings(item, acc)
    elif isinstance(value, list):
        for item in value:
            _strings(item, acc)
    return acc


async def _insert_user(
    telegram_id, *, city, status="approved", university=None, season=SEASON,
    prev_season=None, username=None, phone=None, full_name=None,
):
    async with bot_db._connect() as conn:
        await conn.execute(
            "INSERT INTO users (telegram_id, full_name, username, phone, status, season, "
            "prev_season, event_city, university, registration_date) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                telegram_id, full_name, username, phone, status, season, prev_season,
                city, university, "2026-01-01 00:00:00",
            ),
        )
        await conn.commit()


def _seed_people():
    """Текущий сезон: МГУ 4 (включая NULL-город → Москва), ВШЭ 2, ИТМО 2, ОмГУ 1.
    Один повторный в Москве, один человек прошлого сезона в СПб."""
    rows = [
        (910001, "msk", "approved", "МГУ", SEASON, "YL25", "leak_nick", "+79990001122", "Секретная Фамилия"),
        (910002, "msk", "approved", "МГУ", SEASON, None, None, None, None),
        (910003, "msk", "pending", "МГУ", SEASON, None, None, None, None),
        (910004, "msk", "approved", "ВШЭ", SEASON, None, None, None, None),
        (910005, "msk", "pending", "ВШЭ", SEASON, None, None, None, None),
        (910006, None, "approved", "МГУ", SEASON, None, None, None, None),
        (910007, "spb", "approved", "ИТМО", SEASON, None, None, None, None),
        (910008, "spb", "approved", "ИТМО", SEASON, None, None, None, None),
        (910009, "tyumen", "approved", "ОмГУ", SEASON, None, None, None, None),
        (910010, "spb", "approved", "Архив", "OLD", None, None, None, "Прошлый Делегат"),
    ]
    for tid, city, status, uni, season, prev, username, phone, full_name in rows:
        _run(_insert_user(
            tid, city=city, status=status, university=uni, season=season,
            prev_season=prev, username=username, phone=phone, full_name=full_name,
        ))


def _ready(tmp_path, *, people=True):
    db_path = _use_tmp_db(tmp_path, "miniapp_manager_home_40.db")
    _seed(
        staff=[
            (GAME_MANAGER_ID, "game_manager", None),
            (STATS_CITY_ID, "stats_manager", "spb"),
            # Суперадмин с привязкой: admin_ids всё равно не сужает цифры.
            (ADMIN_ID, "stats_manager", "spb"),
        ],
        settings={
            "miniapp_enabled": "on",
            "event_city_enabled": "on",
            "event_season": SEASON,
            "registration_mode": "full",
            "city_label__msk": "Москва",
            "city_label__spb": "Санкт-Петербург",
            "city_label__tyumen": "Тюмень",
        },
    )
    if people:
        _seed_people()
    return _client(_cfg(db_path))


def _no_people(body: dict):
    assert not (_all_keys(body) & FORBIDDEN_KEYS)
    text = json.dumps(body, ensure_ascii=False)
    for secret in _SECRET_TEXT:
        assert secret not in text
    assert not (set(_strings(body)) & set(_CITY_CODES))


def test_game_stats_stays_game_and_registrations_are_a_different_path(tmp_path):
    client = _ready(tmp_path)
    game = client.get(GAME_PATH, headers=_hdr(ADMIN_ID))
    assert game.status_code == 200, game.text
    game_body = game.json()
    assert "participants" in game_body
    assert "universities" not in game_body and "returning" not in game_body
    assert "past_seasons" not in game_body and "cities" not in game_body
    regs = client.get(REG_PATH, headers=_hdr(GAME_MANAGER_ID))
    assert regs.status_code == 403
    assert regs.json() == {"reason": "no_cap", "cap": "stats"}


def test_registration_numbers_for_superadmin_are_aggregates(tmp_path):
    client = _ready(tmp_path)
    resp = client.get(REG_PATH, headers=_hdr(ADMIN_ID))
    assert resp.status_code == 200, resp.text
    body = resp.json()
    total, top = _run(bot_db.get_stats(city_scope=None, season=SEASON))
    assert body["total"] == total == 9
    assert body["returning"] == _run(bot_db.get_returning_count(city_scope=None, season=SEASON)) == 1
    assert body["past_seasons"] == _run(bot_db.count_past_season_users(SEASON, city_scope=None)) == 1
    assert {row["name"]: row["count"] for row in body["universities"]} == {
        "МГУ": 4, "ВШЭ": 2, "ИТМО": 2,
    }
    assert [(row["name"], row["count"]) for row in body["universities"]] == [
        (str(name), int(count)) for name, count in top
    ]
    assert len(body["universities"]) <= 3
    assert "ОмГУ" not in {row["name"] for row in body["universities"]}
    assert body["city_label"] is None
    assert body["cities"] == [
        {"city_label": "Москва", "total": 6, "pending": 2, "approved": 4},
        {"city_label": "Санкт-Петербург", "total": 2, "pending": 0, "approved": 2},
        {"city_label": "Тюмень", "total": 1, "pending": 0, "approved": 1},
    ]
    _no_people(body)
    # Параметр города клиента не сужает суперадмина.
    narrowed = client.get(REG_PATH, params={"city": "spb"}, headers=_hdr(ADMIN_ID))
    assert narrowed.status_code == 200
    assert narrowed.json() == body


def test_city_manager_sees_only_bound_city(tmp_path):
    client = _ready(tmp_path)
    scope = city_scope("spb")
    resp = client.get(REG_PATH, params={"city": "msk"}, headers=_hdr(STATS_CITY_ID))
    assert resp.status_code == 200, resp.text
    body = resp.json()
    total, top = _run(bot_db.get_stats(city_scope=scope, season=SEASON))
    assert body["total"] == total == 2
    assert body["returning"] == _run(bot_db.get_returning_count(city_scope=scope, season=SEASON)) == 0
    assert body["past_seasons"] == _run(bot_db.count_past_season_users(SEASON, city_scope=scope)) == 1
    assert body["universities"] == [{"name": name, "count": int(count)} for name, count in top]
    assert body["city_label"] == "Санкт-Петербург"
    assert body["cities"] == [
        {"city_label": "Санкт-Петербург", "total": 2, "pending": 0, "approved": 2},
    ]
    text = json.dumps(body, ensure_ascii=False)
    assert "Москва" not in text and "Тюмень" not in text
    _no_people(body)


def test_zero_registrations_are_numbers_not_an_empty_object(tmp_path):
    client = _ready(tmp_path, people=False)
    resp = client.get(REG_PATH, headers=_hdr(ADMIN_ID))
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["total"] == 0
    assert body["returning"] == 0
    assert body["universities"] == []
    assert "past_seasons" not in body
    assert body["cities"]
    for row in body["cities"]:
        assert row["total"] == 0 and row["pending"] == 0 and row["approved"] == 0
        assert row["city_label"]
    assert body != {}


def test_registrations_without_stats_cap_is_403(tmp_path):
    client = _ready(tmp_path, people=False)
    for user in (GAME_MANAGER_ID, DELEGATE_ID):
        resp = client.get(REG_PATH, headers=_hdr(user))
        assert resp.status_code == 403
        assert resp.json() == {"reason": "no_cap", "cap": "stats"}


def test_registrations_section_off_is_403(tmp_path):
    client = _ready(tmp_path, people=False)
    _set("miniapp_section_stats", "off")
    resp = client.get(REG_PATH, headers=_hdr(ADMIN_ID))
    assert resp.status_code == 403
    assert resp.json() == {"reason": "section_off", "section": "stats"}
