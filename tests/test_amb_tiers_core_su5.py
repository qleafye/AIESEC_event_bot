"""Квалифицированная амбассадорка СкиллАп 5: подсчёт приглашённых, ступени, квота O2O,
дедлайн и врезка проверки ступеней во все пути одобрения заявки.

Разделы:
- Задача 1: слой БД (`database/amb_tiers_db.py`), схема, ключи реестра, валидатор дедлайна.
- Задача 2: `services/amb_tiers.check_tiers` и все пути одобрения (бот одиночное, Mini App после
  окна отмены, «Принять всех», авто-одобрение на финале анкеты, вход на площадке).

pytest-asyncio в окружении нет — async через `asyncio.run()`, временная БД — тот же приём,
что `tests/test_referral_credit_32.py::_ready`.
"""
from __future__ import annotations

import asyncio
import sqlite3

from config import config
from database import amb_tiers_db as tdb
from database import db
from settings_schema import SETTINGS_SCHEMA
from settings_validation import validate_setting_value
from tests._dbtpl import fast_init_db

SEASON = "SU26"


def _ready(tmp_path, name="test_amb_tiers_core_su5.db"):
    config.DB_PATH = str(tmp_path / name)
    fast_init_db()
    _run(db.set_setting("event_season", SEASON))


def _run(coro):
    return asyncio.run(coro)


def _seed_user(tid, *, referrer_id=None, status="pending", season=SEASON, full_name=None):
    _run(db.add_user({
        "telegram_id": tid,
        "full_name": full_name or f"Delegate {tid}",
        "registration_date": f"2026-09-01 00:00:{tid % 60:02d}",
        "referrer_id": referrer_id,
        "season": season,
    }))
    _run(db.set_user_status(tid, status))


def _make_ambassador(tid, *, since="2026-01-01 00:00:00"):
    _seed_user(tid, status="approved")
    _run(db.set_ambassador_flag(tid, active=True, at=since))


def _sql(query, params=()):
    conn = sqlite3.connect(config.DB_PATH)
    try:
        rows = conn.execute(query, params).fetchall()
        conn.commit()
        return rows
    finally:
        conn.close()


def _counts(rid, season=SEASON):
    return _run(tdb.referral_counts(rid, season))


# ══════════════════════════════════════════════════════════════════════════════════════════
# Задача 1: слой БД, схема, реестр
# ══════════════════════════════════════════════════════════════════════════════════════════

def test_three_pending_invitees_not_qualified(tmp_path):
    """Приёмка 1: трое подали, никого не одобрили — всего 3, на рассмотрении 3, прошли 0."""
    _ready(tmp_path)
    _make_ambassador(100)
    for tid in (201, 202, 203):
        _seed_user(tid, referrer_id=100)
    assert _counts(100) == {"total": 3, "pending": 3, "qualified": 0, "arrived": 0}

    _run(db.set_user_status(201, "approved"))
    counts = _counts(100)
    assert counts["qualified"] == 1 and counts["pending"] == 2 and counts["total"] == 3


def test_rejected_and_pending_not_qualified_and_other_season_ignored(tmp_path):
    """Приёмка 8: отклонённый и на рассмотрении — не прошли отбор; прошлый сезон не считается."""
    _ready(tmp_path)
    _make_ambassador(100)
    _seed_user(201, referrer_id=100, status="rejected")
    _seed_user(202, referrer_id=100, status="pending")
    _seed_user(203, referrer_id=100, status="approved", season="YL26")
    _seed_user(204, referrer_id=100, status="approved", season=None)
    assert _counts(100) == {"total": 2, "pending": 1, "qualified": 0, "arrived": 0}


def test_self_referral_not_counted(tmp_path):
    """Приёмка 6: /start amb_<свой id> — себя не засчитывает."""
    _ready(tmp_path)
    _make_ambassador(100)
    _sql("UPDATE users SET referrer_id = 100 WHERE telegram_id = 100")
    assert _counts(100)["total"] == 0


