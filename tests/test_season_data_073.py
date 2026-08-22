"""Phase 07.3 Plan 01 (RET-01) — season as a data entity.

Two registry keys (event_season, start_text_returning), two additive `users` columns
(season/prev_season), add_user extended to carry them, and four standalone accessors that
the rest of the phase (визард «Новый сезон», узнавание делегата, карточка менеджера, импорт)
builds on top of.

pytest-asyncio is unavailable in this env — every async call goes through asyncio.run(),
config.DB_PATH points at a tmp_path file, no conftest.py (project convention, see
tests/test_admin_rereg_260817.py / tests/test_cities_registry_260818.py).
"""
import asyncio

from config import config
from database import db
from handlers import admin
from handlers import admin_settings  # Phase 13 (13-06): settings moved out of admin.py
from settings_schema import SETTINGS_SCHEMA


def _db_ready(tmp_path):
    config.DB_PATH = str(tmp_path / "test_season_data_073.db")
    asyncio.run(db.init_db())


def _full_user_data(telegram_id: int, **overrides) -> dict:
    """A representative full anketa — ~30 columns beyond telegram_id, deliberately spanning
    the whole INSERT column list so a positional-parameter shift (T-073-01-01) is caught."""
    data = {
        "telegram_id": telegram_id,
        "username": "@delegate",
        "full_name": "Иван Иванов",
        "email": "ivan@example.com",
        "age": 20,
        "is_aiesec_member": True,
        "source": "instagram",
        "source_details": "story",
        "education_status": "студент",
        "university": "МГУ",
        "course": "3",
        "specialty": "экономика",
        "work_status": False,
        "work_sphere": None,
        "missing_skills": "-",
        "expectations": "нетворкинг",
        "phone": "+79991234567",
        "city": "Москва",
        "referrer_id": None,
        "registration_date": "2026-08-18T12:00:00",
        "is_ambassador_candidate": False,
        "local_committee": "MSU",
        "position": "member",
        "attendance_format": "offline",
        "comments": "",
        "expectations_ar": None,
        "informal_day": None,
        "resume_file_id": "file_123",
        "resume_text": None,
        "resume_url": None,
        "department": None,
        "aiesec_role": None,
        "needs_certificate": "нет",
        "english_level": "B2",
        "allergies": "нет",
        "food_pref": "обычное",
        "arrival": "поездом",
        "housing": "своё",
        "cc_shop": "нет",
        "exp_organizers": None,
        "exp_content": None,
        "volunteer": "нет",
        "payment_status": "not_paid",
        "payment_option": None,
        "receipt_file_id": None,
        "payment_due": None,
        "paid_at": None,
        "arrival_date": "2026-09-01",
        "birth_date": "2005-01-01",
        "study_field": "экономика",
        "goal": "networking,skills",
        "formats": "lectures",
        "vk_username": "@ivan",
        "transport": "поезд",
        "payment_plan_date": "2026-08-25",
        "bed_sharing": None,
        "bed_partner": None,
        "participant_type": "full",
        "alumni_status": "aiesecer",
        "event_city": "msk",
        "season": "YL'26",
        "prev_season": None,
    }
    data.update(overrides)
    return data


# ── Task 1: реестр ──────────────────────────────────────────────────────────────────────────

def test_registry_has_season_keys():
    assert SETTINGS_SCHEMA["event_season"]["group"] == "event"
    assert SETTINGS_SCHEMA["event_season"]["type"] == "text"
    assert SETTINGS_SCHEMA["event_season"]["default"] is None
    assert (
        "per_city" not in SETTINGS_SCHEMA["event_season"]
        or SETTINGS_SCHEMA["event_season"].get("per_city") is not True
    )

    assert SETTINGS_SCHEMA["start_text_returning"]["group"] == "event"
    assert SETTINGS_SCHEMA["start_text_returning"]["type"] == "text"
    assert SETTINGS_SCHEMA["start_text_returning"]["default"] is None
    assert (
        "per_city" not in SETTINGS_SCHEMA["start_text_returning"]
        or SETTINGS_SCHEMA["start_text_returning"].get("per_city") is not True
    )


def test_registry_new_keys_on_event_screen():
    assert "event_season" in admin_settings._EVENT_GROUP_KEYS
    assert "start_text_returning" in admin_settings._EVENT_GROUP_KEYS
    assert "start_text_returning" in admin_settings.HTML_SETTINGS
    assert "event_season" not in admin_settings.HTML_SETTINGS


def test_registry_event_order_unchanged_for_old_keys():
    old_order_literal = [
        "event_date", "event_time", "event_place_name", "event_place_address",
        "contact_person", "contact_vk", "contact_tg", "start_text", "start_text_registered",
        "event_name", "event_type",
    ]
    new_keys = {
        "event_season", "start_text_returning",
        # Phase 17.1 (17.1-02): recall/возвращение — рядом со start_text_returning.
        "start_returning_cta_text", "recall_resume_prompt_text", "recall_generic_prompt_text",
        # Phase 17.1 (17.1-03): empty-state'ы медиа/контактов и «❓ Задать вопрос».
        "program_empty_text", "speakers_empty_text", "contacts_empty_text",
        "ask_question_prompt_text", "ask_question_sent_text",
        # Опросы (22.08): вступительный текст перед опросом -- в группе «событие».
        "poll_intro_text",
    }
    filtered = [k for k in admin_settings._EVENT_FIELD_ORDER if k not in new_keys]
    assert filtered == old_order_literal


# ── Task 2: миграция + add_user round-trip ─────────────────────────────────────────────────

