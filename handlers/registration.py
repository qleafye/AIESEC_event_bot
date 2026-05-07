import logging
import html
from datetime import datetime

from aiogram import Bot, F, Router, types
from aiogram.filters import Command, CommandObject, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.types import FSInputFile, ReplyKeyboardRemove

from config import config
from database.db import add_user, get_user, get_setting
from handlers.states import Registration
from keyboards.builders import (
    get_main_menu_kb,
    get_cancel_kb,
    get_confirm_kb,
    get_skip_kb,
    get_source_kb,
    get_education_status_kb,
    get_universities_kb,
    get_course_kb,
    get_yes_no_kb,
)
from services.sheets import append_to_sheet

router = Router()
logger = logging.getLogger(__name__)

DEFAULT_START_TEXT = (
    "Привет! 👋\n\n"
    "Это бот мероприятия. Зарегистрируйся, чтобы получить доступ ко всей информации.\n\n"
    "Настройте текст приветствия через /admin → Настройки → Приветствие."
)


def _extract_referrer_id(command_args: str | None, current_user_id: int) -> int | None:
    if not command_args:
        return None
    arg = command_args.strip()
    if not arg.isdigit():
        return None
    referrer_id = int(arg)
    if referrer_id == current_user_id:
        return None
    return referrer_id


def _extract_source_tag(command_args: str | None) -> str | None:
    if not command_args:
        return None
    arg = command_args.strip()
    if arg.startswith("src_") and len(arg) > 4:
        return arg[4:]
    return None


def _age_word(age: int) -> str:
    if 11 <= age % 100 <= 19:
        return "лет"
    last = age % 10
    if last == 1:
        return "год"
    if 2 <= last <= 4:
        return "года"
    return "лет"


def _progress(step: int, total: int) -> str:
    return f"({step}/{total})"


def _build_sheet_row(data: dict) -> list:
    details_parts = []
    if data.get("referrer_id"):
        details_parts.append(f"Referrer ID: {data['referrer_id']}")
    details = " | ".join(details_parts) if details_parts else "-"

    return [
        data.get("telegram_id"),
        data.get("username", "-"),
        data.get("registration_date", "-"),
        data.get("full_name", "-"),
        data.get("age", "-"),
        data.get("email", "-"),
        "Yes" if data.get("is_aiesec_member") else "No",
        data.get("source", "Самостоятельно"),
        details,
        data.get("education_status", "-"),
        data.get("university", "-"),
        data.get("course", "-"),
        data.get("specialty", "-"),
        "Yes" if data.get("work_status") else "No",
        data.get("work_sphere", "-"),
        data.get("missing_skills", "-"),
        data.get("expectations", "-"),
    ]


async def _start_registration_flow(message: types.Message, state: FSMContext, referrer_id: int | None = None, source_tag: str | None = None):
    existing_data = await state.get_data()
    saved_referrer_id = referrer_id or existing_data.get("referrer_id")
    saved_source_tag = source_tag or existing_data.get("source")

    await state.clear()
    if saved_referrer_id:
        await state.update_data(referrer_id=saved_referrer_id)
        logger.info(f"Saved referrer_id={saved_referrer_id} for user {message.from_user.id}")
    if saved_source_tag:
        await state.update_data(source=saved_source_tag)
        logger.info(f"Saved source_tag={saved_source_tag} for user {message.from_user.id}")

    await message.answer(
        "Отлично, начинаем регистрацию."
        if not saved_referrer_id
        else "Отлично, ты пришёл по приглашению друга. Начинаем регистрацию."
    )
    await message.answer("Напиши свою Фамилию и Имя:", reply_markup=get_cancel_kb())
    await state.set_state(Registration.full_name)


