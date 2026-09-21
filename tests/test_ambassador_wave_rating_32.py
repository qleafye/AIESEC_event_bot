"""Phase 32 План 3 (D-14/D-29/D-31/D-32/D-38): сервис `services.ambassador_waves` — участие
в волне и рейтинг волны на чтении.

Задача 1: `wave_eligible`/`eligible_wave_ids`/`current_wave_for`/`wave_rating`/
`wave_rating_view` — привязка задания к волне переживает поздний просмотр (D-14), участие
подчиняется D-31/D-32/D-38, имена скрываются на уровне данных (D-29). Задачи 2 (сводка конца
волны) и 3 (подсказка соотношения баллов) дописываются в этот же файл следующими коммитами.

pytest-asyncio недоступен в этом окружении — async через `asyncio.run()`, та же фикстура
временной БД, что `tests/test_ambassador_waves_db_32.py::_ready`.
"""
from __future__ import annotations

import asyncio
import json
from datetime import datetime

from config import config
from database import db
import services.ambassador_waves as waves


def _ready(tmp_path, name="test_ambassador_wave_rating_32.db"):
    config.DB_PATH = str(tmp_path / name)
    asyncio.run(db.init_db())


def _run(coro):
    return asyncio.run(coro)


def _seed_user(tid, *, event_city=None, registration_date=None, referrer_id=None, full_name=None):
    _run(db.add_user({
        "telegram_id": tid,
        "full_name": full_name or f"Delegate {tid}",
        "registration_date": registration_date or f"2026-01-01 00:00:{tid % 60:02d}",
        "event_city": event_city,
        "referrer_id": referrer_id,
    }))


def _make_ambassador(tid, *, since="2025-01-01 00:00:00", **kw):
    _seed_user(tid, **kw)
    _run(db.set_ambassador_flag(tid, active=True, at=since))


# ══════════════════════════════════════════════════════════════════════════════════════════
# Задача 1: участие в волне и рейтинг волны
# ══════════════════════════════════════════════════════════════════════════════════════════

def test_wave_eligible_pure_function_since_before_and_after_start():
    wave = {"starts_at": "2026-10-01 00:00:00"}
    before = {"is_ambassador": 1, "ambassador_since": "2026-09-01 00:00:00"}
    after = {"is_ambassador": 1, "ambassador_since": "2026-10-02 00:00:00"}
    no_since = {"is_ambassador": 1, "ambassador_since": None}
    not_ambassador = {"is_ambassador": 0, "ambassador_since": None}
    assert waves.wave_eligible(before, wave) is True
    assert waves.wave_eligible(after, wave) is False
    assert waves.wave_eligible(no_since, wave) is True  # был амбассадором до этой фазы
    assert waves.wave_eligible(not_ambassador, wave) is False


def test_eligible_wave_ids_pure_wrapper():
    user = {"is_ambassador": 1, "ambassador_since": "2026-10-05 00:00:00"}
    all_waves = [
        {"id": 1, "starts_at": "2026-10-01 00:00:00"},
        {"id": 2, "starts_at": "2026-10-10 00:00:00"},
    ]
    assert waves.eligible_wave_ids(user, all_waves) == {2}


def test_current_wave_for_uses_now_param(tmp_path):
    _ready(tmp_path)
    wave_id = _run(db.create_wave("2026-10-01 00:00:00", "2026-10-08 00:00:00"))
    _run(db.set_wave_state(wave_id, "active"))
    inside = datetime(2026, 10, 3, 12, 0, 0)
    outside = datetime(2026, 9, 20, 12, 0, 0)
    found = _run(waves.current_wave_for(None, now=inside))
    not_found = _run(waves.current_wave_for(None, now=outside))
    assert found is not None and found["id"] == wave_id
    assert not_found is None


def test_wave_rating_scoped_two_tasks_one_wave_one_task_another(tmp_path):
    """Делегат с двумя заданиями волны 1 и одним заданием волны 2 получает в каждой волне
    ровно свои суммы (D-14а)."""
    _ready(tmp_path)
    _make_ambassador(1)
    wave_a = _run(db.create_wave("2026-10-01 00:00:00", "2026-10-08 00:00:00"))
    wave_b = _run(db.create_wave("2026-11-01 00:00:00", "2026-11-08 00:00:00"))
    task_a1 = _run(db.create_task("A1", "Light", 10, "photo", "2026-10-05 00:00:00", None, wave_id=wave_a))
    task_a2 = _run(db.create_task("A2", "Light", 20, "photo", "2026-10-06 00:00:00", None, wave_id=wave_a))
    task_b1 = _run(db.create_task("B1", "Light", 30, "photo", "2026-11-05 00:00:00", None, wave_id=wave_b))
    _run(db.add_coins(1, 10, source="task", task_id=task_a1))
    _run(db.add_coins(1, 20, source="task", task_id=task_a2))
    _run(db.add_coins(1, 30, source="task", task_id=task_b1))

    row_a = next(r for r in _run(waves.wave_rating(wave_a)) if r["user_id"] == 1)
    row_b = next(r for r in _run(waves.wave_rating(wave_b)) if r["user_id"] == 1)
    assert row_a["points"] == 30
    assert row_b["points"] == 30


