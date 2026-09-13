"""Phase 30 (30-06, A2-03): чат-проекция типа шага `lookup` — «напиши первые буквы» → до пяти
inline-кнопок совпадений + «Другое» (30-UI-SPEC.md § «2. lookup» → «Проекция в чат»).

Отдельный шов, а не ветка `handlers/registration.py::_ask_step` — `handlers/registration.py`
стоит на потолке размера (`tests/test_module_size_convention_260816.py`), и `lookup` не
существовал ДО этой фазы: сторож паритета (`reg_engine.CHAT_PROJECTION`) требует выделенного
модуля, второй агрегаторской ветки быть не должно.

Своего `Router` нет — декорирует общий `router`, импортированный из `handlers.registration`
(тот же образец, что `handlers/reg_resume_fork.py`). Импортируется В ХВОСТЕ `registration.py`
(план 30-06, задача 4), ПОСЛЕ существующих швов — золотой снимок порядка
(`tests/test_refac_snapshot_260816.py`) только дополняется.

Собственное состояние `_LookupChat.waiting` — НЕ `Registration.university`/`Registration.city`.
Те состояния уже заняты приёмным хендлером `handlers/reg_steps.py`
(`process_university`/`process_city`, если такие есть), зарегистрированным РАНЬШЕ по порядку
импорта хвоста `handlers/registration.py`. aiogram матчит хендлеры одного состояния в порядке
регистрации, не по специфичности фильтра — переиспользовать общий `State` нельзя, событие
никогда не дошло бы до этого шва. Локальная `StatesGroup` не требует правки
`handlers/states.py` (файл вне списка этого плана).

Каждая мутация состояния (выбор варианта, ответ «Другим» текстом) — немедленный
`_sync_draft_out` (MEMORY «restart-during-registration»: без этого делегат после рестарта
воскресает на прошлой ветке анкеты).
"""
from aiogram import F, types
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from handlers import reg_i18n
from handlers.registration import _advance, _sync_draft_out, router
from reg_engine import (
    STEP_TO_COLUMN, _LOOKUP_ENTITY_NAMES as _ENTITY_NAMES, lookup_other_allowed, prompt,
    validate_answer,
)
from services.lookup import enqueue_merge, search_lookup
from settings_schema import get_setting_typed

_LOOKUP_LIMIT = 5


class _LookupChat(StatesGroup):
    waiting = State()


