import asyncio
import logging
import html
from datetime import datetime

from aiogram import Bot, F, Router, types
from aiogram.filters import Command, CommandObject, StateFilter
from aiogram.fsm.context import FSMContext
import os

from aiogram.types import FSInputFile, ReplyKeyboardRemove, InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.utils.keyboard import ReplyKeyboardBuilder

from config import config
from database.db import add_user, get_user, get_setting, mark_reg_started, clear_reg_started, set_user_subscribed, set_user_status, record_user_consent
from handlers.states import Registration
from keyboards.builders import (
    get_main_menu_kb,
    get_cancel_kb,
    get_confirm_kb,
    get_skip_kb,
    get_phone_kb,
    get_local_committee_kb,
    get_position_kb,
    get_department_kb,
    get_aiesec_role_kb,
    get_english_level_kb,
    get_arrival_kb,
    get_housing_kb,
    get_attendance_format_kb,
    get_informal_day_kb,
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
    "Привет! \U0001f44b\n\n"
    "Это бот мероприятия. Зарегистрируйся, чтобы получить доступ ко всей информации.\n\n"
    "Настройте текст приветствия через /admin → Настройки → Приветствие."
)

# Tatiana: «поздравляем» теперь приходит СРАЗУ после регистрации (раньше — только после
# одобрения). reg_complete_text = пост-регистрационный скрипт; approve_text = отдельный
# скрипт после одобрения заявки. Оба правятся в /admin → Настройки.
DEFAULT_REG_COMPLETE_TEXT = (
    "Поздравляем, твоя заявка принята!\n\n"
    "Мы рассмотрим её в течение 2-3 дней и напишем сюда. "
    "Следи за обновлениями, впереди много интересного.\n\n"
    "Если у тебя возникнут вопросы — не стесняйся задавать их нам!"
)
DEFAULT_APPROVE_TEXT = "Твоя заявка одобрена! Добро пожаловать 🎉"

# --- Approval status decision (Phase 2, D-01..D-03) ---

def _decide_status(reg_mode: str, full_setting: str, short_setting: str) -> str:
    """Form type x per-form moderation setting -> 'pending' | 'approved'.
    Full form uses full_setting, short form uses short_setting; 'manual' -> pending."""
    setting = full_setting if reg_mode == "full" else short_setting
    return "pending" if setting == "manual" else "approved"


# --- Registration Flow Engine ---

# Phase 4 (D-07): each entry is (step_key, setting_key, type). type is "text" (default
# free-text handler), "date" (ДД.ММ.ГГГГ validation), or "consent" (injected dynamically,
# never declared statically here). Iteration sites star-unpack the type so 2-tuples would
# still work during any incremental migration.
REG_FLOW = [
    # YL'26 launch order (Tatiana). Consent + ФИО run before this list (see
    # _start_registration_flow). Order here IS the ask order for the enabled steps.
    ("age", "reg_q_age", "text"),
    ("phone", "reg_q_phone", "text"),
    ("vk", "reg_q_vk", "text"),
    ("city", "reg_q_city", "text"),
    ("education_status", "reg_q_education", "text"),
    ("course", "reg_q_course", "text"),
    ("university", "reg_q_university", "text"),
    ("study_field", "reg_q_study_field", "select"),
    ("goal", "reg_q_goal", "multi"),
    ("formats", "reg_q_formats", "multi"),
    ("expectations", "reg_q_expectations", "text"),
    ("source", "reg_q_source", "text"),
    ("ambassador", "reg_q_ambassador", "ambassador"),
    ("resume", "reg_q_resume", "text"),
    # Remaining steps — default OFF, kept for other events (RusCo/Summit).
    ("email", "reg_q_email", "text"),
    ("local_committee", "reg_q_lc", "text"),
    ("position", "reg_q_position", "text"),
    ("specialty", "reg_q_specialty", "text"),
    ("work_status", "reg_q_work", "text"),
    ("work_sphere", "reg_q_work_sphere", "text"),
    ("missing_skills", "reg_q_skills", "text"),
    ("attendance_format", "reg_q_attendance", "text"),
    ("informal_day", "reg_q_informal_day", "text"),
    ("comments", "reg_q_comments", "text"),
    ("department", "reg_q_department", "text"),
    ("aiesec_role", "reg_q_aiesec_role", "text"),
    ("needs_certificate", "reg_q_certificate", "text"),
    ("english_level", "reg_q_english", "text"),
    ("allergies", "reg_q_allergies", "text"),
    ("food_pref", "reg_q_food", "text"),
    ("arrival", "reg_q_arrival", "text"),
    ("housing", "reg_q_housing", "text"),
    ("bed_sharing", "reg_q_bed_sharing", "text"),   # конфа: делить двуспальную кровать?
    ("bed_partner", "reg_q_bed_partner", "text"),   # конфа: с кем (условно на «Да»)
    ("transport", "reg_q_transport", "text"),
    ("cc_shop", "reg_q_cc_shop", "text"),
    ("exp_organizers", "reg_q_exp_organizers", "text"),
    ("exp_content", "reg_q_exp_content", "text"),
    ("volunteer", "reg_q_volunteer", "text"),
    ("arrival_date", "reg_q_arrival_date", "date"),
    ("birth_date", "reg_q_birth_date", "date"),
    ("payment_plan_date", "reg_q_payment_date", "date"),
]

# Map step_key → type for O(1) dispatch in _ask_step (consent:* keys handled separately).
REG_STEP_TYPES = {step_key: step_type for step_key, _sk, step_type in REG_FLOW}

# Configurable single-select steps: step_key → (options_setting_key, default options).
# Options edited in admin as newline text (reuse source_options pattern). "Другое" appended.
SELECT_CONFIG = {
    "city": ("city_options", [
        "Москва и МО", "Санкт-Петербург", "Новосибирск", "Екатеринбург",
        "Казань", "Нижний Новгород", "Красноярск", "Уфа",
    ]),
    "study_field": ("study_field_options", [
        "Бизнес и управление", "IT и технологии",
        "Социальные и гуманитарные науки", "Математические и естественные науки",
    ]),
}

# Configurable multi-select steps: step_key → (options_setting_key, default options).
MULTI_CONFIG = {
    "goal": ("goal_options", [
        "Найти возможность трудоустройства",
        "Прокачать свои hard и soft skills",
        "Пообщаться с людьми из моей сферы, нетворкинг",
        "Получить карьерную консультацию от HR",
        "Узнать о деятельности компаний",
    ]),
    "formats": ("formats_options", [
        "Панельные дискуссии", "Мастер-классы", "Сессии со спикерами",
        "Нетворкинг-сессии", "Ярмарка открытых вакансий",
    ]),
}


async def _get_options(setting_key: str, defaults: list[str]) -> list[str]:
    """Admin-editable option list (newline text) with a hardcoded fallback."""
    raw = await get_setting(setting_key)
    if raw:
        items = [line.strip() for line in raw.splitlines() if line.strip()]
        if items:
            return items
    return list(defaults)

