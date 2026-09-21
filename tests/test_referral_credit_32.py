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
import json
import pathlib
import re
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
    # status="approved" — амбассадор сам уже одобренный делегат, иначе дефолтный "pending"
    # (см. _seed_user) подхватило бы его же строку под массовое одобрение в тестах задачи 2.
    _seed_user(tid, event_city=event_city, full_name=full_name, status="approved")
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


# ══════════════════════════════════════════════════════════════════════════════════════════
# Задача 2: массовое одобрение и авто-одобрение — два оставшихся шва + тест-сторож
# ══════════════════════════════════════════════════════════════════════════════════════════

def test_bulk_approve_credits_three_invitees_of_one_ambassador(tmp_path):
    """Врезка №2 (массовое одобрение): «Принять всех» на 3 приглашённых одного амбассадора —
    3 строки referral_credits, сводка возвращает правильные числа (D-20/D-22)."""
    _ready(tmp_path)
    _run(db.set_setting("ambassador_referral_coins", "30"))
    _make_ambassador(5001)
    for tid in (6001, 6002, 6003):
        _seed_user(tid, referrer_id=5001)

    ids, summary = _run(applications.claim_approve_all_with_credits(None))

    assert sorted(ids) == [6001, 6002, 6003]
    assert summary == {"credited": 3, "coins": 90, "ambassadors": 1}
    assert _referral_credit_count() == 3


def test_stale_approve_all_second_click_no_new_credits(tmp_path):
    """Устаревшая кнопка «Принять всех», нажатая второй раз — approve_all_pending возвращает
    пустой список (WR-04), credit_for_approved_bulk по пустому списку не создаёт итераций,
    сводка нулевая — вторая строка в тексте подтверждения не появляется."""
    _ready(tmp_path)
    _run(db.set_setting("ambassador_referral_coins", "30"))
    _make_ambassador(5002)
    _seed_user(6004, referrer_id=5002)

    ids1, summary1 = _run(applications.claim_approve_all_with_credits(None))
    ids2, summary2 = _run(applications.claim_approve_all_with_credits(None))

    assert ids1 == [6004]
    assert summary1 == {"credited": 1, "coins": 30, "ambassadors": 1}
    assert ids2 == []
    assert summary2 == {"credited": 0, "coins": 0, "ambassadors": 0}
    assert _referral_credit_count() == 1


def test_full_approval_auto_credits_ambassador(tmp_path, monkeypatch):
    """Врезка №3 (авто-одобрение): `full_approval=auto` зовёт `credit_for_approved` напрямую
    внутри `services/reg_finalize.py` — единственный путь, который не проходит ни через
    `claim_approve`, ни через `claim_approve_all_with_credits`."""
    from services import reg_finalize as rf

    _ready(tmp_path)
    monkeypatch.setattr(config, "ADMIN_IDS", [])
    _make_ambassador(7001)

    async def go():
        await db.set_setting("registration_mode", "full")
        await db.set_setting("full_approval", "auto")
        await db.set_setting("ambassador_referral_coins", "40")
        draft = {
            "telegram_id": 8001, "kind": "new",
            "answers": {"full_name": "Новый Делегат"},
            "meta": {"referrer_id": 7001},
        }
        return await rf.finalize_data(8001, "@newbie", draft)

    result = _run(go())

    assert result["status"] == "approved"
    assert _referral_credit_count() == 1
    rows = _coins_rows(source="referral", user_id=7001)
    assert len(rows) == 1 and rows[0][1] == 40


def test_auto_rejected_applicant_no_credit(tmp_path):
    """Автоотказ фазы 31 побеждает даже при `full_approval=auto` — статус становится
    `rejected`, а не `approved`, начисления нет (D-20)."""
    from services import reg_finalize as rf

    _ready(tmp_path)
    _make_ambassador(7002)

    async def go():
        await db.set_setting("registration_mode", "full")
        await db.set_setting("full_approval", "auto")
        await db.set_setting("ambassador_referral_coins", "40")
        await db.set_setting("reject_rules_enabled", "on")
        await db.create_reject_rule(
            name=None, city=None, tracks=json.dumps(["full"]),
            conditions=json.dumps([[{"step": "course", "op": "in", "values": ["1"]}]]),
            action="reject", reject_text="Мест нет.", enabled=1, created_by=1,
        )
        draft = {
            "telegram_id": 8002, "kind": "new",
            "answers": {"full_name": "Автоотказник", "course": "1"},
            "meta": {"referrer_id": 7002},
        }
        return await rf.finalize_data(8002, "@x", draft)

    result = _run(go())

    assert result["status"] == "rejected"
    assert _referral_credit_count() == 0


