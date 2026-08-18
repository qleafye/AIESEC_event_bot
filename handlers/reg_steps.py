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
from keyboards.builders import get_cancel_kb
from handlers.registration import (
    router,
    _get_enabled_steps, _ask_step_or_recall, finalize_registration,
    _advance, _parse_age,
)

# --- Core Registration ---

@router.message(Registration.full_name)
async def process_full_name(message: types.Message, state: FSMContext, bot: Bot):
    full_name = (message.text or "").strip()
    if len(full_name.split()) < 2:
        await message.answer("Укажи ФИО полностью (минимум фамилию и имя).")
        return

    await state.update_data(full_name=full_name)

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
    data = await state.get_data()
    enabled = await _get_enabled_steps(data)

    if not enabled:
        await finalize_registration(message, state, bot)
        return

    total = len(enabled)
    await state.update_data(_reg_step=1, _reg_total=total)
    await _ask_step_or_recall(enabled[0], message, state, 1, total)


# --- Extended Question Handlers ---

@router.message(Registration.age)
async def process_age(message: types.Message, state: FSMContext, bot: Bot):
    age = _parse_age(message.text)
    if age is None:
        await message.answer("Укажи корректный возраст числом от 10 до 120.")
        return
    await state.update_data(age=age)
    await _advance("age", message, state, bot)


@router.message(Registration.email)
async def process_email(message: types.Message, state: FSMContext, bot: Bot):
    email = (message.text or "").strip()
    if not email or "@" not in email or "." not in email:
        await message.answer("Укажи корректный email (например, name@example.com).")
        return
    await state.update_data(email=email)
    await _advance("email", message, state, bot)


@router.message(Registration.phone, F.contact)
async def process_phone_contact(message: types.Message, state: FSMContext, bot: Bot):
    phone = message.contact.phone_number
    if not phone.startswith("+"):
        phone = f"+{phone}"
    await state.update_data(phone=phone)
    await _advance("phone", message, state, bot)


@router.message(Registration.phone)
async def process_phone(message: types.Message, state: FSMContext, bot: Bot):
    text = (message.text or "").strip()
    if text == "Пропустить":
        await state.update_data(phone="-")
        await _advance("phone", message, state, bot)
        return
    cleaned = text.replace(" ", "").replace("-", "").replace("(", "").replace(")", "")
    if not cleaned:
        await message.answer("Укажи номер телефона или нажми «Пропустить».")
        return
    if not (cleaned.startswith("+") and cleaned[1:].isdigit()) and not cleaned.isdigit():
        await message.answer("Укажи корректный номер телефона или нажми «Пропустить».")
        return
    await state.update_data(phone=text)
    await _advance("phone", message, state, bot)


@router.message(Registration.vk)
async def process_vk(message: types.Message, state: FSMContext, bot: Bot):
    vk = (message.text or "").strip()
    # Tatiana: ник строго в формате @username.
    if not vk.startswith("@") or len(vk) < 2 or " " in vk:
        await message.answer("Укажи ник в ВК в формате @username (начинается с @, без пробелов).")
        return
    await state.update_data(vk_username=vk)
    await _advance("vk", message, state, bot)


@router.message(Registration.city)
async def process_city(message: types.Message, state: FSMContext, bot: Bot):
    text = (message.text or "").strip()
    if not text:
        await message.answer("Выбери город на клавиатуре или напиши свой.")
        return
    if text == "Другое":
        # Tap «Другое» → ask for the free-text value; the next message (any city) is stored.
        await message.answer("Напиши название своего города:", reply_markup=get_cancel_kb())
        return
    await state.update_data(city=text)
    await _advance("city", message, state, bot)


@router.message(Registration.source)
async def process_source(message: types.Message, state: FSMContext, bot: Bot):
    source = (message.text or "").strip()
    if not source:
        await message.answer("Выбери один из вариантов или напиши свой.")
        return
    if source == "Другое":
        await message.answer("Напиши свой вариант:", reply_markup=get_cancel_kb())
        return
    await state.update_data(source=source)
    await _advance("source", message, state, bot)


@router.message(Registration.local_committee)
async def process_local_committee(message: types.Message, state: FSMContext, bot: Bot):
    text = (message.text or "").strip()
    if not text:
        await message.answer("Выбери локальный комитет из списка или напиши свой.")
        return
    if text == "Другое":
        await message.answer("Напиши название своего ЛК:", reply_markup=get_cancel_kb())
        return
    await state.update_data(local_committee=text)
    await _advance("local_committee", message, state, bot)


@router.message(Registration.position)
async def process_position(message: types.Message, state: FSMContext, bot: Bot):
    text = (message.text or "").strip()
    if not text:
        await message.answer("Выбери позицию из списка или напиши свою.")
        return
    if text == "Другое":
        await message.answer("Напиши свою позицию:", reply_markup=get_cancel_kb())
        return
    await state.update_data(position=text)
    await _advance("position", message, state, bot)