def test_arrived_counts_only_entry_point(tmp_path):
    _ready(tmp_path)
    _make_ambassador(100)
    _seed_user(201, referrer_id=100, status="approved")
    _seed_user(202, referrer_id=100, status="approved")
    _sql(
        "INSERT INTO checkins (telegram_id, point, scanned_at, source, created_at, day) "
        "VALUES (201, ?, '2026-11-21 10:00:00', 'scan', '2026-11-21 10:00:00', '2026-11-21')",
        (db.CHECKIN_ENTRY_POINT,),
    )
    _sql(
        "INSERT INTO checkins (telegram_id, point, scanned_at, source, created_at, day) "
        "VALUES (202, 'session:5', '2026-11-21 10:00:00', 'scan', '2026-11-21 10:00:00', '2026-11-21')"
    )
    assert _counts(100)["arrived"] == 1


def test_excluded_invitee_not_counted_anywhere(tmp_path):
    _ready(tmp_path)
    _make_ambassador(100)
    _seed_user(201, referrer_id=100, status="approved")
    _seed_user(202, referrer_id=100, status="approved")
    _sql(
        "INSERT INTO ambassador_exclusions (invitee_id, reason, excluded_by, excluded_at) "
        "VALUES (202, 'накрутка', 1, '2026-09-29 12:00:00')"
    )
    assert _counts(100) == {"total": 1, "pending": 0, "qualified": 1, "arrived": 0}


def test_live_miniapp_approval_in_undo_window_not_qualified(tmp_path):
    """Одобрение в Mini App, у которого ещё не прошло окно отмены, в зачёт не идёт."""
    _ready(tmp_path)
    _make_ambassador(100)
    _seed_user(201, referrer_id=100, status="approved")
    _sql(
        "INSERT INTO application_decisions (telegram_id, decision, reason, decided_by, "
        "decided_at, effects_due_at) VALUES (201, 'approved', NULL, 1, "
        "'2026-09-29 12:00:00', '2026-09-29 12:00:05')"
    )
    assert _counts(100)["qualified"] == 0
    _sql("UPDATE application_decisions SET effects_sent_at = '2026-09-29 12:00:06'")
    assert _counts(100)["qualified"] == 1


def test_bulk_counts_match_single(tmp_path):
    _ready(tmp_path)
    _make_ambassador(100)
    _make_ambassador(101)
    _seed_user(201, referrer_id=100, status="approved")
    _seed_user(202, referrer_id=101, status="pending")
    bulk = _run(tdb.referral_counts_bulk(None, SEASON))
    assert bulk[100] == _counts(100)
    assert bulk[101] == _counts(101)
    assert _run(tdb.referral_counts_bulk([101], SEASON)) == {101: _counts(101)}
    assert _run(tdb.referral_counts_bulk([], SEASON)) == {}


def test_claim_new_tiers_idempotent(tmp_path):
    _ready(tmp_path)
    first = _run(tdb.claim_new_tiers(100, [1], "2026-09-29 12:00:00", 15))
    second = _run(tdb.claim_new_tiers(100, [1], "2026-09-29 12:00:01", 15))
    assert first == [{"tier": 1, "o2o_status": None}]
    assert second == []
    assert len(_run(tdb.list_tiers(100))) == 1


def test_o2o_quota_second_ambassador_waitlisted(tmp_path):
    """Приёмка 5: квота 1 — первый дошедший до ступени 2 получает слот, второй — лист ожидания."""
    _ready(tmp_path)
    a = _run(tdb.claim_new_tiers(100, [1, 2], "2026-09-29 12:00:00", 1))
    b = _run(tdb.claim_new_tiers(101, [1, 2], "2026-09-29 12:00:01", 1))
    assert {"tier": 2, "o2o_status": "granted"} in a
    assert {"tier": 2, "o2o_status": "waitlist"} in b
    assert _run(tdb.o2o_summary()) == {"granted": 1, "waitlist": 1}


