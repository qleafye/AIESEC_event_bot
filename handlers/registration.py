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
    get_skip_kb,
    get_local_committee_kb,
    get_position_kb,
    get_attendance_format_kb,
    get_informal_day_kb,
)
from services.sheets import append_to_sheet

router = Router()
logger = logging.getLogger(__name__)

DEFAULT_START_TEXT = (
    "Привет! \U0001f44b\n\n"
    "Это бот мероприятия. Зарегистрируйся, чтобы получить доступ ко всей информации.\n\n"
    "Настройте текст приветствия через /admin → Настройки → Приветствие."
)

# --- Registration Flow Engine ---

REG_FLOW = [
    ("age", "reg_q_age"),
    ("email", "reg_q_email"),
    ("phone", "reg_q_phone"),
    ("city", "reg_q_city"),
    ("local_committee", "reg_q_lc"),
    ("position", "reg_q_position"),
    ("expectations_ar", "reg_q_expectations_ar"),
    ("informal_day", "reg_q_informal_day"),
    ("attendance_format", "reg_q_attendance"),
    ("comments", "reg_q_comments"),
]

REG_DEFAULTS = {
    "reg_q_age": "off",
    "reg_q_email": "off",
    "reg_q_phone": "off",
    "reg_q_city": "off",
    "reg_q_lc": "on",
    "reg_q_position": "on",
    "reg_q_expectations_ar": "on",
    "reg_q_informal_day": "on",
    "reg_q_attendance": "off",
    "reg_q_comments": "off",
}

REG_LABELS = {
    "reg_q_age": "\U0001f382 Возраст",
    "reg_q_email": "\U0001f4e7 Email",
    "reg_q_phone": "\U0001f4f1 Телефон",
    "reg_q_city": "\U0001f3d9 Город",
    "reg_q_lc": "\U0001f3e2 Лок. комитет",
    "reg_q_position": "\U0001f454 Позиция",
    "reg_q_expectations_ar": "✨ Ожидания",
    "reg_q_informal_day": "\U0001f3d5 Неформальный день",
    "reg_q_attendance": "\U0001f4cd Формат",
    "reg_q_comments": "\U0001f4ac Комментарии",
}


async def _is_step_enabled(setting_key: str) -> bool:
    val = await get_setting(setting_key)
    if val is None:
        return REG_DEFAULTS.get(setting_key, "on") == "on"
    return val == "on"


async def _get_enabled_steps(data: dict) -> list[str]:
    enabled = []
    for step_key, setting_key in REG_FLOW:
        if not await _is_step_enabled(setting_key):
            continue
        enabled.append(step_key)
    return enabled


async def _ask_step(step_key: str, message: types.Message, state: FSMContext, step: int, total: int):
    p = _progress(step, total)
    if step_key == "age":
        await message.answer(f"{p} Напиши свой возраст числом:", reply_markup=get_cancel_kb())
        await state.set_state(Registration.age)
    elif step_key == "email":
        await message.answer(f"{p} Укажи свой email:", reply_markup=get_cancel_kb())
        await state.set_state(Registration.email)
    elif step_key == "phone":
        await message.answer(f"{p} Укажи номер телефона:", reply_markup=get_skip_kb())
        await state.set_state(Registration.phone)
    elif step_key == "city":
        await message.answer(f"{p} Из какого ты города?", reply_markup=get_skip_kb())
        await state.set_state(Registration.city)
    elif step_key == "local_committee":
        await message.answer(f"{p} Локальный комитет:", reply_markup=get_local_committee_kb())
        await state.set_state(Registration.local_committee)
    elif step_key == "position":
        await message.answer(f"{p} Твоя позиция:", reply_markup=get_position_kb())
        await state.set_state(Registration.position)
    elif step_key == "expectations_ar":
        await message.answer(
            f"{p} Какие ваши ожидания от посещения Годового отчета AIESEC в России? "
            "Что бы вы хотели узнать/получить?",
            reply_markup=get_skip_kb(),
        )
        await state.set_state(Registration.expectations_ar)
    elif step_key == "informal_day":
        await message.answer(
            f"{p} Планируете ли вы посетить второй неформальный день годового отчета, "
            "который пройдет загородом?",
            reply_markup=get_informal_day_kb(),
        )
        await state.set_state(Registration.informal_day)
    elif step_key == "attendance_format":
        await message.answer(f"{p} В каком формате ты будешь присутствовать?", reply_markup=get_attendance_format_kb())
        await state.set_state(Registration.attendance_format)
    elif step_key == "comments":
        await message.answer(f"{p} Любые вопросы/комментарии/пожелания:", reply_markup=get_skip_kb())
        await state.set_state(Registration.comments)