# ── Тест-сторож швов (D-20, T-32-05-07): список мест, где users.status становится 'approved' ─

_REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
_SCAN_DIRS = ("services", "handlers", "miniapp", "tools", "database")
_RAW_SQL_RE = re.compile(r"SET status\s*=\s*'approved'")
_SET_STATUS_CALL_RE = re.compile(r"\bset_user_status\(")

# Три боевых шва (начисляют через credit_for_approved/credit_for_approved_bulk) и два
# осознанных исключения (dev-инструменты, начисления НЕ делают, T-32-05-07).
_EXPECTED_APPROVAL_WRITERS = {
    "database/db.py": (
        "боевой шов: approve_user_atomic + approve_all_pending (RAW UPDATE) — их зовут "
        "services.applications.claim_approve / claim_approve_all_with_credits, которые сами "
        "зовут credit_for_approved(_bulk)"
    ),
    "services/reg_finalize.py": (
        "боевой шов: full_approval=auto/short_approval=auto/party_approval=auto зовёт "
        "credit_for_approved напрямую (план 32-05, задача 2)"
    ),
    "handlers/uat_seed.py": (
        "осознанное исключение (T-32-05-07): сидер состояний команды /uat на стенде — "
        "не боевой путь одобрения, начисления намеренно нет"
    ),
    "tools/shoot_screens.py": (
        "осознанное исключение (T-32-05-07): генератор скриншотов для документации — "
        "не боевой путь одобрения, начисления намеренно нет"
    ),
}


def _scan_approval_status_writers() -> dict[str, list[int]]:
    found: dict[str, list[int]] = {}
    for top in _SCAN_DIRS:
        top_dir = _REPO_ROOT / top
        if not top_dir.exists():
            continue
        for path in top_dir.rglob("*.py"):
            rel = path.relative_to(_REPO_ROOT).as_posix()
            try:
                lines = path.read_text(encoding="utf-8").splitlines()
            except UnicodeDecodeError:
                continue
            for i, line in enumerate(lines, start=1):
                if _RAW_SQL_RE.search(line):
                    found.setdefault(rel, []).append(i)
                elif _SET_STATUS_CALL_RE.search(line) and "def set_user_status" not in line:
                    found.setdefault(rel, []).append(i)
    return found


def test_approval_status_writers_guard():
    """Обход исходников: все места, где `users.status` становится `'approved'` (RAW UPDATE
    или вызов `set_user_status`), обязаны быть в явном ожидаемом списке. Появление НОВОГО
    файла означает новый путь одобрения — он либо начисляет (врезан `credit_for_approved`),
    либо добавлен в исключения с объяснением, а не тихо забыт."""
    found = _scan_approval_status_writers()
    actual_files = set(found)
    expected_files = set(_EXPECTED_APPROVAL_WRITERS)

    unexpected = actual_files - expected_files
    assert not unexpected, (
        "Новое место, где заявка становится одобренной: "
        + "; ".join(f"{f}:{ln}" for f in sorted(unexpected) for ln in found[f])
        + " — либо врежьте credit_for_approved, либо добавьте файл в список исключений "
        "с объяснением (tests/test_referral_credit_32.py::_EXPECTED_APPROVAL_WRITERS)."
    )

    missing = expected_files - actual_files
    assert not missing, f"Ожидаемые швы пропали из исходников: {missing} — план устарел?"


# ══════════════════════════════════════════════════════════════════════════════════════════
# Задача 3: подсказка модератору на карточке заявки + бэкафилл задним числом
# ══════════════════════════════════════════════════════════════════════════════════════════

def _badge_kinds(payload):
    return [b["kind"] for b in payload["badges"]]


def test_referrer_badge_shown_with_count_when_referrer_is_ambassador(tmp_path):
    _ready(tmp_path)
    _run(db.set_setting("ambassador_referral_coins", "20"))
    _make_ambassador(9001, full_name="Амбассадор Иванов")
    # Один уже одобренный приглашённый этого амбассадора — счётчик должен увидеть 1.
    _seed_user(9101, referrer_id=9001)
    _run(applications.claim_approve(9101))
    assert _referral_credit_count() == 1

    # Новая заявка ТОГО ЖЕ амбассадора, ещё не одобрена — карточка должна показать бейдж.
    _seed_user(9102, referrer_id=9001)
    payload = _run(applications.card_payload(_run(db.get_user(9102))))

    assert "referrer" in _badge_kinds(payload)
    referrer_badge = next(b for b in payload["badges"] if b["kind"] == "referrer")
    assert "Амбассадор Иванов" in referrer_badge["text"]
    assert "1" in referrer_badge["text"]