@router.message(Command("start"), StateFilter("*"))
async def cmd_start(message: types.Message, state: FSMContext, command: CommandObject | None = None):
    user_id = message.from_user.id
    logger.info(f"User {user_id} requested /start")

    user = await get_user(user_id)
    args = command.args if command else None
    referrer_id = _extract_referrer_id(args, user_id)
    source_tag = _extract_source_tag(args)
    if referrer_id:
        logger.info(f"Deep-link referrer_id={referrer_id} for user {user_id}")

    if user:
        start_text = await get_setting("start_text") or DEFAULT_START_TEXT
        start_photo = await get_setting("start_photo_file_id")

        if start_photo:
            try:
                await message.answer_photo(start_photo, caption=start_text, reply_markup=get_main_menu_kb(), parse_mode="HTML")
                return
            except Exception:
                pass

        try:
            photo = FSInputFile("resources/start.jpg")
            await message.answer_photo(photo, caption=start_text, reply_markup=get_main_menu_kb(), parse_mode="HTML")
        except Exception:
            await message.answer(start_text, reply_markup=get_main_menu_kb(), parse_mode="HTML")
        return

    await _start_registration_flow(message, state, referrer_id=referrer_id, source_tag=source_tag)


@router.message(StateFilter(Registration), F.text.in_({"Отмена", "/cancel"}))
async def cancel_registration(message: types.Message, state: FSMContext):
    await state.clear()
    await message.answer(
        "Регистрация отменена. Чтобы начать заново, отправь /start.",
        reply_markup=ReplyKeyboardRemove(),
    )


@router.message(Registration.full_name)
async def process_full_name(message: types.Message, state: FSMContext):
    full_name = (message.text or "").strip()
    if len(full_name.split()) < 2:
        await message.answer("Пожалуйста, укажи и имя, и фамилию.")
        return

    await state.update_data(full_name=full_name)
    await message.answer("Теперь напиши свой возраст числом:", reply_markup=get_cancel_kb())
    await state.set_state(Registration.age)


@router.message(Registration.age)
async def process_age(message: types.Message, state: FSMContext, bot: Bot):
    raw_age = (message.text or "").strip()
    if not raw_age.isdigit():
        await message.answer("Возраст должен быть числом. Попробуй еще раз.")
        return

    age = int(raw_age)
    if age < 10 or age > 120:
        await message.answer("Укажи корректный возраст числом от 10 до 120.")
        return

    await state.update_data(age=age)

    data = await state.get_data()
    name = html.escape(data.get("full_name", ""))
    await message.answer(
        f"<b>{name}</b>, {age} {_age_word(age)} — всё верно?",
        reply_markup=get_confirm_kb(),
        parse_mode="HTML",
    )
    await state.set_state(Registration.confirm)


@router.message(Registration.confirm)
async def process_confirm(message: types.Message, state: FSMContext, bot: Bot):
    text = (message.text or "").strip()

    if text == "Изменить":
        await message.answer("Напиши свою Фамилию и Имя:", reply_markup=get_cancel_kb())
        await state.set_state(Registration.full_name)
        return

    if text != "Всё верно ✓":
        await message.answer(
            "Нажми «Всё верно ✓» или «Изменить».",
            reply_markup=get_confirm_kb(),
        )
        return

    mode = await get_setting("registration_mode")
    if mode != "full":
        await finalize_registration(message, state, bot)
        return

    data = await state.get_data()
    has_source = bool(data.get("source"))
    total = 4
    if not has_source:
        total += 1

    await state.update_data(_reg_step=0, _reg_total=total)
    await message.answer("Отлично! Осталось несколько вопросов о тебе.")

    if has_source:
        await state.update_data(_reg_step=1)
        await message.answer(
            f"{_progress(1, total)} Учишься ли ты сейчас?",
            reply_markup=get_education_status_kb(),
        )
        await state.set_state(Registration.education_status)
    else:
        await state.update_data(_reg_step=1)
        await message.answer(
            f"{_progress(1, total)} Откуда ты узнал(а) о форуме?",
            reply_markup=get_source_kb(),
        )
        await state.set_state(Registration.source)


# --- Full form handlers ---

@router.message(Registration.source)
async def process_source(message: types.Message, state: FSMContext):
    source = (message.text or "").strip()
    if not source:
        await message.answer("Выбери один из вариантов или напиши свой.")
        return
    await state.update_data(source=source)

    data = await state.get_data()
    step = data.get("_reg_step", 1) + 1
    total = data.get("_reg_total", 5)
    await state.update_data(_reg_step=step)

    await message.answer(
        f"{_progress(step, total)} Учишься ли ты сейчас?",
        reply_markup=get_education_status_kb(),
    )
    await state.set_state(Registration.education_status)