def test_out_of_wave_task_and_manual_coins_excluded_from_wave_but_in_lifetime(tmp_path):
    """Задание «вне волн» и ручные коины менеджера не попадают в рейтинг волны, но попадают
    в общий зачёт (D-14/D-15)."""
    _ready(tmp_path)
    _make_ambassador(1)
    wave_id = _run(db.create_wave("2026-10-01 00:00:00", "2026-10-08 00:00:00"))
    task_in = _run(db.create_task("In", "Light", 10, "photo", "2026-10-05 00:00:00", None, wave_id=wave_id))
    task_out = _run(db.create_task("Out", "Light", 999, "photo", "2026-10-05 00:00:00", None))
    _run(db.add_coins(1, 10, source="task", task_id=task_in))
    _run(db.add_coins(1, 999, source="task", task_id=task_out))
    _run(db.add_coins(1, 500, source="manual"))

    row = next(r for r in _run(waves.wave_rating(wave_id)) if r["user_id"] == 1)
    assert row["points"] == 10
    assert _run(db.get_balance(1)) == 10 + 999 + 500


def test_reviewed_after_wave_end_still_counts_in_wave(tmp_path):
    """Сдача, одобренная ПОСЛЕ ends_at волны, всё равно считается в эту волну (D-14а) —
    привязка идёт по заданию, не по дате проверки."""
    _ready(tmp_path)
    _make_ambassador(1)
    wave_id = _run(db.create_wave("2026-01-01 00:00:00", "2026-01-08 00:00:00"))
    wave = _run(db.get_wave(wave_id))
    task_id = _run(db.create_task("A", "Light", 10, "photo", "2026-01-05 00:00:00", None, wave_id=wave_id))
    sub_id = _run(db.create_submission(task_id, 1, "photo", "file123", "2026-01-06 00:00:00"))
    won = _run(db.claim_submission(sub_id, 999, "approved", coins_awarded=10))
    assert won is True
    submission = _run(db.get_submission(sub_id))
    assert str(submission["reviewed_at"]) > str(wave["ends_at"])  # проверено ПОСЛЕ конца волны
    _run(db.add_coins(1, 10, source="task", task_id=task_id))

    row = next(r for r in _run(waves.wave_rating(wave_id)) if r["user_id"] == 1)
    assert row["points"] == 10


def test_referral_credit_wave_binding(tmp_path):
    """Реферальное начисление с wave_id волны 1 попадает в волну 1; начисление с
    wave_id=NULL — никуда (D-14б)."""
    _ready(tmp_path)
    _make_ambassador(1)
    _seed_user(2)
    _seed_user(3)
    wave_id = _run(db.create_wave("2026-10-01 00:00:00", "2026-10-08 00:00:00"))
    _run(db.claim_referral_credit(2, 1, 50, wave_id))
    _run(db.claim_referral_credit(3, 1, 40, None))

    row = next(r for r in _run(waves.wave_rating(wave_id)) if r["user_id"] == 1)
    assert row["points"] == 50


def test_joined_mid_wave_absent_from_rating(tmp_path):
    """Стал амбассадором посреди волны — отсутствует в рейтинге этой волны (D-31)."""
    _ready(tmp_path)
    _seed_user(1)
    wave_id = _run(db.create_wave("2026-10-01 00:00:00", "2026-10-08 00:00:00"))
    _run(db.set_wave_state(wave_id, "active"))
    _run(db.set_ambassador_flag(1, active=True, at="2026-10-03 00:00:00"))  # после starts_at

    assert all(r["user_id"] != 1 for r in _run(waves.wave_rating(wave_id)))


def test_left_ambassador_absent_from_rating_balance_and_rank_unchanged(tmp_path):
    """Вышедший исчезает из рейтинга текущей волны, а get_balance/get_user_rank не меняются
    ни на балл (D-32)."""
    _ready(tmp_path)
    _make_ambassador(1)
    wave_id = _run(db.create_wave("2026-10-01 00:00:00", "2026-10-08 00:00:00"))
    task_id = _run(db.create_task("A", "Light", 10, "photo", "2026-10-05 00:00:00", None, wave_id=wave_id))
    _run(db.add_coins(1, 10, source="task", task_id=task_id))

    assert any(r["user_id"] == 1 for r in _run(waves.wave_rating(wave_id)))
    balance_before = _run(db.get_balance(1))
    rank_before = _run(db.get_user_rank(1))

    _run(db.set_ambassador_flag(1, active=False, at="2026-10-06 00:00:00"))

    assert all(r["user_id"] != 1 for r in _run(waves.wave_rating(wave_id)))
    assert _run(db.get_balance(1)) == balance_before
    assert _run(db.get_user_rank(1)) == rank_before


