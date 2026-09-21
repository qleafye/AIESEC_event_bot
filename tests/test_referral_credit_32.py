"""Phase 32 План 5 (D-20/D-21/D-22/D-23/D-37): авто-баллы амбассадору за одобренного
приглашённого — одна идемпотентная функция, врезанная во ВСЕ три пути одобрения заявки.

Три раздела — по одному на задачу плана:
- Задача 1: `services/referrals.py` (`credit_for_approved`) + одиночное одобрение
  (`services.applications.claim_approve`).
- Задача 2: массовое одобрение (`claim_approve_all_with_credits`) и авто-одобрение
  (`services/reg_finalize.py`) — плюс тест-сторож швов (список мест, где статус становится
  `'approved'`).
- Задача 3: подсказка модератору на карточке заявки + бэкафилл задним числом.

pytest-asyncio недоступен в этом окружении — async через `asyncio.run()`, фикстура временной
БД — тот же приём, что `tests/test_ambassador_waves_db_32.py::_ready(tmp_path)`.
"""
from __future__ import annotations

import asyncio
import sqlite3

from config import config
from database import db
from services import applications, referrals


def _ready(tmp_path, name="test_referral_credit_32.db"):
    config.DB_PATH = str(tmp_path / name)
    asyncio.run(db.init_db())


def _run(coro):
    return asyncio.run(coro)


def _seed_user(tid, *, referrer_id=None, event_city=None, full_name=None, status="pending"):
    """`_ensure_column(users, status, "TEXT DEFAULT 'approved'")` (обратная совместимость со
    старыми строками до появления колонки статуса) значит, что свежая строка получает
    status='approved' по умолчанию — тесты этого модуля ставят статус ЯВНО, как и
    `tests/test_applications_db.py::_seed_user`, иначе «поданная заявка» в тесте уже была бы
    одобренной."""
    _run(db.add_user({
        "telegram_id": tid,
        "full_name": full_name or f"Delegate {tid}",
        "registration_date": f"2026-09-01 00:00:{tid % 60:02d}",
        "event_city": event_city,
        "referrer_id": referrer_id,
    }))
    _run(db.set_user_status(tid, status))


def _make_ambassador(tid, *, event_city=None, full_name=None, since="2026-01-01 00:00:00"):
    _seed_user(tid, event_city=event_city, full_name=full_name)
    _run(db.set_ambassador_flag(tid, active=True, at=since))


def _active_wave(*, event_city=None, starts_at="2026-09-01 00:00:00", ends_at="2026-12-31 23:59:59"):
    """Волна, накрывающая «сейчас» (используется дефолтный `msk_now()` внутри
    `services.ambassador_waves.current_wave_for` — 2026-09-21 попадает в этот диапазон)."""
    wave_id = _run(db.create_wave(starts_at, ends_at, event_city=event_city))
    _run(db.set_wave_state(wave_id, "active"))
    return wave_id


def _coins_rows(source=None, user_id=None):
    con = sqlite3.connect(config.DB_PATH)
    try:
        sql = "SELECT user_id, delta, reason, source FROM coins WHERE 1=1"
        params = []
        if source is not None:
            sql += " AND source = ?"
            params.append(source)
        if user_id is not None:
            sql += " AND user_id = ?"
            params.append(user_id)
        return con.execute(sql, params).fetchall()
    finally:
        con.close()


def _referral_credit_count():
    con = sqlite3.connect(config.DB_PATH)
    try:
        return con.execute("SELECT COUNT(*) FROM referral_credits").fetchone()[0]
    finally:
        con.close()


# ══════════════════════════════════════════════════════════════════════════════════════════
# Задача 1: services/referrals.py — credit_for_approved + одиночное одобрение
# ══════════════════════════════════════════════════════════════════════════════════════════

def test_aiogram_free():
    from pathlib import Path
    src = Path(referrals.__file__).read_text(encoding="utf-8")
    assert "aiogram" not in src, "services/referrals.py не должен упоминать aiogram"


def test_single_approve_credits_ambassador_once(tmp_path):
    _ready(tmp_path)
    _run(db.set_setting("ambassador_referral_coins", "100"))
    _make_ambassador(1001)
    _seed_user(2001, referrer_id=1001)
    _run(db.approve_user_atomic(2001))  # заявка приглашённого уже одобрена ДО начисления

    result = _run(referrals.credit_for_approved(2001))

    assert result == {"referrer_id": 1001, "coins": 100, "wave_id": None, "invitee_name": "Delegate 2001"}
    assert _referral_credit_count() == 1
    rows = _coins_rows(source="referral", user_id=1001)
    assert len(rows) == 1
    assert rows[0][1] == 100
    assert "2001" in rows[0][2] or "Delegate 2001" in rows[0][2]


