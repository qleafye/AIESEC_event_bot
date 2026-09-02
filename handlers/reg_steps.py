"""Phase 13 REFAC (13-03, REFAC-02): registration per-state process_* step handlers.

Extracted from handlers/registration.py (the ~40 handlers that fire one per FSM state,
process_full_name through process_volunteer) to keep registration.py under the ~800-line
guideline (CONCERNS.md). Every handler here decorates the SAME shared `router` object
defined in handlers/registration.py -- imported below, not redefined -- so main.py (which
includes registration.router by object reference) never changes and handler registration
order is controlled entirely by WHEN this module is imported (bottom of registration.py,
after the reg_flow seam import, matching this block's original last-in-file position).

Behavior is byte-for-byte unchanged from the pre-move code -- every handler body below is a
verbatim relocation, not a rewrite.
"""
from aiogram import F, types, Bot
from aiogram.fsm.context import FSMContext

from handlers.states import Registration
from handlers.registration import (
    router,
    _get_enabled_steps, _ask_step_or_recall, finalize_registration, _after_full_name,
    _advance, _err_kb,
)
from reg_engine import validate_answer, apply_answer, STEP_TO_COLUMN

# --- Core Registration ---

# Phase 21 (21-06, FORM-SYNC-01): каждый простой текстовый шаг ниже — один и тот же контур:
# validate_answer(step_key, raw) -> (value, err); err -> сообщить и остаться; иначе сохранить
# value под своей колонкой (STEP_TO_COLUMN, совпадает с тем, что писал сам хендлер раньше:
# vk_username/is_ambassador_candidate — единственные шаги с несовпадающим именем) и продолжить.
# Тела НЕ дублируют проверку — вся она теперь в reg_engine.validate_answer (T-21-05: один
# судья для чата и Mini App), здесь только STORE + ADVANCE.

@router.message(Registration.full_name)
async def process_full_name(message: types.Message, state: FSMContext, bot: Bot):
    value, err = validate_answer("full_name", message.text)
    if err:
        await message.answer(err)
        return
    await state.update_data(full_name=value)

    # Phase 7 (SHORT-04): the old `registration_mode != "full"` early-exit straight to
    # finalize() is REMOVED. The generic engine below now runs for every track, including
    # "short" — `_get_enabled_steps` (Task 1) resolves the short track's own `reg_q_*__short`
    # keys, and with zero such keys set it returns [] exactly like today, so the
    # "no steps -> finalize" branch four lines down still fires and reproduces the historical
    # short-form behavior STRUCTURALLY, not via a duplicated special case. Two consequences
    # that are NOT regressions:
    # 1. A short form with >=1 enabled question now reaches _advance and shows the
    #    _build_summary confirmation screen before finalizing, same as the full form — this
    #    is the whole point of the phase (a real multi-question track, not just ФИО).
    # 2. `registration_mode` is no longer read here at all. The only place the live mode
    #    setting affects the question set is `_resolve_track` at flow start (Task 1c) — a
    #    delegate who already started a track keeps finishing in that track even if the
    #    manager flips the toggle mid-session.
    await _after_full_name(message, state, bot)


# --- Extended Question Handlers ---

async def _thin_step(step_key: str, message: types.Message, state: FSMContext, bot: Bot,
                      *, kb_on_error: bool = False):
    """Общий контур «валидировать → сохранить под своей колонкой → _advance», без побочных
    правил (education_status/work_status используют apply_answer напрямую — см. ниже)."""
    value, err = validate_answer(step_key, message.text)
    if err:
        await message.answer(err, reply_markup=_err_kb(message.text) if kb_on_error else None)
        return
    column = STEP_TO_COLUMN.get(step_key, step_key)
    await state.update_data(**{column: value})
    await _advance(step_key, message, state, bot)


@router.message(Registration.age)
async def process_age(message: types.Message, state: FSMContext, bot: Bot):
    await _thin_step("age", message, state, bot)