async def _advance(after_step: str, message: types.Message, state: FSMContext, bot: Bot):
    data = await state.get_data()
    enabled = await _get_enabled_steps(data)

    try:
        idx = enabled.index(after_step)
        next_idx = idx + 1
    except ValueError:
        next_idx = 0

    if next_idx < len(enabled):
        step = data.get("_reg_step", 0) + 1
        total = data.get("_reg_total", len(enabled))
        await state.update_data(_reg_step=step)
        await _ask_step(enabled[next_idx], message, state, step, total)
    else:
        await finalize_registration(message, state, bot)


# --- Helpers ---

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
        data.get("email", "-"),
        data.get("source", "-"),
        details,
        data.get("phone", "-"),
        data.get("city", "-"),
        data.get("local_committee", "-"),
        data.get("position", "-"),
        data.get("expectations_ar", "-"),
        data.get("informal_day", "-"),
        data.get("attendance_format", "-"),
        data.get("comments", "-"),
    ]


# --- /start ---

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
                await message.answer_photo(start_photo, caption=start_text, reply_markup=await get_main_menu_kb(), parse_mode="HTML")
                return
            except Exception:
                pass

        try:
            photo = FSInputFile("resources/start.jpg")
            await message.answer_photo(photo, caption=start_text, reply_markup=await get_main_menu_kb(), parse_mode="HTML")
        except Exception:
            await message.answer(start_text, reply_markup=await get_main_menu_kb(), parse_mode="HTML")
        return

    await _start_registration_flow(message, state, referrer_id=referrer_id, source_tag=source_tag)


@router.message(StateFilter(Registration), F.text.in_({"Отмена", "/cancel"}))
async def cancel_registration(message: types.Message, state: FSMContext):
    await state.clear()
    await message.answer(
        "Регистрация отменена. Чтобы начать заново, отправь /start.",
        reply_markup=ReplyKeyboardRemove(),
    )


# --- Core Registration ---

@router.message(Registration.full_name)
async def process_full_name(message: types.Message, state: FSMContext, bot: Bot):
    full_name = (message.text or "").strip()
    if len(full_name.split()) < 2:
        await message.answer("Пожалуйста, укажи и имя, и фамилию.")
        return

    await state.update_data(full_name=full_name)

    mode = await get_setting("registration_mode")
    if mode != "full":
        await finalize_registration(message, state, bot)
        return

    data = await state.get_data()
    enabled = await _get_enabled_steps(data)

    if not enabled:
        await finalize_registration(message, state, bot)
        return

    total = len(enabled)
    await state.update_data(_reg_step=1, _reg_total=total)
    await _ask_step(enabled[0], message, state, 1, total)


# --- Extended Question Handlers ---

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
    await _advance("age", message, state, bot)


@router.message(Registration.email)
async def process_email(message: types.Message, state: FSMContext, bot: Bot):
    email = (message.text or "").strip()
    if not email or "@" not in email or "." not in email:
        await message.answer("Укажи корректный email (например, name@example.com).")
        return
    await state.update_data(email=email)
    await _advance("email", message, state, bot)


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


