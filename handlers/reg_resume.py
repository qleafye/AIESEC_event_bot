"""Phase 21 Plan 09 (FORM-SYNC-02/03, D-18): «▶️ Продолжить с шага N / 🔄 Заново» — the screen
`cmd_start` shows for a fresh/in-flight `reg_drafts` row (kind='new' mid-registration, or
kind='edit' — a fallback entry into editing an already-registered current-season anketa in the
bot, D-18: "мастер правки — в приложении", this is only the fallback).

Imports the SAME shared `router` object `handlers/registration.py` defines (byte-for-byte the
same seam pattern as `handlers/reg_flow.py`/`handlers/reg_steps.py`) and decorates it directly
— never redefined, so `main.py` (which includes `registration.router` by object reference)
never changes. Imported LAST, at the very bottom of `handlers/registration.py` (after
`reg_flow`/`reg_steps`/`reg_consent`) — its handlers register LAST within `registration.router`,
so they land in the TAIL of the golden order+filter snapshot
(`tests/test_refac_snapshot_260816.py`), never reordering anything already there.
"""
from aiogram import Bot, F, types
from aiogram.fsm.context import FSMContext
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from database.db import get_user, get_reg_draft, delete_reg_draft, set_reg_draft_surface
from settings_schema import get_setting_typed
from services.reg_handoff import SURFACE_BOT
from handlers.states import Registration
from keyboards.builders import get_confirm_kb, get_main_menu_kb
from handlers.registration import (
    router,
    _get_enabled_steps, _get_consent_steps, _ask_step_or_recall, _ask_full_name,
    _build_summary, finalize_registration, _start_registration_flow,
)
# Phase 27 (27-05, LANG-02): say()/tr_for() переводят делегатские отправки этого шва на
# отправке.
from handlers import reg_i18n


async def offer_resume(message: types.Message, draft: dict) -> None:
    """Phase 21 (21-09, D-18): единственный экран для ОБОИХ сценариев — свежий kind='new'
    (двойной /start посреди анкеты) и kind='edit' (?start=edit fallback / черновик правки,
    начатый в приложении). Кнопки — из реестра (`reg_resume_continue_label`/
    `reg_resume_restart_label`), подстановка {step}/{total} — только `.replace`, не `.format`
    (T-073-03-05: текст менеджера может содержать посторонние {}).

    UAT-фикс 27-05 (LANG-02): подстановка идёт ПОСЛЕ перевода шаблона (`reg_i18n.tr_fmt`), не
    ДО — иначе `src_hash` подставленной строки не совпадает с хешем исходного шаблона в
    `tr_map`, и переведённая в БД кнопка всё равно уходит делегату по-русски."""
    answers = draft.get("answers") or {}
    probe = {
        "participant_type": draft.get("participant_type"),
        "event_city": draft.get("event_city"),
        **answers,
    }
    enabled = await _get_enabled_steps(probe)
    total = len(enabled) or 1
    step_no = 1
    if draft.get("step") in enabled:
        step_no = enabled.index(draft["step"]) + 1
    lang, tr_map = await reg_i18n.ctx_for(message)
    continue_label = reg_i18n.tr_fmt(
        await get_setting_typed("reg_resume_continue_label"), lang, tr_map,
        step=step_no, total=total,
    )
    restart_label = reg_i18n.tr_text(await get_setting_typed("reg_resume_restart_label"), lang, tr_map)
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=continue_label, callback_data="reg_resume:continue")],
        [InlineKeyboardButton(text=restart_label, callback_data="reg_resume:restart")],
    ])
    await reg_i18n.say(message, "У тебя есть незаконченная анкета — что дальше?", reply_markup=kb)


async def resume_from_draft(tap_message: types.Message, state: FSMContext, bot: Bot, draft: dict) -> None:
    """Phase 21 (21-09) + quick 260904-3vm: тело восстановления FSM из черновика — вынесено из
    `reg_resume_continue` (поведение байт-в-байт прежнее), чтобы им же пользовался
    `handlers/reg_handoff.py::reg_handoff_to_bot` («✍️ Продолжить в чате» — возврат владения из
    приложения). Черновик передаётся вызывающим — он уже прочитан ПО СОБСТВЕННОМУ id тапнувшего
    (T-21-01/T-3vm-05), здесь второй раз не перечитывается."""
    telegram_id = tap_message.from_user.id
    await state.clear()
    fsm_patch = dict(draft.get("answers") or {})
    if draft.get("participant_type"):
        fsm_patch["participant_type"] = draft["participant_type"]
    if draft.get("event_city"):
        fsm_patch["event_city"] = draft["event_city"]
    fsm_patch["_draft_kind"] = draft.get("kind") or "new"
    fsm_patch["_draft_version"] = draft.get("version", 0)
    if draft.get("kind") == "edit":
        # T-073-03-02 idiom (rereg_start/admin_rereg): the OWN row, fetched by the tapper's own
        # id — never anything from the callback payload. Lets steps not yet touched in THIS
        # edit still show the familiar «Прошлый ответ … Оставить/Изменить» recall screen
        # instead of asking a question the delegate already answered a season ago.
        user_row = await get_user(telegram_id)
        if user_row:
            fsm_patch["_prior_answers"] = dict(user_row)
    await state.update_data(**fsm_patch)

    data = await state.get_data()
    enabled = await _get_enabled_steps(data)
    step = draft.get("step")

    if not enabled:
        await finalize_registration(tap_message, state, bot)
        return
    if step in enabled:
        idx = enabled.index(step)
        total = len(enabled)
        await state.update_data(_reg_step=idx + 1, _reg_total=total)
        await _ask_step_or_recall(enabled[idx], tap_message, state, idx + 1, total)
        return
    if step == "full_name":
        await _ask_full_name(tap_message, state)
        return
    # Pitfall 6 (RESEARCH): the step is unrecognized — either it was never reached (still in
    # consents/ФИО) or it was toggled off while the draft sat idle. The same safe fallback as
    # a fresh flow start: consents -> ФИО -> first enabled question. Never crashes, never
    # loses answers already merged into FSM data above.
    consent_steps = await _get_consent_steps()
    if consent_steps:
        await state.update_data(_consent_queue=consent_steps, _consent_i=0)
        await _ask_step_or_recall(consent_steps[0], tap_message, state, 1, len(consent_steps))
    else:
        await _ask_full_name(tap_message, state)