@router.message(Registration.education_status)
async def process_education_status(message: types.Message, state: FSMContext, bot: Bot):
    status = (message.text or "").strip()
    if not status:
        await message.answer("Выбери один из вариантов.")
        return
    await state.update_data(education_status=status)
    if not status.startswith("Да"):
        await state.update_data(university="-", course="-", specialty="-", study_field="-")
    await _advance("education_status", message, state, bot)


@router.message(Registration.university)
async def process_university(message: types.Message, state: FSMContext, bot: Bot):
    uni = (message.text or "").strip()
    if not uni:
        await message.answer("Выбери ВУЗ из списка или напиши свой.")
        return
    if uni == "Другое":
        await message.answer("Напиши название своего ВУЗа:", reply_markup=get_cancel_kb())
        return
    await state.update_data(university=uni)
    await _advance("university", message, state, bot)


@router.message(Registration.course)
async def process_course(message: types.Message, state: FSMContext, bot: Bot):
    course = (message.text or "").strip()
    if not course:
        await message.answer("Выбери курс.")
        return
    await state.update_data(course=course)
    await _advance("course", message, state, bot)


@router.message(Registration.specialty)
async def process_specialty(message: types.Message, state: FSMContext, bot: Bot):
    text = (message.text or "").strip()
    if not text:
        await message.answer("Напиши или нажми «Пропустить».")
        return
    await state.update_data(specialty="-" if text == "Пропустить" else text)
    await _advance("specialty", message, state, bot)


@router.message(Registration.work_status)
async def process_work_status(message: types.Message, state: FSMContext, bot: Bot):
    answer = (message.text or "").strip()
    if answer not in ("Да", "Нет"):
        await message.answer("Выбери «Да» или «Нет».")
        return
    working = answer == "Да"
    await state.update_data(work_status=working)
    if not working:
        await state.update_data(work_sphere="-")
    await _advance("work_status", message, state, bot)


@router.message(Registration.work_sphere)
async def process_work_sphere(message: types.Message, state: FSMContext, bot: Bot):
    text = (message.text or "").strip()
    if not text:
        await message.answer("Напиши сферу работы или нажми «Пропустить».")
        return
    await state.update_data(work_sphere="-" if text == "Пропустить" else text)
    await _advance("work_sphere", message, state, bot)


@router.message(Registration.missing_skills)
async def process_missing_skills(message: types.Message, state: FSMContext, bot: Bot):
    text = (message.text or "").strip()
    if not text:
        await message.answer("Напиши или нажми «Пропустить».")
        return
    await state.update_data(missing_skills="-" if text == "Пропустить" else text)
    await _advance("missing_skills", message, state, bot)


@router.message(Registration.expectations)
async def process_expectations(message: types.Message, state: FSMContext, bot: Bot):
    text = (message.text or "").strip()
    if not text:
        await message.answer("Напиши или нажми «Пропустить».")
        return
    await state.update_data(expectations="-" if text == "Пропустить" else text)
    await _advance("expectations", message, state, bot)


@router.message(Registration.informal_day)
async def process_informal_day(message: types.Message, state: FSMContext, bot: Bot):
    text = (message.text or "").strip()
    if text not in ("Да", "Нет", "Буду только в онлайне"):
        await message.answer("Выбери один из вариантов.")
        return
    await state.update_data(informal_day=text)
    await _advance("informal_day", message, state, bot)


@router.message(Registration.attendance_format)
async def process_attendance_format(message: types.Message, state: FSMContext, bot: Bot):
    text = (message.text or "").strip()
    if text not in ("Offline", "Online"):
        await message.answer("Выбери «Offline» или «Online».")
        return
    await state.update_data(attendance_format=text)
    await _advance("attendance_format", message, state, bot)


@router.message(Registration.comments)
async def process_comments(message: types.Message, state: FSMContext, bot: Bot):
    text = (message.text or "").strip()
    if not text:
        await message.answer("Напиши или нажми «Пропустить».")
        return
    await state.update_data(comments="-" if text == "Пропустить" else text)
    await _advance("comments", message, state, bot)


# --- Conference (RusCo) reg-flow handlers ---

async def _store_choice(field: str, after: str, message: types.Message, state: FSMContext, bot: Bot):
    text = (message.text or "").strip()
    if not text:
        await message.answer("Выбери вариант на клавиатуре или напиши ответ.")
        return
    if text == "Другое":
        # Covers every «Другое»-bearing choice step routed through here (department,
        # aiesec_role, …): ask for the free-text value, stay in state, store the next reply.
        await message.answer("Напиши свой вариант:", reply_markup=get_cancel_kb())
        return
    await state.update_data(**{field: text})
    await _advance(after, message, state, bot)


async def _store_text(field: str, after: str, message: types.Message, state: FSMContext, bot: Bot):
    text = (message.text or "").strip()
    if not text:
        await message.answer("Напиши или нажми «Пропустить».")
        return
    await state.update_data(**{field: "-" if text == "Пропустить" else text})
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