def test_returned_ambassador_treated_as_newcomer_mid_wave(tmp_path):
    """Вернувшийся снова считается новичком посреди волны — в текущую волну не попадает
    (D-38)."""
    _ready(tmp_path)
    _seed_user(1)
    wave_id = _run(db.create_wave("2026-10-01 00:00:00", "2026-10-08 00:00:00"))
    _run(db.set_wave_state(wave_id, "active"))
    _run(db.set_ambassador_flag(1, active=True, at="2025-01-01 00:00:00"))
    _run(db.set_ambassador_flag(1, active=False, at="2026-09-15 00:00:00"))
    _run(db.set_ambassador_flag(1, active=True, at="2026-10-03 00:00:00"))  # вернулся посреди волны

    assert all(r["user_id"] != 1 for r in _run(waves.wave_rating(wave_id)))


def test_zero_points_ambassador_stays_in_rating(tmp_path):
    """Тот, у кого 0 баллов, в списке остаётся — он участник волны."""
    _ready(tmp_path)
    _make_ambassador(1)
    wave_id = _run(db.create_wave("2026-10-01 00:00:00", "2026-10-08 00:00:00"))
    rating = _run(waves.wave_rating(wave_id))
    assert len(rating) == 1
    assert rating[0]["user_id"] == 1
    assert rating[0]["points"] == 0


def test_equal_points_share_place_and_shift_next(tmp_path):
    """Равные баллы делят одно место, следующее место сдвигается на число разделивших
    (спортивное ранжирование 1-1-3)."""
    _ready(tmp_path)
    _make_ambassador(1)
    _make_ambassador(2)
    _make_ambassador(3)
    wave_id = _run(db.create_wave("2026-10-01 00:00:00", "2026-10-08 00:00:00"))
    task_id = _run(db.create_task("A", "Light", 50, "photo", "2026-10-05 00:00:00", None, wave_id=wave_id))
    _run(db.add_coins(1, 50, source="task", task_id=task_id))
    _run(db.add_coins(2, 50, source="task", task_id=task_id))
    _run(db.add_coins(3, 20, source="task", task_id=task_id))

    places = {r["user_id"]: r["place"] for r in _run(waves.wave_rating(wave_id))}
    assert places[1] == 1
    assert places[2] == 1
    assert places[3] == 3


def test_names_hidden_when_toggle_off_checks_structure_content(tmp_path):
    """При выключенном тумблере имён ни одно чужое имя не встречается в сериализованном
    ответе (D-29) — проверка содержимого структуры, не просто длины `rows`."""
    _ready(tmp_path)
    _make_ambassador(1, full_name="Иван Иванов")
    _make_ambassador(2, full_name="Пётр Петров")
    wave_id = _run(db.create_wave("2026-10-01 00:00:00", "2026-10-08 00:00:00"))
    task_id = _run(db.create_task("A", "Light", 10, "photo", "2026-10-05 00:00:00", None, wave_id=wave_id))
    _run(db.add_coins(1, 10, source="task", task_id=task_id))
    _run(db.add_coins(2, 20, source="task", task_id=task_id))
    _run(db.set_setting("wave_rating_show_names", "off"))

    view = _run(waves.wave_rating_view(wave_id, 1))
    serialized = json.dumps(view, ensure_ascii=False)
    assert "Пётр Петров" not in serialized
    assert "Иван Иванов" not in serialized
    assert view["rows"] == []
    assert view["own"]["place"] == 2
    # Участников (2) меньше числа призовых мест (дефолт 3) — отсечки ещё нет.
    assert view["own"]["gap_to_prize"] is None


def test_wave_rating_view_non_participant_own_is_none(tmp_path):
    _ready(tmp_path)
    _make_ambassador(1)
    wave_id = _run(db.create_wave("2026-10-01 00:00:00", "2026-10-08 00:00:00"))
    view = _run(waves.wave_rating_view(wave_id, 999999))
    assert view["own"] is None


def test_wave_rating_view_gap_to_prize_zero_in_zone_and_positive_below_cutoff(tmp_path):
    """gap_to_prize = 0, если уже в призовой зоне; положительное число — сколько баллов не
    хватает до последнего призового места, когда участников достаточно, чтобы отсечка была."""
    _ready(tmp_path)
    for uid, points in ((1, 100), (2, 80), (3, 60), (4, 40)):
        _make_ambassador(uid)
    wave_id = _run(db.create_wave("2026-10-01 00:00:00", "2026-10-08 00:00:00"))
    task_id = _run(db.create_task("A", "Light", 100, "photo", "2026-10-05 00:00:00", None, wave_id=wave_id))
    for uid, points in ((1, 100), (2, 80), (3, 60), (4, 40)):
        _run(db.add_coins(uid, points, source="task", task_id=task_id))
    _run(db.set_setting("wave_prize_places", "2"))  # призовые места 1-2 (100, 80)

    view_in_zone = _run(waves.wave_rating_view(wave_id, 2))  # 2-е место, в зоне
    assert view_in_zone["own"]["gap_to_prize"] == 0

    view_below = _run(waves.wave_rating_view(wave_id, 4))  # 4-е место, 40 против отсечки 80
    assert view_below["own"]["gap_to_prize"] == 40