def test_claim_tier_notification_once(tmp_path):
    _ready(tmp_path)
    _run(tdb.claim_new_tiers(100, [1], "2026-09-29 12:00:00", 15))
    row = _run(tdb.claim_tier_notification(100, 1, "2026-09-29 12:00:01"))
    assert row is not None and row["tier"] == 1
    assert _run(tdb.claim_tier_notification(100, 1, "2026-09-29 12:00:02")) is None
    _run(tdb.release_tier_notification(100, 1))
    assert _run(tdb.claim_tier_notification(100, 1, "2026-09-29 12:00:03")) is not None


def test_init_db_twice_keeps_tier_rows(tmp_path):
    """Идемпотентность миграции — настоящим init_db на уже наполненной БД."""
    config.DB_PATH = str(tmp_path / "migr.db")
    _run(db.init_db())
    _run(tdb.claim_new_tiers(100, [1, 2], "2026-09-29 12:00:00", 15))
    _sql(
        "INSERT INTO ambassador_exclusions (invitee_id, reason, excluded_by, excluded_at) "
        "VALUES (5, 'накрутка', 1, '2026-09-29 12:00:00')"
    )
    _run(db.init_db())
    assert len(_run(tdb.list_tiers(100))) == 2
    assert _sql("SELECT COUNT(*) FROM ambassador_exclusions")[0][0] == 1


def test_registry_keys_defaults():
    expected = {
        "amb_qualified_program": "off", "amb_tier1_threshold": 1, "amb_tier2_threshold": 3,
        "amb_tier3_threshold": 7, "amb_o2o_quota": 15, "amb_count_deadline": "2026-11-14 23:59",
    }
    for key, default in expected.items():
        assert SETTINGS_SCHEMA[key]["group"] == "game", key
        assert SETTINGS_SCHEMA[key]["default"] == default, key
    for key in ("amb_tier1_text", "amb_tier2_granted_text", "amb_tier2_waitlist_text",
                "amb_tier3_text"):
        default = SETTINGS_SCHEMA[key]["default"]
        assert SETTINGS_SCHEMA[key]["group"] == "game"
        assert "SkillUp" not in default, key
        assert "{name}" not in default and "{username}" not in default, key
    assert "СкиллАп" in SETTINGS_SCHEMA["amb_tier1_text"]["default"]
    assert "{left}" in SETTINGS_SCHEMA["amb_tier1_text"]["default"]


def test_every_tier_text_has_manual_english():
    from services.i18n_form_manual import FORM_DEFAULT_EN

    for key in ("amb_tier1_text", "amb_tier2_granted_text", "amb_tier2_waitlist_text",
                "amb_tier3_text"):
        assert SETTINGS_SCHEMA[key]["default"] in FORM_DEFAULT_EN, key


def test_deadline_validator():
    value, error = validate_setting_value("amb_count_deadline", "2026-11-14 23:59")
    assert error is None and value == "2026-11-14 23:59"
    value, error = validate_setting_value("amb_count_deadline", "14.11.2026")
    assert value is None and "2026-11-14 23:59" in error
    value, error = validate_setting_value("amb_count_deadline", "нет")
    assert error is None and value == ""


# ══════════════════════════════════════════════════════════════════════════════════════════
# Задача 2: check_tiers и все пути одобрения
# ══════════════════════════════════════════════════════════════════════════════════════════

import json  # noqa: E402
from datetime import datetime, timedelta  # noqa: E402

from services import amb_tiers, applications  # noqa: E402


def _on(*, deadline="2099-01-01 00:00", quota=None):
    _run(db.set_setting("amb_qualified_program", "on"))
    _run(db.set_setting("amb_count_deadline", deadline))
    if quota is not None:
        _run(db.set_setting("amb_o2o_quota", str(quota)))


def _approve_bot(tid, by=1):
    """Бот-путь целиком: флип + запись решения с уже отправленными эффектами."""
    won = _run(applications.claim_approve(tid))
    if won:
        _run(applications.record_decision(
            tid, "approved", None, by, datetime.now(), effects_already_sent=True,
        ))
    return won