REG_DEFAULTS = {
    "reg_q_age": "on",
    "reg_q_vk": "on",            # ник в ВК (@username) — YL'26
    "reg_q_email": "off",
    "reg_q_phone": "off",
    "reg_q_city": "off",
    "reg_q_source": "on",
    "reg_q_lc": "off",
    "reg_q_position": "off",
    "reg_q_education": "on",
    "reg_q_university": "on",
    "reg_q_course": "on",
    "reg_q_study_field": "on",   # «Направление обучения» (select) — заменяет специальность
    "reg_q_specialty": "off",
    "reg_q_work": "on",
    "reg_q_work_sphere": "on",
    "reg_q_skills": "on",
    "reg_q_expectations": "on",
    "reg_q_attendance": "off",
    "reg_q_informal_day": "off",
    "reg_q_comments": "off",
    "reg_q_department": "off",
    "reg_q_aiesec_role": "off",
    "reg_q_certificate": "off",
    "reg_q_english": "off",
    "reg_q_allergies": "off",
    "reg_q_food": "off",
    "reg_q_arrival": "off",
    "reg_q_housing": "off",
    "reg_q_bed_sharing": "off",
    "reg_q_bed_partner": "off",
    "reg_q_transport": "off",
    "reg_q_payment_date": "off",
    "reg_q_cc_shop": "off",
    "reg_q_exp_organizers": "off",
    "reg_q_exp_content": "off",
    "reg_q_volunteer": "off",
    "reg_q_arrival_date": "off",
    "reg_q_birth_date": "off",
    "reg_q_goal": "off",
    "reg_q_formats": "off",
    "reg_q_ambassador": "off",
    "reg_q_resume": "off",
}

REG_LABELS = {
    "reg_q_age": "\U0001f382 Возраст",
    "reg_q_vk": "\U0001f535 ВК",
    "reg_q_email": "\U0001f4e7 Email",
    "reg_q_phone": "\U0001f4f1 Телефон",
    "reg_q_city": "\U0001f3d9 Город",
    "reg_q_source": "\U0001f4e2 Источник",
    "reg_q_lc": "\U0001f3e2 Лок. комитет",
    "reg_q_position": "\U0001f454 Позиция",
    "reg_q_education": "\U0001f393 Образование",
    "reg_q_university": "\U0001f3eb ВУЗ",
    "reg_q_course": "\U0001f4d6 Курс",
    "reg_q_specialty": "\U0001f4dd Специальность",
    "reg_q_work": "\U0001f4bc Работа",
    "reg_q_work_sphere": "\U0001f3ed Сфера работы",
    "reg_q_skills": "\U0001f4a1 Навыки",
    "reg_q_expectations": "\U0001f4ac Ожидания (общие)",
    "reg_q_informal_day": "\U0001f3d5 Неформальный день",
    "reg_q_attendance": "\U0001f4cd Формат",
    "reg_q_comments": "\U0001f4ac Доп. комментарии",
    "reg_q_department": "🏢 Департамент",
    "reg_q_aiesec_role": "🎖 Позиция AIESEC",
    "reg_q_certificate": "📄 Справка в ВУЗ",
    "reg_q_english": "🇬🇧 Англ. язык",
    "reg_q_allergies": "🤧 Аллергии",
    "reg_q_food": "🥗 Питание",
    "reg_q_arrival": "🚌 Приезд",
    "reg_q_housing": "🏠 Проживание",
    "reg_q_bed_sharing": "🛏 Общая кровать",
    "reg_q_bed_partner": "🛏 Сосед по кровати",
    "reg_q_transport": "🚗 Трансфер",
    "reg_q_payment_date": "💳 Дата оплаты",
    "reg_q_cc_shop": "🛍 CC-shop",
    "reg_q_exp_organizers": "💬 Ожидания: организация",
    "reg_q_exp_content": "💬 Ожидания: контент",
    "reg_q_volunteer": "🙋 Волонтёр",
    "reg_q_arrival_date": "📅 Дата приезда",
    "reg_q_birth_date": "🎂 Дата рождения",
    "reg_q_study_field": "🎯 Направление обучения",
    "reg_q_goal": "🎯 Цель участия",
    "reg_q_formats": "📋 Форматы форума",
    "reg_q_ambassador": "🧡 Амбассадор",
    "reg_q_resume": "\U0001f4c4 Резюме",
}

# --- Event-type presets (admin one-tap bulk toggle) ---
# A preset lists the reg_q_* keys to turn ON (everything else in REG_DEFAULTS is turned
# OFF) plus the payment module flag. Applying a preset is an explicit admin action that
# writes the same settings the per-question toggles write — it changes NOTHING until
# tapped, so live bots keep their current flow. Extra questions can still be flipped on
# individually afterwards (see REG_CATEGORIES «➕ Экстра»).
REG_PRESETS = {
    "forum": {
        "label": "🏛 Форум (YouLead)",
        "payment_enabled": "off",
        "on": [
            "reg_q_age", "reg_q_vk", "reg_q_source", "reg_q_education",
            "reg_q_university", "reg_q_course", "reg_q_study_field", "reg_q_work",
            "reg_q_work_sphere", "reg_q_skills", "reg_q_expectations",
        ],
    },
    "conf": {
        "label": "🎤 Конференция (RusCo)",
        "payment_enabled": "on",
        "on": [
            "reg_q_age", "reg_q_vk", "reg_q_phone", "reg_q_lc", "reg_q_work",
            "reg_q_department", "reg_q_aiesec_role", "reg_q_english", "reg_q_allergies",
            "reg_q_food", "reg_q_arrival", "reg_q_bed_sharing", "reg_q_bed_partner",
            "reg_q_transport", "reg_q_payment_date",
            "reg_q_cc_shop", "reg_q_exp_organizers", "reg_q_volunteer",
        ],
    },
}

# Display grouping for the admin question-toggle view. Disjoint buckets covering every
# REG_FLOW key exactly once — purely cosmetic (helps the manager find a question), does
# not affect which questions are asked (that is REG_DEFAULTS + per-key settings).
REG_CATEGORIES = [
    ("👥 Общие", ["reg_q_age", "reg_q_vk", "reg_q_work"]),
    ("🏛 Форум", [
        "reg_q_education", "reg_q_course", "reg_q_university", "reg_q_study_field",
        "reg_q_expectations", "reg_q_source", "reg_q_work_sphere", "reg_q_skills",
    ]),
    ("🎤 Конфа", [
        "reg_q_phone", "reg_q_lc", "reg_q_department", "reg_q_aiesec_role",
        "reg_q_english", "reg_q_allergies", "reg_q_food", "reg_q_arrival",
        "reg_q_bed_sharing", "reg_q_bed_partner",
        "reg_q_transport", "reg_q_cc_shop", "reg_q_exp_organizers", "reg_q_volunteer",
        "reg_q_payment_date",
    ]),
    ("➕ Экстра", [
        "reg_q_city", "reg_q_goal", "reg_q_formats", "reg_q_ambassador", "reg_q_resume",
        "reg_q_email", "reg_q_position", "reg_q_specialty", "reg_q_attendance",
        "reg_q_informal_day", "reg_q_comments", "reg_q_certificate", "reg_q_housing",
        "reg_q_exp_content", "reg_q_arrival_date", "reg_q_birth_date",
    ]),
]


async def _prompt(step_key: str, default: str) -> str:
    """Editable question wording: admin override reg_prompt_<step_key> else the default.
    Lets organizers set exact per-event wording (YL'26/Summit/…) without code changes."""
    return await get_setting(f"reg_prompt_{step_key}") or default


async def _is_step_enabled(setting_key: str) -> bool:
    val = await get_setting(setting_key)
    if val is None:
        return REG_DEFAULTS.get(setting_key, "on") == "on"
    return val == "on"


async def _is_module_enabled(key: str) -> bool:
    """Phase 4 module flag check — None/absent/'off'/anything-but-'on' → False (D-15 fail-safe)."""
    val = await get_setting(key)
    return val == "on"


