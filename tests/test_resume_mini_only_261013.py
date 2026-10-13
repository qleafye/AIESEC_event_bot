"""Режим резюме «Без резюме — только мини-анкета» (`reg_resume_mode = mini`): шаги резюме не
спрашиваются, три шага мини-профиля — всегда, перед первым вопросом — напоминание про бумажное
резюме. asyncio.run() + временная БД, как в соседних тестах."""
import asyncio

from config import config
from database import db
import domain.regform.engine as reg_engine
from domain.settings.schema import SETTINGS_SCHEMA
from handlers.reg import reg_schema
from handlers.regform import admin_reg_percity
from services.registration import reg_finalize as rf
from tests._dbtpl import fast_init_db

NOTICE = "Просим иметь резюме с собой на форуме в распечатанном виде."


def _ready(tmp_path, name="mini_only_261013.db"):
    config.DB_PATH = str(tmp_path / name)
    fast_init_db()


def test_schema_has_mini_option_and_notice_key():
    spec = SETTINGS_SCHEMA["reg_resume_mode"]
    assert spec["options"][-1] == "mini"
    assert spec["option_labels"]["mini"] == "Без резюме — только мини-анкета"
    assert spec["default"] == "file_or_text"
    assert SETTINGS_SCHEMA["reg_resume_mini_only_notice_text"]["default"] == NOTICE


def test_enabled_steps_mini_mode(tmp_path):
    _ready(tmp_path)

    async def go():
        await db.set_setting("reg_resume_mode", "mini")
        for k in ("reg_q_mini_projects", "reg_q_mini_portfolio", "reg_q_mini_direction"):
            await db.set_setting(k, "off")
        return await reg_engine.enabled_steps({"participant_type": "full", "resume_type": None})

    steps = asyncio.run(go())
    assert "resume" not in steps and "resume_link" not in steps
    for k in reg_engine.MINI_RESUME_STEPS:
        assert k in steps


def test_prompt_notice(tmp_path):
    _ready(tmp_path)

    async def go():
        base = await reg_engine._prompt_base("mini_projects")
        await db.set_setting("reg_resume_mode", "fork")
        in_fork = await reg_engine.prompt("mini_projects")
        await db.set_setting("reg_resume_mode", "mini")
        in_mini = await reg_engine.prompt("mini_projects")
        other = await reg_engine.prompt("mini_direction")
        spec = await reg_engine.step_spec("mini_projects")
        await db.set_setting("reg_resume_mini_only_notice_text", "")
        empty = await reg_engine.prompt("mini_projects")
        return base, in_fork, in_mini, other, spec, empty

    base, in_fork, in_mini, other, spec, empty = asyncio.run(go())
    assert in_fork == base
    assert in_mini == f"{NOTICE}\n\n{base}"
    assert not other.startswith(NOTICE)
    assert spec["prompt"].startswith(NOTICE)
    assert empty == base


def test_sheet_headers_mini_equal_fork(tmp_path):
    _ready(tmp_path)

    async def go():
        await db.set_setting("reg_resume_mode", "fork")
        fork = await reg_schema.active_sheet_headers()
        await db.set_setting("reg_resume_mode", "mini")
        mini = await reg_schema.active_sheet_headers()
        return fork, mini

    fork, mini = asyncio.run(go())
    assert fork == mini


def test_percity_cycle_includes_mini():
    cycle = admin_reg_percity._RESUME_MODE_CYCLE
    assert cycle == ("file_or_text", "text_only", "fork", "mini")
    assert admin_reg_percity._next_resume_mode("fork") == "mini"
    assert admin_reg_percity._next_resume_mode("mini") == "file_or_text"
    assert admin_reg_percity._RESUME_MODE_HUMAN["mini"]


def test_finalize_sets_resume_type_mini(tmp_path, monkeypatch):
    _ready(tmp_path)
    monkeypatch.setattr(config, "ADMIN_IDS", [])

    async def go():
        await db.set_setting("registration_mode", "full")
        await db.set_setting("full_approval", "manual")
        await db.set_setting("reg_resume_mode", "mini")
        draft = {"telegram_id": 900813001, "kind": "new", "meta": {},
                 "answers": {"full_name": "Анна Аннова", "mini_projects": "x"}}
        await rf.finalize_data(900813001, "@anna", draft)
        return await db.get_user(900813001)

    user = asyncio.run(go())
    assert user["resume_type"] == "mini"


def test_combined_default_prompt_has_manual_en_translation(tmp_path):
    """tr() ищет строку целиком: составной дефолт (напоминание + вопрос) обязан быть в ручном
    словаре, иначе английский Mini App покажет вопрос по-русски."""
    from services.i18n.i18n_form_manual import FORM_DEFAULT_EN
    _ready(tmp_path)

    async def go():
        await db.set_setting("reg_resume_mode", "mini")
        return await reg_engine.prompt("mini_projects")

    combined = asyncio.run(go())
    assert combined in FORM_DEFAULT_EN