def _approve_web(tid, by=1, now=None):
    """Mini App одиночное: флип + живая строка журнала одной транзакцией (эффекты ждут окна
    отмены) — тот же вызов, что miniapp/routers/applications.py::_decide."""
    return _run(applications.claim_web_decision(tid, "approved", None, by, now or datetime.now()))


def _events():
    rows = _sql("SELECT payload FROM miniapp_outbox WHERE kind = 'amb_tier_reached' ORDER BY id")
    return [json.loads(r[0]) for r in rows]


def _tiers(tid):
    return [(r["tier"], r["o2o_status"]) for r in _run(tdb.list_tiers(tid))]


def _flush(now):
    return _run(applications.flush_due_decisions(now, lambda kind, row: None))


def test_program_off_no_rows_no_events(tmp_path):
    """Приёмка 11 / D-09: тумблер выключен — ни строк ступеней, ни событий."""
    _ready(tmp_path)
    _make_ambassador(100)
    for tid in (201, 202, 203):
        _seed_user(tid, referrer_id=100)
    _approve_bot(201)
    _run(applications.claim_approve_all_with_credits(None))
    assert _run(amb_tiers.check_tiers([100])) == []
    assert _tiers(100) == []
    assert _events() == []


def test_acceptance_1_to_4_single_then_bulk(tmp_path):
    """Приёмка 1–4: трое подали — ничего; одно одобрение — ступень 1 (осталось 2); ещё
    двое (один через «Принять всех») — ровно одно событие ступени 2, слот granted;
    повторное «Принять всех» и повторный check_tiers — ничего нового."""
    _ready(tmp_path)
    _on()
    _make_ambassador(100)
    for tid in (201, 202, 203):
        _seed_user(tid, referrer_id=100)
    assert _run(amb_tiers.check_tiers([100])) == []
    assert _events() == []

    _approve_bot(201)
    assert _tiers(100) == [(1, None)]
    assert _events() == [{"telegram_id": 100, "tier": 1, "left": 2}]

    _approve_bot(202)
    assert len(_events()) == 1
    ids, _summary = _run(applications.claim_approve_all_with_credits(None))
    assert ids == [203]
    assert _tiers(100) == [(1, None), (2, "granted")]
    events = _events()
    assert len(events) == 2 and events[1] == {"telegram_id": 100, "tier": 2, "left": 0}

    ids2, _ = _run(applications.claim_approve_all_with_credits(None))
    assert ids2 == []
    assert _run(amb_tiers.check_tiers([100])) == []
    assert len(_events()) == 2 and len(_tiers(100)) == 2


def test_quota_one_second_ambassador_waitlist(tmp_path):
    """Приёмка 5: квота 1 — второй амбассадор, дошедший до 3, получает лист ожидания."""
    _ready(tmp_path)
    _on(quota=1)
    _make_ambassador(100)
    _make_ambassador(101)
    for tid in (201, 202, 203):
        _seed_user(tid, referrer_id=100)
    for tid in (211, 212, 213):
        _seed_user(tid, referrer_id=101)
    for tid in (201, 202, 203, 211, 212, 213):
        _approve_bot(tid)
    assert (2, "granted") in _tiers(100)
    assert (2, "waitlist") in _tiers(101)


def test_self_ref_does_not_trigger_tier(tmp_path):
    """Приёмка 6: амбассадор со ссылкой на самого себя ступень не получает."""
    _ready(tmp_path)
    _on()
    _seed_user(100, status="pending")
    _run(db.set_ambassador_flag(100, active=True, at="2026-01-01 00:00:00"))
    _sql("UPDATE users SET referrer_id = 100 WHERE telegram_id = 100")
    _approve_bot(100)
    assert _tiers(100) == []
    assert _events() == []


def test_deadline_passed_counts_grow_no_tier(tmp_path):
    """Приёмка 7: после дедлайна счётчик растёт, новой ступени нет."""
    _ready(tmp_path)
    _on(deadline="2020-01-01 00:00")
    _make_ambassador(100)
    _seed_user(201, referrer_id=100)
    _approve_bot(201)
    assert _counts(100)["qualified"] == 1
    assert _tiers(100) == []
    assert _events() == []


