"""Сверка, переотправка и выдача роли пишут журнал числами и без текста письма."""
import asyncio
import json

import pytest

from config import config
from database import db
from services.applications import decision_delivery
from services.sheets import sheet_reconcile
from tests._dbtpl import fast_init_db

ACTOR = 7
DELEGATE_ID = 330401


def _ready(tmp_path, name="test_admin_actions_card_rest_33.db"):
    config.DB_PATH = str(tmp_path / name)
    fast_init_db()


async def _rows():
    async with db._connect() as conn:
        async with conn.execute(
            "SELECT actor_id, subject_id, action, before_json, after_json "
            "FROM admin_actions ORDER BY id"
        ) as cur:
            raw = await cur.fetchall()
    out = []
    for actor_id, subject_id, action, before_json, after_json in raw:
        out.append({
            "actor_id": actor_id,
            "subject_id": subject_id,
            "action": action,
            "before": json.loads(before_json) if before_json else None,
            "after": json.loads(after_json) if after_json else None,
        })
    return out


@pytest.fixture(autouse=True)
def _clear_inflight():
    sheet_reconcile._INFLIGHT.clear()
    decision_delivery._INFLIGHT.clear()
    yield
    sheet_reconcile._INFLIGHT.clear()
    decision_delivery._INFLIGHT.clear()


async def _empty_report(*, city_scope=None):
    return {"ok": True, "missing_rows": [], "status_mismatch": [], "error": None}


def test_append_missing_journals_counts_only(tmp_path, monkeypatch):
    _ready(tmp_path)
    monkeypatch.setattr(sheet_reconcile, "build_report", _empty_report)

    result = asyncio.run(sheet_reconcile.apply_append_missing(by_admin=ACTOR))
    rows = asyncio.run(_rows())

    assert result["ok"] is True
    assert len(rows) == 1
    row = rows[0]
    assert row["actor_id"] == ACTOR
    assert row["subject_id"] is None
    assert row["action"] == "sheet_append_missing"
    assert row["before"] == {"missing": 0}
    assert row["after"] == {"appended": 0, "failed": 0}
    assert isinstance(row["after"]["failed"], int)
    blob = json.dumps(row["before"], ensure_ascii=False) + json.dumps(row["after"], ensure_ascii=False)
    assert "telegram_id" not in blob


def test_fix_statuses_journals_counts(tmp_path, monkeypatch):
    _ready(tmp_path)
    monkeypatch.setattr(sheet_reconcile, "build_report", _empty_report)

    result = asyncio.run(sheet_reconcile.apply_fix_statuses(by_admin=ACTOR))
    rows = asyncio.run(_rows())

    assert result["ok"] is True
    assert len(rows) == 1
    row = rows[0]
    assert row["subject_id"] is None
    assert row["action"] == "sheet_fix_statuses"
    assert row["before"] == {"mismatch": 0}
    assert row["after"] == {"fixed": 0, "failed": 0}
    assert isinstance(row["after"]["failed"], int)


def test_resend_batch_journals_counts(tmp_path):
    _ready(tmp_path)

    class _Bot:
        async def send_message(self, *args, **kwargs):
            raise AssertionError("пустой пакет не шлёт письма")

    result = asyncio.run(decision_delivery.resend_undelivered_decisions(_Bot(), by_admin=ACTOR))
    rows = asyncio.run(_rows())

    assert result["ok"] is True
    assert len(rows) == 1
    row = rows[0]
    assert row["actor_id"] == ACTOR
    assert row["subject_id"] is None
    assert row["action"] == "resend_decisions"
    assert row["before"] == {"resendable": 0}
    assert row["after"] == {"done": 0, "failed": 0}


def test_resend_one_journals_subject(tmp_path, monkeypatch):
    _ready(tmp_path)

    async def _mark_delivered(bot, tid, decision, reason=None, sheet=False, resend=False):
        async with db._connect() as conn:
            await conn.execute(
                "UPDATE users SET decision_delivery_status = 'delivered' WHERE telegram_id = ?",
                (tid,),
            )
            await conn.commit()

    monkeypatch.setattr(
        "services.applications.application_effects.apply_decision_effects",
        _mark_delivered,
    )

    async def go():
        await db.add_user({
            "telegram_id": DELEGATE_ID,
            "full_name": "Иван Иванов",
            "registration_date": "2026-01-01",
        })
        async with db._connect() as conn:
            await conn.execute(
                "UPDATE users SET status = 'approved' WHERE telegram_id = ?",
                (DELEGATE_ID,),
            )
            await conn.commit()
        result = await decision_delivery.resend_one_decision(object(), DELEGATE_ID, by_admin=ACTOR)
        return result, await db.list_admin_actions(DELEGATE_ID)

    result, rows = asyncio.run(go())
    assert result["ok"] is True and result["delivered"] is True
    assert len(rows) == 1
    row = rows[0]
    assert row["actor_id"] == ACTOR
    assert row["subject_id"] == DELEGATE_ID
    assert row["action"] == "resend_decision"
    assert row["before"] == {"status": "approved"}
    assert row["after"] == {"delivered": True}
    blob = json.dumps(row["before"], ensure_ascii=False) + json.dumps(row["after"], ensure_ascii=False)
    assert "Иван" not in blob


def test_locked_rerun_does_not_journal(tmp_path):
    _ready(tmp_path)
    assert sheet_reconcile._claim("append:*") is True
    try:
        result = asyncio.run(sheet_reconcile.apply_append_missing(by_admin=ACTOR))
        rows = asyncio.run(_rows())
    finally:
        sheet_reconcile._release("append:*")

    assert result["ok"] is False
    assert "уже выполняется" in result["error"]
    assert rows == []


def test_role_grant_journals(tmp_path):
    from handlers.access import admin_roles
    from tests.test_roles_phase8 import FakeCallback

    _ready(tmp_path)
    config.ADMIN_IDS = [ACTOR]
    target = 330402

    async def go():
        cb = FakeCallback(f"roles_addrole:{target}:reg_manager", user_id=ACTOR)
        await admin_roles.roles_assign(cb, bot=None)
        first = await db.list_admin_actions(target)
        again = FakeCallback(f"roles_addrole:{target}:reg_manager", user_id=ACTOR)
        await admin_roles.roles_assign(again, bot=None)
        second = await db.list_admin_actions(target)
        return first, second

    first, second = asyncio.run(go())
    assert len(first) == 1
    row = first[0]
    assert row["action"] == "grant_role"
    assert row["actor_id"] == ACTOR
    assert row["subject_id"] == target
    assert row["before"] == {"role": None}
    assert row["after"] == {"role": "reg_manager", "city": None}
    assert len(second) == 1


def test_role_grant_failure_silent(tmp_path):
    from handlers.access import admin_roles
    from tests.test_roles_phase8 import FakeCallback

    _ready(tmp_path)
    config.ADMIN_IDS = [ACTOR]
    target = 330403

    async def go():
        cb = FakeCallback(f"roles_addrole:{target}:not_a_role", user_id=ACTOR)
        await admin_roles.roles_assign(cb, bot=None)
        return await _rows(), cb.answers

    rows, answers = asyncio.run(go())
    assert rows == []
    assert answers == [("Неизвестная роль", True)]
