"""Phase 21 Plan 09 (FORM-SYNC-02/03, D-18): «▶️ Продолжить с шага N / 🔄 Заново» — the screen
`cmd_start` shows for a fresh/in-flight `reg_drafts` row (kind='new' mid-registration, or
kind='edit' — a fallback entry into editing an already-registered current-season anketa in the
bot, D-18: "мастер правки — в приложении", this is only the fallback).

Imports the SAME shared `router` object `handlers/registration.py` defines (byte-for-byte the
same seam pattern as `handlers/reg/reg_flow.py`/`handlers/reg/reg_steps.py`) and decorates it directly
— never redefined, so `main.py` (which includes `registration.router` by object reference)
never changes. Imported LAST, at the very bottom of `handlers/registration.py` (after
`reg_flow`/`reg_steps`/`reg_consent`) — its handlers register LAST within `registration.router`,
so they land in the TAIL of the golden order+filter snapshot
(`tests/test_refac_snapshot_260816.py`), never reordering anything already there.
"""
import logging

from aiogram import Bot, F, types
from aiogram.fsm.context import FSMContext
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

import domain.regform.engine as reg_engine
from database.db import get_user, get_reg_draft, delete_reg_draft, set_reg_draft_surface, upsert_reg_draft
from domain.settings.schema import get_setting_typed
from services.registration.reg_handoff import SURFACE_BOT
from services.registration import reg_edit_policy  # Квик 260911-w2m: гейт правки уже поданной анкеты
from handlers.states import Registration
from keyboards.builders import get_confirm_kb, get_main_menu_kb
from handlers.registration import (
    router,
    _get_enabled_steps, _get_consent_steps, _ask_step_or_recall, _ask_full_name,
    _build_summary, finalize_registration, _start_registration_flow,
)
# Phase 27 (27-05, LANG-02): say()/tr_for() переводят делегатские отправки этого шва на
# отправке.
from handlers.i18n import reg_i18n
from handlers.reg.reg_summary import show_summary  # приёмка 09.10: «Продолжить» дочитанной анкеты

logger = logging.getLogger(__name__)


