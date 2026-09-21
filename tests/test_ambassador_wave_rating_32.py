"""Phase 32 План 3 (D-14/D-16/D-17/D-21/D-29/D-31/D-32/D-38): сервис
`services.ambassador_waves` — участие в волне, рейтинг волны на чтении, сводка конца волны,
подсказка соотношения баллов.

Три раздела по задачам плана:
- Задача 1: `wave_eligible`/`eligible_wave_ids`/`current_wave_for`/`wave_rating`/
  `wave_rating_view` — привязка задания к волне переживает поздний просмотр (D-14), участие
  подчиняется D-31/D-32/D-38, имена скрываются на уровне данных (D-29).
- Задача 2: `wave_end_summary`/`close_wave`/`wave_number_label`.
- Задача 3: `referral_ratio_hint` + подсказка на экране `handlers.admin_settings`.

pytest-asyncio недоступен в этом окружении — async через `asyncio.run()`, та же фикстура
временной БД, что `tests/test_ambassador_waves_db_32.py::_ready`.
"""
from __future__ import annotations

import asyncio
import json
from datetime import datetime

import cities
from config import config
from database import db
import services.ambassador_waves as waves
from handlers.admin_settings import _settings_edit_screen


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


# ══════════════════════════════════════════════════════════════════════════════════════════
# Городской скоуп волны (баг: `wave_rating` звал `list_ambassadors(city_scope=...)` сырой
# строкой `wave["event_city"]`, а не дескриптором `cities.city_scope(...)`; на любой волне с
# реальным городом `database.db._city_clause` падал `ValueError: too many values to unpack`
# — см. `tests/test_ambassador_wave_scheduling_32.py::test_send_wave_end_ping_only_city_
# managers_and_has_numbers`, где этот баг был задокументирован, но не исправлен)
# ══════════════════════════════════════════════════════════════════════════════════════════

def test_wave_rating_city_scoped_wave_includes_only_that_city(tmp_path):
    """Волна Тюмени видит амбассадоров Тюмени, но не амбассадора явно другого города
    (Москвы)."""
    _ready(tmp_path)
    _make_ambassador(1, event_city="tyumen")
    _make_ambassador(2, event_city="msk")
    wave_id = _run(db.create_wave(
        "2026-10-01 00:00:00", "2026-10-08 00:00:00", event_city="tyumen",
    ))
    task_id = _run(db.create_task("A", "Light", 10, "photo", "2026-10-05 00:00:00", None, wave_id=wave_id))
    _run(db.add_coins(1, 10, source="task", task_id=task_id))

    ids = {r["user_id"] for r in _run(waves.wave_rating(wave_id))}
    assert 1 in ids
    assert 2 not in ids


def test_wave_rating_city_scoped_wave_view_and_end_summary_work(tmp_path):
    """`wave_rating_view` и `wave_end_summary` не падают на волне с реальным городом."""
    _ready(tmp_path)
    _make_ambassador(1, event_city="tyumen")
    wave_id = _run(db.create_wave(
        "2026-10-01 00:00:00", "2026-10-08 00:00:00", event_city="tyumen",
    ))
    task_id = _run(db.create_task("A", "Light", 10, "photo", "2026-10-05 00:00:00", None, wave_id=wave_id))
    _run(db.add_coins(1, 10, source="task", task_id=task_id))

    view = _run(waves.wave_rating_view(wave_id, 1))
    assert view["own"]["points"] == 10

    summary = _run(waves.wave_end_summary(wave_id))
    assert summary["top"][0]["user_id"] == 1


def test_wave_rating_all_cities_wave_unchanged(tmp_path):
    """Волна «все города» (event_city=None) рейтингует всех, независимо от их города, — та же
    семантика, что и до фикса."""
    _ready(tmp_path)
    _make_ambassador(1, event_city="tyumen")
    _make_ambassador(2, event_city="msk")
    _make_ambassador(3, event_city=None)
    wave_id = _run(db.create_wave("2026-10-01 00:00:00", "2026-10-08 00:00:00"))

    ids = {r["user_id"] for r in _run(waves.wave_rating(wave_id))}
    assert ids == {1, 2, 3}


def test_wave_rating_default_city_scope_catches_null_and_own_code(tmp_path):
    """Волна дефолтного города (Москва, `cities.city_scope("msk")` — дескриптор ИСКЛЮЧЕНИЕМ
    остальных городов) видит и явных московских амбассадоров, и тех, у кого `event_city`
    пустой/NULL (не мигрировавшие до модуля городов), но не амбассадоров других городов."""
    assert cities.city_scope("msk") == ("msk", ("spb", "tyumen"))
    _ready(tmp_path)
    _make_ambassador(1, event_city="msk")
    _make_ambassador(2, event_city=None)
    _make_ambassador(3, event_city="spb")
    _make_ambassador(4, event_city="tyumen")
    wave_id = _run(db.create_wave(
        "2026-10-01 00:00:00", "2026-10-08 00:00:00", event_city="msk",
    ))

    ids = {r["user_id"] for r in _run(waves.wave_rating(wave_id))}
    assert ids == {1, 2}


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


# ══════════════════════════════════════════════════════════════════════════════════════════
# Задача 2: сводка конца волны и переход состояния
# ══════════════════════════════════════════════════════════════════════════════════════════

