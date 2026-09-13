"""Phase 30 (30-06, A2-04): чат-проекция типа шага `composite` («Образование») — карточка-рекап
«Проверь образование» ПОСЛЕ обычной последовательности вопросов (30-UI-SPEC.md § «3. composite»
→ «Проекция в чат»: «обычная последовательность вопросов (как сегодня) + новая карточка-рекап
после последнего под-поля»).

Отдельный шов, а не ветка `handlers/registration.py::_ask_step` — тот же потолок
(`tests/test_module_size_convention_260816.py`), тот же сторож паритета
(`reg_engine.CHAT_PROJECTION`). ВАЖНО: отдельные под-вопросы группы «Образование»
(`education_status`/`course`/`university`/`study_field`) продолжают спрашиваться и приниматься
СТАРЫМИ ветками/швами (`handlers/registration.py::_ask_step`, `handlers/reg_steps.py`,
`handlers/reg_types_lookup.py` для ВУЗа) — этот модуль НЕ дублирует их отображение, только
ДОБАВЛЯЕТ карточку-рекап между последним под-полем и следующим вопросом анкеты. `_ask_step`
зовёт `maybe_show_recap()` ПЕРЕД любой другой веткой (диспетчер плана 30-06, задача 4): функция
определяет «группа образования только что завершена» ПРИСУТСТВИЕМ значений всех РЕЛЕВАНТНЫХ
частей в FSM data (шаги группы идут подряд в `REG_FLOW`, `_advance` продвигается строго по
одному — к моменту, когда спрашивается шаг ПОСЛЕ группы, все её части уже отвечены) — без
привязки к тому, какой конкретно шов принял последний ответ (лёгаси-ветка/lookup).

Своё состояние `_CompositeChat.confirm` — НЕ переиспользует `Registration.*` (та же причина,
что у `handlers/reg_types_lookup.py`: общие состояния уже заняты приёмными хендлерами,
зарегистрированными раньше). Локальная `StatesGroup` не требует правки `handlers/states.py`.

`_sync_draft_out` на карточке и на подтверждении — пометка «делегат сейчас ждёт следующий шаг
{step_key}» переживает рестарт (после рестарта FSM бот просто переспросит {step_key} напрямую,
минуя рекап — деградация мягкая, не бесконечный тупик, MEMORY «restart-during-registration»).
"""
import html

from aiogram import F, types
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from handlers import reg_i18n
from handlers.registration import _ask_step, _sync_draft_out, router
from reg_engine import (
    STEP_TO_COLUMN,
    _COMPOSITE_GROUPS,
    _COMPOSITE_TOGGLE_STEP,
    composite_parts,
    degrade_kind,
    is_studying,
    label_for,
    studying_statuses,
)
from settings_schema import get_setting_typed


class _CompositeChat(StatesGroup):
    confirm = State()


async def maybe_show_recap(step_key: str, message: types.Message, state: FSMContext, data: dict,
                            step: int, total: int, participant_type: str | None,
                            city_code: str | None, flags: dict[str, bool]) -> bool:
    """`True` — рекап показан ВМЕСТО `step_key` (вызывающий `_ask_step` обязан вернуться сразу);
    `False` — спрашивать `step_key` как обычно (группа ещё не завершена, тумблер выключен, или
    рекап этой группы уже подтверждён в этой сессии, `_composite_recap_done_<group>`)."""
    if degrade_kind("composite", flags) != "composite":
        return False
    for group, _group_steps in _COMPOSITE_GROUPS.items():
        if data.get(f"_composite_recap_done_{group}"):
            continue
        parts = await composite_parts(group, participant_type, city_code)
        if not parts or step_key in parts:
            continue
        toggle_step = _COMPOSITE_TOGGLE_STEP.get(group)
        studying = True
        if toggle_step and toggle_step in parts:
            studying = is_studying(data.get(toggle_step, ""), await studying_statuses())
        relevant = [sk for sk in parts if sk == toggle_step or studying]
        if not relevant:
            continue
        last_col = STEP_TO_COLUMN.get(relevant[-1], relevant[-1])
        if last_col not in data:
            continue  # группа структурно есть, но фактически ещё не завершена — спросить как обычно
        await _send_recap(group, relevant, message, state, data, step_key, step, total)
        return True
    return False


async def _send_recap(group: str, relevant: list[str], message: types.Message, state: FSMContext,
                       data: dict, step_key: str, step: int, total: int) -> None:
    lang, tr_map = await reg_i18n.ctx_for(message)
    heading = reg_i18n.tr_text(await get_setting_typed("reg_composite_chat_check_heading_text"), lang, tr_map)
    lines = [heading, ""]
    for part in relevant:
        col = STEP_TO_COLUMN.get(part, part)
        raw_value = data.get(col) or "-"
        value = await reg_i18n.display_value_for_step(part, raw_value, lang, tr_map)
        label = reg_i18n.tr_text(label_for(part), lang, tr_map)
        lines.append(f"<i>{html.escape(str(label))}</i>: <b>{html.escape(str(value))}</b>")
    text = "\n".join(lines)

    confirm_label = reg_i18n.tr_text(await get_setting_typed("reg_composite_chat_confirm_button_text"), lang, tr_map)
    fix_label = reg_i18n.tr_text(await get_setting_typed("reg_composite_chat_fix_button_text"), lang, tr_map)
    kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text=confirm_label, callback_data="regedu:confirm"),
        InlineKeyboardButton(text=fix_label, callback_data="regedu:fix"),
    ]])

    await state.update_data(_composite_pending={
        "group": group, "step_key": step_key, "step": step, "total": total,
        "first_part": relevant[0],
    })
    data = await state.get_data()
    await _sync_draft_out(message.chat.id, state, data, step_key, answered_col=None)
    from handlers.registration import _safe_answer  # тот же ленивый стиль, что и у соседей
    await _safe_answer(message, text, reply_markup=kb, parse_mode="HTML")
    await state.set_state(_CompositeChat.confirm)


@router.callback_query(F.data.startswith("regedu:"), _CompositeChat.confirm)
async def regedu_pick(callback: types.CallbackQuery, state: FSMContext):
    raw = callback.data or ""
    token = raw.split(":", 1)[1] if ":" in raw else ""
    try:
        await callback.message.edit_reply_markup(reply_markup=None)
    except Exception:
        pass
    await callback.answer()
    data = await state.get_data()
    pending = data.get("_composite_pending") or {}
    if not pending or token not in ("confirm", "fix"):
        # Закрытый словарь (T-30-15) — незнакомый токен/устаревшая карточка молча игнорируется.
        return
    tap_message = callback.message.model_copy(update={"from_user": callback.from_user})
    group = pending.get("group")

    if token == "confirm":
        await state.update_data(**{f"_composite_recap_done_{group}": True})
        data = await state.get_data()
        await _sync_draft_out(tap_message.chat.id, state, data, pending["step_key"], answered_col=None)
        await _ask_step(pending["step_key"], tap_message, state, pending["step"], pending["total"])
        return

    # "fix" — назад к первому под-шагу группы, значения уже введённых частей сохраняются
    # (30-UI-SPEC.md: «Исправить» не сбрасывает карточку, только переоткрывает её).
    first_part = pending["first_part"]
    await _sync_draft_out(tap_message.chat.id, state, data, first_part, answered_col=None)
    await _ask_step(first_part, tap_message, state, pending["step"], pending["total"])