@router.message(Registration.city)
async def process_city(message: types.Message, state: FSMContext, bot: Bot):
    text = (message.text or "").strip()
    if not text:
        await message.answer("Напиши город или нажми «Пропустить».")
        return
    await state.update_data(city="-" if text == "Пропустить" else text)
    await _advance("city", message, state, bot)


@router.message(Registration.local_committee)
async def process_local_committee(message: types.Message, state: FSMContext, bot: Bot):
    text = (message.text or "").strip()
    if not text:
        await message.answer("Выбери локальный комитет из списка или напиши свой.")
        return
    await state.update_data(local_committee=text)
    await _advance("local_committee", message, state, bot)


@router.message(Registration.position)
async def process_position(message: types.Message, state: FSMContext, bot: Bot):
    text = (message.text or "").strip()
    if not text:
        await message.answer("Выбери позицию из списка или напиши свою.")
        return
    await state.update_data(position=text)
    await _advance("position", message, state, bot)


@router.message(Registration.expectations_ar)
async def process_expectations_ar(message: types.Message, state: FSMContext, bot: Bot):
    text = (message.text or "").strip()
    if not text:
        await message.answer("Напиши или нажми «Пропустить».")
        return
    await state.update_data(expectations_ar="-" if text == "Пропустить" else text)
    await _advance("expectations_ar", message, state, bot)


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


# --- Finalize ---

async def finalize_registration(message: types.Message, state: FSMContext, bot: Bot):
    data = await state.get_data()
    data["telegram_id"] = message.from_user.id
    data["username"] = f"@{message.from_user.username}" if message.from_user.username else "-"
    data["registration_date"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    data.setdefault("email", "-")
    data.setdefault("phone", "-")
    data.setdefault("city", "-")
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
    data.setdefault("local_committee", "-")
    data.setdefault("position", "-")
    data.setdefault("expectations_ar", "-")
    data.setdefault("informal_day", "-")
    data.setdefault("attendance_format", "-")
    data.setdefault("comments", "-")

    await add_user(data)

    try:
        await append_to_sheet(_build_sheet_row(data))
    except Exception as e:
        logger.error(f"Failed to append user {message.from_user.id} to Google Sheet: {e}")

    if config.ADMIN_IDS:
        safe_name = html.escape(str(data.get("full_name", "-")))
        safe_username = html.escape(str(data.get("username", "-")))
        admin_text = (
            f"\U0001f195 <b>Новая регистрация!</b>\n"
            f"\U0001f464 {safe_name} ({safe_username})"
        )
        if data.get("local_committee") and data["local_committee"] != "-":
            admin_text += f"\n\U0001f3e2 {html.escape(str(data['local_committee']))}"
        if data.get("position") and data["position"] != "-":
            admin_text += f"\n\U0001f454 {html.escape(str(data['position']))}"

        for admin_id in config.ADMIN_IDS:
            try:
                await bot.send_message(admin_id, admin_text, parse_mode="HTML")
            except Exception as e:
                logger.error(f"Failed to notify admin {admin_id}: {e}")

    await state.clear()
    complete_text = await get_setting("reg_complete_text") or "Регистрация завершена! Увидимся на годовом отчете!"
    await message.answer(complete_text, reply_markup=await get_main_menu_kb(), parse_mode="HTML")

    bonus_enabled = await get_setting("reg_bonus_enabled")
    if bonus_enabled == "on":
        bonus_caption = await get_setting("reg_bonus_caption") or "\U0001f381 Бонус за регистрацию!"
        bonus_photo = await get_setting("reg_bonus_photo_file_id")
        bonus_doc = await get_setting("reg_bonus_doc_file_id")
        try:
            if bonus_doc:
                await message.answer_document(bonus_doc, caption=bonus_caption, parse_mode="HTML")
            elif bonus_photo:
                await message.answer_photo(bonus_photo, caption=bonus_caption, parse_mode="HTML")
        except Exception as e:
            logger.error(f"Failed to send bonus to {message.from_user.id}: {e}")