async def offer_resume(message: types.Message, draft: dict, referrer_id: int | None = None) -> None:
    """Phase 21 (21-09, D-18): единственный экран для ОБОИХ сценариев — свежий kind='new'
    (двойной /start посреди анкеты) и kind='edit' (?start=edit fallback / черновик правки,
    начатый в приложении). Кнопки — из реестра (`reg_resume_continue_label`/
    `reg_resume_restart_label`), подстановка {step}/{total} — только `.replace`, не `.format`
    (T-073-03-05: текст менеджера может содержать посторонние {}).

    UAT-фикс 27-05 (LANG-02): подстановка идёт ПОСЛЕ перевода шаблона (`reg_i18n.tr_fmt`), не
    ДО — иначе `src_hash` подставленной строки не совпадает с хешем исходного шаблона в
    `tr_map`, и переведённая в БД кнопка всё равно уходит делегату по-русски.

    `referrer_id` — реф-ссылка, открытая поверх незаконченного черновика. Обе кнопки экрана
    сбрасывают FSM, поэтому реферер переживает экран только в meta черновика: оттуда его
    забирают `resume_from_draft` («Продолжить») и `reg_resume_restart_yes` («Заново»)."""
    if referrer_id:
        try:
            await upsert_reg_draft(
                message.from_user.id, kind=draft.get("kind") or "new",
                meta_patch={"referrer_id": referrer_id}, source="bot",
            )
            draft.setdefault("meta", {})["referrer_id"] = referrer_id
        except Exception as e:
            logger.error(f"offer_resume: referrer_id persist failed for {message.from_user.id}: {e}")
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
    if draft.get("step") == reg_engine.STEP_DONE:
        # Приёмка 10.10: анкета дочитана, «Продолжить» ведёт на сводку — «шаг 14 из 14» врал.
        continue_label = reg_i18n.tr_text(await get_setting_typed("reg_resume_review_label"), lang, tr_map)
    else:
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
    `handlers/reg/reg_handoff.py::reg_handoff_to_bot` («✍️ Продолжить в чате» — возврат владения из
    приложения). Черновик передаётся вызывающим — он уже прочитан ПО СОБСТВЕННОМУ id тапнувшего
    (T-21-01/T-3vm-05), здесь второй раз не перечитывается."""
    telegram_id = tap_message.from_user.id
    # Квик 260911-w2m (Р-4 #3): единственная врезка, закрывающая ДВА входа —
    # `reg_resume:continue` (ниже) и `reg_handoff.py::reg_handoff_to_bot` («✍️ Продолжить в
    # чате») — оба зовут это общее тело. `reg_resume:restart_yes` НЕ проходит через эту
    # функцию и остаётся открытым намеренно (Р-4 #4): та ветка не открывает мастер, а
    # отменяет правку (удаляет черновик, «Изменения отменены») — закрыть её значило бы
    # запереть делегата с черновиком, который нельзя ни отправить, ни отменить.
    if (draft.get("kind") or "new") == "edit":
        user_row = await get_user(telegram_id)
        # Квик 260922-wrg: open_gate = edit_gate + resubmit_gate — та же точка входа закрывает
        # и правку уже поданной, и повторную подачу после отказа одной строкой.
        can_edit, closed_text = await reg_edit_policy.open_gate(user_row)
        if not can_edit:
            await reg_i18n.say(tap_message, closed_text, reply_markup=await get_main_menu_kb(telegram_id))
            return
    if not draft.get("event_city"):
        # Квик 27.09: черновик без города (след старого обхода) продолжается только с городом —
        # известным (`services.cities.known_city`) или спрошенным; `city_pick` по маркеру
        # `_resume_after_city` вернётся сюда с тем же черновиком, ответы не теряются.
        from handlers.reg.reg_city_gate import form_city_or_ask
        go, city = await form_city_or_ask(tap_message, state, resume=True)
        if not go:
            return
        if city:
            draft = {**draft, "event_city": city}
            try:
                draft["version"] = await upsert_reg_draft(
                    telegram_id, kind=draft.get("kind") or "new", event_city=city, source="bot",
                )
            except Exception as e:
                logger.error(f"resume_from_draft: city persist failed for {telegram_id}: {e}")
    await state.clear()
    fsm_patch = dict(draft.get("answers") or {})
    if draft.get("participant_type"):
        fsm_patch["participant_type"] = draft["participant_type"]
    if draft.get("event_city"):
        fsm_patch["event_city"] = draft["event_city"]
    if (draft.get("meta") or {}).get("referrer_id"):
        fsm_patch["referrer_id"] = draft["meta"]["referrer_id"]
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
    if step == reg_engine.STEP_DONE:
        # UAT 07.09 (T-d6t-04): маркер «все включённые шаги отвечены» — не доезжает до
        # fallback «согласия -> ФИО» ниже. Приёмка 09.10: ровно то, что бот делает сам после
        # последнего ответа в чате, — сводка «Всё верно / Изменить», а не отправка заявки мимо
        # подтверждения (маркер теперь ставит и чат, «Продолжить» жмут и со сводки).
        await show_summary(tap_message, state, data)
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
    count = 0
    if draft:
        # Приёмка 09.10: считаем отвеченные вопросы, а не поля черновика (было 18 при 14).
        answers = draft.get("answers") or {}
        enabled = await _get_enabled_steps({
            "participant_type": draft.get("participant_type"),
            "event_city": draft.get("event_city"), **answers,
        })
        count = reg_engine.answered_step_count(answers, enabled)
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
    # «Заново» стирает ответы, но не то, кто пригласил: реферер лежит в meta удалённого черновика.
    referrer_id = ((draft or {}).get("meta") or {}).get("referrer_id")
    # Квик 27.09: и не город — черновик уже удалён, а FSM после перезапуска бота пуст, так
    # что без явной передачи анкета стартовала без города (прод: заявки с event_city NULL).
    await _start_registration_flow(
        tap_message, state, referrer_id=referrer_id, event_city=(draft or {}).get("event_city"),
    )


async def reply_already_submitted(message: types.Message, state: FSMContext) -> None:
    """Приёмка 09.10 (ревью): чат на сводке, а анкету уже подало приложение — черновик забран
    и удалён. `finalize_registration` раньше собирал заявку из FSM и подавал её второй раз;
    теперь (черновик в этой сессии был — `_draft_version` в FSM — и исчез) состояние
    сбрасывается и делегат получает тот же ответ, что на любую уже поданную анкету."""
    logger.warning(f"finalize_registration: draft for {message.from_user.id} vanished — treated as submitted")
    await state.clear()
    text = await get_setting_typed("reg_already_submitted_text")
    await reg_i18n.say(message, text, reply_markup=await get_main_menu_kb(message.from_user.id))