@router.callback_query(F.data == "reg_resume:continue")
async def reg_resume_continue(callback: types.CallbackQuery, state: FSMContext, bot: Bot):
    """T-21-01: черновик читается ТОЛЬКО по callback.from_user.id — ни один аргумент этого
    callback'а не несёт telegram_id, поэтому чужой черновик восстановить нельзя."""
    telegram_id = callback.from_user.id
    draft = await get_reg_draft(telegram_id)
    if not draft:
        await callback.answer(
            await reg_i18n.tr_for(callback, "Черновик не найден — начни заново с /start."),
            show_alert=True,
        )
        return
    try:
        await callback.message.edit_reply_markup(reply_markup=None)
    except Exception:
        pass
    await callback.answer()

    # Quick 260904-3vm (эстафета): «Продолжить» в этом экране — тоже точка возврата владения
    # в чат (D-18 остаётся, но теперь он ещё и передаёт active_surface).
    await set_reg_draft_surface(telegram_id, SURFACE_BOT)

    tap_message = callback.message.model_copy(update={"from_user": callback.from_user})
    await resume_from_draft(tap_message, state, bot, draft)


@router.callback_query(F.data == "reg_resume:restart")
async def reg_resume_restart(callback: types.CallbackQuery, state: FSMContext):
    """UAT-фикс 27-05 (LANG-02): та же перестановка «перевод сначала, подстановка после», что
    и в `offer_resume` выше — `{count}` подставляется ПОСЛЕ `reg_i18n.tr_fmt`, не до. Кнопки
    «Да, начать заново»/«Нет, продолжить» переводятся тем же вызовом `reg_i18n.tr_text` —
    раньше первая была голым русским литералом мимо любого перевода, а вторая совпадала с
    ярусом A случайно (общий литерал с экраном отмены анкеты в `reg_flow.py`), так что пара
    расходилась по языку на одном и том же экране."""
    try:
        await callback.message.edit_reply_markup(reply_markup=None)
    except Exception:
        pass
    draft = await get_reg_draft(callback.from_user.id)
    count = len(draft.get("answers") or {}) if draft else 0
    lang, tr_map = await reg_i18n.ctx_for(callback.message)
    text = reg_i18n.tr_fmt(
        await get_setting_typed("reg_resume_restart_confirm_text"), lang, tr_map, count=count,
    )
    kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(
            text=reg_i18n.tr_text("Да, начать заново", lang, tr_map), callback_data="reg_resume:restart_yes"
        ),
        InlineKeyboardButton(
            text=reg_i18n.tr_text("Нет, продолжить", lang, tr_map), callback_data="reg_resume:continue"
        ),
    ]])
    await reg_i18n.say(callback.message, text, reply_markup=kb, parse_mode="HTML")
    await callback.answer()


@router.callback_query(F.data == "reg_resume:restart_yes")
async def reg_resume_restart_yes(callback: types.CallbackQuery, state: FSMContext, bot: Bot):
    try:
        await callback.message.edit_reply_markup(reply_markup=None)
    except Exception:
        pass
    await callback.answer()
    draft = await get_reg_draft(callback.from_user.id)
    kind = (draft or {}).get("kind") or "new"
    await delete_reg_draft(callback.from_user.id)
    if kind == "edit":
        # D-20: правка одобренной анкеты не удаляет саму анкету — «Заново» для kind='edit'
        # значит «отменить изменения», не «начать регистрацию заново» (делегат уже
        # зарегистрирован в этом сезоне и остаётся им).
        await state.clear()
        await reg_i18n.say(
            callback.message,
            "Изменения отменены — анкета осталась прежней.",
            reply_markup=await get_main_menu_kb(callback.from_user.id),
        )
        return
    tap_message = callback.message.model_copy(update={"from_user": callback.from_user})
    await _start_registration_flow(tap_message, state)