def test_unparseable_or_empty_deadline_means_no_deadline(tmp_path):
    _ready(tmp_path)
    _on(deadline="14.11.2026")
    assert _run(amb_tiers.deadline_passed()) is False
    _run(db.set_setting("amb_count_deadline", ""))
    assert _run(amb_tiers.deadline_passed()) is False


def test_not_ambassador_gets_no_tier(tmp_path):
    """D-02: без is_ambassador = 1 ступеней нет."""
    _ready(tmp_path)
    _on()
    _seed_user(100, status="approved")
    _seed_user(201, referrer_id=100)
    _approve_bot(201)
    assert _tiers(100) == []


def test_zero_to_three_in_one_bulk_single_event_for_top_tier(tmp_path):
    """0 -> 3 одним «Принять всех»: две строки, одно событие ступени 2, младшая помечена."""
    _ready(tmp_path)
    _on()
    _make_ambassador(100)
    for tid in (201, 202, 203):
        _seed_user(tid, referrer_id=100)
    _run(applications.claim_approve_all_with_credits(None))
    rows = {r["tier"]: r for r in _run(tdb.list_tiers(100))}
    assert set(rows) == {1, 2}
    assert rows[1]["notified_at"] is not None
    assert rows[2]["notified_at"] is None
    assert _events() == [{"telegram_id": 100, "tier": 2, "left": 0}]


def test_failure_inside_check_tiers_does_not_undo_approval_or_credit(tmp_path, monkeypatch):
    _ready(tmp_path)
    _on()
    _run(db.set_setting("ambassador_referral_coins", "30"))
    _make_ambassador(100)
    _seed_user(201, referrer_id=100)

    async def boom(*_a, **_kw):
        raise RuntimeError("сбой подсчёта")

    monkeypatch.setattr(tdb, "referral_counts", boom)
    assert _approve_bot(201) is True
    assert _run(db.get_user(201))["status"] == "approved"
    assert _run(db.get_referral_credit(201)) is not None
    assert _tiers(100) == []


def test_web_single_approval_tier_only_after_undo_window(tmp_path):
    """Mini App одиночное: ступени нет, пока окно отмены не прошло; после flush — есть."""
    _ready(tmp_path)
    _on()
    _make_ambassador(100)
    _seed_user(201, referrer_id=100)
    now = datetime.now()
    _approve_web(201, now=now)
    assert _run(amb_tiers.check_tiers([100])) == []
    assert _tiers(100) == []
    _flush(now + timedelta(seconds=applications.UNDO_WINDOW_SECONDS + 1))
    assert _tiers(100) == [(1, None)]
    assert len(_events()) == 1


def test_web_single_approval_undone_gives_no_tier(tmp_path):
    _ready(tmp_path)
    _on()
    _make_ambassador(100)
    _seed_user(201, referrer_id=100)
    now = datetime.now()
    decision_id = _approve_web(201, now=now)
    assert _run(applications.undo_decision(decision_id))["ok"] is True
    _flush(now + timedelta(seconds=applications.UNDO_WINDOW_SECONDS + 1))
    assert _tiers(100) == []
    assert _events() == []


def test_undo_window_live_approval_not_counted_by_other_path(tmp_path):
    """A и B одобрены ботом, C — в Mini App (живая строка журнала): проверка ступеней от
    другого пути даёт ступень 1, но не ступень 2; после flush — ступень 2 и одно событие."""
    _ready(tmp_path)
    _on()
    _make_ambassador(100)
    for tid in (201, 202, 203):
        _seed_user(tid, referrer_id=100)
    _approve_bot(201)
    _approve_bot(202)
    now = datetime.now()
    _approve_web(203, now=now)
    _run(amb_tiers.check_tiers([100]))
    assert _tiers(100) == [(1, None)]
    _flush(now + timedelta(seconds=applications.UNDO_WINDOW_SECONDS + 1))
    assert _tiers(100) == [(1, None), (2, "granted")]
    assert [e["tier"] for e in _events()] == [1, 2]


