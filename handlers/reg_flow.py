"""Phase 13 REFAC (13-03, REFAC-02): registration entry/forks/consent/generic-input handlers.

Extracted from handlers/registration.py (party_pick through process_consent_ignore -- the
fork taps, cancel confirmation, confirm/resume steps, the generic date/select/multi-select
input handlers, ambassador and consent) to keep registration.py under the ~800-line
guideline (CONCERNS.md). Every handler here decorates the SAME shared `router` object
defined in handlers/registration.py -- imported below, not redefined -- so main.py (which
includes registration.router by object reference) never changes.

Import order matters (T-13-04): this module is imported from the bottom of
handlers/registration.py BEFORE `handlers.reg_steps`, matching this block's original
position immediately after cmd_start/the FSM engine and immediately before the per-state
process_* step block.

Behavior is byte-for-byte unchanged from the pre-move code -- every handler body below is a
verbatim relocation, not a rewrite.
"""
from datetime import datetime

from aiogram import Bot, F, types
from aiogram.filters import StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton

from config import config
from database.db import get_user, get_setting, record_user_consent
from settings_schema import get_setting_typed
from cities import CITIES, is_city_enabled
from handlers.states import Registration
from keyboards.builders import get_cancel_kb, get_main_menu_kb
from handlers.registration import (
    router,
    _PARTY_TAG_MAP, MULTI_CONFIG,
    _start_registration_flow, _is_returning_row,
    _city_fork_then_continue, _continue_after_city,
    _consent_key_matches, _ask_step_or_recall, _ask_full_name,
    finalize_registration, _advance, _get_options, _multi_kb,
    _is_allowed_resume, _resume_too_large,
)


@router.callback_query(F.data.startswith("party_pick:"))
async def party_pick(callback: types.CallbackQuery, state: FSMContext):
    """D-10 fork tap. The token is mapped through the SAME _PARTY_TAG_MAP the deep-link
    extractor uses (T-05-04-01: one closed token vocabulary, so no arbitrary track value can
    reach _start_registration_flow) — an unmapped token is rejected and answered, not routed
    anywhere. The party_enabled gate is re-checked here since the setting can flip between
    the fork being rendered and the user tapping it."""
    token = callback.data.split(":", 1)[1]
    if token == "full":
        chosen_track = None
    elif token in _PARTY_TAG_MAP:
        chosen_track = _PARTY_TAG_MAP[token]
    else:
        await callback.answer("Некорректный выбор.", show_alert=True)
        return

    if chosen_track and await get_setting_typed("party_enabled") != "on":  # REG-02: registry-backed
        # Render-then-flip window (T-05-04-01): the track closed between render and tap.
        await callback.answer("Регистрация на вечеринку уже закрыта.", show_alert=True)
        return

    await callback.answer()
    try:
        await callback.message.edit_reply_markup(reply_markup=None)
    except Exception:
        pass
    # callback.message.from_user is the BOT — swap in the tapping user, same fix as
    # party_fallback_full (T-05-01 deviation), so mark_reg_started records the right id.
    tap_message = callback.message.model_copy(update={"from_user": callback.from_user})
    await _start_registration_flow(tap_message, state, participant_type=chosen_track)


@router.callback_query(F.data == "admin_rereg")
async def admin_rereg(callback: types.CallbackQuery, state: FSMContext):
    """quick-260817-4pj: admin-only re-registration entry point, replaces the old
    admin-rereg reply-keyboard FSM flow. registration.router is NOT covered by
    CapabilityMiddleware (it only sits on admin.router), so the ADMIN_IDS check below is the
    sole guard — anyone who guessed this callback_data could otherwise reset their own FSM and
    start a registration (T-4pj-01)."""
    if callback.from_user.id not in config.ADMIN_IDS:
        await callback.answer("Недостаточно прав", show_alert=True)
        return
    await callback.answer()
    await state.clear()
    try:
        await callback.message.edit_reply_markup(reply_markup=None)
    except Exception:
        pass
    # callback.message.from_user is the BOT — swap in the tapping admin, same fix as
    # party_fallback_full, so mark_reg_started records the tapping admin's id (T-4pj-02).
    tap_message = callback.message.model_copy(update={"from_user": callback.from_user})
    # UAT 19.08: раньше admin_rereg шёл сразу в _start_registration_flow, минуя форк города
    # (при включённых городах админ не мог выбрать город при переереге). Идём тем же путём,
    # что и rereg_start ниже: город → форк вечеринки → старт анкеты.
    data = await state.get_data()
    await _city_fork_then_continue(
        tap_message,
        state,
        data.get("event_city"),
        data.get("referrer_id"),
        data.get("source"),
        data.get("participant_type") if data.get("_track_from_link") else None,
        None,
    )


