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

Phase 30 (30-08, задача A): атрибуты списка-справочника «чипы»/«поиск» (`reg_engine.
lookup_render_flags`, комбинирует их с глобальными тумблерами `reg_form_chips`/`reg_form_
lookup_search`) управляют этим швом СВЕРХ прежнего поведения — чипы показываются инлайн-
кнопками сразу на вопросе шага, выключенный атрибутом список поиска не зовёт `search_lookup`
на печатаемый текст, оба выключенных атрибута разом превращают шаг в обычное текстовое поле
(та же деградация, что `form_types.js::lookupControl` делает в Mini App).
"""
from aiogram import F, types
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from handlers import reg_i18n
from handlers.registration import _advance, _sync_draft_out, router
from reg_engine import (
    STEP_TO_COLUMN, _LOOKUP_ENTITY_NAMES as _ENTITY_NAMES, lookup_other_allowed,
    lookup_render_flags, prompt, validate_answer,
)
from services.lookup import enqueue_merge, search_lookup, top_chips
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
                    progress_prefix: str, participant_type: str | None, city_code: str | None,
                    v2_flags: dict[str, bool] | None = None) -> None:
    """Показ шага — вопрос шага (та же точка правды, что у легаси-ветки, `reg_engine.prompt`)
    плюс подсказка «напиши первые буквы» (`reg_lookup_hint_default_text`, уже заведён планом
    30-03). Кнопок на этом экране нет ПО УМОЛЧАНИЮ — они появляются ПОСЛЕ первого текстового
    сообщения (30-UI-SPEC.md: «напиши первые буквы» — это и есть чат-эквивалент поиска).

    Phase 30 (30-08, задача A): `v2_flags` — девять глобальных тумблеров, переданные вызывающим
    (`handlers/registration.py::_ask_step`, тот же объект, каким уже владеет диспетчер, второго
    похода в реестр не заводим). `reg_engine.lookup_render_flags` сводит их с атрибутами списка
    (`<list_key>_chips_enabled`/`_search_enabled`, план 30-07) — результат сохраняется в FSM
    data (`_lookup_chips_enabled`/`_lookup_search_enabled`), чтобы `receive_lookup_text` не
    считал их заново на каждое сообщение делегата. Если чипы включены — топ-8 частых значений
    показываются СРАЗУ инлайн-кнопками (тот же список, что видит Mini App, `services.lookup.
    top_chips`) — делегат может тапнуть, не печатая ни буквы; `None` (вызов без `v2_flags`,
    сегодня такого нет) — оба атрибута читаются как выключенные (безопасный дефолт: сегодняшнее
    поведение без чипов)."""
    render_flags = await lookup_render_flags(step_key, v2_flags or {})
    hint = await get_setting_typed("reg_lookup_hint_default_text")
    prompt_text = await prompt(step_key, participant_type, city_code)
    # Найдено на приёмке (стенд, lang=en, 17.09): вопрос и подсказку раньше склеивали В ОДНУ
    # строку ДО перевода — `tr()` ищет по хешу ВСЕГО текста, склейка «Выбери свой город\n\n
    # Начни вводить...» не совпадает ни с одним из двух переводов по отдельности, делегат видел
    # обе строки русскими даже когда обе давно переведены. Переводим КАЖДЫЙ кусок отдельно, ДО
    # склейки (`reg_i18n.say` ниже переведёт уже готовую английскую строку ещё раз — это no-op,
    # `services.i18n.tr` fail-soft отдаёт тот же текст, если перевода для него не нашлось).
    lang, tr_map = await reg_i18n.ctx_for(message)
    text = f"{progress_prefix}{reg_i18n.tr_text(prompt_text, lang, tr_map)}"
    if hint:
        text = f"{text}\n\n{reg_i18n.tr_text(hint, lang, tr_map)}"

    initial_results: list[dict] = []
    kb = None
    if render_flags["chips_enabled"]:
        chips = await top_chips(step_key, city_code, limit=_LOOKUP_LIMIT)
        if chips:
            initial_results = [{"canonical": c} for c in chips]
            other_label = ""
            if await lookup_other_allowed(step_key):
                entity = _ENTITY_NAMES.get(step_key, "")
                other_label = (await get_setting_typed("reg_form_own_chip_text") or "").replace("{entity}", entity)
            kb = _build_kb(initial_results, other_label)

    await state.update_data(
        _lookup_step=step_key, _lookup_results=initial_results, _lookup_other=False,
        _lookup_chips_enabled=render_flags["chips_enabled"],
        _lookup_search_enabled=render_flags["search_enabled"],
    )
    await reg_i18n.say(message, text, reply_markup=kb)
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

    chips_enabled = bool(data.get("_lookup_chips_enabled"))
    search_enabled = data.get("_lookup_search_enabled", True)
    if not search_enabled and not chips_enabled:
        # Phase 30 (30-08, задача A): оба атрибута списка выключены — тот же локальный разворот
        # в голое текстовое поле, что `form_types.js::lookupControl` делает при `spec.lookup`
        # `{chips_enabled:false, search_enabled:false}` (`degrade_kind()` эту комбинацию не
        # видит, она про атрибуты СПИСКА, не про глобальные тумблеры типа). Сырой текст пишется
        # НАПРЯМУЮ, без `search_lookup`/`enqueue_merge` — на этом сочетании справочник-с-очередью
        # уже не действует, шаг ведёт себя как обычное текстовое поле.
        value, err = validate_answer(step_key, text)
        if err:
            await reg_i18n.say(message, err)
            return
        await state.update_data(**{STEP_TO_COLUMN.get(step_key, step_key): value})
        data = await state.get_data()
        await _sync_draft_out(
            message.chat.id, state, data, step_key,
            answered_col=STEP_TO_COLUMN.get(step_key, step_key),
        )
        await _advance(step_key, message, state, bot)
        return

    if not search_enabled:
        # Phase 30 (30-08, задача A): поиск выключен атрибутом списка, чипы остались — делегат
        # обязан тапнуть одну из уже показанных кнопок (30-UI-SPEC.md §2: без поиска остаётся
        # статичный список ≤8 кнопок, не свободный ввод). Свободный текст здесь — переспрос, а
        # не «Другое» (та ветка гейтится отдельно `_lookup_other`/`lookup_other_allowed` выше).
        pick_hint = await get_setting_typed("reg_form_pick_option_text")
        await reg_i18n.say(message, pick_hint or text)
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
        # `{query}` — открытое множество (свободный ввод делегата), в отличие от `{entity}`
        # (закрытые "ВУЗ"/"город") статическую пару для каждого значения не завести. Переводим
        # ШАБЛОН, потом подставляем — тот же порядок, что `reg_i18n.tr_fmt` уже вводит для
        # ровно такого класса плейсхолдеров (докстринг `tr_fmt`).
        empty_title_template = await get_setting_typed("reg_lookup_empty_title_text") or ""
        empty_title = await reg_i18n.tr_for(message, empty_title_template)
        empty_title = empty_title.replace("{query}", text)
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
