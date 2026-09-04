"""Quick 260904-3vm (решение владельца 04.09: «эстафета» вместо двустороннего синхрона фазы 21,
`.planning/uat-logs/UAT-260904-delegate-checklist.md` — D2, D7, D8, D15, D16, E2).

Анкета в каждый момент открыта РОВНО в одном месте. Этот файл — единый набор сторожей всей
эстафеты, дописывается по мере задач плана (владение в БД -> гвард бота -> Mini App API ->
короткий трек/D15). pytest-asyncio недоступен — весь async через asyncio.run() (та же
конвенция, что `tests/test_reg_drafts.py`).
"""
import asyncio

import aiosqlite

from config import config
from database import db as bot_db
from services import reg_handoff
from services.reg_handoff import SURFACE_BOT, SURFACE_APP, draft_holder
import reg_engine

USER_ID = 910100200


def _ready(tmp_path, name="reg_handoff.db"):
    config.DB_PATH = str(tmp_path / name)
    asyncio.run(bot_db.init_db())


def _run(coro):
    return asyncio.run(coro)


async def _fetch_draft_row(telegram_id):
    async with bot_db._connect() as conn:
        conn.row_factory = aiosqlite.Row
        async with conn.execute(
            "SELECT * FROM reg_drafts WHERE telegram_id = ?", (telegram_id,)
        ) as cur:
            row = await cur.fetchone()
            return dict(row) if row else None


# ══════════════════════════════════════════════════════════════════════════════════════════════
# Задача 1 — владение черновиком в БД + событие сброса FSM бота
# ══════════════════════════════════════════════════════════════════════════════════════════════

# ── миграция колонки ────────────────────────────────────────────────────────────────────────

def test_active_surface_column_exists_after_init_db(tmp_path):
    _ready(tmp_path)

    async def go():
        async with bot_db._connect() as conn:
            cur = await conn.execute("PRAGMA table_info(reg_drafts)")
            cols = [r[1] for r in await cur.fetchall()]
            return cols

    cols = _run(go())
    assert "active_surface" in cols


def test_existing_row_without_active_surface_reads_as_none(tmp_path):
    """Строка, вставленная напрямую (симулирует запись ДО фичи, когда колонки не было в
    INSERT) — `active_surface` читается как NULL, данные не теряются."""
    _ready(tmp_path)

    async def go():
        async with bot_db._connect() as conn:
            await conn.execute(
                "INSERT INTO reg_drafts (telegram_id, kind, answers, meta, version, "
                "updated_by, updated_at, created_at) VALUES (?, 'new', '{\"full_name\": \"Иван\"}', "
                "'{}', 1, 'bot', '2026-01-01 00:00:00', '2026-01-01 00:00:00')",
                (USER_ID,),
            )
            await conn.commit()
        return await bot_db.get_reg_draft(USER_ID)

    draft = _run(go())
    assert draft["answers"] == {"full_name": "Иван"}
    assert draft.get("active_surface") is None


# ── draft_holder (чистая функция) ───────────────────────────────────────────────────────────

def test_draft_holder_null_surface_with_answers_is_bot():
    assert draft_holder({"active_surface": None, "answers": {"full_name": "X"}}) == SURFACE_BOT


def test_draft_holder_empty_draft_is_nobody():
    assert draft_holder({"active_surface": None, "answers": {}}) is None


def test_draft_holder_explicit_app_surface_wins_even_if_empty():
    assert draft_holder({"active_surface": "app", "answers": {}}) == SURFACE_APP


def test_draft_holder_none_draft_is_nobody():
    assert draft_holder(None) is None


def test_draft_holder_submitting_is_nobody():
    assert draft_holder({
        "active_surface": "app", "submitting_at": "2026-09-04 00:00:00", "answers": {"a": 1},
    }) is None


# ── upsert_reg_draft / set_reg_draft_surface ────────────────────────────────────────────────

def test_upsert_new_row_writes_active_surface(tmp_path):
    _ready(tmp_path)

    async def go():
        await bot_db.upsert_reg_draft(
            USER_ID, kind="new", step="full_name", patch={"full_name": "Иван"},
            source="bot", active_surface="bot",
        )
        return await bot_db.get_reg_draft(USER_ID)

    draft = _run(go())
    assert draft["active_surface"] == "bot"


def test_upsert_without_active_surface_does_not_overwrite(tmp_path):
    _ready(tmp_path)

    async def go():
        await bot_db.upsert_reg_draft(
            USER_ID, kind="new", step="full_name", patch={"full_name": "Иван"},
            source="miniapp", active_surface="app",
        )
        await bot_db.upsert_reg_draft(
            USER_ID, kind="new", step="email", patch={"email": "a@b.ru"},
            source="miniapp",
        )
        return await bot_db.get_reg_draft(USER_ID)

    draft = _run(go())
    assert draft["active_surface"] == "app"