@router.message(Registration.education_status)
async def process_education_status(message: types.Message, state: FSMContext):
    status = (message.text or "").strip()
    if not status:
        await message.answer("Выбери один из вариантов.")
        return
    await state.update_data(education_status=status)

    data = await state.get_data()
    step = data.get("_reg_step", 2) + 1
    total = data.get("_reg_total", 5)

    if status.startswith("Да"):
        total += 3
        await state.update_data(_reg_step=step, _reg_total=total)
        await message.answer(
            f"{_progress(step, total)} В каком ВУЗе/колледже ты учишься?",
            reply_markup=get_universities_kb(),
        )
        await state.set_state(Registration.university)
    else:
        await state.update_data(
            university="-", course="-", specialty="-",
            _reg_step=step, _reg_total=total,
        )
        await message.answer(
            f"{_progress(step, total)} Работаешь ли ты сейчас?",
            reply_markup=get_yes_no_kb(),
        )
        await state.set_state(Registration.work_status)


@router.message(Registration.university)
async def process_university(message: types.Message, state: FSMContext):
    uni = (message.text or "").strip()
    if not uni:
        await message.answer("Выбери ВУЗ из списка или напиши свой.")
        return
    await state.update_data(university=uni)

    data = await state.get_data()
    step = data.get("_reg_step", 3) + 1
    total = data.get("_reg_total", 8)
    await state.update_data(_reg_step=step)

    await message.answer(
        f"{_progress(step, total)} На каком ты курсе?",
        reply_markup=get_course_kb(),
    )
    await state.set_state(Registration.course)


@router.message(Registration.course)
async def process_course(message: types.Message, state: FSMContext):
    course = (message.text or "").strip()
    if not course:
        await message.answer("Выбери курс.")
        return
    await state.update_data(course=course)

    data = await state.get_data()
    step = data.get("_reg_step", 4) + 1
    total = data.get("_reg_total", 8)
    await state.update_data(_reg_step=step)

    await message.answer(
        f"{_progress(step, total)} Какая у тебя специальность?",
        reply_markup=get_skip_kb(),
    )
    await state.set_state(Registration.specialty)


@router.message(Registration.specialty)
async def process_specialty(message: types.Message, state: FSMContext):
    text = (message.text or "").strip()
    if not text:
        await message.answer("Напиши специальность или нажми «Пропустить».")
        return

    await state.update_data(specialty="-" if text == "Пропустить" else text)

    data = await state.get_data()
    step = data.get("_reg_step", 5) + 1
    total = data.get("_reg_total", 8)
    await state.update_data(_reg_step=step)

    await message.answer(
        f"{_progress(step, total)} Работаешь ли ты сейчас?",
        reply_markup=get_yes_no_kb(),
    )
    await state.set_state(Registration.work_status)


@router.message(Registration.work_status)
async def process_work_status(message: types.Message, state: FSMContext):
    answer = (message.text or "").strip()
    if answer not in ("Да", "Нет"):
        await message.answer("Выбери «Да» или «Нет».")
        return

    working = answer == "Да"
    await state.update_data(work_status=working)

    data = await state.get_data()
    step = data.get("_reg_step", 6) + 1
    total = data.get("_reg_total", 8)

    if working:
        total += 1
        await state.update_data(_reg_step=step, _reg_total=total)
        await message.answer(
            f"{_progress(step, total)} В какой сфере ты работаешь?",
            reply_markup=get_skip_kb(),
        )
        await state.set_state(Registration.work_sphere)
    else:
        await state.update_data(work_sphere="-", _reg_step=step, _reg_total=total)
        await message.answer(
            f"{_progress(step, total)} Каких навыков тебе сейчас не хватает?",
            reply_markup=get_skip_kb(),
        )
        await state.set_state(Registration.missing_skills)