@router.message(Registration.email)
async def process_email(message: types.Message, state: FSMContext, bot: Bot):
    await _thin_step("email", message, state, bot)


@router.message(Registration.phone, F.contact)
async def process_phone_contact(message: types.Message, state: FSMContext, bot: Bot):
    phone = message.contact.phone_number
    if not phone.startswith("+"):
        phone = f"+{phone}"
    await state.update_data(phone=phone)
    await _advance("phone", message, state, bot)


@router.message(Registration.phone)
async def process_phone(message: types.Message, state: FSMContext, bot: Bot):
    await _thin_step("phone", message, state, bot)


@router.message(Registration.vk)
async def process_vk(message: types.Message, state: FSMContext, bot: Bot):
    await _thin_step("vk", message, state, bot)


@router.message(Registration.city)
async def process_city(message: types.Message, state: FSMContext, bot: Bot):
    await _thin_step("city", message, state, bot, kb_on_error=True)


@router.message(Registration.source)
async def process_source(message: types.Message, state: FSMContext, bot: Bot):
    await _thin_step("source", message, state, bot, kb_on_error=True)


@router.message(Registration.local_committee)
async def process_local_committee(message: types.Message, state: FSMContext, bot: Bot):
    await _thin_step("local_committee", message, state, bot, kb_on_error=True)


@router.message(Registration.position)
async def process_position(message: types.Message, state: FSMContext, bot: Bot):
    await _thin_step("position", message, state, bot, kb_on_error=True)


@router.message(Registration.education_status)
async def process_education_status(message: types.Message, state: FSMContext, bot: Bot):
    value, err = validate_answer("education_status", message.text)
    if err:
        await message.answer(err)
        return
    data = await state.get_data()
    # apply_answer (APPLY_GOLDEN): не «Да…» -> ВУЗ/курс/специальность/направление прочерком.
    await state.set_data(apply_answer(data, "education_status", value))
    await _advance("education_status", message, state, bot)


@router.message(Registration.university)
async def process_university(message: types.Message, state: FSMContext, bot: Bot):
    await _thin_step("university", message, state, bot, kb_on_error=True)


@router.message(Registration.course)
async def process_course(message: types.Message, state: FSMContext, bot: Bot):
    await _thin_step("course", message, state, bot)


@router.message(Registration.specialty)
async def process_specialty(message: types.Message, state: FSMContext, bot: Bot):
    await _thin_step("specialty", message, state, bot)


@router.message(Registration.work_status)
async def process_work_status(message: types.Message, state: FSMContext, bot: Bot):
    value, err = validate_answer("work_status", message.text)
    if err:
        await message.answer(err)
        return
    data = await state.get_data()
    # apply_answer (APPLY_GOLDEN): work_status=Нет -> work_sphere прочерком.
    await state.set_data(apply_answer(data, "work_status", value))
    await _advance("work_status", message, state, bot)


@router.message(Registration.work_sphere)
async def process_work_sphere(message: types.Message, state: FSMContext, bot: Bot):
    await _thin_step("work_sphere", message, state, bot)


@router.message(Registration.missing_skills)
async def process_missing_skills(message: types.Message, state: FSMContext, bot: Bot):
    await _thin_step("missing_skills", message, state, bot)


@router.message(Registration.expectations)
async def process_expectations(message: types.Message, state: FSMContext, bot: Bot):
    await _thin_step("expectations", message, state, bot)


@router.message(Registration.informal_day)
async def process_informal_day(message: types.Message, state: FSMContext, bot: Bot):
    await _thin_step("informal_day", message, state, bot)


@router.message(Registration.attendance_format)
async def process_attendance_format(message: types.Message, state: FSMContext, bot: Bot):
    await _thin_step("attendance_format", message, state, bot)


@router.message(Registration.comments)
async def process_comments(message: types.Message, state: FSMContext, bot: Bot):
    await _thin_step("comments", message, state, bot)


# --- Conference (RusCo) reg-flow handlers ---

