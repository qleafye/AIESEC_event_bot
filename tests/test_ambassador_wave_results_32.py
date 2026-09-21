"""Phase 32 План 11 (D-07/D-16/D-17/D-18/D-19): итоги волны — подготовленный ботом топ,
подтверждение менеджера, неизменяемый снимок призёров и две рассылки (общая + призёрам).

Задача 1 — `services.ambassador_waves.announce_results`/`prize_places_for` (без aiogram, чистые
БД-тесты). Задача 2 — экран итогов и подтверждение в `handlers/admin_game_waves.py` (стиль
`tests/test_ambassador_waves_crud_32.py`: Fake* объекты, прямой вызов хендлеров). Задача 3 —
рассылка из снимка в `services/scheduler.py` (стиль `tests/test_ambassador_wave_scheduling_32.py`:
FakeBot, реальный AsyncIOScheduler на временном jobstore для проверки постановки джобы, прямой
вызов цели джобы для проверки содержимого рассылки).

pytest-asyncio недоступен в этом окружении — async через `asyncio.run()`.
"""
from __future__ import annotations

import asyncio
from datetime import datetime

from aiogram.exceptions import TelegramForbiddenError
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage

import cities
from config import config
from database import db
from services import ambassador_waves as aw
import services.scheduler as sched


ADMIN_ID = 921101
MSK_MANAGER_ID = 921102
SPB_MANAGER_ID = 921103


def _ready(tmp_path, name="wave_results.db"):
    config.DB_PATH = str(tmp_path / name)
    asyncio.run(db.init_db())
    config.ADMIN_IDS = [ADMIN_ID]


def _run(coro):
    return asyncio.run(coro)


def _dt(day: str) -> str:
    return datetime.strptime(day, "%d.%m.%Y").strftime("%Y-%m-%d 00:00:00")


def _dt_end(day: str) -> str:
    return datetime.strptime(day, "%d.%m.%Y").strftime("%Y-%m-%d 23:59:59")


def _seed_ambassador(tid, *, event_city=None, full_name=None, since="2025-01-01 00:00:00"):
    _run(db.add_user({
        "telegram_id": tid,
        "full_name": full_name or f"Delegate {tid}",
        "registration_date": "2026-01-01 00:00:00",
        "event_city": event_city,
    }))
    _run(db.set_ambassador_flag(tid, active=True, at=since))


def _award(uid, task_id, points):
    _run(db.add_coins(uid, points, source="task", task_id=task_id))


def _make_wave_with_task(*, event_city=None, prize_places=None):
    wave_id = _run(db.create_wave(
        _dt("01.10.2026"), _dt_end("08.10.2026"), event_city=event_city, prize_places=prize_places,
    ))
    task_id = _run(db.create_task(
        "Задание", "Light", 100, "text", "2026-10-08 23:59:59", ADMIN_ID, wave_id=wave_id,
    ))
    _run(db.set_wave_state(wave_id, "active"))
    _run(db.set_wave_state(wave_id, "closing", expected_state="active"))
    return wave_id, task_id


def _codes():
    codes = cities.city_codes()
    assert len(codes) >= 2, "нужно хотя бы два города в тестовом реестре"
    return codes[0], codes[1]


def _bind_manager(manager_id: int, city: str):
    _run(db.add_staff(manager_id, "reg_manager", ADMIN_ID))
    _run(db.set_staff_city(manager_id, city))


# ══════════════════════════════════════════════════════════════════════════════════════════
# Задача 1: announce_results / prize_places_for
# ══════════════════════════════════════════════════════════════════════════════════════════

def test_prize_places_for_uses_wave_override_then_setting_then_floor_at_one(tmp_path):
    _ready(tmp_path)
    assert _run(aw.prize_places_for({"prize_places": 5})) == 5
    assert _run(aw.prize_places_for({"prize_places": None})) == 3  # дефолт настройки
    assert _run(aw.prize_places_for({"prize_places": 0})) == 3  # 0 -> falsy -> настройка
    assert _run(aw.prize_places_for({"prize_places": -2})) == 1  # кривое значение -> 1
    # `get_setting_typed` для типа "int" САМА возвращает дефолт при <=0 (settings_schema.py
    # `_parse_setting`) — "0" в настройке читается как дефолт 3, не как 0; собственный пол
    # `prize_places_for` в единицу защищает только от волнового `prize_places` (нет такой
    # проверки типа у override-поля), проверено выше отдельным кейсом (-2 -> 1).
    _run(db.set_setting("wave_prize_places", "0"))
    assert _run(aw.prize_places_for({})) == 3


def test_announce_results_writes_exactly_prize_places_rows_and_transitions_state(tmp_path):
    _ready(tmp_path)
    wave_id, task_id = _make_wave_with_task(prize_places=2)
    _seed_ambassador(1)
    _seed_ambassador(2)
    _seed_ambassador(3)
    _award(1, task_id, 30)
    _award(2, task_id, 20)
    _award(3, task_id, 10)

    result = _run(aw.announce_results(wave_id))
    assert result is not None
    assert len(result["winners"]) == 2
    assert result["total"] == 3
    assert result["standings"][1] == (1, 30)
    assert result["standings"][3] == (3, 10)

    wave = _run(db.get_wave(wave_id))
    assert wave["state"] == "announced"
    rows = _run(db.get_wave_results(wave_id))
    assert len(rows) == 2
    assert {r["user_id"] for r in rows} == {1, 2}