def test_wave_end_summary_top_and_pending(tmp_path):
    _ready(tmp_path)
    _make_ambassador(1)
    _make_ambassador(2)
    _make_ambassador(3)
    wave_id = _run(db.create_wave("2026-10-01 00:00:00", "2026-10-08 00:00:00"))
    task_1 = _run(db.create_task("T1", "Light", 100, "photo", "2026-10-05 00:00:00", None, wave_id=wave_id))
    task_2 = _run(db.create_task("T2", "Light", 50, "photo", "2026-10-06 00:00:00", None, wave_id=wave_id))
    _run(db.add_coins(1, 100, source="task", task_id=task_1))
    _run(db.add_coins(2, 50, source="task", task_id=task_2))
    # ещё две сдачи на проверке
    _run(db.create_submission(task_1, 3, "photo", "f1", "2026-10-06 00:00:00"))
    _run(db.create_submission(task_2, 1, "photo", "f2", "2026-10-07 00:00:00"))

    summary = _run(waves.wave_end_summary(wave_id))
    assert summary["pending"] == 2
    assert [r["points"] for r in summary["top"]] == [100, 50, 0]


def test_wave_end_summary_pending_excludes_other_wave_and_outside(tmp_path):
    """pending не считает сдачи по заданиям чужой волны и по заданиям «вне волн»."""
    _ready(tmp_path)
    _make_ambassador(1)
    wave_a = _run(db.create_wave("2026-10-01 00:00:00", "2026-10-08 00:00:00"))
    wave_b = _run(db.create_wave("2026-11-01 00:00:00", "2026-11-08 00:00:00"))
    task_a = _run(db.create_task("A", "Light", 10, "photo", "2026-10-05 00:00:00", None, wave_id=wave_a))
    task_b = _run(db.create_task("B", "Light", 10, "photo", "2026-11-05 00:00:00", None, wave_id=wave_b))
    task_out = _run(db.create_task("Out", "Light", 10, "photo", "2026-10-05 00:00:00", None))
    _run(db.create_submission(task_a, 1, "photo", "fa", "2026-10-06 00:00:00"))
    _run(db.create_submission(task_b, 1, "photo", "fb", "2026-11-06 00:00:00"))
    _run(db.create_submission(task_out, 1, "photo", "fo", "2026-10-06 00:00:00"))

    summary = _run(waves.wave_end_summary(wave_a))
    assert summary["pending"] == 1


def test_close_wave_true_then_false(tmp_path):
    _ready(tmp_path)
    wave_id = _run(db.create_wave("2026-10-01 00:00:00", "2026-10-08 00:00:00"))
    _run(db.set_wave_state(wave_id, "active"))
    first = _run(waves.close_wave(wave_id))
    second = _run(waves.close_wave(wave_id))
    assert first is True
    assert second is False


def test_close_wave_calls_set_wave_state_with_expected_state_active(tmp_path, monkeypatch):
    _ready(tmp_path)
    wave_id = _run(db.create_wave("2026-10-01 00:00:00", "2026-10-08 00:00:00"))
    _run(db.set_wave_state(wave_id, "active"))

    calls = []

    async def _spy(wid, state, *, expected_state=None):
        calls.append((wid, state, expected_state))
        return await db.set_wave_state(wid, state, expected_state=expected_state)

    monkeypatch.setattr(waves, "set_wave_state", _spy)
    _run(waves.close_wave(wave_id))
    assert calls == [(wave_id, "closing", "active")]


def test_wave_number_label_no_word_nazvanie_uses_number():
    label = waves.wave_number_label({"number": 3})
    assert "название" not in label.lower()
    assert "3" in label


# ══════════════════════════════════════════════════════════════════════════════════════════
# Задача 3: подсказка соотношения баллов
# ══════════════════════════════════════════════════════════════════════════════════════════

def test_referral_ratio_hint_none_when_no_active_tasks(tmp_path):
    _ready(tmp_path)
    assert _run(waves.referral_ratio_hint()) is None


def test_referral_ratio_hint_contains_ratio_and_median(tmp_path):
    _ready(tmp_path)
    for coins in (50, 100, 150):
        _run(db.create_task(f"T{coins}", "Light", coins, "photo", "2026-10-01 00:00:00", None))
    _run(db.set_setting("ambassador_referral_coins", "100"))

    hint = _run(waves.referral_ratio_hint())
    assert hint is not None
    assert "1" in hint
    assert "100" in hint


def test_referral_ratio_hint_zero_says_not_awarded(tmp_path):
    _ready(tmp_path)
    _run(db.create_task("T", "Light", 100, "photo", "2026-10-01 00:00:00", None))
    _run(db.set_setting("ambassador_referral_coins", "0"))

    hint = _run(waves.referral_ratio_hint())
    assert hint is not None
    assert "не начисля" in hint


def test_settings_screen_shows_hint_for_referral_key(tmp_path):
    _ready(tmp_path)
    _run(db.create_task("T", "Light", 100, "photo", "2026-10-01 00:00:00", None))
    _run(db.set_setting("ambassador_referral_coins", "50"))

    text, _kb = _run(_settings_edit_screen("ambassador_referral_coins", None))
    assert "≈" in text


def test_settings_screen_no_hint_for_neighbor_key(tmp_path):
    _ready(tmp_path)
    _run(db.create_task("T", "Light", 100, "photo", "2026-10-01 00:00:00", None))
    _run(db.set_setting("ambassador_referral_coins", "50"))

    text, _kb = _run(_settings_edit_screen("game_resubmit_limit", None))
    assert "≈" not in text
    assert "приглашённый" not in text


def test_settings_screen_survives_exception_inside_hint(tmp_path, monkeypatch):
    """Подсказка не имеет права уронить экран настроек — исключение внутри подсчёта
    (замоканный list_active_tasks) не мешает экрану отрисоваться (T-32-03-05)."""
    _ready(tmp_path)
    _run(db.set_setting("ambassador_referral_coins", "50"))

    async def _boom(*args, **kwargs):
        raise RuntimeError("boom")

    monkeypatch.setattr(waves, "list_active_tasks", _boom)
    text, kb = _run(_settings_edit_screen("ambassador_referral_coins", None))
    assert text
    assert kb is not None