async def _store_choice(field: str, after: str, message: types.Message, state: FSMContext, bot: Bot):
    # field == after == validate_answer step_key for every caller below (department, aiesec_role,
    # needs_certificate, english_level, alumni_status, arrival, housing, bed_sharing, transport,
    # volunteer) — reg_engine._CHOICE_STEPS covers the same set with the same generic error/
    # «Другое»-prompt text this function used to inline.
    value, err = validate_answer(field, message.text)
    if err:
        await message.answer(err, reply_markup=_err_kb(message.text))
        return
    await state.update_data(**{field: value})
    await _advance(after, message, state, bot)


async def _store_text(field: str, after: str, message: types.Message, state: FSMContext, bot: Bot):
    value, err = validate_answer(field, message.text)
    if err:
        await message.answer(err)
        return
    await state.update_data(**{field: value})
    await _advance(after, message, state, bot)


@router.message(Registration.department)
async def process_department(message: types.Message, state: FSMContext, bot: Bot):
    await _store_choice("department", "department", message, state, bot)


@router.message(Registration.aiesec_role)
async def process_aiesec_role(message: types.Message, state: FSMContext, bot: Bot):
    await _store_choice("aiesec_role", "aiesec_role", message, state, bot)


@router.message(Registration.needs_certificate)
async def process_needs_certificate(message: types.Message, state: FSMContext, bot: Bot):
    await _store_choice("needs_certificate", "needs_certificate", message, state, bot)


@router.message(Registration.english_level)
async def process_english_level(message: types.Message, state: FSMContext, bot: Bot):
    await _store_choice("english_level", "english_level", message, state, bot)


@router.message(Registration.allergies)
async def process_allergies(message: types.Message, state: FSMContext, bot: Bot):
    await _store_text("allergies", "allergies", message, state, bot)


@router.message(Registration.food_pref)
async def process_food_pref(message: types.Message, state: FSMContext, bot: Bot):
    await _store_text("food_pref", "food_pref", message, state, bot)


@router.message(Registration.alumni_status)
async def process_alumni_status(message: types.Message, state: FSMContext, bot: Bot):
    await _store_choice("alumni_status", "alumni_status", message, state, bot)


@router.message(Registration.arrival)
async def process_arrival(message: types.Message, state: FSMContext, bot: Bot):
    await _store_choice("arrival", "arrival", message, state, bot)


@router.message(Registration.housing)
async def process_housing(message: types.Message, state: FSMContext, bot: Bot):
    await _store_choice("housing", "housing", message, state, bot)


@router.message(Registration.bed_sharing)
async def process_bed_sharing(message: types.Message, state: FSMContext, bot: Bot):
    # On «Да» the bed_partner step is enabled by _get_enabled_steps; on «Нет» it's skipped.
    await _store_choice("bed_sharing", "bed_sharing", message, state, bot)


@router.message(Registration.bed_partner)
async def process_bed_partner(message: types.Message, state: FSMContext, bot: Bot):
    await _store_text("bed_partner", "bed_partner", message, state, bot)


@router.message(Registration.transport)
async def process_transport(message: types.Message, state: FSMContext, bot: Bot):
    await _store_choice("transport", "transport", message, state, bot)


@router.message(Registration.cc_shop)
async def process_cc_shop(message: types.Message, state: FSMContext, bot: Bot):
    await _store_text("cc_shop", "cc_shop", message, state, bot)


@router.message(Registration.exp_organizers)
async def process_exp_organizers(message: types.Message, state: FSMContext, bot: Bot):
    await _store_text("exp_organizers", "exp_organizers", message, state, bot)


@router.message(Registration.exp_content)
async def process_exp_content(message: types.Message, state: FSMContext, bot: Bot):
    await _store_text("exp_content", "exp_content", message, state, bot)


@router.message(Registration.volunteer)
async def process_volunteer(message: types.Message, state: FSMContext, bot: Bot):
    await _store_choice("volunteer", "volunteer", message, state, bot)