@router.callback_query(F.data == "rereg_start")
async def rereg_start(callback: types.CallbackQuery, state: FSMContext):
    """Phase 07.3 (RET-02/T-073-03-01): registration.router is NOT covered by
    CapabilityMiddleware (only admin.router is) — same posture as admin_rereg above. Anyone who
    guessed this callback_data could otherwise reset a returning delegate's own FSM without
    actually being a returning delegate, so this handler re-verifies the TAPPER (never anything
    from the callback payload) against `_is_returning_row` before doing anything else."""
    user = await get_user(callback.from_user.id)
    event_season = await get_setting("event_season")
    if not _is_returning_row(user, event_season):
        await callback.answer("Ты уже зарегистрирован(а) на этот сезон", show_alert=True)
        return
    await callback.answer()
    try:
        await callback.message.edit_reply_markup(reply_markup=None)
    except Exception:
        pass
    # T-073-03-02: the snapshot is taken ONLY from the tapping user's OWN row, fetched above by
    # callback.from_user.id — never from any callback/deep-link parameter, so a crafted
    # callback_data can never pull someone else's prior answers into this session.
    await state.update_data(_prior_answers=dict(user))
    # callback.message.from_user is the BOT, not the tapping delegate — same fix as admin_rereg
    # above (T-4pj-02), so mark_reg_started/attribution downstream records the tapper's own id.
    tap_message = callback.message.model_copy(update={"from_user": callback.from_user})
    data = await state.get_data()
    await _city_fork_then_continue(
        tap_message,
        state,
        data.get("event_city"),
        data.get("referrer_id"),
        data.get("source"),
        data.get("participant_type") if data.get("_track_from_link") else None,
        None,
    )


@router.callback_query(F.data == "party_fallback_full")
async def party_fallback_full(callback: types.CallbackQuery, state: FSMContext):
    """D-11a explicit opt-in: the ONLY way out of the "party closed" message. Starts the
    ordinary full flow (participant_type left at its default 'full') — never sets a party
    track, carries no user-supplied parameters (T-05-01-03)."""
    await callback.answer()
    try:
        await callback.message.edit_reply_markup(reply_markup=None)
    except Exception:
        pass
    # callback.message.from_user is the BOT (it authored that message) — swap in the actual
    # tapping user (callback.from_user) so mark_reg_started records the right telegram_id.
    # model_copy() preserves the bound _bot private attr, so .answer() still works.
    tap_message = callback.message.model_copy(update={"from_user": callback.from_user})
    await _start_registration_flow(tap_message, state)


@router.callback_query(F.data.startswith("city_pick:"))
async def city_pick(callback: types.CallbackQuery, state: FSMContext):
    """CITY-03 city-screen tap. The code is checked against the CLOSED CITIES vocabulary
    (T-071-09: no crafted callback payload can select a city outside the registry) before any
    action; is_city_enabled is re-checked AFTER render (T-071-10, render-then-flip window —
    same pattern as party_pick's party_enabled re-check). Continues through the SAME
    _continue_after_city tail cmd_start uses, so the party fork still fires if applicable and
    referrer/source attribution (persisted into FSM by cmd_start before showing this screen)
    is picked back up there, not passed again here."""
    code = callback.data.split(":", 1)[1]
    if code not in {c["code"] for c in CITIES}:
        await callback.answer("Некорректный выбор.", show_alert=True)
        return

    if not await is_city_enabled(code):
        await callback.answer("Регистрация на этот город закрыта.", show_alert=True)
        return

    await callback.answer()
    try:
        await callback.message.edit_reply_markup(reply_markup=None)
    except Exception:
        pass
    # callback.message.from_user is the BOT — swap in the tapping user, same fix as party_pick.
    tap_message = callback.message.model_copy(update={"from_user": callback.from_user})
    data = await state.get_data()
    pending_track = data.get("participant_type")
    await _continue_after_city(tap_message, state, code, None, None, pending_track, None)


_CANCEL_CONFIRM_KB = InlineKeyboardMarkup(inline_keyboard=[[
    InlineKeyboardButton(text="Да, отменить", callback_data="reg_cancel_yes"),
    InlineKeyboardButton(text="Нет, продолжить", callback_data="reg_cancel_no"),
]])


@router.message(StateFilter(Registration), F.text.in_({"Отмена", "/cancel"}))
async def cancel_registration(message: types.Message, state: FSMContext):
    # Confirm before wiping the form — one accidental tap on the «Отмена» reply
    # button used to drop the whole registration with no undo. State is left intact
    # so «Нет, продолжить» resumes exactly where the user was.
    await message.answer(
        "Точно отменить регистрацию? Все введённые ответы сотрутся.",
        reply_markup=_CANCEL_CONFIRM_KB,
    )