async def _get_enabled_steps(data: dict) -> list[str]:
    enabled = []
    # edu_conditional (default on): skip ВУЗ/курс/специальность when "не учусь". Turn OFF
    # for events (e.g. YL'26) that ask образование as a level, not a Да/Нет, and want those
    # steps always shown.
    edu_conditional = (await get_setting("edu_conditional") or "on") == "on"
    studying = str(data.get("education_status", "")).startswith("Да")
    for step_key, setting_key, *_rest in REG_FLOW:
        if not await _is_step_enabled(setting_key):
            continue
        if step_key == "informal_day" and data.get("attendance_format") == "Online":
            continue
        # Source came from a src_ deep-link tag — don't ask «Откуда узнал», it's authoritative.
        if step_key == "source" and data.get("_source_from_tag"):
            continue
        # Tatiana: «Где будешь жить» — только если приезжает Заранее (в дни конфы жильё не нужно).
        # Backward-safe: gate only when arrival was actually asked; else housing stays unconditional.
        if step_key == "housing" and "arrival" in data and data.get("arrival") != "Заранее":
            continue
        # «С кем на кровати» спрашиваем только если согласился делить (bed_sharing = «Да»).
        if step_key == "bed_partner" and not str(data.get("bed_sharing", "")).startswith("Да"):
            continue
        if edu_conditional and step_key == "university" and not studying:
            continue
        if edu_conditional and step_key == "course" and not studying:
            continue
        if edu_conditional and step_key == "specialty" and not studying:
            continue
        if edu_conditional and step_key == "study_field" and not studying:
            continue
        if step_key == "work_sphere" and not data.get("work_status"):
            continue
        enabled.append(step_key)
    # Consents run BEFORE ФИО now (handled in _start_registration_flow /
    # process_consent_accept), so they are no longer part of the question engine.
    return enabled


# Fallback when consent_enabled is on but consent_list is empty: one «обработка ПД» consent
# (ссылки на документы уже в приветственном сообщении, поэтому хватает одной кнопки).
DEFAULT_CONSENTS = [("Согласие на обработку персональных данных", "personal_data")]


async def _consent_entries() -> list[tuple[str, str]]:
    """Parse consent_list ('Видимое название | ключ' per line) → [(label, key)].
    Empty/invalid list → DEFAULT_CONSENTS. Shared by step-building and rendering so the
    label/key stay in sync."""
    raw = await get_setting("consent_list") or ""
    entries: list[tuple[str, str]] = []
    # Accept ';' as a line separator too: on mobile Telegram Enter=send splits a
    # multi-line entry into separate messages (only the first survives set_setting),
    # so admins can put all consents on one line using ';'. Newline data unaffected.
    for line in raw.replace(";", "\n").strip().splitlines():
        line = line.strip()
        if not line or "|" not in line:
            continue
        label, consent_key = line.split("|", 1)
        consent_key = consent_key.strip()
        if consent_key:
            entries.append((label.strip(), consent_key))
    return entries or DEFAULT_CONSENTS


async def _get_consent_steps() -> list[str]:
    """Consent step keys (consent:<key>) when consent_enabled is on, else []. Shared by
    the full-form engine and the short-form path so consents fire regardless of form length."""
    if not await _is_module_enabled("consent_enabled"):
        return []
    return [f"consent:{key}" for _label, key in await _consent_entries()]


async def _payment_price_block() -> str:
    """Price list prepended to the payment-date question so the user sees the cost(s)
    while picking a plan date (prices otherwise appear only post-approval, in
    handlers.payment). Empty when the payment module is off or no options are set.
    Informational only — tariff selection still happens after approval."""
    if not await _is_module_enabled("payment_enabled"):
        return ""
    from handlers.payment import _parse_options  # local import avoids circular import
    options = _parse_options(await get_setting("payment_options") or "")
    if not options:
        return ""
    lines = []
    for label, price in options:
        price_txt = f"{price} ₽" if price > 0 else "бесплатно"
        lines.append(f"• {html.escape(label)} — {price_txt}")
    return "💳 <b>Стоимость участия:</b>\n" + "\n".join(lines) + "\n\n"


