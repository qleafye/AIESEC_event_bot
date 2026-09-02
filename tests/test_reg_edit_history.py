"""Phase 21 Plan 05 Task 1 (FORM-SYNC-04, D-12, D-13, D-15, D-16): контракт узкого UPDATE
ответов (`update_user_answers`) и следа правок (`reg_answer_history`, `users.edited_at`/
`edited_source`), снятый ДО реализации (RED). Функции `update_user_answers`/
`record_answer_history`/`get_answer_history`/`mark_user_edited` в `database/db.py` ещё не
существуют — этот файл обязан падать на ImportError/AttributeError с их именами.

pytest-asyncio недоступен — async через asyncio.run(), фикстура временной БД — тот же приём,
что `tests/test_reg_resume_ttl_260820.py::_ready(tmp_path)`.
"""
import asyncio
from datetime import datetime, timedelta

from config import config
from database import db

USER_ID = 900300400

ALLOWED_COLUMNS = {"full_name", "phone", "university"}


def _ready(tmp_path, name="reg_edit_history.db"):
    config.DB_PATH = str(tmp_path / name)
    asyncio.run(db.init_db())


async def _seed_user():
    await db.add_user({
        "telegram_id": USER_ID,
        "full_name": "Иван Иванов",
        "phone": "+79990000000",
        "university": "СПбГУ",
        "referrer_id": 42,
        "registration_date": "2026-08-01 10:00:00",
        "source": "friend",
        "event_city": "msk",
    })
    await db.set_user_status(USER_ID, "approved")


# ── update_user_answers: узкий UPDATE по allowlist ─────────────────────────────────────────

def test_update_user_answers_writes_only_allowed_intersection(tmp_path):
    _ready(tmp_path)

    async def go():
        await _seed_user()
        n = await db.update_user_answers(
            USER_ID, {"full_name": "Пётр Петров", "status": "pending"},
            allowed_columns=ALLOWED_COLUMNS,
        )
        return n, await db.get_user(USER_ID)

    n, user = asyncio.run(go())
    assert n == 1  # только full_name — status вне allowlist
    assert user["full_name"] == "Пётр Петров"
    assert user["status"] == "approved"  # не тронут


def test_update_user_answers_does_not_touch_attribution_or_status(tmp_path):
    _ready(tmp_path)

    async def go():
        await _seed_user()
        await db.update_user_answers(
            USER_ID, {"phone": "+79991112233"}, allowed_columns=ALLOWED_COLUMNS,
        )
        return await db.get_user(USER_ID)

    user = asyncio.run(go())
    assert user["registration_date"] == "2026-08-01 10:00:00"
    assert user["referrer_id"] == 42
    assert user["username"] is None or user["username"] == user.get("username")
    assert user["source"] == "friend"
    assert user["status"] == "approved"
    assert user["payment_status"] == "not_paid"


def test_update_user_answers_empty_intersection_writes_nothing(tmp_path):
    _ready(tmp_path)

    async def go():
        await _seed_user()
        n = await db.update_user_answers(
            USER_ID, {"status": "pending", "referrer_id": 999},
            allowed_columns=ALLOWED_COLUMNS,
        )
        return n, await db.get_user(USER_ID)

    n, user = asyncio.run(go())
    assert n == 0
    assert user["status"] == "approved"
    assert user["referrer_id"] == 42


# ── record_answer_history / get_answer_history ─────────────────────────────────────────────

def test_record_and_get_answer_history_newest_first(tmp_path):
    _ready(tmp_path)

    async def go():
        await _seed_user()
        await db.record_answer_history(
            USER_ID,
            [{"column": "full_name", "old": "Иван Иванов", "new": "Пётр Петров"}],
            source="miniapp", season="2026",
        )
        await db.record_answer_history(
            USER_ID,
            [{"column": "phone", "old": "+79990000000", "new": "+79991112233"}],
            source="bot", season="2026",
        )
        return await db.get_answer_history(USER_ID, limit=5)

    rows = asyncio.run(go())
    assert len(rows) == 2
    # новыми вперёд -> последняя запись (phone/bot) первая
    assert rows[0]["source"] == "bot"
    assert rows[1]["source"] == "miniapp"
    assert isinstance(rows[0]["changes"], list)
    assert rows[0]["changes"][0]["column"] == "phone"


def test_record_answer_history_empty_changes_no_row(tmp_path):
    _ready(tmp_path)

    async def go():
        await _seed_user()
        await db.record_answer_history(USER_ID, [], source="bot", season="2026")
        return await db.get_answer_history(USER_ID, limit=5)

    rows = asyncio.run(go())
    assert rows == []


# ── mark_user_edited ─────────────────────────────────────────────────────────────────────

def test_mark_user_edited_sets_and_updates_fields(tmp_path):
    _ready(tmp_path)

    async def go():
        await _seed_user()
        await db.mark_user_edited(USER_ID, "miniapp")
        first = await db.get_user(USER_ID)
        # искусственно откатываем edited_at, чтобы убедиться, что повторный вызов его продвигает
        async with db._connect() as conn:
            stamp = (datetime.now() - timedelta(hours=1)).strftime("%Y-%m-%d %H:%M:%S")
            await conn.execute(
                "UPDATE users SET edited_at = ? WHERE telegram_id = ?", (stamp, USER_ID)
            )
            await conn.commit()
        rolled_back = await db.get_user(USER_ID)
        await db.mark_user_edited(USER_ID, "bot")
        second = await db.get_user(USER_ID)
        return first, rolled_back, second

    first, rolled_back, second = asyncio.run(go())
    assert first["edited_at"] is not None
    assert first["edited_source"] == "miniapp"
    assert second["edited_at"] > rolled_back["edited_at"]
    assert second["edited_source"] == "bot"


# ── миграция на непустой БД: старые данные не теряются ─────────────────────────────────────

def test_new_tables_and_columns_do_not_wipe_existing_data(tmp_path):
    _ready(tmp_path)

    async def go():
        await _seed_user()
        await db.init_db()  # повторный вызов на непустой БД
        return await db.get_user(USER_ID)

    user = asyncio.run(go())
    assert user is not None
    assert user["full_name"] == "Иван Иванов"
    assert "edited_at" in user
    assert "edited_source" in user