@router.callback_query(F.data == "reg_cancel_yes", StateFilter(Registration))
async def cancel_registration_confirm(callback: types.CallbackQuery, state: FSMContext):
    # WR-05: state-scoped so a stale "Точно отменить?" button can only clear an active
    # registration — never wipe unrelated FSM state (e.g. an admin mid-broadcast/settings).
    await state.clear()
    try:
        await callback.message.edit_reply_markup(reply_markup=None)
    except Exception:
        pass
    # UAT 19.08: при отмене повторной регистрации (rereg) у делегата есть строка в БД и
    # главное меню ему положено; новичку меню тоже не мешает — кнопки упираются в
    # ensure_registered («сначала /start»). Раньше ReplyKeyboardRemove оставлял без меню.
    await callback.message.answer(
        "Регистрация отменена. Чтобы начать заново, отправь /start.",
        reply_markup=await get_main_menu_kb(callback.from_user.id),
    )
    await callback.answer()


@router.callback_query(F.data == "reg_cancel_no", StateFilter(Registration))
async def cancel_registration_dismiss(callback: types.CallbackQuery):
    # Keep the FSM state untouched — the current step's question is still above, so
    # the user just carries on answering it.
    try:
        await callback.message.edit_reply_markup(reply_markup=None)
    except Exception:
        pass
    await callback.answer("Продолжаем 👍")


# --- QW-01 confirmation step ---

@router.message(Registration.confirm, F.text == "Всё верно")
async def process_confirm_ok(message: types.Message, state: FSMContext, bot: Bot):
    await finalize_registration(message, state, bot)


@router.message(Registration.confirm, F.text == "Изменить")
async def process_confirm_edit(message: types.Message, state: FSMContext):
    # D-02: 'Изменить' restarts the whole flow — no per-field editing.
    await _start_registration_flow(message, state)


# --- QW-03 resume upload step ---

@router.message(Registration.resume, F.document)
async def process_resume(message: types.Message, state: FSMContext, bot: Bot):
    if not _is_allowed_resume(message.document.file_name):
        await message.answer("Принимаются только PDF или DOCX. Прикрепи файл ещё раз.")
        return
    if _resume_too_large(message.document.file_size):
        await message.answer("❌ Файл слишком большой (максимум 10 МБ). Прикрепи резюме меньшего размера.")
        return
    # file_id only, no download (D-10). Keep the original file name in FSM (non-persisted,
    # no DB/sheet column) so the Nextcloud upload preserves the real .pdf/.docx extension.
    await state.update_data(
        resume_file_id=message.document.file_id,
        resume_file_name=message.document.file_name,
    )
    await _advance("resume", message, state, bot)


@router.message(Registration.resume, F.text)
async def process_resume_text(message: types.Message, state: FSMContext, bot: Bot):
    # Tatiana: резюме можно либо файлом, либо текстом. Обязательно (без «Пропустить»).
    text = (message.text or "").strip()
    if not text:
        await message.answer("Напиши резюме текстом или прикрепи файл (PDF или DOCX).")
        return
    await state.update_data(resume_text=text)
    await _advance("resume", message, state, bot)


@router.message(Registration.resume)
async def process_resume_invalid(message: types.Message, state: FSMContext):
    # Neither a document nor text (sticker/photo/etc.) — re-prompt, never crash (D-10).
    await message.answer("Пришли резюме текстом или прикрепи файл (PDF или DOCX).")


# --- Phase 4: date-type step (MOD-02) ---

def _validate_date_range(step_key: str, dt: datetime) -> str | None:
    """LOW: sanity range check for a parsed date step. Loose bounds — reject only clearly-wrong
    input (typos, impossible dates), never a plausible real value. Returns a user-facing error
    message, or None if the date is acceptable."""
    today = datetime.now()
    if step_key == "birth_date":
        if dt > today:
            return "Дата рождения не может быть в будущем. Проверь и введи ещё раз."
        if dt.year < today.year - 100 or dt.year > today.year - 10:
            return "Проверь дату рождения (год выглядит неправдоподобно) и введи ещё раз."
    elif step_key == "arrival_date":
        if dt.date() < today.date():
            return "Дата приезда не может быть в прошлом. Введи корректную дату."
        if dt.year > today.year + 2:
            return "Проверь дату приезда (слишком далеко в будущем) и введи ещё раз."
    return None


@router.message(Registration.date_input)
async def process_date_input(message: types.Message, state: FSMContext, bot: Bot):
    raw = (message.text or "").strip()
    try:
        dt = datetime.strptime(raw, "%d.%m.%Y")
    except ValueError:
        await message.answer("Формат даты: ДД.ММ.ГГГГ. Попробуй ещё раз.")
        return
    data = await state.get_data()
    step_key = data.get("_current_date_step", "arrival_date")
    range_err = _validate_date_range(step_key, dt)
    if range_err:
        await message.answer(range_err)
        return
    await state.update_data(**{step_key: raw})
    await _advance(step_key, message, state, bot)


