"""Сводка анкеты в чате «Проверь свои ответы» + «Всё верно / Изменить».

Приёмка 09.10: общая для ответа на последний шаг (`registration._advance`) и «Продолжить»
дочитанной анкеты (`reg_resume.resume_from_draft`, маркер `reg_engine.STEP_DONE`). Вынесена из
`handlers/registration.py` (агрегатор у потолка размера). Хендлеров нет — только функция,
золотой снимок порядка регистрации не меняется.
"""
from aiogram import types
from aiogram.fsm.context import FSMContext

from handlers.i18n import reg_i18n
from handlers.states import Registration
from keyboards.builders import get_confirm_kb


async def show_summary(message: types.Message, state: FSMContext, data: dict) -> None:
    """QW-01: сводка + клавиатура подтверждения перед отправкой (D-01).

    Phase 27 (27-05, LANG-02): подписи сводки переводятся ЗДЕСЬ — составная строка не найдётся
    в карте переводов как единое целое (докстринг `_build_summary`); врезка внутри
    `_safe_answer` переведёт только клавиатуру. Карта закрытых вариантов — один поход в БД на
    рендер (докстринг `reg_i18n.summary_value_maps`). Приёмка 09.10: строка «Город форума» —
    `reg_city_gate.summary_data`. `_build_summary`/`_safe_answer` берутся через модуль
    `registration` в момент вызова — монкипатч в тестах продолжает работать."""
    from handlers import registration
    from handlers.reg_city_gate import summary_data

    lang, tr_map = await reg_i18n.ctx_for(message)
    value_maps = await reg_i18n.summary_value_maps(lang, tr_map)
    summary = registration._build_summary(await summary_data(data), lang, tr_map, value_maps)
    await registration._safe_answer(message, summary, reply_markup=get_confirm_kb(), parse_mode="HTML")
    await state.set_state(Registration.confirm)
