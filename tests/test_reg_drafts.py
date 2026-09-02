"""Phase 21 Plan 05 Task 1 (FORM-SYNC-02, D-19, D-21): контракт `reg_drafts` — общего места

анкеты «в полёте» для бота и Mini App, снятый ДО реализации (RED). Функции
`upsert_reg_draft`/`get_reg_draft`/`claim_reg_draft`/`release_reg_draft`/`delete_reg_draft`/
`touch_reg_draft_activity` в `database/db.py` ещё не существуют — этот файл обязан падать
на ImportError/AttributeError с их именами, а не на ошибке фикстуры.

pytest-asyncio недоступен — async через asyncio.run(), фикстура временной БД — тот же приём,
что `tests/test_reg_resume_ttl_260820.py::_ready(tmp_path)`.
"""
import asyncio
from datetime import datetime, timedelta

from config import config
from database import db

USER_ID = 900100200


def _ready(tmp_path, name="reg_drafts.db"):
    config.DB_PATH = str(tmp_path / name)
    asyncio.run(db.init_db())


# ── upsert / get ──────────────────────────────────────────────────────────────────────────

def test_upsert_creates_row_then_bumps_version(tmp_path):
    _ready(tmp_path)

    async def go():
        v1 = await db.upsert_reg_draft(
            USER_ID, kind="new", step="full_name",
            patch={"full_name": "Иван"}, source="bot",
        )
        v2 = await db.upsert_reg_draft(
            USER_ID, kind="new", step="email",
            patch={"email": "a@b.ru"}, source="bot",
        )
        return v1, v2

    v1, v2 = asyncio.run(go())
    assert isinstance(v1, int) and isinstance(v2, int)
    assert v2 > v1


def test_field_versions_set_only_for_patched_columns(tmp_path):
    _ready(tmp_path)

    async def go():
        v1 = await db.upsert_reg_draft(
            USER_ID, kind="new", step="full_name",
            patch={"full_name": "Иван"}, source="bot",
        )
        v2 = await db.upsert_reg_draft(
            USER_ID, kind="new", step="email",
            patch={"email": "a@b.ru"}, source="bot",
        )
        draft = await db.get_reg_draft(USER_ID)
        return v1, v2, draft

    v1, v2, draft = asyncio.run(go())
    fv = draft["meta"]["field_versions"]
    assert fv["full_name"] == v1
    assert fv["email"] == v2
    # full_name не тронут вторым апсертом
    assert fv["full_name"] != v2


def test_answers_holds_only_passed_columns(tmp_path):
    _ready(tmp_path)

    async def go():
        await db.upsert_reg_draft(
            USER_ID, kind="new", step="full_name",
            patch={"full_name": "Иван"}, source="bot",
        )
        return await db.get_reg_draft(USER_ID)

    draft = asyncio.run(go())
    assert draft["answers"] == {"full_name": "Иван"}


def test_service_keys_underscore_prefix_never_land_in_answers(tmp_path):
    _ready(tmp_path)

    async def go():
        await db.upsert_reg_draft(
            USER_ID, kind="new", step="full_name",
            patch={"full_name": "Иван", "_client_ts": 12345, "_nonce": "x"}, source="bot",
        )
        return await db.get_reg_draft(USER_ID)

    draft = asyncio.run(go())
    assert "_client_ts" not in draft["answers"]
    assert "_nonce" not in draft["answers"]
    assert draft["answers"] == {"full_name": "Иван"}


def test_get_reg_draft_returns_dicts_not_json_strings(tmp_path):
    _ready(tmp_path)

    async def go():
        await db.upsert_reg_draft(
            USER_ID, kind="new", step="full_name",
            patch={"full_name": "Иван"}, source="bot",
        )
        return await db.get_reg_draft(USER_ID)

    draft = asyncio.run(go())
    assert isinstance(draft["answers"], dict)
    assert isinstance(draft["meta"], dict)