def test_referrer_badge_absent_when_referrer_not_ambassador(tmp_path):
    _ready(tmp_path)
    _seed_user(9003)  # обычный делегат, не амбассадор
    _seed_user(9103, referrer_id=9003)

    payload = _run(applications.card_payload(_run(db.get_user(9103))))

    assert "referrer" not in _badge_kinds(payload)


def test_referrer_badge_absent_when_no_referrer(tmp_path):
    _ready(tmp_path)
    _seed_user(9104)

    payload = _run(applications.card_payload(_run(db.get_user(9104))))

    assert "referrer" not in _badge_kinds(payload)


def test_backfill_dry_run_writes_nothing_but_counts_candidates(tmp_path):
    _ready(tmp_path)
    _run(db.set_setting("ambassador_referral_coins", "25"))
    _make_ambassador(9201)
    _seed_user(9301, referrer_id=9201, status="approved")
    _seed_user(9302, referrer_id=9201, status="approved")

    summary = _run(referrals.backfill_approved(dry_run=True))

    assert summary["candidates"] == 2
    assert _referral_credit_count() == 0  # dry-run ничего не пишет


def test_backfill_apply_creates_rows_without_wave_source_backfill(tmp_path):
    _ready(tmp_path)
    _run(db.set_setting("ambassador_referral_coins", "25"))
    _make_ambassador(9202)
    _seed_user(9303, referrer_id=9202, status="approved")

    summary = _run(referrals.backfill_approved(dry_run=False))

    assert summary == {"candidates": 1, "credited": 1, "coins": 25, "ambassadors": 1}
    con = sqlite3.connect(config.DB_PATH)
    try:
        row = con.execute(
            "SELECT wave_id, source FROM referral_credits WHERE invitee_id = ?", (9303,)
        ).fetchone()
    finally:
        con.close()
    assert row == (None, "backfill")


def test_backfill_second_run_does_not_duplicate(tmp_path):
    _ready(tmp_path)
    _run(db.set_setting("ambassador_referral_coins", "25"))
    _make_ambassador(9203)
    _seed_user(9304, referrer_id=9203, status="approved")

    _run(referrals.backfill_approved(dry_run=False))
    summary2 = _run(referrals.backfill_approved(dry_run=False))

    assert summary2 == {"candidates": 0, "credited": 0, "coins": 0, "ambassadors": 0}
    assert _referral_credit_count() == 1


def test_backfill_skips_already_live_credited_invitee(tmp_path):
    """Уже начисленный «живым» путём (source='approval') приглашённый в кандидаты
    бэкафилла не попадает."""
    _ready(tmp_path)
    _run(db.set_setting("ambassador_referral_coins", "25"))
    _make_ambassador(9204)
    _seed_user(9305, referrer_id=9204)
    _run(applications.claim_approve(9305))  # «живое» начисление, source='approval'
    assert _referral_credit_count() == 1

    summary = _run(referrals.backfill_approved(dry_run=True))

    assert summary["candidates"] == 0
    assert _referral_credit_count() == 1


# ── Волна (D-31/D-38): wave_id резолвится в момент начисления, только если пригласивший
# участвует в ней прямо сейчас ─────────────────────────────────────────────────────────────

def test_active_wave_eligible_referrer_gets_wave_id(tmp_path):
    _ready(tmp_path)
    _run(db.set_setting("ambassador_referral_coins", "15"))
    wave_id = _active_wave()  # активная волна, накрывающая «сейчас»
    _make_ambassador(9401, since="2026-01-01 00:00:00")  # стал амбассадором ДО волны
    _seed_user(9501, referrer_id=9401)

    result = _run(applications.claim_approve(9501))
    credit = _run(db.get_referral_credit(9501))

    assert result is True
    assert credit["wave_id"] == wave_id


def test_mid_wave_joiner_referrer_gets_no_wave_id(tmp_path):
    """D-31/D-38: пригласивший стал амбассадором ПОСЛЕ старта активной волны — в неё не
    участвует, начисление идёт (D-37 не нарушен — он ДЕЙСТВУЮЩИЙ амбассадор), но `wave_id`
    остаётся `None` (только общий зачёт)."""
    _ready(tmp_path)
    _run(db.set_setting("ambassador_referral_coins", "15"))
    _active_wave(starts_at="2026-09-01 00:00:00", ends_at="2026-12-31 23:59:59")
    _make_ambassador(9402, since="2026-09-15 00:00:00")  # стал амбассадором ПОСЛЕ старта волны
    _seed_user(9502, referrer_id=9402)

    _run(applications.claim_approve(9502))
    credit = _run(db.get_referral_credit(9502))

    assert credit["wave_id"] is None