def test_undo_window_live_approval_undone_stays_at_two(tmp_path):
    _ready(tmp_path)
    _on()
    _make_ambassador(100)
    for tid in (201, 202, 203):
        _seed_user(tid, referrer_id=100)
    _approve_bot(201)
    _approve_bot(202)
    now = datetime.now()
    decision_id = _approve_web(203, now=now)
    assert _run(applications.undo_decision(decision_id))["ok"] is True
    _flush(now + timedelta(seconds=applications.UNDO_WINDOW_SECONDS + 1))
    assert _tiers(100) == [(1, None)]
    assert _counts(100)["qualified"] == 2


def test_auto_approval_on_finalize_gives_tier(tmp_path, monkeypatch):
    """Авто-одобрение на финале анкеты (full_approval=auto) — ступень 1."""
    from services import reg_finalize as rf

    _ready(tmp_path)
    _on()
    monkeypatch.setattr(config, "ADMIN_IDS", [])
    _make_ambassador(100)

    async def go():
        await db.set_setting("registration_mode", "full")
        await db.set_setting("full_approval", "auto")
        draft = {
            "telegram_id": 8001, "kind": "new",
            "answers": {"full_name": "Новый Делегат"},
            "meta": {"referrer_id": 100},
        }
        return await rf.finalize_data(8001, "@newbie", draft)

    result = _run(go())
    assert result["status"] == "approved"
    assert _tiers(100) == [(1, None)]
    assert len(_events()) == 1


def test_onsite_approval_path_gives_tier(tmp_path):
    """Вход на площадке: approve_onsite + record_decision(effects_already_sent=True) — та же
    пара вызовов, что services.onsite_reg.approve_at_door."""
    _ready(tmp_path)
    _on()
    _make_ambassador(100)
    _seed_user(201, referrer_id=100)
    assert _run(db.approve_onsite(201, by_staff_id=7, season=SEASON)) is True
    _run(applications.record_decision(
        201, "approved", "на месте", 7, datetime.now(), effects_already_sent=True,
    ))
    assert _tiers(100) == [(1, None)]


# ── Веб-одобрение: статус и строка окна отмены — одной транзакцией ───────────────────────

def test_web_decision_status_and_undo_row_are_atomic(tmp_path):
    """Если строку окна отмены записать не удалось, статус тоже не меняется: одобрения без
    строки окна отмены (его засчитал бы подсчёт ступеней) не бывает ни на миг."""
    _ready(tmp_path)
    _on()
    _make_ambassador(100)
    for tid in (201, 202, 203):
        _seed_user(tid, referrer_id=100)
    _approve_bot(201)
    _approve_bot(202)
    _sql(
        "CREATE TRIGGER boom BEFORE INSERT ON application_decisions "
        "BEGIN SELECT RAISE(ABORT, 'locked'); END"
    )
    try:
        _approve_web(203)
    except Exception:
        pass
    else:
        raise AssertionError("ожидали ошибку записи журнала")
    assert _run(db.get_user(203))["status"] == "pending"
    _run(amb_tiers.check_tiers([100]))
    assert _tiers(100) == [(1, None)]


def test_web_decision_second_claim_loses_without_row(tmp_path):
    _ready(tmp_path)
    _seed_user(201)
    first = _run(applications.claim_web_decision(201, "approved", None, 1, datetime.now()))
    second = _run(applications.claim_web_decision(201, "rejected", "x", 1, datetime.now()))
    assert first and second is None
    user = _run(db.get_user(201))
    assert user["status"] == "approved" and user["approved_at"]
    rows = _sql("SELECT decision, effects_sent_at FROM application_decisions WHERE telegram_id = 201")
    assert rows == [("approved", None)]


def test_miniapp_decide_uses_single_transaction_claim():
    import inspect

    from miniapp.routers import applications as router

    source = inspect.getsource(router._decide)
    assert "claim_web_decision" in source
    assert "claim_approve" not in source and "record_decision" not in source


