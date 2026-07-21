"""Quick 260721-msh — «Аламни/айсекер» registration question.

Same convention as tests/test_registration_phase5.py: asyncio.run() + tmp DB_PATH
(pytest-asyncio is unavailable in this env).
"""
import asyncio

from config import config
from database import db
from handlers import registration as reg


def _use_tmp_db(tmp_path):
    config.DB_PATH = str(tmp_path / "test_alumni.db")


# ── question engine ────────────────────────────────────────────────────────────

def test_alumni_step_off_by_default(tmp_path):
    """New question must not appear in a live flow until an admin turns it on."""
    _use_tmp_db(tmp_path)

    async def go():
        await db.init_db()
        steps = await reg._get_enabled_steps({})
        assert "alumni_status" not in steps

    asyncio.run(go())


def test_alumni_step_enabled_when_setting_on(tmp_path):
    _use_tmp_db(tmp_path)

    async def go():
        await db.init_db()
        await db.set_setting("reg_q_alumni_status", "on")
        steps = await reg._get_enabled_steps({})
        assert "alumni_status" in steps
        # Asked right after the phone question (Tatiana's list order).
        assert steps.index("alumni_status") > steps.index("phone") if "phone" in steps else True

    asyncio.run(go())


def test_alumni_step_asked_for_both_party_subtracks(tmp_path):
    """Unlike housing/bed_* it is NOT overnight-only — a no-overnight guest sees it too."""
    _use_tmp_db(tmp_path)

    async def go():
        await db.init_db()
        await db.set_setting("reg_q_alumni_status", "on")
        for track in ("party_overnight", "party_noovernight"):
            steps = await reg._get_enabled_steps({"participant_type": track})
            assert "alumni_status" in steps, track

    asyncio.run(go())


def test_alumni_in_party_preset(tmp_path):
    """The 🎉 Party one-tap preset must switch the question on for party tracks."""
    _use_tmp_db(tmp_path)

    async def go():
        await db.init_db()
        await reg._apply_party_preset()
        assert await db.get_setting("reg_q_alumni_status__party") == "on"
        # Full track untouched (D-07 isolation).
        assert await db.get_setting("reg_q_alumni_status") is None

    asyncio.run(go())


def test_alumni_prompt_default_and_override(tmp_path):
    _use_tmp_db(tmp_path)

    async def go():
        await db.init_db()
        assert await reg._prompt("alumni_status", "Ты аламни или айсекер?") == "Ты аламни или айсекер?"
        await db.set_setting("reg_prompt_alumni_status__party", "Кто ты — аламни или айсекер?")
        got = await reg._prompt("alumni_status", "Ты аламни или айсекер?", "party_overnight")
        assert got == "Кто ты — аламни или айсекер?"

    asyncio.run(go())


# ── sheet columns ──────────────────────────────────────────────────────────────

def test_alumni_column_in_static_headers():
    assert "Аламни/айсекер" in reg.SHEET_HEADERS


def test_alumni_column_in_active_headers_when_on(tmp_path):
    _use_tmp_db(tmp_path)

    async def go():
        await db.init_db()
        await db.set_setting("reg_q_alumni_status", "on")
        assert "Аламни/айсекер" in await reg.active_sheet_headers()

    asyncio.run(go())


def test_alumni_column_in_party_headers_when_on(tmp_path):
    _use_tmp_db(tmp_path)

    async def go():
        await db.init_db()
        await db.set_setting("reg_q_alumni_status__party", "on")
        assert "Аламни/айсекер" in await reg.party_sheet_headers()

    asyncio.run(go())


def test_alumni_value_lands_in_sheet_row():
    values = reg._sheet_value_map({"alumni_status": "Аламни"})
    assert values["Аламни/айсекер"] == "Аламни"


# ── persistence ────────────────────────────────────────────────────────────────

def test_add_user_round_trips_alumni_status(tmp_path):
    _use_tmp_db(tmp_path)

    async def go():
        await db.init_db()
        await db.add_user({
            "telegram_id": 777,
            "full_name": "Иван Аламни",
            "registration_date": "2026-07-21 12:00:00",
            "alumni_status": "Айсекер",
        })
        user = await db.get_user(777)
        assert user["alumni_status"] == "Айсекер"

    asyncio.run(go())