async def _ask_step(step_key: str, message: types.Message, state: FSMContext, step: int, total: int):
    p = await _progress(step, total)
    if step_key == "age":
        await message.answer(f"{p}{await _prompt('age', 'Напиши свой возраст числом:')}", reply_markup=get_cancel_kb())
        await state.set_state(Registration.age)
    elif step_key == "email":
        await message.answer(f"{p}{await _prompt('email', 'Укажи свой email:')}", reply_markup=get_cancel_kb())
        await state.set_state(Registration.email)
    elif step_key == "phone":
        await message.answer(f"{p}{await _prompt('phone', 'Укажи номер телефона:')}", reply_markup=get_phone_kb())
        await state.set_state(Registration.phone)
    elif step_key == "vk":
        await message.answer(
            f"{p}{await _prompt('vk', 'Введи свой ник в ВК в формате @username:')}",
            reply_markup=get_cancel_kb(),
        )
        await state.set_state(Registration.vk)
    elif step_key == "city":
        opt_key, default = SELECT_CONFIG["city"]
        options = await _get_options(opt_key, default)
        await message.answer(
            f"{p}{await _prompt('city', 'Из какого ты города?')}",
            reply_markup=_reply_kb(options, add_other=True),
        )
        await state.set_state(Registration.city)
    elif step_key == "source":
        await message.answer(f"{p}{await _prompt('source', 'Откуда ты узнал(а) о нас?')}", reply_markup=await get_source_kb())
        await state.set_state(Registration.source)
    elif step_key == "local_committee":
        await message.answer(f"{p}{await _prompt('local_committee', 'Локальный комитет:')}", reply_markup=get_local_committee_kb())
        await state.set_state(Registration.local_committee)
    elif step_key == "position":
        await message.answer(f"{p}{await _prompt('position', 'Твоя позиция:')}", reply_markup=get_position_kb())
        await state.set_state(Registration.position)
    elif step_key == "education_status":
        await message.answer(f"{p}{await _prompt('education_status', 'Учишься ли ты сейчас?')}", reply_markup=get_education_status_kb())
        await state.set_state(Registration.education_status)
    elif step_key == "university":
        # Mode toggle (reg_university_mode): "list" = pick from база вузов, "text" = free input.
        mode = await get_setting("reg_university_mode") or "text"
        if mode == "text":
            await message.answer(f"{p}{await _prompt('university', 'Введи название твоего ВУЗа:')}", reply_markup=get_skip_kb())
        else:
            uni_opts = await get_setting("university_options")
            if uni_opts and uni_opts.strip():
                options = [l.strip() for l in uni_opts.splitlines() if l.strip()]
                kb = _reply_kb(options, add_other=True)
            else:
                kb = get_universities_kb()  # fallback: config.UNIVERSITIES
            await message.answer(f"{p}{await _prompt('university', 'В каком ВУЗе/колледже ты учишься?')}", reply_markup=kb)
        await state.set_state(Registration.university)
    elif step_key == "course":
        await message.answer(f"{p}{await _prompt('course', 'На каком ты курсе?')}", reply_markup=get_course_kb())
        await state.set_state(Registration.course)
    elif step_key == "specialty":
        await message.answer(f"{p}{await _prompt('specialty', 'Какая у тебя специальность?')}", reply_markup=get_skip_kb())
        await state.set_state(Registration.specialty)
    elif step_key == "work_status":
        await message.answer(f"{p}{await _prompt('work_status', 'Работаешь ли ты сейчас?')}", reply_markup=get_yes_no_kb())
        await state.set_state(Registration.work_status)
    elif step_key == "work_sphere":
        await message.answer(f"{p}{await _prompt('work_sphere', 'В какой сфере ты работаешь?')}", reply_markup=get_skip_kb())
        await state.set_state(Registration.work_sphere)
    elif step_key == "missing_skills":
        await message.answer(f"{p}{await _prompt('missing_skills', 'Каких навыков тебе сейчас не хватает?')}", reply_markup=get_skip_kb())
        await state.set_state(Registration.missing_skills)
    elif step_key == "expectations":
        event_name = await get_setting("event_name") or "мероприятия"
        await message.answer(
            f"{p}{await _prompt('expectations', f'Что ты ожидаешь от {event_name}? Что хотел(а) бы узнать или получить?')}",
            reply_markup=get_skip_kb(),
        )
        await state.set_state(Registration.expectations)
    elif step_key == "informal_day":
        await message.answer(
            f"{p}{await _prompt('informal_day', 'Планируете ли вы посетить второй неформальный день (пройдёт загородом)?')}",
            reply_markup=get_informal_day_kb(),
        )
        await state.set_state(Registration.informal_day)
    elif step_key == "attendance_format":
        await message.answer(f"{p}{await _prompt('attendance_format', 'В каком формате ты будешь присутствовать?')}", reply_markup=get_attendance_format_kb())
        await state.set_state(Registration.attendance_format)
    elif step_key == "comments":
        await message.answer(f"{p}{await _prompt('comments', 'Любые вопросы/комментарии/пожелания:')}", reply_markup=get_skip_kb())
        await state.set_state(Registration.comments)
    elif step_key == "department":
        await message.answer(f"{p}{await _prompt('department', 'Твой департамент:')}", reply_markup=get_department_kb())
        await state.set_state(Registration.department)
    elif step_key == "aiesec_role":
        await message.answer(f"{p}{await _prompt('aiesec_role', 'Твоя позиция (Member/TL/Manager/VP/LCP/Coordinator):')}", reply_markup=get_aiesec_role_kb())
        await state.set_state(Registration.aiesec_role)
    elif step_key == "needs_certificate":
        await message.answer(f"{p}{await _prompt('needs_certificate', 'Нужна справка в ВУЗ?')}", reply_markup=get_yes_no_kb())
        await state.set_state(Registration.needs_certificate)
    elif step_key == "english_level":
        await message.answer(f"{p}{await _prompt('english_level', 'Уровень английского:')}", reply_markup=get_english_level_kb())
        await state.set_state(Registration.english_level)
    elif step_key == "allergies":
        await message.answer(f"{p}{await _prompt('allergies', 'Есть ли у тебя аллергии на продукты/запахи? (если нет — поставь «-»)')}", reply_markup=get_skip_kb())
        await state.set_state(Registration.allergies)
    elif step_key == "food_pref":
        await message.answer(f"{p}{await _prompt('food_pref', 'Особенности питания? Напиши, если ты веган/вегетарианец (иначе — обычное):')}", reply_markup=get_skip_kb())
        await state.set_state(Registration.food_pref)
    elif step_key == "arrival":
        await message.answer(f"{p}{await _prompt('arrival', 'Когда приедешь?')}", reply_markup=get_arrival_kb())
        await state.set_state(Registration.arrival)
    elif step_key == "housing":
        await message.answer(f"{p}{await _prompt('housing', 'Где будешь жить?')}", reply_markup=get_housing_kb())
        await state.set_state(Registration.housing)
    elif step_key == "bed_sharing":
        await message.answer(
            f"{p}{await _prompt('bed_sharing', 'На площадке много двуспальных кроватей. Готов(а) спать с кем-то на одной кровати?')}",
            reply_markup=_reply_kb(["Да", "Нет"]),
        )
        await state.set_state(Registration.bed_sharing)
    elif step_key == "bed_partner":
        await message.answer(
            f"{p}{await _prompt('bed_partner', 'С кем хотел(а) бы делить кровать? Напиши имя или «без разницы».')}",
            reply_markup=get_skip_kb(),
        )
        await state.set_state(Registration.bed_partner)
    elif step_key == "transport":
        await message.answer(
            f"{p}{await _prompt('transport', 'Как добираешься до площадки?')}",
            reply_markup=_reply_kb(["Трансфер до площадки", "Самостоятельно"]),
        )
        await state.set_state(Registration.transport)
    elif step_key == "payment_plan_date":
        # Deadline is pulled live from the payment_deadline setting (stored «ДД.ММ.ГГГГ ЧЧ:ММ»,
        # show just the date) — was hardcoded before, so it never synced with admin config.
        deadline = await get_setting("payment_deadline")
        dl_date = deadline.split()[0] if deadline else ""
        dl_note = f" Крайний срок: {dl_date}." if dl_date else ""
        default = f"Когда планируешь оплатить взнос?{dl_note} Введи дату (ДД.ММ.ГГГГ):"
        # Admin overrides may embed {deadline} to place the date wherever they want.
        prompt = (await _prompt('payment_plan_date', default)).replace("{deadline}", dl_date)
        price_block = await _payment_price_block()  # show tariff prices before asking the date
        await message.answer(f"{p}{price_block}{prompt}", reply_markup=get_cancel_kb())
        await state.update_data(_current_date_step="payment_plan_date")
        await state.set_state(Registration.date_input)
    elif step_key == "cc_shop":
        await message.answer(f"{p}{await _prompt('cc_shop', 'Что бы ты хотел(а) видеть в CC-shop?')}", reply_markup=get_skip_kb())
        await state.set_state(Registration.cc_shop)
    elif step_key == "exp_organizers":
        await message.answer(f"{p}{await _prompt('exp_organizers', 'Ожидания от команды организаторов?')}", reply_markup=get_skip_kb())
        await state.set_state(Registration.exp_organizers)
    elif step_key == "exp_content":
        await message.answer(f"{p}{await _prompt('exp_content', 'Ожидания от контента?')}", reply_markup=get_skip_kb())
        await state.set_state(Registration.exp_content)
    elif step_key == "volunteer":
        await message.answer(f"{p}{await _prompt('volunteer', 'Хочешь быть волонтёром?')}", reply_markup=get_yes_no_kb())
        await state.set_state(Registration.volunteer)
    elif step_key == "resume":
        # No «Отмена» here — tapping it cleared the whole form (все ответы терялись).
        # «Пропустить» finishes registration without a resume instead.
        await message.answer(
            f"{p}{await _prompt('resume', 'Прикрепи резюме файлом (PDF или DOCX) или напиши его текстом. Если резюме нет — нажми «Пропустить».')}",
            reply_markup=get_skip_kb(),
        )
        await state.set_state(Registration.resume)
    elif REG_STEP_TYPES.get(step_key) == "date":
        # Phase 4 (MOD-02): generic date-type step — one handler validates ДД.ММ.ГГГГ.
        label = REG_LABELS.get(f"reg_q_{step_key}", "Дата")
        await message.answer(f"{p}{await _prompt(step_key, f'{label} (ДД.ММ.ГГГГ):')}", reply_markup=get_cancel_kb())
        await state.update_data(_current_date_step=step_key)
        await state.set_state(Registration.date_input)
    elif REG_STEP_TYPES.get(step_key) == "select":
        # Configurable single-select (study_field etc.) — options from settings.
        opt_key, default = SELECT_CONFIG.get(step_key, (f"{step_key}_options", []))
        options = await _get_options(opt_key, default)
        label = REG_LABELS.get(f"reg_q_{step_key}", "Выбери вариант")
        await message.answer(f"{p}{await _prompt(step_key, f'{label}:')}", reply_markup=_reply_kb(options, add_other=True))
        await state.update_data(_current_select_step=step_key)
        await state.set_state(Registration.select_input)
    elif REG_STEP_TYPES.get(step_key) == "multi":
        # Configurable multi-select via inline toggle keyboard.
        opt_key, default = MULTI_CONFIG.get(step_key, (f"{step_key}_options", []))
        options = await _get_options(opt_key, default)
        label = REG_LABELS.get(f"reg_q_{step_key}", "Выбери варианты")
        await state.update_data(_current_multi_step=step_key, **{f"_multi_{step_key}": []})
        await message.answer(
            f"{p}{await _prompt(step_key, f'{label} (можно выбрать несколько):')}",
            reply_markup=_multi_kb(step_key, options, set()),
        )
        await state.set_state(Registration.multi_input)
    elif step_key == "ambassador":
        await message.answer(
            f"{p}{await _prompt('ambassador', 'Хочешь стать амбассадором форума?')}",
            reply_markup=_reply_kb(["Да!", "Пока нет"]),
        )
        await state.set_state(Registration.ambassador)
    elif step_key.startswith("consent:"):
        # Phase 4 (MOD-03, D-03/D-04): one consent per step, PDF attached if configured.
        consent_key = step_key.split(":", 1)[1]
        label = next(
            (lbl for lbl, k in await _consent_entries() if k == consent_key), consent_key
        )
        pdf_file_id = await get_setting(f"consent_pdf_{consent_key}")
        # Ссылки на документы уже в приветственном сообщении — показываем короткий вопрос.
        caption = f"{await _prompt(f'consent_{consent_key}', html.escape(label))}"
        btn_text = await get_setting("consent_button_text") or "Согласен(-на)"
        kb = InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text=btn_text, callback_data=f"consent_accept:{consent_key}")
        ]])
        if pdf_file_id:
            await message.answer_document(pdf_file_id, caption=caption, reply_markup=kb, parse_mode="HTML")
        else:
            await message.answer(caption, reply_markup=kb, parse_mode="HTML")
        await state.update_data(_consent_key=consent_key)
        await state.set_state(Registration.consent_pending)


