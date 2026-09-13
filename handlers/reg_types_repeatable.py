"""Phase 30 (30-06, A2-05): чат-проекция типа шага `repeatable` — «Название проекта?» →
«Опиши коротко» → «Добавить ещё? Да / Готово» (30-CONTEXT.md § «Решения по итогам
30-RESEARCH.md», п.1; первый и единственный потребитель этой фазы — `mini_portfolio`, «Опыт и
проекты»).

Отдельный шов, а не ветка `handlers/registration.py::_ask_step` — тот же потолок агрегатора
(`tests/test_module_size_convention_260816.py`), тот же сторож паритета
(`reg_engine.CHAT_PROJECTION`). Своего `Router` нет — декорирует общий `router` из
`handlers.registration`, тот же образец, что `handlers/reg_resume_fork.py`.

Стадия блока («ждём название» / «ждём описание» / «ждём Да/Готово») хранится в данных FSM
ТЕКУЩЕГО шага (`_repeat_stage`), а не отдельным `State` на под-вопрос — единственное новое
состояние (`Registration.mini_portfolio_repeat`, `handlers/states.py`, план 30-06 задача 3)
занимает весь цикл добавления блоков. Не переиспользует `Registration.mini_portfolio` — то
состояние уже занято `handlers/reg_extra_steps.py::process_mini_portfolio`, зарегистрированным
раньше по порядку импорта хвоста `registration.py` (aiogram матчит хендлеры одного состояния в
порядке регистрации, не по специфичности фильтра).

Значение колонки собирается `dump_repeatable`/`validate_answer(..., repeatable_max_items=...)`
ПОСЛЕ КАЖДОГО завершённого блока (не в конце всей цепочки) — рестарт посреди «Добавить ещё?»
не теряет уже введённые блоки, `_sync_draft_out` пишет их в общий черновик сразу же (MEMORY
«restart-during-registration»)."""
from aiogram import F, types
from aiogram.fsm.context import FSMContext

from handlers import reg_i18n
from handlers.registration import _advance, _sync_draft_out, router
from handlers.states import Registration
from keyboards.builders import get_cancel_kb, get_skip_kb
from reg_engine import (
    STEP_TO_COLUMN,
    _REPEATABLE_NOUNS,
    _SKIP_ALLOWED_STEPS,
    prompt,
    repeatable_max,
    validate_answer,
)
from settings_schema import get_setting_typed


async def ask_step(step_key: str, message: types.Message, state: FSMContext,
                    progress_prefix: str, participant_type: str | None, city_code: str | None) -> None:
    """Первый под-вопрос блока — «Название {noun}?» (первый блок цикла); «Пропустить» здесь
    сохраняет сегодняшнее поведение шага целиком (30-06-PLAN.md <action>)."""
    await state.update_data(
        _repeat_step=step_key, _repeat_items=[], _repeat_title=None, _repeat_stage="title",
    )
    noun = _REPEATABLE_NOUNS.get(step_key, "")
    title_prompt = (await get_setting_typed("reg_repeatable_chat_title_prompt_text") or "").replace("{noun}", noun)
    text = f"{progress_prefix}{await prompt(step_key, participant_type, city_code)}\n\n{title_prompt}"
    kb = get_skip_kb() if step_key in _SKIP_ALLOWED_STEPS else get_cancel_kb()
    await reg_i18n.say(message, text, reply_markup=kb)
    await state.set_state(Registration.mini_portfolio_repeat)


async def _ask_description(message: types.Message, state: FSMContext) -> None:
    text = await get_setting_typed("reg_repeatable_chat_description_prompt_text")
    await reg_i18n.say(message, text, reply_markup=get_cancel_kb())


async def _ask_more(message: types.Message, state: FSMContext) -> None:
    from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

    prompt_text = await get_setting_typed("reg_repeatable_chat_prompt_text")
    yes_label = await get_setting_typed("reg_repeatable_chat_yes_button_text")
    done_label = await get_setting_typed("reg_repeatable_chat_done_button_text")
    kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text=yes_label, callback_data="regrepeat:add"),
        InlineKeyboardButton(text=done_label, callback_data="regrepeat:done"),
    ]])
    await state.update_data(_repeat_stage="ask_more")
    await reg_i18n.say(message, prompt_text, reply_markup=kb)


