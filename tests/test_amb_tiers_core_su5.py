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
