"""Приёмка 10.10: номер шага анкеты в чате = позиция шага в актуальном списке шагов.

Раньше номер был бегущим счётчиком «+1 за ответ», а «Продолжить с шага N из M» считал позицию.
Расходились они в двух местах:
- развилка резюме: первый подшаг ветки («ссылка», «нет резюме») получал номер самого вопроса
  резюме — «(14/14)», следующий уже «15/17», до «17/17» счётчик не доходил;
- «Исправить» на карточке «Проверь образование»: вопрос «Учишься сейчас?» получал номер шага
  после группы, и дальше счёт шёл от него.

Fake-объекты aiogram — из `tests/test_skillup_resume_fork_ui_28.py`.
"""
import asyncio
import re

from database import db
from handlers import registration as reg
from handlers.reg import reg_extra_steps, reg_resume_fork, reg_types_composite

from tests.test_skillup_resume_fork_ui_28 import (
    UID,
    _FakeCallback,
    _FakeMessage,
    _state,
    _texts,
    _use_tmp_db,
)

_PREFIX = re.compile(r"^\((\d+)/(\d+)\) ")


def _numbers(msg) -> list[tuple[int, int]]:
    out = []
    for text in _texts(msg):
        m = _PREFIX.match(text or "")
        if m:
            out.append((int(m.group(1)), int(m.group(2))))
    return out


async def _fork_setup(uid, **extra):
    await db.set_setting("reg_show_progress", "on")
    await db.set_setting("reg_resume_mode", "fork")
    for key in ("reg_q_resume", "reg_q_resume_link", "reg_q_mini_projects", "reg_q_mini_portfolio", "reg_q_mini_direction"):
        await db.set_setting(key, "on")
    state = _state(uid)
    await state.update_data(participant_type="full", full_name="Тест", **extra)
    data = await state.get_data()
    enabled = await reg._get_enabled_steps(data)
    resume_no = enabled.index("resume") + 1
    # Так номер вопроса резюме выставляет _advance.
    await state.update_data(_reg_step=resume_no, _reg_total=len(enabled))
    return state, resume_no


def test_link_branch_numbers_next_step_in_new_list(tmp_path):
    _use_tmp_db(tmp_path, "step_no_link.db")
    uid = UID + 101

    async def go():
        state, resume_no = await _fork_setup(uid)
        callback = _FakeCallback("regfork:link", uid)
        await reg_resume_fork.regfork_pick(callback, state)
        enabled = await reg._get_enabled_steps(await state.get_data())
        return resume_no, enabled, _numbers(callback.message)

    resume_no, enabled, numbers = asyncio.run(go())
    expected = (enabled.index("resume_link") + 1, len(enabled))
    assert numbers == [expected], numbers
    assert expected[0] == resume_no + 1


def test_mini_branch_numbers_are_monotonic_and_reach_total(tmp_path):
    _use_tmp_db(tmp_path, "step_no_mini.db")
    uid = UID + 102

    async def go():
        state, resume_no = await _fork_setup(uid)
        callback = _FakeCallback("regfork:mini", uid)
        await reg_resume_fork.regfork_pick(callback, state)
        msg = callback.message
        await reg_extra_steps.process_mini_projects(
            _with_sent(uid, msg, "Делал сайт для клиента, роль — фронтенд"), state, bot=None)
        await reg_extra_steps.process_mini_portfolio(_with_sent(uid, msg, "Пропустить"), state, bot=None)
        enabled = await reg._get_enabled_steps(await state.get_data())
        return resume_no, enabled, _numbers(msg)

    resume_no, enabled, numbers = asyncio.run(go())
    total = len(enabled)
    mini = [enabled.index(k) + 1 for k in ("mini_projects", "mini_portfolio", "mini_direction")]
    assert numbers == [(n, total) for n in mini], numbers
    assert mini[0] == resume_no + 1
    assert mini == sorted(mini) and mini[1] - mini[0] == 1 and mini[2] - mini[1] == 1


def test_back_to_fork_restores_resume_number(tmp_path):
    _use_tmp_db(tmp_path, "step_no_back.db")
    uid = UID + 103

    async def go():
        state, resume_no = await _fork_setup(uid)
        total_before = (await state.get_data())["_reg_total"]
        await reg_resume_fork.regfork_pick(_FakeCallback("regfork:mini", uid), state)
        back = _FakeCallback("regfork:back", uid)
        await reg_resume_fork.regfork_pick(back, state)
        return resume_no, total_before, _numbers(back.message)

    resume_no, total_before, numbers = asyncio.run(go())
    assert numbers and numbers[0] == (resume_no, total_before), numbers


def test_fix_on_education_card_numbers_by_position(tmp_path):
    _use_tmp_db(tmp_path, "step_no_fix.db")
    uid = UID + 104

    async def go():
        await db.set_setting("reg_show_progress", "on")
        await db.set_setting("reg_form_v2_enabled", "on")
        await db.set_setting("reg_form_edu_card", "on")
        state = _state(uid)
        await state.update_data(
            participant_type="full", education_status="Да, в ВУЗе или колледже",
            course="2", university="СПбГУ", study_field="Информационные технологии",
        )
        enabled = await reg._get_enabled_steps(await state.get_data())
        after = enabled[enabled.index("study_field") + 1]
        after_no = enabled.index(after) + 1
        await reg._ask_step(after, _FakeMessage(uid), state, after_no, len(enabled))

        fix = _FakeCallback("regedu:fix", uid)
        await reg_types_composite.regedu_pick(fix, state)
        # Делегат заново отвечает «Учишься сейчас?» — следующий вопрос получает номер +1.
        await reg._advance("education_status", _with_sent(uid, fix.message, None), state, None)
        return enabled, _numbers(fix.message)

    enabled, numbers = asyncio.run(go())
    total = len(enabled)
    first = enabled.index("education_status") + 1
    assert numbers[:2] == [(first, total), (first + 1, total)], numbers


def _with_sent(uid, shared, text):
    """Новое входящее сообщение, ответы бота на которое пишутся в общий список `shared.sent`."""
    msg = _FakeMessage(uid, text=text)
    msg.sent = shared.sent
    return msg