def test_credit_fires_through_claim_approve_hook(tmp_path):
    """Врезка №1: `services.applications.claim_approve` зовёт `credit_for_approved`
    ТОЛЬКО при реально выигранном флипе, возвращаемое значение не меняется."""
    _ready(tmp_path)
    _run(db.set_setting("ambassador_referral_coins", "50"))
    _make_ambassador(1002)
    _seed_user(2002, referrer_id=1002)

    won = _run(applications.claim_approve(2002))

    assert won is True
    assert _referral_credit_count() == 1
    assert len(_coins_rows(source="referral", user_id=1002)) == 1


def test_second_approve_call_no_second_credit(tmp_path):
    """Повторный вызов claim_approve на уже одобренной заявке — approve_user_atomic
    возвращает False, второе начисление не создаётся (D-22)."""
    _ready(tmp_path)
    _run(db.set_setting("ambassador_referral_coins", "50"))
    _make_ambassador(1003)
    _seed_user(2003, referrer_id=1003)

    won1 = _run(applications.claim_approve(2003))
    won2 = _run(applications.claim_approve(2003))

    assert won1 is True
    assert won2 is False
    assert _referral_credit_count() == 1


def test_rejected_then_approved_again_no_second_credit(tmp_path):
    """«Отклонили и одобрили снова» (D-22): после первого одобрения заявку вернули в
    pending (revert_user_to_pending — тот же путь, что undo-окно решения) и одобрили
    заново — второго начисления быть не должно, `claim_referral_credit` держит уникальность
    первичным ключом."""
    _ready(tmp_path)
    _run(db.set_setting("ambassador_referral_coins", "50"))
    _make_ambassador(1004)
    _seed_user(2004, referrer_id=1004)

    _run(applications.claim_approve(2004))
    assert _referral_credit_count() == 1

    reverted = _run(db.revert_user_to_pending(2004, "approved"))
    assert reverted is True
    _run(applications.claim_approve(2004))

    assert _referral_credit_count() == 1
    assert len(_coins_rows(source="referral", user_id=1004)) == 1


def test_referrer_not_ambassador_no_credit(tmp_path):
    """D-37: обычный делегат со старой реф-ссылкой (is_ambassador=0) ничего не приносит."""
    _ready(tmp_path)
    _run(db.set_setting("ambassador_referral_coins", "50"))
    _seed_user(1005)  # не амбассадор
    _seed_user(2005, referrer_id=1005)

    _run(applications.claim_approve(2005))

    assert _referral_credit_count() == 0
    assert _coins_rows(source="referral") == []


def test_referrer_left_ambassadors_no_new_credit_but_old_stays(tmp_path):
    """D-37: пригласивший вышел из амбассадоров — за НОВОГО приглашённого начисления нет,
    а прежняя строка referral_credits остаётся на месте."""
    _ready(tmp_path)
    _run(db.set_setting("ambassador_referral_coins", "50"))
    _make_ambassador(1006)
    _seed_user(2006, referrer_id=1006)
    _run(applications.claim_approve(2006))
    assert _referral_credit_count() == 1

    _run(db.set_ambassador_flag(1006, active=False, at="2026-09-10 00:00:00"))
    _seed_user(2007, referrer_id=1006)
    _run(applications.claim_approve(2007))

    assert _referral_credit_count() == 1  # прежняя строка на месте, новой не добавилось


def test_zero_coins_setting_no_credit_at_all(tmp_path):
    """Сумма 0 (дефолт на живом событии) — начисления нет ни в referral_credits, ни в coins."""
    _ready(tmp_path)
    _run(db.set_setting("ambassador_referral_coins", "0"))
    _make_ambassador(1007)
    _seed_user(2008, referrer_id=1007)

    _run(applications.claim_approve(2008))

    assert _referral_credit_count() == 0
    assert _coins_rows(source="referral") == []


def test_pending_application_no_credit(tmp_path):
    """D-20: за ПОДАННУЮ (ещё не одобренную) заявку не начисляется ничего — прямой вызов
    credit_for_approved на pending-заявке безопасен и возвращает None."""
    _ready(tmp_path)
    _run(db.set_setting("ambassador_referral_coins", "50"))
    _make_ambassador(1008)
    _seed_user(2009, referrer_id=1008)  # статус по умолчанию 'pending'

    result = _run(referrals.credit_for_approved(2009))

    assert result is None
    assert _referral_credit_count() == 0