@router.message(Registration.mini_portfolio_repeat)
async def receive_repeat_field(message: types.Message, state: FSMContext, bot):
    data = await state.get_data()
    step_key = data.get("_repeat_step")
    if not step_key:
        return
    stage = data.get("_repeat_stage", "title")
    text = (message.text or "").strip()
    if stage != "title" and not text:
        return

    if stage == "title":
        if not data.get("_repeat_items"):
            # «Пропустить» на первом вопросе — легаси-поведение шага целиком (T-30-10 не
            # применяется: пустой список валиден, validate_answer сам решает "-" против пусто).
            skip_value, skip_err = validate_answer(step_key, text)
            if skip_err is None and skip_value == "-":
                await state.update_data(**{STEP_TO_COLUMN.get(step_key, step_key): skip_value})
                data = await state.get_data()
                await _sync_draft_out(
                    message.chat.id, state, data, step_key,
                    answered_col=STEP_TO_COLUMN.get(step_key, step_key),
                )
                await _advance(step_key, message, state, bot)
                return
        if not text:
            return
        await state.update_data(_repeat_title=text, _repeat_stage="description")
        await _ask_description(message, state)
        return

    # stage == "description"
    items = list(data.get("_repeat_items") or [])
    items.append({"title": data.get("_repeat_title") or "", "description": text})
    limit = await repeatable_max(step_key)
    value, err = validate_answer(step_key, items, repeatable_max_items=limit)
    if err:
        await reg_i18n.say(message, err)
        return
    await state.update_data(_repeat_items=items, **{STEP_TO_COLUMN.get(step_key, step_key): value})
    data = await state.get_data()
    await _sync_draft_out(
        message.chat.id, state, data, step_key, answered_col=STEP_TO_COLUMN.get(step_key, step_key),
    )
    if limit is not None and len(items) >= limit:
        # T-30-16: серверная проверка лимита ПЕРЕД показом кнопки «Да» — достигнут максимум,
        # цепочка завершается без дальнейших вопросов.
        await _advance(step_key, message, state, bot)
        return
    await _ask_more(message, state)


@router.callback_query(F.data.startswith("regrepeat:"), Registration.mini_portfolio_repeat)
async def regrepeat_pick(callback: types.CallbackQuery, state: FSMContext, bot):
    raw = callback.data or ""
    token = raw.split(":", 1)[1] if ":" in raw else ""
    try:
        await callback.message.edit_reply_markup(reply_markup=None)
    except Exception:
        pass
    await callback.answer()
    data = await state.get_data()
    step_key = data.get("_repeat_step")
    if not step_key or data.get("_repeat_stage") != "ask_more":
        # Закрытый словарь (T-30-15) — устаревшая карточка/чужая стадия молча игнорируется.
        return
    tap_message = callback.message.model_copy(update={"from_user": callback.from_user})

    if token == "add":
        # T-30-16: второй, серверный барьер лимита — перед записью в receive_repeat_field выше,
        # здесь же лимит уже проверен (кнопка «Да» не была бы показана при достижении максимума),
        # но повторная проверка на случай гонки (два тапа подряд) не помешает.
        limit = await repeatable_max(step_key)
        items = data.get("_repeat_items") or []
        if limit is not None and len(items) >= limit:
            await _advance(step_key, tap_message, state, bot)
            return
        await state.update_data(_repeat_stage="title", _repeat_title=None)
        noun = _REPEATABLE_NOUNS.get(step_key, "")
        title_prompt = (await get_setting_typed("reg_repeatable_chat_title_prompt_text") or "").replace("{noun}", noun)
        await reg_i18n.say(tap_message, title_prompt, reply_markup=get_cancel_kb())
        return
    if token == "done":
        await _advance(step_key, tap_message, state, bot)
        return
    # Закрытый словарь (T-30-15) — незнакомый токен молча игнорируется.