async def _advance(after_step: str, message: types.Message, state: FSMContext, bot: Bot):
    data = await state.get_data()
    enabled = await _get_enabled_steps(data)

    try:
        idx = enabled.index(after_step)
        next_idx = idx + 1
    except ValueError:
        # WR-01: the just-answered step is no longer in the enabled list (a question was
        # toggled off mid-flow, or a conditional removed it). Finalize instead of bouncing
        # the user back to step 0 — a silent restart is the wrong failure mode.
        next_idx = len(enabled)

    if next_idx < len(enabled):
        step = data.get("_reg_step", 0) + 1
        total = data.get("_reg_total", len(enabled))
        await state.update_data(_reg_step=step)
        await _ask_step(enabled[next_idx], message, state, step, total)
    else:
        # QW-01: show a summary + confirm keyboard before finalizing the full form (D-01).
        await message.answer(_build_summary(data), reply_markup=get_confirm_kb(), parse_mode="HTML")
        await state.set_state(Registration.confirm)


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


async def _progress(step: int, total: int) -> str:
    """Optional «(3/9) » numbering prefix. Off by default (Tatiana: убрать нумерацию);
    organizers can switch it back on with reg_show_progress=on. Returns a trailing space
    so prompts read «{p}{question}» with no stray gap when disabled."""
    if (await get_setting("reg_show_progress") or "off") == "on":
        return f"({step}/{total}) "
    return ""


def _reply_kb(options: list[str], add_other: bool = False, add_skip: bool = False):
    """Build a one-time reply keyboard from a dynamic option list (no hardcoded buttons)."""
    kb = ReplyKeyboardBuilder()
    for opt in options:
        kb.button(text=opt)
    if add_other:
        kb.button(text="Другое")
    if add_skip:
        kb.button(text="Пропустить")
    kb.adjust(2)
    return kb.as_markup(resize_keyboard=True, one_time_keyboard=True)