def test_get_reg_draft_none_when_missing(tmp_path):
    _ready(tmp_path)
    assert asyncio.run(db.get_reg_draft(USER_ID)) is None


# ── claim / release / delete ─────────────────────────────────────────────────────────────

def test_claim_reg_draft_second_call_returns_none(tmp_path):
    _ready(tmp_path)

    async def go():
        await db.upsert_reg_draft(
            USER_ID, kind="new", step="full_name",
            patch={"full_name": "Иван"}, source="bot",
        )
        first = await db.claim_reg_draft(USER_ID)
        second = await db.claim_reg_draft(USER_ID)
        return first, second

    first, second = asyncio.run(go())
    assert isinstance(first, dict)
    assert second is None


def test_release_reg_draft_allows_claim_again(tmp_path):
    _ready(tmp_path)

    async def go():
        await db.upsert_reg_draft(
            USER_ID, kind="new", step="full_name",
            patch={"full_name": "Иван"}, source="bot",
        )
        await db.claim_reg_draft(USER_ID)
        await db.release_reg_draft(USER_ID)
        return await db.claim_reg_draft(USER_ID)

    result = asyncio.run(go())
    assert isinstance(result, dict)


def test_delete_reg_draft_removes_row_idempotent(tmp_path):
    _ready(tmp_path)

    async def go():
        await db.upsert_reg_draft(
            USER_ID, kind="new", step="full_name",
            patch={"full_name": "Иван"}, source="bot",
        )
        await db.delete_reg_draft(USER_ID)
        after_first = await db.get_reg_draft(USER_ID)
        await db.delete_reg_draft(USER_ID)  # повторный вызов не падает
        after_second = await db.get_reg_draft(USER_ID)
        return after_first, after_second

    after_first, after_second = asyncio.run(go())
    assert after_first is None
    assert after_second is None


# ── поля черновика без искажений ─────────────────────────────────────────────────────────

def test_draft_fields_roundtrip_unchanged(tmp_path):
    _ready(tmp_path)

    async def go():
        await db.upsert_reg_draft(
            USER_ID, kind="edit", participant_type="full", event_city="msk",
            step="phone", patch={"phone": "+79990000000"}, source="miniapp",
        )
        return await db.get_reg_draft(USER_ID)

    draft = asyncio.run(go())
    assert draft["kind"] == "edit"
    assert draft["participant_type"] == "full"
    assert draft["event_city"] == "msk"
    assert draft["step"] == "phone"
    assert draft["updated_by"] == "miniapp"


def test_updated_at_advances_on_every_write(tmp_path):
    _ready(tmp_path)

    async def go():
        await db.upsert_reg_draft(
            USER_ID, kind="new", step="full_name",
            patch={"full_name": "Иван"}, source="bot",
        )
        first = await db.get_reg_draft(USER_ID)
        # искусственно откатываем updated_at назад, чтобы гарантировать, что второй апсерт
        # его продвинет вперёд относительно старого значения
        async with db._connect() as conn:
            stamp = (datetime.now() - timedelta(hours=1)).strftime("%Y-%m-%d %H:%M:%S")
            await conn.execute(
                "UPDATE reg_drafts SET updated_at = ? WHERE telegram_id = ?", (stamp, USER_ID)
            )
            await conn.commit()
        rolled_back = await db.get_reg_draft(USER_ID)
        await db.upsert_reg_draft(
            USER_ID, kind="new", step="email",
            patch={"email": "a@b.ru"}, source="bot",
        )
        second = await db.get_reg_draft(USER_ID)
        return first, rolled_back, second

    first, rolled_back, second = asyncio.run(go())
    assert second["updated_at"] > rolled_back["updated_at"]


# ── touch_reg_draft_activity ──────────────────────────────────────────────────────────────

