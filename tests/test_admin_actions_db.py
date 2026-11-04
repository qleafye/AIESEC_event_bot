"""Журнал действий менеджера по карточке: запись, список, удаление вместе с делегатом."""
import asyncio
import inspect
import re

from config import config
from database import db
from tests._dbtpl import fast_init_db

_STAMP = re.compile(r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}$")


def _ready(tmp_path, name="test_admin_actions.db"):
    config.DB_PATH = str(tmp_path / name)
    fast_init_db()


def test_record_admin_action_roundtrip(tmp_path):
    _ready(tmp_path)

    async def go():
        new_id = await db.record_admin_action(
            actor_id=9,
            subject_id=5,
            action="city_move",
            before={"event_city": "spb"},
            after={"event_city": "msk"},
        )
        rows = await db.list_admin_actions(5)
        return new_id, rows

    new_id, rows = asyncio.run(go())
    assert new_id == rows[0]["id"]
    assert rows[0]["actor_id"] == 9
    assert rows[0]["subject_id"] == 5
    assert rows[0]["action"] == "city_move"
    assert rows[0]["before"] == {"event_city": "spb"}
    assert rows[0]["after"] == {"event_city": "msk"}
    assert isinstance(rows[0]["before"], dict)
    assert isinstance(rows[0]["after"], dict)
    assert _STAMP.match(rows[0]["created_at"])


def test_record_rejects_empty_action(tmp_path):
    _ready(tmp_path)

    async def go():
        try:
            await db.record_admin_action(actor_id=1, subject_id=2, action="  ")
        except ValueError:
            pass
        else:
            raise AssertionError("пустой action должен отказать")
        async with db._connect() as conn:
            async with conn.execute("SELECT COUNT(*) FROM admin_actions") as cur:
                return (await cur.fetchone())[0]

    assert asyncio.run(go()) == 0


def test_null_before_after_stay_null(tmp_path):
    _ready(tmp_path)

    async def go():
        await db.record_admin_action(
            actor_id=1, subject_id=2, action="reset_stuck", before=None, after=None,
        )
        async with db._connect() as conn:
            async with conn.execute(
                "SELECT before_json, after_json FROM admin_actions"
            ) as cur:
                return await cur.fetchone()

    before_json, after_json = asyncio.run(go())
    assert before_json is None
    assert after_json is None
    listed = asyncio.run(db.list_admin_actions(2))
    assert listed[0]["before"] is None
    assert listed[0]["after"] is None


def test_list_newest_first_and_ignores_other_subject(tmp_path):
    _ready(tmp_path)

    async def go():
        await db.record_admin_action(actor_id=1, subject_id=5, action="grant_edit")
        await db.record_admin_action(actor_id=1, subject_id=5, action="revoke_edit")
        await db.record_admin_action(actor_id=1, subject_id=6, action="grant_role")
        return await db.list_admin_actions(5)

    rows = asyncio.run(go())
    assert [row["action"] for row in rows] == ["revoke_edit", "grant_edit"]
    assert all(row["subject_id"] == 5 for row in rows)


def test_record_does_not_touch_answer_history(tmp_path):
    _ready(tmp_path)

    async def go():
        await db.record_admin_action(
            actor_id=9, subject_id=5, action="city_move",
            before={"event_city": "spb"}, after={"event_city": "msk"},
        )
        return await db.get_answer_history(5)

    assert asyncio.run(go()) == []


def test_record_does_not_schedule_sheet_logs(tmp_path):
    _ready(tmp_path)
    source = inspect.getsource(db.record_admin_action)
    assert "record_answer_history" not in source
    assert "schedule_sheet_logs_sync" not in source
    assert "services.sheets" not in source

    async def go():
        await db.record_admin_action(actor_id=1, subject_id=2, action="grant_edit")

    asyncio.run(go())


def test_purge_drops_subject_keeps_actor_and_batch(tmp_path):
    _ready(tmp_path)

    async def go():
        first = await db.record_admin_action(
            actor_id=9, subject_id=5, action="city_move",
        )
        second = await db.record_admin_action(
            actor_id=5, subject_id=6, action="grant_role",
        )
        third = await db.record_admin_action(
            actor_id=9, subject_id=None, action="sheet_fix_statuses",
        )
        await db.purge_user(5)
        async with db._connect() as conn:
            async with conn.execute(
                "SELECT id, subject_id, actor_id, action FROM admin_actions ORDER BY id"
            ) as cur:
                left = [tuple(row) for row in await cur.fetchall()]
        return first, second, third, left

    first, second, third, left = asyncio.run(go())
    assert first not in {row[0] for row in left}
    assert left == [
        (second, 6, 5, "grant_role"),
        (third, None, 9, "sheet_fix_statuses"),
    ]