@router.message(Registration.work_sphere)
async def process_work_sphere(message: types.Message, state: FSMContext):
    text = (message.text or "").strip()
    if not text:
        await message.answer("Напиши сферу работы или нажми «Пропустить».")
        return

    await state.update_data(work_sphere="-" if text == "Пропустить" else text)

    data = await state.get_data()
    step = data.get("_reg_step", 7) + 1
    total = data.get("_reg_total", 9)
    await state.update_data(_reg_step=step)

    await message.answer(
        f"{_progress(step, total)} Каких навыков тебе сейчас не хватает?",
        reply_markup=get_skip_kb(),
    )
    await state.set_state(Registration.missing_skills)


@router.message(Registration.missing_skills)
async def process_missing_skills(message: types.Message, state: FSMContext):
    text = (message.text or "").strip()
    if not text:
        await message.answer("Напиши или нажми «Пропустить».")
        return

    await state.update_data(missing_skills="-" if text == "Пропустить" else text)

    data = await state.get_data()
    step = data.get("_reg_step", 8) + 1
    total = data.get("_reg_total", 9)
    await state.update_data(_reg_step=step)

    await message.answer(
        f"{_progress(step, total)} Что ты ожидаешь от форума?",
        reply_markup=get_skip_kb(),
    )
    await state.set_state(Registration.expectations)


@router.message(Registration.expectations)
async def process_expectations(message: types.Message, state: FSMContext, bot: Bot):
    text = (message.text or "").strip()
    if not text:
        await message.answer("Напиши или нажми «Пропустить».")
        return

    await state.update_data(expectations="-" if text == "Пропустить" else text)
    await finalize_registration(message, state, bot)


# --- Finalize ---

async def finalize_registration(message: types.Message, state: FSMContext, bot: Bot):
    data = await state.get_data()
    data["telegram_id"] = message.from_user.id
    data["username"] = f"@{message.from_user.username}" if message.from_user.username else "-"
    data["registration_date"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    data.setdefault("email", "-")
    data.setdefault("is_aiesec_member", False)
    data.setdefault("source", "Реферальная ссылка" if data.get("referrer_id") else "Самостоятельно")
    data.setdefault("source_details", f"Referrer ID: {data.get('referrer_id', '-')}")
    data.setdefault("education_status", "-")
    data.setdefault("university", "-")
    data.setdefault("course", "-")
    data.setdefault("specialty", "-")
    data.setdefault("work_status", False)
    data.setdefault("work_sphere", "-")
    data.setdefault("missing_skills", "-")
    data.setdefault("expectations", "-")

    await add_user(data)

    try:
        await append_to_sheet(_build_sheet_row(data))
    except Exception as e:
        logger.error(f"Failed to append user {message.from_user.id} to Google Sheet: {e}")

    if config.ADMIN_IDS:
        safe_name = html.escape(str(data.get("full_name", "-")))
        safe_username = html.escape(str(data.get("username", "-")))
        safe_source = html.escape(str(data.get("source", "-")))
        admin_text = (
            f"🆕 <b>Новая регистрация!</b>\n"
            f"👤 {safe_name} ({safe_username})\n"
            f"🎂 {data.get('age', '-')}\n"
            f"📝 {safe_source}"
        )
        for admin_id in config.ADMIN_IDS:
            try:
                await bot.send_message(admin_id, admin_text, parse_mode="HTML")
            except Exception as e:
                logger.error(f"Failed to notify admin {admin_id}: {e}")

    await state.clear()
    await message.answer("Регистрация завершена! Увидимся на форуме! 🎉", reply_markup=get_main_menu_kb())

    bonus_enabled = await get_setting("reg_bonus_enabled")
    if bonus_enabled == "on":
        bonus_caption = await get_setting("reg_bonus_caption") or "🎁 Бонус за регистрацию!"
        bonus_photo = await get_setting("reg_bonus_photo_file_id")
        bonus_doc = await get_setting("reg_bonus_doc_file_id")
        try:
            if bonus_doc:
                await message.answer_document(bonus_doc, caption=bonus_caption, parse_mode="HTML")
            elif bonus_photo:
                await message.answer_photo(bonus_photo, caption=bonus_caption, parse_mode="HTML")
        except Exception as e:
            logger.error(f"Failed to send bonus to {message.from_user.id}: {e}")
