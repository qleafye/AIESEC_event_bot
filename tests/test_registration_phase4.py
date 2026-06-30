"""Phase 4 + YL'26 registration-engine tests.

Sync helpers tested directly; async flow helpers driven via asyncio.run() with
config.DB_PATH pointed at a tmp_path file (no pytest-asyncio in this env).
"""
import asyncio

from config import config
from database import db
from handlers import registration as reg


def _use_tmp_db(tmp_path):
    config.DB_PATH = str(tmp_path / "test_forum.db")


# ── REG_FLOW shape ───────────────────────────────────────────────────────────

def test_reg_flow_all_3_tuples_with_type():
    assert all(len(t) == 3 for t in reg.REG_FLOW)
    assert reg.REG_STEP_TYPES["arrival_date"] == "date"
    assert reg.REG_STEP_TYPES["birth_date"] == "date"
    assert reg.REG_STEP_TYPES["study_field"] == "select"
    assert reg.REG_STEP_TYPES["goal"] == "multi"
    assert reg.REG_STEP_TYPES["formats"] == "multi"


def test_new_steps_default_off():
    for key in ("reg_q_birth_date", "reg_q_study_field", "reg_q_goal",
                "reg_q_formats", "reg_q_ambassador", "reg_q_arrival_date"):
        assert reg.REG_DEFAULTS[key] == "off"


# ── sheet row + summary carry the new fields ─────────────────────────────────

def test_sheet_row_includes_new_fields():
    data = {
        "telegram_id": 1, "registration_date": "2026-01-01", "full_name": "A B",
        "birth_date": "01.01.2000", "study_field": "IT и технологии",
        "goal": "Нетворкинг", "formats": "Мастер-классы",
        "is_ambassador_candidate": True,
    }
    row = reg._build_sheet_row(data)
    assert "01.01.2000" in row
    assert "IT и технологии" in row
    assert "Нетворкинг" in row
    assert "Мастер-классы" in row
    assert "Да" in row  # ambassador


def test_sheet_header_matches_row_width():
    # header column count must equal the data-row column count, same order
    row = reg._build_sheet_row({"telegram_id": 1, "registration_date": "x", "full_name": "A"})
    assert len(reg.SHEET_HEADERS) == len(row)
    assert reg.SHEET_HEADERS[0] == "ID Telegram"
    assert reg.SHEET_HEADERS[-1] == "Амбассадор"


def test_summary_includes_new_fields():
    data = {
        "full_name": "A B", "birth_date": "01.01.2000",
        "study_field": "IT и технологии", "goal": "Нетворкинг",
        "formats": "Мастер-классы", "is_ambassador_candidate": True,
    }
    s = reg._build_summary(data)
    assert "Дата рождения" in s and "01.01.2000" in s
    assert "Направление обучения" in s
    assert "Цель участия" in s and "Форматы форума" in s
    assert "Амбассадор" in s


# ── consent step injection (consent_enabled gate) ────────────────────────────

def test_consent_steps_absent_by_default(tmp_path):
    _use_tmp_db(tmp_path)
    asyncio.run(db.init_db())
    steps = asyncio.run(reg._get_enabled_steps({"education_status": "Нет", "work_status": False}))
    assert not any(s.startswith("consent:") for s in steps)


def test_consent_steps_injected_last_when_enabled(tmp_path):
    _use_tmp_db(tmp_path)
    asyncio.run(db.init_db())
    asyncio.run(db.set_setting("consent_enabled", "on"))
    asyncio.run(db.set_setting("consent_list", "A|data\nB|policy\nbad-no-pipe\nNoKey|"))
    steps = asyncio.run(reg._get_enabled_steps({"education_status": "Нет", "work_status": False}))
    consents = [s for s in steps if s.startswith("consent:")]
    assert consents == ["consent:data", "consent:policy"]
    assert steps[-2:] == ["consent:data", "consent:policy"]


# ── edu_conditional toggle ───────────────────────────────────────────────────

def test_edu_conditional_on_skips_uni_when_not_studying(tmp_path):
    _use_tmp_db(tmp_path)
    asyncio.run(db.init_db())
    for k in ("reg_q_education", "reg_q_university", "reg_q_course", "reg_q_specialty"):
        asyncio.run(db.set_setting(k, "on"))
    steps = asyncio.run(reg._get_enabled_steps({"education_status": "Нет", "work_status": False}))
    assert "university" not in steps and "course" not in steps and "specialty" not in steps


def test_edu_conditional_off_shows_uni_always(tmp_path):
    _use_tmp_db(tmp_path)
    asyncio.run(db.init_db())
    for k in ("reg_q_education", "reg_q_university", "reg_q_course", "reg_q_specialty"):
        asyncio.run(db.set_setting(k, "on"))
    asyncio.run(db.set_setting("edu_conditional", "off"))
    steps = asyncio.run(reg._get_enabled_steps({"education_status": "Нет", "work_status": False}))
    assert "university" in steps and "course" in steps and "specialty" in steps


# ── configurable option lists ────────────────────────────────────────────────

def test_get_options_uses_setting_then_default(tmp_path):
    _use_tmp_db(tmp_path)
    asyncio.run(db.init_db())
    # empty setting → defaults
    opts = asyncio.run(reg._get_options("city_options", ["X", "Y"]))
    assert opts == ["X", "Y"]
    # admin overrides
    asyncio.run(db.set_setting("city_options", "Москва\nКазань\n"))
    opts2 = asyncio.run(reg._get_options("city_options", ["X", "Y"]))
    assert opts2 == ["Москва", "Казань"]


# ── editable question wording (reg_prompt_<step>) ────────────────────────────

def test_prompt_falls_back_to_default(tmp_path):
    _use_tmp_db(tmp_path)
    asyncio.run(db.init_db())
    assert asyncio.run(reg._prompt("age", "Сколько тебе лет?")) == "Сколько тебе лет?"


def test_prompt_uses_admin_override(tmp_path):
    _use_tmp_db(tmp_path)
    asyncio.run(db.init_db())
    asyncio.run(db.set_setting("reg_prompt_age", "Введи возраст:"))
    assert asyncio.run(reg._prompt("age", "Сколько тебе лет?")) == "Введи возраст:"