def test_touch_activity_advances_updated_at_without_version_or_answers_change(tmp_path):
    _ready(tmp_path)

    async def go():
        v1 = await db.upsert_reg_draft(
            USER_ID, kind="new", step="full_name",
            patch={"full_name": "Иван"}, source="bot",
        )
        before = await db.get_reg_draft(USER_ID)
        async with db._connect() as conn:
            stamp = (datetime.now() - timedelta(hours=1)).strftime("%Y-%m-%d %H:%M:%S")
            await conn.execute(
                "UPDATE reg_drafts SET updated_at = ? WHERE telegram_id = ?", (stamp, USER_ID)
            )
            await conn.commit()
        rolled_back = await db.get_reg_draft(USER_ID)
        await db.touch_reg_draft_activity(USER_ID)
        after = await db.get_reg_draft(USER_ID)
        return v1, before, rolled_back, after

    v1, before, rolled_back, after = asyncio.run(go())
    assert after["updated_at"] > rolled_back["updated_at"]
    assert after["version"] == v1
    assert after["answers"] == before["answers"]


# ── get_nudge_candidates отсекает активный в приложении черновик (D-21) ───────────────────

def _seed_reg_started(hours_ago: float):
    stamp = (datetime.now() - timedelta(hours=hours_ago)).strftime("%Y-%m-%d %H:%M:%S")

    async def go():
        await db.mark_reg_started(USER_ID, "dasha", "full", "msk")
        async with db._connect() as conn:
            await conn.execute(
                "UPDATE reg_started SET started_at = ? WHERE telegram_id = ?", (stamp, USER_ID)
            )
            await conn.commit()

    asyncio.run(go())


def _cutoff(minutes_ago: float) -> str:
    return (datetime.now() - timedelta(minutes=minutes_ago)).strftime("%Y-%m-%d %H:%M:%S")


def test_nudge_skips_delegate_with_fresh_draft(tmp_path):
    _ready(tmp_path)
    _seed_reg_started(hours_ago=3)  # started_at старый -> кандидат по reg_started

    async def go():
        await db.upsert_reg_draft(
            USER_ID, kind="new", step="phone",
            patch={"phone": "+7999"}, source="miniapp",
        )  # updated_at свежий (только что записан)
        cutoff = _cutoff(minutes_ago=120)
        candidates = await db.get_nudge_candidates(cutoff)
        reg_started_row = await db.get_incomplete_rows()
        return candidates, reg_started_row

    candidates, reg_started_row = asyncio.run(go())
    assert USER_ID not in candidates
    assert len(reg_started_row) == 1  # reg_started не тронут (D-21)


def test_nudge_includes_delegate_with_stale_draft(tmp_path):
    _ready(tmp_path)
    _seed_reg_started(hours_ago=3)

    async def go():
        await db.upsert_reg_draft(
            USER_ID, kind="new", step="phone",
            patch={"phone": "+7999"}, source="miniapp",
        )
        stale_stamp = (datetime.now() - timedelta(hours=5)).strftime("%Y-%m-%d %H:%M:%S")
        async with db._connect() as conn:
            await conn.execute(
                "UPDATE reg_drafts SET updated_at = ? WHERE telegram_id = ?",
                (stale_stamp, USER_ID),
            )
            await conn.commit()
        cutoff = _cutoff(minutes_ago=120)
        candidates = await db.get_nudge_candidates(cutoff)
        reg_started_row = await db.get_incomplete_rows()
        return candidates, reg_started_row

    candidates, reg_started_row = asyncio.run(go())
    assert USER_ID in candidates
    assert len(reg_started_row) == 1


def test_nudge_includes_delegate_without_any_draft(tmp_path):
    _ready(tmp_path)
    _seed_reg_started(hours_ago=3)

    async def go():
        cutoff = _cutoff(minutes_ago=120)
        candidates = await db.get_nudge_candidates(cutoff)
        reg_started_row = await db.get_incomplete_rows()
        return candidates, reg_started_row

    candidates, reg_started_row = asyncio.run(go())
    assert USER_ID in candidates
    assert len(reg_started_row) == 1