# --- YL'26: configurable single-select step ---

@router.message(Registration.select_input)
async def process_select_input(message: types.Message, state: FSMContext, bot: Bot):
    text = (message.text or "").strip()
    if not text:
        await message.answer("Выбери вариант на клавиатуре или напиши свой.")
        return
    if text == "Другое":
        await message.answer("Напиши свой вариант:", reply_markup=get_cancel_kb())
        return
    data = await state.get_data()
    step_key = data.get("_current_select_step", "study_field")
    await state.update_data(**{step_key: text})
    await _advance(step_key, message, state, bot)


# --- YL'26: configurable multi-select step (inline toggles) ---

async def _multi_options(step_key: str) -> list[str]:
    opt_key, default = MULTI_CONFIG.get(step_key, (f"{step_key}_options", []))
    return await _get_options(opt_key, default)


@router.callback_query(F.data.startswith("regmulti:"), Registration.multi_input)
async def process_multi_toggle(callback: types.CallbackQuery, state: FSMContext):
    _, step_key, idx_raw = callback.data.split(":", 2)
    try:
        idx = int(idx_raw)
    except ValueError:
        await callback.answer()
        return
    data = await state.get_data()
    selected = set(data.get(f"_multi_{step_key}", []))
    if idx in selected:
        selected.discard(idx)
    else:
        selected.add(idx)
    await state.update_data(**{f"_multi_{step_key}": sorted(selected)})
    options = await _multi_options(step_key)
    try:
        await callback.message.edit_reply_markup(reply_markup=_multi_kb(step_key, options, selected))
    except Exception:
        pass
    await callback.answer()


@router.callback_query(F.data.startswith("regmulti_done:"), Registration.multi_input)
async def process_multi_done(callback: types.CallbackQuery, state: FSMContext, bot: Bot):
    step_key = callback.data.split(":", 1)[1]
    data = await state.get_data()
    selected = sorted(set(data.get(f"_multi_{step_key}", [])))
    options = await _multi_options(step_key)
    chosen = [options[i] for i in selected if 0 <= i < len(options)]
    if not chosen:
        await callback.answer("Выбери хотя бы один вариант.", show_alert=True)
        return
    await state.update_data(**{step_key: ", ".join(chosen)})
    try:
        await callback.message.edit_reply_markup(reply_markup=None)
    except Exception:
        pass
    await callback.answer("Сохранено")
    await _advance(step_key, callback.message, state, bot)


@router.message(Registration.multi_input)
async def process_multi_ignore(message: types.Message):
    await message.answer("Отмечай варианты кнопками выше и нажми «Готово».")


# --- YL'26: ambassador yes/no ---

@router.message(Registration.ambassador)
async def process_ambassador(message: types.Message, state: FSMContext, bot: Bot):
    text = (message.text or "").strip()
    if not text:
        await message.answer("Выбери «Да!» или «Пока нет».")
        return
    await state.update_data(is_ambassador_candidate=text.lower().startswith("да"))
    await _advance("ambassador", message, state, bot)


# --- Phase 4: consent steps (MOD-03, CONS-02) ---

@router.callback_query(F.data.startswith("consent_accept:"), Registration.consent_pending)
async def process_consent_accept(callback: types.CallbackQuery, state: FSMContext, bot: Bot):
    consent_key = callback.data.split(":", 1)[1]
    data = await state.get_data()
    # CR-7: only the currently-active consent may advance the flow. A stale re-tap of an
    # earlier consent card (user scrolled up) carries an old key → ignore silently, record
    # nothing, do not advance. This guarantees every required consent is accepted in order.
    if not _consent_key_matches(consent_key, data.get("_consent_key")):
        await callback.answer()
        return
    await record_user_consent(callback.from_user.id, consent_key)  # D-02 audit row
    await callback.answer("✅ Принято")
    # Defense-in-depth: disable the tapped card's button so it can't be re-used.
    try:
        await callback.message.edit_reply_markup(reply_markup=None)
    except Exception:
        pass
    # Consents run before ФИО: walk the consent queue, then ask ФИО.
    queue = data.get("_consent_queue", [])
    i = data.get("_consent_i", 0) + 1
    if i < len(queue):
        await state.update_data(_consent_i=i)
        await _ask_step_or_recall(queue[i], callback.message, state, i + 1, len(queue))
    else:
        await _ask_full_name(callback.message, state)


@router.message(Registration.consent_pending)
async def process_consent_ignore(message: types.Message):
    # SC#2: consent cannot be skipped via text — only the consent button advances.
    btn_text = await get_setting("consent_button_text") or "Согласен(-на)"
    await message.answer(f"Нажми кнопку «{btn_text}» для продолжения.")