def _multi_kb(step_key: str, options: list[str], selected: set[int]):
    """Inline toggle keyboard for a multi-select step. Each option toggles via
    regmulti:<step_key>:<idx>; «Готово» finalizes via regmulti_done:<step_key>."""
    rows = []
    for i, opt in enumerate(options):
        mark = "✅ " if i in selected else "▫️ "
        rows.append([InlineKeyboardButton(text=f"{mark}{opt}", callback_data=f"regmulti:{step_key}:{i}")])
    rows.append([InlineKeyboardButton(text="Готово", callback_data=f"regmulti_done:{step_key}")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _sheet_details(data: dict) -> str:
    parts = []
    if data.get("referrer_id"):
        parts.append(f"Referrer ID: {data['referrer_id']}")
    return " | ".join(parts) if parts else "-"


# Google Sheet columns: (header, gate_setting_or_None, value_fn). gate=None → always
# written (identity/system columns). gate=reg_q_* → column appears only when that question
# is enabled, so the sheet width tracks the active preset instead of always being 44 wide.
# Order is the historical SHEET_HEADERS order — do not reorder (append aligns by position).
SHEET_COLUMNS = [
    ("ID Telegram", None, lambda d: d.get("telegram_id") or "-"),
    ("Username", None, lambda d: d.get("username") or "-"),
    ("Дата регистрации", None, lambda d: d.get("registration_date") or "-"),
    ("ФИО", None, lambda d: d.get("full_name") or "-"),
    ("Email", "reg_q_email", lambda d: d.get("email") or "-"),
    ("Источник", "reg_q_source", lambda d: d.get("source") or "-"),
    ("Детали", None, _sheet_details),
    ("Телефон", "reg_q_phone", lambda d: d.get("phone") or "-"),
    ("Город", "reg_q_city", lambda d: d.get("city") or "-"),
    ("Локальный комитет", "reg_q_lc", lambda d: d.get("local_committee") or "-"),
    ("Позиция", "reg_q_position", lambda d: d.get("position") or "-"),
    ("Образование", "reg_q_education", lambda d: d.get("education_status") or "-"),
    ("ВУЗ", "reg_q_university", lambda d: d.get("university") or "-"),
    ("Курс", "reg_q_course", lambda d: d.get("course") or "-"),
    ("Специальность", "reg_q_specialty", lambda d: d.get("specialty") or "-"),
    ("Работает", "reg_q_work", lambda d: "Yes" if d.get("work_status") else "No"),
    ("Сфера работы", "reg_q_work_sphere", lambda d: d.get("work_sphere") or "-"),
    ("Не хватает навыков", "reg_q_skills", lambda d: d.get("missing_skills") or "-"),
    ("Ожидания", "reg_q_expectations", lambda d: d.get("expectations") or "-"),
    ("Ожидания (AR)", "reg_q_expectations", lambda d: d.get("expectations_ar") or "-"),
    ("Неформальный день", "reg_q_informal_day", lambda d: d.get("informal_day") or "-"),
    ("Формат участия", "reg_q_attendance", lambda d: d.get("attendance_format") or "-"),
    ("Комментарии", "reg_q_comments", lambda d: d.get("comments") or "-"),
    ("Департамент", "reg_q_department", lambda d: d.get("department") or "-"),
    ("Роль AIESEC", "reg_q_aiesec_role", lambda d: d.get("aiesec_role") or "-"),
    ("Справка в ВУЗ", "reg_q_certificate", lambda d: d.get("needs_certificate") or "-"),
    ("Английский", "reg_q_english", lambda d: d.get("english_level") or "-"),
    ("Аллергии", "reg_q_allergies", lambda d: d.get("allergies") or "-"),
    ("Питание", "reg_q_food", lambda d: d.get("food_pref") or "-"),
    ("Приезд", "reg_q_arrival", lambda d: d.get("arrival") or "-"),
    ("Проживание", "reg_q_housing", lambda d: d.get("housing") or "-"),
    ("Общая кровать", "reg_q_bed_sharing", lambda d: d.get("bed_sharing") or "-"),
    ("Сосед по кровати", "reg_q_bed_partner", lambda d: d.get("bed_partner") or "-"),
    ("CC-shop", "reg_q_cc_shop", lambda d: d.get("cc_shop") or "-"),
    ("Ожидания от орг", "reg_q_exp_organizers", lambda d: d.get("exp_organizers") or "-"),
    ("Ожидания от контента", "reg_q_exp_content", lambda d: d.get("exp_content") or "-"),
    ("Волонтёр", "reg_q_volunteer", lambda d: d.get("volunteer") or "-"),
    ("Дата приезда", "reg_q_arrival_date", lambda d: d.get("arrival_date") or "-"),
    ("Дата рождения", "reg_q_birth_date", lambda d: d.get("birth_date") or "-"),
    ("Направление обучения", "reg_q_study_field", lambda d: d.get("study_field") or "-"),
    ("Цель участия", "reg_q_goal", lambda d: d.get("goal") or "-"),
    ("Форматы форума", "reg_q_formats", lambda d: d.get("formats") or "-"),
    ("Амбассадор", "reg_q_ambassador", lambda d: "Да" if d.get("is_ambassador_candidate") else "-"),
    ("ВК", "reg_q_vk", lambda d: d.get("vk_username") or "-"),
    ("Трансфер", "reg_q_transport", lambda d: d.get("transport") or "-"),
    ("Дата план. оплаты", "reg_q_payment_date", lambda d: d.get("payment_plan_date") or "-"),
]

# Full static header list (all columns) — kept for reference/tests. Live sync uses the
# dynamic active_sheet_headers() below.
SHEET_HEADERS = [h for h, _g, _f in SHEET_COLUMNS]


def _build_sheet_row(data: dict) -> list:
    """Full-width row (every column) — reference/tests. Live path uses active_sheet_row()."""
    return [fn(data) for _h, _g, fn in SHEET_COLUMNS]


def _sheet_value_map(data: dict) -> dict:
    return {h: fn(data) for h, _g, fn in SHEET_COLUMNS}


async def active_sheet_headers() -> list[str]:
    """Headers for only the columns whose gating question is enabled (system columns
    always included). The sheet width follows the active preset. NOTE: this reflects the
    CURRENT toggles — set the event type before delegates register (the physical header row
    is created once by ensure_sheet_header and is not rewritten if toggles change later)."""
    out = []
    for header, gate, _fn in SHEET_COLUMNS:
        if gate is None or await _is_step_enabled(gate):
            out.append(header)
    return out


async def active_sheet_row(data: dict) -> list:
    """Row projected onto the active (enabled) columns, aligned to active_sheet_headers()."""
    headers = await active_sheet_headers()
    values = _sheet_value_map(data)
    return [values.get(h, "-") for h in headers]


def _esc(value) -> str:
    """Null-coalesce to '-' and HTML-escape free text for the summary message."""
    text = value if (value is not None and str(value) != "") else "-"
    return html.escape(str(text))


def _build_summary(data: dict) -> str:
    """QW-01 pre-finalize summary of the full-form answers (HTML, escaped)."""
    lines = ["<b>Проверь свои ответы:</b>", ""]
    fields = [
        ("ФИО", data.get("full_name")),
        ("Возраст", data.get("age")),
        ("Дата приезда", data.get("arrival_date")),
        ("Дата рождения", data.get("birth_date")),
        ("Email", data.get("email")),
        ("Телефон", data.get("phone")),
        ("ВК", data.get("vk_username")),
        ("Город", data.get("city")),
        ("Источник", data.get("source")),
        ("Лок. комитет", data.get("local_committee")),
        ("Позиция", data.get("position")),
        ("Образование", data.get("education_status")),
        ("ВУЗ", data.get("university")),
        ("Курс", data.get("course")),
        ("Специальность", data.get("specialty")),
        ("Направление обучения", data.get("study_field")),
        ("Работа", "Да" if data.get("work_status") else "Нет"),
        ("Сфера работы", data.get("work_sphere")),
        ("Навыки", data.get("missing_skills")),
        ("Ожидания", data.get("expectations")),
        ("Неформальный день", data.get("informal_day")),
        ("Формат", data.get("attendance_format")),
        ("Комментарии", data.get("comments")),
        ("Департамент", data.get("department")),
        ("Позиция AIESEC", data.get("aiesec_role")),
        ("Справка в ВУЗ", data.get("needs_certificate")),
        ("Английский", data.get("english_level")),
        ("Аллергии", data.get("allergies")),
        ("Питание", data.get("food_pref")),
        ("Приезд", data.get("arrival")),
        ("Проживание", data.get("housing")),
        ("Общая кровать", data.get("bed_sharing")),
        ("Сосед по кровати", data.get("bed_partner")),
        ("Трансфер", data.get("transport")),
        ("Дата план. оплаты", data.get("payment_plan_date")),
        ("CC-shop", data.get("cc_shop")),
        ("Ожидания от орг", data.get("exp_organizers")),
        ("Ожидания от контента", data.get("exp_content")),
        ("Волонтёр", data.get("volunteer")),
        ("Цель участия", data.get("goal")),
        ("Форматы форума", data.get("formats")),
        ("Амбассадор", "Да" if data.get("is_ambassador_candidate") else None),
    ]
    for label, value in fields:
        if value is None or str(value) == "":
            continue
        lines.append(f"<b>{label}:</b> {_esc(value)}")
    if data.get("resume_file_id"):
        lines.append("<b>Резюме:</b> прикреплено файлом")
    elif data.get("resume_text"):
        lines.append(f"<b>Резюме:</b> {_esc(data.get('resume_text'))}")
    return "\n".join(lines)


def _is_allowed_resume(file_name: str | None) -> bool:
    """QW-03: accept only PDF/DOCX by extension (case-insensitive)."""
    if not file_name:
        return False
    name = file_name.lower()
    return name.endswith(".pdf") or name.endswith(".docx")


# --- /start ---

async def _start_registration_flow(message: types.Message, state: FSMContext, referrer_id: int | None = None, source_tag: str | None = None):
    # SCHED-02: record the dropout row at flow start (fail-soft — never block registration).
    try:
        await mark_reg_started(message.from_user.id, message.from_user.username)
    except Exception as e:
        logger.error(f"Failed to mark reg_started for {message.from_user.id}: {e}")

    existing_data = await state.get_data()
    saved_referrer_id = referrer_id or existing_data.get("referrer_id")
    saved_source_tag = source_tag or existing_data.get("source")
    # A src_ deep-link tag is authoritative: skip the «Источник» question so the delegate's
    # answer can't overwrite the campaign tag. Organic users (no tag) still get asked.
    source_from_tag = bool(source_tag) or existing_data.get("_source_from_tag", False)

    await state.clear()
    if saved_referrer_id:
        await state.update_data(referrer_id=saved_referrer_id)
        logger.info(f"Saved referrer_id={saved_referrer_id} for user {message.from_user.id}")
    if saved_source_tag:
        await state.update_data(source=saved_source_tag)
        logger.info(f"Saved source_tag={saved_source_tag} for user {message.from_user.id}")
        if source_from_tag:
            await state.update_data(_source_from_tag=True)

    await message.answer(
        "Отлично, начинаем регистрацию."
        if not saved_referrer_id
        else "Отлично, ты пришёл по приглашению друга. Начинаем регистрацию."
    )
    # Tatiana: согласие — САМЫЙ первый шаг (перед ФИО). Run consents first; ФИО follows.
    consent_steps = await _get_consent_steps()
    if consent_steps:
        await state.update_data(_consent_queue=consent_steps, _consent_i=0)
        await _ask_step(consent_steps[0], message, state, 1, len(consent_steps))
    else:
        await _ask_full_name(message, state)


async def _ask_full_name(message: types.Message, state: FSMContext):
    await message.answer(await _prompt("full_name", "Напиши свои ФИО (Фамилия Имя Отчество):"), reply_markup=get_cancel_kb())
    await state.set_state(Registration.full_name)


async def _send_welcome(message: types.Message, text: str, photo_file_id: str | None, kb, user_id: int):
    short_text = len(text) <= 1024
    photo_sent = False

    photos_to_try = []
    if photo_file_id:
        photos_to_try.append(("DB", photo_file_id))
    if os.path.exists("resources/start.jpg"):
        photos_to_try.append(("file", FSInputFile("resources/start.jpg")))

    for src, photo in photos_to_try:
        if photo_sent:
            break
        if short_text:
            try:
                await message.answer_photo(photo, caption=text, reply_markup=kb, parse_mode="HTML")
                logger.info(f"Welcome: {src} photo + caption for {user_id}")
                return
            except Exception as e:
                logger.warning(f"Welcome: {src} photo+caption failed: {e}")
        try:
            await message.answer_photo(photo)
            photo_sent = True
            logger.info(f"Welcome: {src} photo (no caption) for {user_id}")
        except Exception as e:
            logger.warning(f"Welcome: {src} photo failed: {e}")

    try:
        await message.answer(text, reply_markup=kb, parse_mode="HTML")
    except Exception:
        await message.answer(text, reply_markup=kb)
    logger.info(f"Welcome: text sent for {user_id}")


# --- QW-02 subscription check (observe-only, fail-open) ---

def _membership_status_to_bool(status: str) -> bool:
    """Map a Telegram chat-member status to subscribed/not. Restricted counts as not."""
    return status in ("creator", "administrator", "member")


async def is_subscribed(bot: Bot, channel, user_id: int) -> bool | None:
    """True/False membership; None on any error (bot not admin / unknown channel) — fail-open (D-07)."""
    try:
        member = await bot.get_chat_member(channel, user_id)
        return _membership_status_to_bool(member.status)
    except Exception as e:
        logger.warning(f"Subscription check failed for {user_id} on {channel!r}: {e}")
        return None


# bot is placed BEFORE the defaulted `command` param (a non-default arg after a default is a SyntaxError);
# aiogram injects by name so position is purely a syntax constraint.
@router.message(Command("start"), StateFilter("*"))
async def cmd_start(message: types.Message, state: FSMContext, bot: Bot, command: CommandObject | None = None):
    user_id = message.from_user.id
    logger.info(f"User {user_id} requested /start")

    # QW-02: observe-only subscription check — never blocks the user (D-04), never crashes /start (D-07).
    try:
        channel = await get_setting("contact_tg")
        if channel:
            result = await is_subscribed(bot, channel, user_id)
            if result is not None:
                await set_user_subscribed(user_id, result)
    except Exception as e:
        logger.warning(f"Subscription check skipped for {user_id}: {e}")

    # VERIF-01/02: pre-selection gate (D-13, default off → live flow untouched).
    # Fail-soft like the subscription check above: a glitch never crashes /start.
    try:
        if (await get_setting("preselect_enabled") or "off") == "on":
            from services.allowlist import is_allowed, allowlist_size, _parse_manual_ids
            if allowlist_size() == 0:
                # Owner-confirmed fail-open (Open Q2): admit everyone; the refresh job
                # alerts admins. Do NOT lock out a whole event over a sheet/quota glitch.
                logger.warning(f"Pre-selection ON but allowlist empty — fail-open admit for {user_id}")
            else:
                manual_ids = _parse_manual_ids(await get_setting("preselect_manual_ids"))
                uname = message.from_user.username
                if uname is None and user_id not in manual_ids:
                    prompt = await get_setting("preselect_no_username_text") or (
                        "Чтобы продолжить, задайте @username в настройках Telegram и снова отправьте /start."
                    )
                    await message.answer(html.escape(prompt))
                    return
                if uname is not None and not is_allowed(uname) and user_id not in manual_ids:
                    fail = await get_setting("preselect_fail_text") or "Отбор не пройден."
                    link = await get_setting("preselect_link")
                    text = html.escape(fail)
                    if link:
                        text += "\n" + html.escape(link)
                    await message.answer(text)
                    return
    except Exception as e:
        logger.warning(f"Pre-selection gate skipped for {user_id}: {e}")

    user = await get_user(user_id)
    args = command.args if command else None
    referrer_id = _extract_referrer_id(args, user_id)
    source_tag = _extract_source_tag(args)
    if referrer_id:
        logger.info(f"Deep-link referrer_id={referrer_id} for user {user_id}")

    start_text = await get_setting("start_text") or DEFAULT_START_TEXT
    start_photo = await get_setting("start_photo_file_id")
    logger.info(f"Settings: start_text={start_text[:80]!r}, start_photo={start_photo!r}")

    if user and (user.get("status") or "approved") != "rejected":
        # D-05a: a rejected user falls through to re-register; others see the welcome menu.
        logger.info(f"User {user_id} already registered")
        await _send_welcome(message, start_text, start_photo, await get_main_menu_kb(user_id), user_id)

        if user_id in config.ADMIN_IDS:
            kb = ReplyKeyboardBuilder()
            kb.button(text="\U0001f504 Пройти регистрацию заново")
            await message.answer(
                "Вы админ — можете пройти регистрацию заново для теста.",
                reply_markup=kb.as_markup(resize_keyboard=True, one_time_keyboard=True),
            )
            await state.set_state(Registration.admin_rereg)
        return

    logger.info(f"User {user_id} not registered, showing welcome then registration")
    await _send_welcome(message, start_text, start_photo, None, user_id)
    await _start_registration_flow(message, state, referrer_id=referrer_id, source_tag=source_tag)


@router.message(StateFilter(Registration), F.text.in_({"Отмена", "/cancel"}))
async def cancel_registration(message: types.Message, state: FSMContext):
    await state.clear()
    await message.answer(
        "Регистрация отменена. Чтобы начать заново, отправь /start.",
        reply_markup=ReplyKeyboardRemove(),
    )


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
    await state.update_data(resume_file_id=message.document.file_id)  # file_id only, no download (D-10)
    await _advance("resume", message, state, bot)


@router.message(Registration.resume, F.text)
async def process_resume_text(message: types.Message, state: FSMContext, bot: Bot):
    # Tatiana: резюме можно либо файлом, либо текстом. Text branch stores resume_text.
    text = (message.text or "").strip()
    if text == "Пропустить":
        # Skip without a resume — finish the form, keep all previous answers.
        await _advance("resume", message, state, bot)
        return
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

@router.message(Registration.date_input)
async def process_date_input(message: types.Message, state: FSMContext, bot: Bot):
    raw = (message.text or "").strip()
    try:
        datetime.strptime(raw, "%d.%m.%Y")
    except ValueError:
        await message.answer("Формат даты: ДД.ММ.ГГГГ. Попробуй ещё раз.")
        return
    data = await state.get_data()
    step_key = data.get("_current_date_step", "arrival_date")
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
    await record_user_consent(callback.from_user.id, consent_key)  # D-02 audit row
    await callback.answer("✅ Принято")
    # Consents run before ФИО: walk the consent queue, then ask ФИО.
    data = await state.get_data()
    queue = data.get("_consent_queue", [])
    i = data.get("_consent_i", 0) + 1
    if i < len(queue):
        await state.update_data(_consent_i=i)
        await _ask_step(queue[i], callback.message, state, i + 1, len(queue))
    else:
        await _ask_full_name(callback.message, state)


@router.message(Registration.consent_pending)
async def process_consent_ignore(message: types.Message):
    # SC#2: consent cannot be skipped via text — only the consent button advances.
    btn_text = await get_setting("consent_button_text") or "Согласен(-на)"
    await message.answer(f"Нажми кнопку «{btn_text}» для продолжения.")


# --- Admin re-registration ---

@router.message(Registration.admin_rereg, F.text == "\U0001f504 Пройти регистрацию заново")
async def process_admin_rereg(message: types.Message, state: FSMContext):
    await state.clear()
    await _start_registration_flow(message, state)


@router.message(Registration.admin_rereg)
async def process_admin_rereg_skip(message: types.Message, state: FSMContext):
    await state.clear()
    await message.answer("Ок, остаёмся.", reply_markup=await get_main_menu_kb(message.from_user.id))



# --- Core Registration ---

@router.message(Registration.full_name)
async def process_full_name(message: types.Message, state: FSMContext, bot: Bot):
    full_name = (message.text or "").strip()
    if len(full_name.split()) < 2:
        await message.answer("Укажи ФИО полностью (минимум фамилию и имя).")
        return

    await state.update_data(full_name=full_name)

    mode = await get_setting("registration_mode")
    if mode != "full":
        # WR-03: the short form asks no question steps. Required consents were already
        # collected before ФИО (see _start_registration_flow), so finalize now.
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


# --- Finalize ---

async def send_completion_and_bonus(bot: Bot, telegram_id: int, with_menu: bool = True):
    """Deliver approve_text (post-approval script) + the configured registration bonus.
    Reused by the non-payment approval path, the free/single payment path (handlers.payment),
    and the admin receipt-confirm path (handlers.admin). Fail-soft: a blocked/unknown user
    never raises. `with_menu=False` skips the main-menu keyboard when the caller already sent it."""
    try:
        complete_text = await get_setting("approve_text") or DEFAULT_APPROVE_TEXT
        kwargs = {"parse_mode": "HTML"}
        if with_menu:
            kwargs["reply_markup"] = await get_main_menu_kb(telegram_id)
        await bot.send_message(telegram_id, complete_text, **kwargs)

        if await get_setting("reg_bonus_enabled") == "on":
            bonus_caption = await get_setting("reg_bonus_caption") or "\U0001f381 Бонус за регистрацию!"
            bonus_photo = await get_setting("reg_bonus_photo_file_id")
            bonus_doc = await get_setting("reg_bonus_doc_file_id")
            if bonus_doc:
                await bot.send_document(telegram_id, bonus_doc, caption=bonus_caption, parse_mode="HTML")
            elif bonus_photo:
                await bot.send_photo(telegram_id, bonus_photo, caption=bonus_caption, parse_mode="HTML")
    except Exception as e:
        logger.error(f"Failed to send completion/bonus to {telegram_id}: {e}")


async def approve_user(bot: Bot, telegram_id: int):
    """Send the post-approval welcome (complete text + main menu + bonus) to a user
    by chat id. Reused by the auto-approve path here and the manager manual-approve
    path (admin.py). Fail-soft: a blocked/unknown user never raises."""
    try:
        # Phase 4 (D-09): payment module gates the welcome. When ON, the payment flow owns
        # all messaging (its own option/requisites/receipt path); the completion text + bonus
        # land after the manager confirms the receipt (admin rcpt_confirm) or immediately for
        # a free/single option. When OFF, behaviour is byte-identical to before.
        if await _is_module_enabled("payment_enabled"):
            from handlers.payment import start_payment_step  # local import avoids circular
            await start_payment_step(bot, telegram_id)
            return

        await send_completion_and_bonus(bot, telegram_id)
    except Exception as e:
        logger.error(f"Failed to send approval welcome to {telegram_id}: {e}")


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
    data.setdefault("resume_file_id", None)
    data.setdefault("resume_text", None)

    await add_user(data)

    # SCHED-02: registration finished — drop the dropout row (fail-soft).
    try:
        await clear_reg_started(message.from_user.id)
    except Exception as e:
        logger.error(f"Failed to clear reg_started for {message.from_user.id}: {e}")

    # Fire-and-forget the Google Sheet write so the user is NOT blocked on a ~5s network
    # round-trip (auth + open + append, plus up to 3 retries with sleeps). append_to_sheet
    # is fail-soft and logs its own errors. Build the row inline (needs current settings),
    # then hand the network I/O to the background.
    try:
        _sheet_row = await active_sheet_row(data)
        asyncio.create_task(append_to_sheet(_sheet_row))
    except Exception as e:
        logger.error(f"Failed to schedule sheet append for {message.from_user.id}: {e}")

    # Phase 2 (D-01..D-04): decide approval status from form type + per-form setting, persist it.
    reg_mode = await get_setting("registration_mode") or "short"
    full_setting = await get_setting("full_approval") or "manual"
    short_setting = await get_setting("short_approval") or "auto"
    status = _decide_status(reg_mode, full_setting, short_setting)
    try:
        await set_user_status(message.from_user.id, status)
    except Exception as e:
        logger.error(f"Failed to set status for {message.from_user.id}: {e}")

    # Admin notify: always for approved; for pending only when pending_notify_mode='instant' (D-15).
    notify_admins = status == "approved" or (
        status == "pending" and (await get_setting("pending_notify_mode") or "batched") == "instant"
    )
    if config.ADMIN_IDS and notify_admins:
        safe_name = html.escape(str(data.get("full_name", "-")))
        safe_username = html.escape(str(data.get("username", "-")))
        admin_text = (
            f"\U0001f195 <b>Новая регистрация!</b>\n"
            f"\U0001f464 {safe_name} ({safe_username})"
        )
        if status == "pending":
            admin_text += "\n⏳ Ожидает одобрения (/admin → Заявки)"
        if data.get("local_committee") and data["local_committee"] != "-":
            admin_text += f"\n\U0001f3e2 {html.escape(str(data['local_committee']))}"
        if data.get("position") and data["position"] != "-":
            admin_text += f"\n\U0001f454 {html.escape(str(data['position']))}"
        if data.get("age"):
            admin_text += f"\n\U0001f382 {data['age']}"
        safe_source = html.escape(str(data.get("source", "-")))
        if safe_source != "-":
            admin_text += f"\n\U0001f4dd {safe_source}"

        for admin_id in config.ADMIN_IDS:
            try:
                await bot.send_message(admin_id, admin_text, parse_mode="HTML")
            except Exception as e:
                logger.error(f"Failed to notify admin {admin_id}: {e}")

    await state.clear()
    # Tatiana: «поздравляем»-скрипт приходит сразу после регистрации — всем (и pending, и
    # approved). Approve/reject досылают свои отдельные скрипты позже.
    submitted = await get_setting("reg_complete_text") or DEFAULT_REG_COMPLETE_TEXT
    try:
        await message.answer(submitted, reply_markup=ReplyKeyboardRemove(), parse_mode="HTML")
    except Exception:
        await message.answer(submitted, reply_markup=ReplyKeyboardRemove())
    if status == "approved":
        await approve_user(bot, message.from_user.id)