# ── Прыжок через ступени: о разборе резюме сообщаем всегда ────────────────────────────────

def test_notify_tiers_for_keeps_o2o_tier():
    assert amb_tiers.notify_tiers_for([1]) == [1]
    assert amb_tiers.notify_tiers_for([1, 2]) == [2]
    assert amb_tiers.notify_tiers_for([1, 2, 3]) == [2, 3]
    assert amb_tiers.notify_tiers_for([2, 3]) == [2, 3]
    assert amb_tiers.notify_tiers_for([3]) == [3]
    assert amb_tiers.notify_tiers_for([]) == []


def _jump_to_seven(quota=None):
    _on(quota=quota)
    _make_ambassador(100)
    for tid in range(201, 208):
        _seed_user(tid, referrer_id=100)
    _run(applications.claim_approve_all_with_credits(None))


def test_zero_to_seven_notifies_o2o_slot_and_top(tmp_path):
    """0 -> 7 одним «Принять всех»: событие о слоте разбора резюме и о нетворкинге; ступень 1
    помечена сразу (никакого «осталось 0»)."""
    _ready(tmp_path)
    _jump_to_seven()
    rows = {r["tier"]: r for r in _run(tdb.list_tiers(100))}
    assert rows[1]["notified_at"] is not None
    assert rows[2]["notified_at"] is None and rows[3]["notified_at"] is None
    assert [e["tier"] for e in _events()] == [2, 3]


def test_zero_to_seven_waitlist_still_told(tmp_path):
    _ready(tmp_path)
    _run(db.set_setting("amb_o2o_quota", "1"))
    _run(tdb.claim_new_tiers(999, [2], "2026-09-01 00:00:00", 1))
    _jump_to_seven(quota=1)
    assert _tiers(100) == [(1, None), (2, "waitlist"), (3, None)]
    assert [e["tier"] for e in _events()] == [2, 3]


def test_zero_to_seven_backfill_notify_sends_o2o_message(tmp_path):
    """Бэкафилл с сообщениями у того, кто давно дошёл до 7: о разборе резюме он тоже узнаёт."""
    _ready(tmp_path)
    _make_ambassador(100)
    for tid in range(201, 208):
        _seed_user(tid, referrer_id=100, status="approved")
    _on()
    _run(amb_tiers.check_tiers([100], notify=True, force=True))
    assert [e["tier"] for e in _events()] == [2, 3]


# ── Квота 0 и пороги ─────────────────────────────────────────────────────────────────────

def test_quota_zero_means_no_slots(tmp_path):
    from settings_schema import _parse_setting

    assert validate_setting_value("amb_o2o_quota", "0") == ("0", None)
    assert _parse_setting("amb_o2o_quota", "0") == 0
    assert _parse_setting("amb_o2o_quota", None) == 15
    assert _parse_setting("amb_o2o_quota", "мусор") == 15
    _ready(tmp_path)
    _on(quota=0)
    _make_ambassador(100)
    for tid in (201, 202, 203):
        _seed_user(tid, referrer_id=100)
    _run(applications.claim_approve_all_with_credits(None))
    assert _tiers(100) == [(1, None), (2, "waitlist")]


def test_other_int_keys_zero_still_default():
    """allow_zero — только у квоты: у прочих int 0 по-прежнему = значение по умолчанию."""
    from settings_schema import _parse_setting

    assert _parse_setting("amb_tier2_threshold", "0") == 3


def test_threshold_zero_rejected_with_hint():
    for key in ("amb_tier1_threshold", "amb_tier2_threshold", "amb_tier3_threshold"):
        value, error = validate_setting_value(key, "0")
        assert value is None and "1 или больше" in error, key
    assert validate_setting_value("amb_tier1_threshold", "1") == ("1", None)


