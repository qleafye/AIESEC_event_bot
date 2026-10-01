"""Прод Юлид, 25.09 и 27.09: московское правило автоотказа «Курс из списка» (city=msk)
отклонило заявку из СПб (RU) и из Тюмени (EN).

Обе анкеты были поданы без города (обходной вход: повторный /start без города, «Заново»
после рестарта) — в `reg_events` у обеих `form_completed.event_city` пуст, хотя `start`/
`form_started` первой попытки несли spb/tyumen. На финале `active_rules(event_city=None)`
сравнивал `normalize_city("msk")` с `normalize_city(None)`, а `normalize_city(None)` — это
город по умолчанию, Москва. Итог: правило Москвы применилось к анкете без города.

Сторожа:
- финал анкеты без города в данных добирает город из того, что уже известно
  (`services.known_city`: черновик -> reg_started -> заявка сезона -> воронка сезона),
  и оценивает правила по НЕМУ — московское правило к тюменской/питерской анкете не применяется;
- если город не известен вовсе, а модуль городов включён, правило конкретного города к анкете
  не применяется (лучше ручная модерация, чем отказ по догадке); правило «все города» — да;
- модуль городов выключен — прежнее поведение (анкета без города = город по умолчанию).

pytest-asyncio недоступен — async через asyncio.run(), БД во временном файле.
"""
import asyncio
import json

import pytest

from config import config
from database import db
import services.reject_rules as rr
from services import reg_finalize as rf
from tests._dbtpl import fast_init_db

SEASON = "YL 26/2"


def _ready(tmp_path, *, cities_on=True, name="reject_city_unknown.db"):
    config.DB_PATH = str(tmp_path / name)
    fast_init_db()

    async def go():
        await db.set_setting("registration_mode", "full")
        await db.set_setting("full_approval", "manual")
        await db.set_setting("event_season", SEASON)
        await db.set_setting("reject_rules_enabled", "on")
        if cities_on:
            await db.set_setting("event_city_enabled", "on")

    asyncio.run(go())


async def _msk_course_rule(city="msk"):
    return await db.create_reject_rule(
        name="Курс из списка", city=city, tracks=json.dumps(["full"]),
        conditions=json.dumps([[{"step": "course", "op": "in", "values": ["1", "2"]}]]),
        action="reject", reject_text="Мест на 1-2 курс нет.", enabled=1, created_by=1,
    )


@pytest.mark.parametrize("uid,city,lang,course", [
    (5152902380, "spb", "ru", "2"),
    (1290968042, "tyumen", "en", "1"),
])
def test_city_less_submission_uses_known_city_not_moscow(tmp_path, uid, city, lang, course):
    """Трасса прода: воронка помнит город первой попытки, подача пришла без города."""
    _ready(tmp_path)

    async def go():
        await _msk_course_rule()
        await db.record_reg_event(uid, "start", event_city=city, season=SEASON)
        await db.record_reg_event(uid, "form_started", event_city=city, season=SEASON)
        await db.record_reg_event(uid, "start", event_city=None, season=SEASON)
        await db.record_reg_event(uid, "form_started", event_city=None, season=SEASON)
        draft = {
            "telegram_id": uid, "kind": "new",
            "answers": {"full_name": "Делегат", "course": course, "lang": lang,
                        "participant_type": "full"},
        }
        result = await rf.finalize_data(uid, "@d", draft)
        return result, await db.get_user(uid), await db.list_auto_reject_log(include_returned=True)

    result, user, log_rows = asyncio.run(go())
    assert result["status"] == "pending", result
    assert result.get("auto_rejected") is not True
    assert user["status"] == "pending"
    assert user.get("auto_reject_rule_ids") in (None, "null")
    assert user["event_city"] == city
    assert log_rows == []


@pytest.mark.parametrize("city", ["spb", "tyumen"])
def test_explicit_other_city_not_hit_by_moscow_rule(tmp_path, city):
    _ready(tmp_path)

    async def go():
        await _msk_course_rule()
        draft = {"telegram_id": 910900001, "kind": "new",
                 "answers": {"course": "1", "event_city": city, "lang": "en"}}
        return await rf.finalize_data(910900001, "@d", draft)

    assert asyncio.run(go())["status"] == "pending"


def test_moscow_submission_still_rejected(tmp_path):
    _ready(tmp_path)

    async def go():
        await _msk_course_rule()
        draft = {"telegram_id": 910900002, "kind": "new",
                 "answers": {"course": "1", "event_city": "msk"}}
        return await rf.finalize_data(910900002, "@d", draft)

    assert asyncio.run(go())["status"] == "rejected"


def test_unknown_city_with_cities_module_on_skips_city_rules(tmp_path):
    _ready(tmp_path)
    asyncio.run(_msk_course_rule())
    assert asyncio.run(rr.active_rules(event_city=None, participant_type="full")) == []
    assert asyncio.run(rr.active_rules(event_city="", participant_type="full")) == []
    assert len(asyncio.run(rr.active_rules(event_city="msk", participant_type="full"))) == 1


def test_unknown_city_still_gets_all_cities_rule(tmp_path):
    _ready(tmp_path)
    asyncio.run(_msk_course_rule(city=None))
    assert len(asyncio.run(rr.active_rules(event_city=None, participant_type="full"))) == 1


def test_cities_module_off_city_less_is_default_city(tmp_path):
    """Стек без модуля городов: анкета без города — это город по умолчанию, как и раньше."""
    _ready(tmp_path, cities_on=False)
    asyncio.run(_msk_course_rule())
    assert len(asyncio.run(rr.active_rules(event_city=None, participant_type="full"))) == 1