def test_migration_adds_season_columns(tmp_path):
    _db_ready(tmp_path)

    async def _inspect():
        import aiosqlite
        async with aiosqlite.connect(config.DB_PATH) as conn:
            async with conn.execute("PRAGMA table_info(users)") as cursor:
                rows = await cursor.fetchall()
        return {row[1] for row in rows}

    columns = asyncio.run(_inspect())
    assert "season" in columns
    assert "prev_season" in columns

    # Idempotency — second init_db() must not raise.
    asyncio.run(db.init_db())


def test_migration_preserves_existing_rows(tmp_path):
    _db_ready(tmp_path)

    async def _run():
        await db.add_user(_full_user_data(111, season=None, prev_season=None))
        await db.init_db()
        return await db.get_user(111)

    user = asyncio.run(_run())
    assert user["full_name"] == "Иван Иванов"
    assert user["university"] == "МГУ"
    assert user["season"] is None
    assert user["prev_season"] is None


def test_add_user_roundtrip_all_columns(tmp_path):
    _db_ready(tmp_path)
    payload = _full_user_data(222, season="YL'26", prev_season="YL'25")

    async def _run():
        await db.add_user(payload)
        return await db.get_user(222)

    user = asyncio.run(_run())
    for key, value in payload.items():
        if key in ("is_aiesec_member", "work_status", "is_ambassador_candidate"):
            assert bool(user[key]) == bool(value), key
            continue
        assert user[key] == value, f"{key}: expected {value!r}, got {user[key]!r}"


def test_add_user_overwrites_season_on_reregistration(tmp_path):
    _db_ready(tmp_path)

    async def _run():
        await db.add_user(_full_user_data(333, season="YL'26", prev_season=None))
        await db.update_payment_status(333, "receipt_sent")
        await db.update_payment_status(333, "paid", receipt_file_id="rc1")
        await db.add_user(_full_user_data(333, season="YL'27", prev_season="YL'26"))
        return await db.get_user(333)

    user = asyncio.run(_run())
    assert user["season"] == "YL'27"
    assert user["prev_season"] == "YL'26"
    # WR-06 invariant: add_user never touches payment columns, even on re-registration.
    assert user["payment_status"] == "paid"
    assert user["receipt_file_id"] == "rc1"


# ── Task 3: четыре аксессора ────────────────────────────────────────────────────────────────

def test_mark_season_ended_stamps_current_and_null(tmp_path):
    _db_ready(tmp_path)

    async def _run():
        await db.add_user(_full_user_data(1, season=None))
        await db.add_user(_full_user_data(2, season="YL'26"))
        await db.add_user(_full_user_data(3, season="YL'25"))
        affected = await db.mark_season_ended("YL'26")
        u1 = await db.get_user(1)
        u2 = await db.get_user(2)
        u3 = await db.get_user(3)
        return affected, u1, u2, u3

    affected, u1, u2, u3 = asyncio.run(_run())
    assert affected == 2
    assert u1["season"] == "YL'26"
    assert u2["season"] == "YL'26"
    assert u3["season"] == "YL'25"


def test_mark_season_ended_legacy_literal(tmp_path):
    _db_ready(tmp_path)

    async def _run():
        await db.add_user(_full_user_data(1, season=None))
        affected = await db.mark_season_ended(None)
        user = await db.get_user(1)
        return affected, user

    affected, user = asyncio.run(_run())
    assert affected == 1
    assert user["season"] == "legacy"


def test_count_current_season_users_matches_marked(tmp_path):
    _db_ready(tmp_path)

    async def _run():
        await db.add_user(_full_user_data(1, season=None))
        await db.add_user(_full_user_data(2, season="YL'26"))
        await db.add_user(_full_user_data(3, season="YL'25"))
        before = await db.count_current_season_users("YL'26")
        affected = await db.mark_season_ended("YL'26")
        return before, affected

    before, affected = asyncio.run(_run())
    assert before == affected == 2


def test_mark_season_ended_touches_nothing_else(tmp_path):
    _db_ready(tmp_path)

    async def _run():
        await db.add_user(_full_user_data(1, season="YL'26"))
        await db.update_payment_status(1, "paid", receipt_file_id="rc1")
        before = await db.get_user(1)
        await db.mark_season_ended("YL'26")
        after = await db.get_user(1)
        return before, after

    before, after = asyncio.run(_run())
    for key in before:
        if key == "season":
            continue
        assert before[key] == after[key], f"{key} changed by mark_season_ended"
    assert after["season"] == "YL'26"


def test_get_returning_count(tmp_path):
    _db_ready(tmp_path)

    async def _run():
        await db.add_user(_full_user_data(1, prev_season=None))
        await db.add_user(_full_user_data(2, prev_season=""))
        await db.add_user(_full_user_data(3, prev_season="YL'25"))
        return await db.get_returning_count()

    count = asyncio.run(_run())
    assert count == 1


def test_reset_payment_for_new_season(tmp_path):
    _db_ready(tmp_path)

    async def _run():
        await db.add_user(_full_user_data(1))
        await db.add_user(_full_user_data(2))
        await db.update_payment_status(1, "receipt_sent")
        await db.update_payment_status(1, "paid", receipt_file_id="rc1")
        await db.update_payment_status(2, "receipt_sent")
        await db.update_payment_status(2, "paid", receipt_file_id="rc2")
        await db.reset_payment_for_new_season(1)
        u1 = await db.get_user(1)
        u2 = await db.get_user(2)
        return u1, u2

    u1, u2 = asyncio.run(_run())
    assert u1["payment_status"] == "not_paid"
    assert u1["payment_option"] is None
    assert u1["receipt_file_id"] is None
    assert u1["payment_due"] is None
    assert u1["paid_at"] is None
    # Another user's row untouched.
    assert u2["payment_status"] == "paid"
    assert u2["receipt_file_id"] == "rc2"