def _build_kb(results: list[dict], other_label: str) -> InlineKeyboardMarkup:
    """Закрытый словарь `reglookup:pick:<индекс>`/`reglookup:other` (T-30-15) — индекс
    проверяется по АКТУАЛЬНОМУ списку результатов из FSM data на приёме, не по значению из
    callback_data напрямую."""
    rows = [
        [InlineKeyboardButton(text=item["canonical"], callback_data=f"reglookup:pick:{i}")]
        for i, item in enumerate(results)
    ]
    if other_label:
        rows.append([InlineKeyboardButton(text=other_label, callback_data="reglookup:other")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def ask_step(step_key: str, message: types.Message, state: FSMContext,
                    progress_prefix: str, participant_type: str | None, city_code: str | None) -> None:
    """Показ шага — вопрос шага (та же точка правды, что у легаси-ветки, `reg_engine.prompt`)
    плюс подсказка «напиши первые буквы» (`reg_lookup_hint_default_text`, уже заведён планом
    30-03). Кнопок на этом экране нет — они появляются ПОСЛЕ первого текстового сообщения
    (30-UI-SPEC.md: «напиши первые буквы» — это и есть чат-эквивалент поиска)."""
    hint = await get_setting_typed("reg_lookup_hint_default_text")
    text = f"{progress_prefix}{await prompt(step_key, participant_type, city_code)}"
    if hint:
        text = f"{text}\n\n{hint}"
    await state.update_data(_lookup_step=step_key, _lookup_results=[], _lookup_other=False)
    await reg_i18n.say(message, text)
    await state.set_state(_LookupChat.waiting)


@router.message(_LookupChat.waiting)
async def receive_lookup_text(message: types.Message, state: FSMContext, bot):
    """Один хендлер на оба под-режима стадии `waiting` (обычный поиск / свободный текст после
    «Другое») — стадия хранится в FSM data (`_lookup_other`), не отдельным состоянием (та же
    экономия состояний, что у repeatable, план `<action>` задачи 3)."""
    data = await state.get_data()
    step_key = data.get("_lookup_step")
    if not step_key:
        return
    text = (message.text or "").strip()
    if not text:
        return

    if data.get("_lookup_other"):
        # T-30-14 не касается этой ветки — сырой текст уходит в enqueue_merge (LIKE-запрос
        # видит только search_lookup ниже), но всё равно проходит общий validate_answer.
        value, err = validate_answer(step_key, text)
        if err:
            await reg_i18n.say(message, err)
            return
        await enqueue_merge(step_key, text, step_key, message.chat.id)
        await state.update_data(_lookup_other=False, **{STEP_TO_COLUMN.get(step_key, step_key): value})
        data = await state.get_data()
        await _sync_draft_out(
            message.chat.id, state, data, step_key,
            answered_col=STEP_TO_COLUMN.get(step_key, step_key),
        )
        await _advance(step_key, message, state, bot)
        return

    entity = _ENTITY_NAMES.get(step_key, "")
    results = await search_lookup(step_key, text, limit=_LOOKUP_LIMIT)
    # Phase 30 (30-07, задача 4, A2-03): атрибут «свой вариант» списка-справочника гейтит
    # кнопку «Другое» в чате — выключенный атрибут не показывает её вовсе (а не просто не
    # принимает свободный текст после неё, кнопки которой нет).
    other_label = ""
    if await lookup_other_allowed(step_key):
        other_label = (await get_setting_typed("reg_form_own_chip_text") or "").replace("{entity}", entity)
    await state.update_data(_lookup_results=results)
    if not results:
        empty_title = (await get_setting_typed("reg_lookup_empty_title_text") or "").replace("{query}", text)
        await reg_i18n.say(message, empty_title, reply_markup=_build_kb([], other_label))
        return
    hint = await get_setting_typed("reg_lookup_hint_default_text")
    await reg_i18n.say(message, hint or text, reply_markup=_build_kb(results, other_label))


@router.callback_query(F.data.startswith("reglookup:"), _LookupChat.waiting)
async def reglookup_pick(callback: types.CallbackQuery, state: FSMContext, bot):
    raw = callback.data or ""
    token = raw.split(":", 1)[1] if ":" in raw else ""
    try:
        await callback.message.edit_reply_markup(reply_markup=None)
    except Exception:
        pass
    await callback.answer()
    data = await state.get_data()
    step_key = data.get("_lookup_step")
    if not step_key:
        return
    tap_message = callback.message.model_copy(update={"from_user": callback.from_user})

    if token == "other":
        await state.update_data(_lookup_other=True)
        entity = _ENTITY_NAMES.get(step_key, "")
        own_text = (await get_setting_typed("reg_form_own_option_text") or "").replace("{entity}", entity)
        await reg_i18n.say(tap_message, own_text)
        return

    if token.startswith("pick:"):
        try:
            idx = int(token.split(":", 1)[1])
        except ValueError:
            return
        results = data.get("_lookup_results") or []
        if idx < 0 or idx >= len(results):
            # T-30-15: индекс вне текущего списка результатов — молча игнорируется.
            return
        value = results[idx]["canonical"]
        await state.update_data(**{STEP_TO_COLUMN.get(step_key, step_key): value})
        data = await state.get_data()
        await _sync_draft_out(
            tap_message.chat.id, state, data, step_key,
            answered_col=STEP_TO_COLUMN.get(step_key, step_key),
        )
        await _advance(step_key, tap_message, state, bot)
        return
    # Закрытый словарь (T-30-15) — незнакомый токен молча игнорируется, клавиатура уже погашена.
