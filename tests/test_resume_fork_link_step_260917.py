"""Приёмка 17.09: «Дать ссылку» в развилке резюме уводила на обзор без вопроса о ссылке —
шаг resume_link жил за отдельным тумблером reg_q_resume_link (по умолчанию выключен)."""
import asyncio

import reg_engine
from config import config
from database import db


def _use_tmp_db(tmp_path):
    config.DB_PATH = str(tmp_path / "test_resume_fork_link_step_260917.db")
    asyncio.run(db.init_db())


def test_link_branch_asks_link_in_fork_mode_without_separate_toggle(tmp_path):
    _use_tmp_db(tmp_path)

    async def scenario():
        await db.set_setting("reg_q_resume", "on")
        await db.set_setting("reg_resume_mode", "fork")
        steps = await reg_engine.enabled_steps({"participant_type": "full", "resume_type": "link"})
        assert "resume_link" in steps
        steps_file = await reg_engine.enabled_steps({"participant_type": "full", "resume_type": "file"})
        assert "resume_link" not in steps_file

    asyncio.run(scenario())


def test_link_step_stays_off_outside_fork_mode(tmp_path):
    _use_tmp_db(tmp_path)

    async def scenario():
        await db.set_setting("reg_q_resume", "on")
        await db.set_setting("reg_resume_mode", "file_or_text")
        steps = await reg_engine.enabled_steps({"participant_type": "full", "resume_type": "link"})
        assert "resume_link" not in steps

    asyncio.run(scenario())