def test_threshold_order_pure_check():
    from settings_validation import amb_threshold_order_error

    current = {"amb_tier1_threshold": 1, "amb_tier2_threshold": 3, "amb_tier3_threshold": 7}
    assert amb_threshold_order_error("amb_tier2_threshold", "5", current) is None
    assert amb_threshold_order_error("amb_o2o_quota", "0", current) is None
    error = amb_threshold_order_error("amb_tier1_threshold", "3", current)
    assert error and "Ступень 2" in error and "1 / 3 / 7" in error
    assert amb_threshold_order_error("amb_tier3_threshold", "3", current)
    assert amb_threshold_order_error("amb_tier2_threshold", "8", current)


def test_threshold_order_checked_on_save_bot_and_web(tmp_path):
    import settings_ops

    _ready(tmp_path)
    assert _run(settings_ops.cross_setting_error("amb_tier2_threshold", "1")) is not None
    assert _run(settings_ops.cross_setting_error("amb_tier2_threshold", "4")) is None
    assert _run(settings_ops.cross_setting_error("amb_tier2_threshold", "-")) is None
    check = _run(settings_ops.validate_batch_item(
        "amb_tier3_threshold", "2", visible_codes=[], selected_city=None, cities_on=False,
    ))
    assert check.error and "Ступень 3" in check.error

    import inspect

    from handlers import admin_settings
    assert "cross_setting_error" in inspect.getsource(admin_settings.settings_edit_value)


# ── Ступени при вступлении в амбассадоры ──────────────────────────────────────────────────

def test_new_ambassador_gets_tiers_for_earlier_approvals(tmp_path):
    """Приглашённых одобрили, когда пригласивший ещё не был амбассадором: ступени приходят в
    момент вступления, а не только со следующим одобрением."""
    _ready(tmp_path)
    _on()
    _seed_user(100, status="approved")
    for tid in (201, 202, 203):
        _seed_user(tid, referrer_id=100)
    _run(applications.claim_approve_all_with_credits(None))
    assert _tiers(100) == []
    _run(db.set_ambassador_flag(100, active=True, at="2026-09-30 12:00:00"))
    _run(amb_tiers.check_tiers_for_new_ambassador(100))
    assert _tiers(100) == [(1, None), (2, "granted")]
    assert [e["tier"] for e in _events()] == [2]


def test_new_ambassador_check_is_fail_soft_and_off_noop(tmp_path, monkeypatch):
    _ready(tmp_path)
    _make_ambassador(100)
    _seed_user(201, referrer_id=100, status="approved")
    _run(amb_tiers.check_tiers_for_new_ambassador(100))  # программа выключена
    assert _tiers(100) == []
    _on()

    async def boom(*_a, **_kw):
        raise RuntimeError("сбой")

    monkeypatch.setattr(amb_tiers, "check_tiers", boom)
    _run(amb_tiers.check_tiers_for_new_ambassador(100))  # не бросает


def test_every_ambassador_join_path_checks_tiers():
    """Сторож: каждая точка, где человек становится амбассадором, зовёт проверку ступеней."""
    import inspect

    from handlers import reg_ambassador, user_actions
    from miniapp.routers import form

    for fn in (reg_ambassador.regamb_want, user_actions.ambassador_join, form.draft_ambassador):
        source = inspect.getsource(fn)
        assert "set_ambassador_flag" in source and "check_tiers_for_new_ambassador" in source, fn


def test_deadline_minute_is_inclusive(tmp_path):
    """«2026-11-14 23:59» — одобрение в 23:59:30 ещё даёт ступень, в 00:00:00 уже нет."""
    _ready(tmp_path)
    _on(deadline="2026-11-14 23:59")
    assert _run(amb_tiers.deadline_passed(datetime(2026, 11, 14, 23, 59, 0))) is False
    assert _run(amb_tiers.deadline_passed(datetime(2026, 11, 14, 23, 59, 59, 999999))) is False
    assert _run(amb_tiers.deadline_passed(datetime(2026, 11, 15, 0, 0, 0))) is True
    _make_ambassador(100)
    _seed_user(201, referrer_id=100, status="approved")
    _run(amb_tiers.check_tiers([100], now=datetime(2026, 11, 14, 23, 59, 30)))
    assert _tiers(100) == [(1, None)]