def test_announce_results_repeat_call_returns_none_snapshot_unchanged(tmp_path):
    _ready(tmp_path)
    wave_id, task_id = _make_wave_with_task(prize_places=1)
    _seed_ambassador(1)
    _award(1, task_id, 10)

    first = _run(aw.announce_results(wave_id))
    assert first is not None
    rows_after_first = _run(db.get_wave_results(wave_id))

    second = _run(aw.announce_results(wave_id))
    assert second is None
    rows_after_second = _run(db.get_wave_results(wave_id))
    assert rows_after_first == rows_after_second


def test_announce_results_on_active_wave_does_nothing(tmp_path):
    _ready(tmp_path)
    wave_id = _run(db.create_wave(_dt("01.10.2026"), _dt_end("08.10.2026")))
    _run(db.set_wave_state(wave_id, "active"))  # НЕ 'closing'

    result = _run(aw.announce_results(wave_id))
    assert result is None
    assert _run(db.get_wave_results(wave_id)) == []
    wave = _run(db.get_wave(wave_id))
    assert wave["state"] == "active"


def test_announce_results_late_approval_keeps_snapshot_but_changes_general_ledger(tmp_path):
    """T-32-11-01: сдача, одобренная ПОСЛЕ объявления, не меняет снимок, но меняет общий зачёт."""
    _ready(tmp_path)
    wave_id, task_id = _make_wave_with_task(prize_places=1)
    _seed_ambassador(1)
    _seed_ambassador(2)
    _award(1, task_id, 50)
    _award(2, task_id, 10)

    result = _run(aw.announce_results(wave_id))
    assert result["standings"][2] == (2, 10)
    snapshot_before = _run(db.get_wave_results(wave_id))
    balance_before = _run(db.get_balance(2))

    # Сдача участника 2 проверена уже ПОСЛЕ объявления — начисление задним числом.
    _award(2, task_id, 1000)

    snapshot_after = _run(db.get_wave_results(wave_id))
    assert snapshot_after == snapshot_before  # снимок не изменился
    balance_after = _run(db.get_balance(2))
    assert balance_after == balance_before + 1000  # общий зачёт изменился

    # Живой rating (для админки/дальнейших волн) тоже честно отражает позднее начисление —
    # это НЕ снимок, и меняться ему можно и нужно (D-14).
    live_rating = _run(aw.wave_rating(wave_id))
    live_points = {r["user_id"]: r["points"] for r in live_rating}
    assert live_points[2] == 1010


def test_announce_results_prize_places_one_vs_three(tmp_path):
    _ready(tmp_path)
    wave_id, task_id = _make_wave_with_task(prize_places=1)
    for uid, pts in ((1, 40), (2, 30), (3, 20), (4, 10)):
        _seed_ambassador(uid)
        _award(uid, task_id, pts)
    result = _run(aw.announce_results(wave_id))
    assert len(result["winners"]) == 1

    wave_id2, task_id2 = _make_wave_with_task(prize_places=3)
    for uid, pts in ((11, 40), (12, 30), (13, 20), (14, 10)):
        _seed_ambassador(uid)
        _award(uid, task_id2, pts)
    result2 = _run(aw.announce_results(wave_id2))
    assert len(result2["winners"]) == 3


def test_announce_results_tie_at_cutoff_is_deterministic(tmp_path):
    """Равные баллы на границе призовых мест — спортивное ранжирование (1-2-2-4), срез по
    prize_places строго по индексу (детерминированная вторичная сортировка `wave_rating`:
    раньше вступивший выше, затем меньший telegram_id)."""
    _ready(tmp_path)
    wave_id, task_id = _make_wave_with_task(prize_places=2)
    _seed_ambassador(1, since="2025-01-01 00:00:00")
    _seed_ambassador(2, since="2025-01-02 00:00:00")
    _seed_ambassador(3, since="2025-01-03 00:00:00")
    _award(1, task_id, 20)
    _award(2, task_id, 20)  # ничья с 1 за первое место
    _award(3, task_id, 10)

    result = _run(aw.announce_results(wave_id))
    winner_ids = [w["user_id"] for w in result["winners"]]
    assert winner_ids == [1, 2]  # оба поделили 1-е место, prize_places=2 -> оба вошли
    assert result["standings"][1] == (1, 20)
    assert result["standings"][2] == (1, 20)
    assert result["standings"][3] == (3, 10)  # спортивное ранжирование: 1,1,3 — не 1,1,2


def test_announce_results_no_coin_writes():
    import inspect
    src = inspect.getsource(aw)
    assert "add_coins" not in src  # D-19: бот не начисляет бонусных коинов призёрам
