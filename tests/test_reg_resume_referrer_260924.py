"""Реф-ссылка, открытая поверх незаконченного черновика, не теряется (запуск СкиллАп 5, 24.09).

Живой баг: приглашённый по `?start=amb_<id>` с черновиком видел «Продолжить/Заново», любая
кнопка сбрасывала FSM, реферер пропадал — вопрос «Откуда узнал(а)?» задавался вопреки
`reg_skip_source_for_referred`, а `users.referrer_id` оставался пустым. Теперь экран кладёт
реферера в meta черновика, обе кнопки забирают его оттуда.
"""
import asyncio

from database import db
from handlers import reg_resume

from tests.test_reg_resume_draft import (
    USER_ID, _FakeCallback, _KBCapturingMessage, _new_state, _seed_new_draft, _use_tmp_db,
)

REFERRER = USER_ID + 42


def test_offer_resume_persists_referrer_in_draft_meta(tmp_path):
    _use_tmp_db(tmp_path)

    async def go():
        await _seed_new_draft(USER_ID, patch={"age": "22"})
        draft = await db.get_reg_draft(USER_ID)
        await reg_resume.offer_resume(_KBCapturingMessage(USER_ID), draft, referrer_id=REFERRER)
        return await db.get_reg_draft(USER_ID)

    draft_after = asyncio.run(go())
    assert draft_after["meta"].get("referrer_id") == REFERRER
    assert draft_after["answers"] == {"age": "22"}


def test_continue_restores_referrer_into_fsm(tmp_path):
    _use_tmp_db(tmp_path)

    async def go():
        await db.set_setting("reg_q_age", "on")
        await _seed_new_draft(USER_ID, patch={"age": "22"})
        draft = await db.get_reg_draft(USER_ID)
        await reg_resume.offer_resume(_KBCapturingMessage(USER_ID), draft, referrer_id=REFERRER)
        state = _new_state(USER_ID)
        await reg_resume.reg_resume_continue(_FakeCallback("reg_resume:continue", USER_ID), state, bot=object())
        return await state.get_data()

    data = asyncio.run(go())
    assert data.get("referrer_id") == REFERRER


def test_restart_yes_keeps_referrer(tmp_path):
    _use_tmp_db(tmp_path)

    async def go():
        await _seed_new_draft(USER_ID, patch={"age": "22"})
        draft = await db.get_reg_draft(USER_ID)
        await reg_resume.offer_resume(_KBCapturingMessage(USER_ID), draft, referrer_id=REFERRER)
        state = _new_state(USER_ID)
        await reg_resume.reg_resume_restart_yes(_FakeCallback("reg_resume:restart_yes", USER_ID), state, bot=object())
        return await state.get_data(), await db.get_reg_draft(USER_ID)

    data, draft_after = asyncio.run(go())
    assert data.get("referrer_id") == REFERRER
    assert draft_after["answers"] == {}
    assert draft_after["meta"].get("referrer_id") == REFERRER


def test_offer_resume_without_referrer_leaves_meta_untouched(tmp_path):
    _use_tmp_db(tmp_path)

    async def go():
        await _seed_new_draft(USER_ID, patch={"age": "22"})
        draft = await db.get_reg_draft(USER_ID)
        version = draft["version"]
        await reg_resume.offer_resume(_KBCapturingMessage(USER_ID), draft)
        return version, await db.get_reg_draft(USER_ID)

    version, draft_after = asyncio.run(go())
    assert "referrer_id" not in draft_after["meta"]
    assert draft_after["version"] == version