def test_set_reg_draft_surface_touches_only_surface(tmp_path):
    _ready(tmp_path)

    async def go():
        v1 = await bot_db.upsert_reg_draft(
            USER_ID, kind="new", step="full_name", patch={"full_name": "Иван"},
            source="bot", active_surface="bot",
        )
        await bot_db.set_reg_draft_surface(USER_ID, "app")
        draft = await bot_db.get_reg_draft(USER_ID)
        return v1, draft

    v1, draft = _run(go())
    assert draft["active_surface"] == "app"
    assert draft["version"] == v1
    assert draft["answers"] == {"full_name": "Иван"}


def test_set_reg_draft_surface_missing_row_is_noop(tmp_path):
    _ready(tmp_path)
    _run(bot_db.set_reg_draft_surface(USER_ID, "app"))  # не должно бросать


# ── has_submitted_anketa ─────────────────────────────────────────────────────────────────────

def test_has_submitted_anketa_true_when_submitted_this_season():
    row = {"season": "2026", "status": "approved", "registration_date": "2026-09-01"}
    assert reg_engine.has_submitted_anketa(row, "2026") is True


def test_has_submitted_anketa_false_when_registration_date_empty():
    row = {"season": "2026", "status": "approved", "registration_date": ""}
    assert reg_engine.has_submitted_anketa(row, "2026") is False


def test_has_submitted_anketa_false_when_rejected():
    row = {"season": "2026", "status": "rejected", "registration_date": "2026-09-01"}
    assert reg_engine.has_submitted_anketa(row, "2026") is False


def test_has_submitted_anketa_false_when_no_row():
    assert reg_engine.has_submitted_anketa(None, "2026") is False


def test_has_submitted_anketa_false_when_season_mismatch():
    row = {"season": "2025", "status": "approved", "registration_date": "2025-09-01"}
    assert reg_engine.has_submitted_anketa(row, "2026") is False


# ── miniapp/outbox.py: reg_fsm_reset kind ───────────────────────────────────────────────────

def test_enqueue_reg_fsm_reset_does_not_raise(tmp_path):
    _ready(tmp_path)
    from miniapp import outbox as miniapp_outbox_module

    row_id = _run(miniapp_outbox_module.enqueue(
        "reg_fsm_reset", {"telegram_id": USER_ID, "reason": "takeover"},
    ))
    assert row_id is not None


def test_enqueue_unknown_kind_still_raises():
    from miniapp import outbox as miniapp_outbox_module
    import pytest

    with pytest.raises(ValueError):
        _run(miniapp_outbox_module.enqueue("not_a_real_kind", {}))


# ── services/miniapp_outbox.py::_handle_row("reg_fsm_reset", ...) ──────────────────────────

class _FakeBot:
    id = 42

    def __init__(self):
        self.sent = []

    async def send_message(self, chat_id, text, **kwargs):
        self.sent.append((chat_id, text))


def test_reg_fsm_reset_takeover_clears_fsm_and_notifies(monkeypatch):
    from aiogram.fsm.storage.memory import MemoryStorage
    from aiogram.fsm.storage.base import StorageKey
    from services import miniapp_outbox as svc_outbox

    storage = MemoryStorage()
    svc_outbox.init_fsm_storage(storage)
    try:
        bot = _FakeBot()
        key = StorageKey(bot_id=bot.id, chat_id=USER_ID, user_id=USER_ID)
        _run(storage.set_state(key, "Registration:course"))
        _run(storage.set_data(key, {"full_name": "Иван"}))

        async def fake_get_setting_typed(k):
            return "📱 Анкета открыта в приложении"

        monkeypatch.setattr(svc_outbox, "get_setting_typed", fake_get_setting_typed)

        _run(svc_outbox._handle_row(bot, "reg_fsm_reset", {
            "telegram_id": USER_ID, "reason": "takeover",
        }))

        assert _run(storage.get_state(key)) is None
        assert _run(storage.get_data(key)) == {}
        assert len(bot.sent) == 1
        assert bot.sent[0][0] == USER_ID
    finally:
        svc_outbox.init_fsm_storage(None)


def test_reg_fsm_reset_submitted_clears_fsm_without_notifying():
    from aiogram.fsm.storage.memory import MemoryStorage
    from aiogram.fsm.storage.base import StorageKey
    from services import miniapp_outbox as svc_outbox

    storage = MemoryStorage()
    svc_outbox.init_fsm_storage(storage)
    try:
        bot = _FakeBot()
        key = StorageKey(bot_id=bot.id, chat_id=USER_ID, user_id=USER_ID)
        _run(storage.set_state(key, "Registration:course"))

        _run(svc_outbox._handle_row(bot, "reg_fsm_reset", {
            "telegram_id": USER_ID, "reason": "submitted",
        }))

        assert _run(storage.get_state(key)) is None
        assert bot.sent == []
    finally:
        svc_outbox.init_fsm_storage(None)


def test_reg_fsm_reset_without_init_fsm_storage_is_fail_soft():
    from services import miniapp_outbox as svc_outbox

    svc_outbox.init_fsm_storage(None)
    bot = _FakeBot()
    # Не должно бросать — просто предупреждение в лог.
    _run(svc_outbox._handle_row(bot, "reg_fsm_reset", {
        "telegram_id": USER_ID, "reason": "takeover",
    }))
    assert bot.sent == []
