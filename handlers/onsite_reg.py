"""Регистрация на месте (FORUM-CHECKIN.md D-41), чатовая часть: короткая анкета по ссылке
`?start=walkin_<город>`.

Человек стоит у стойки проблемных случаев, волонтёр показывает ему QR ссылки (экран менеджера
«📝 Регистрация на месте», handlers/admin_onsite_reg.py). Бот задаёт три вопроса — ФИО, телефон,
вуз (можно пропустить) — и заводит строку `users` pending с `onsite_kind='walkin'`
(`database.db.create_onsite_user`). QR и уведомлений менеджерам здесь НЕТ (D-02): решение о
входе принимает волонтёр у стойки, QR придёт после одобрения (`services.onsite_reg.
after_onsite_approved`).

Существующую анкету ссылка не трогает никогда: одобренный текущего сезона получает подсказку
«покажи 🎟 Мой QR», остальные — «подойди к стойке, волонтёр найдёт по фамилии».

Все тексты — из реестра (group "reg"), с переводом на язык человека (D-34). В лог не пишутся ни
телефон, ни ФИО (T-wt3-09).

Роутер подключается в main.py сразу перед `registration.router`: FSM-состояния `OnsiteReg`
должны забираться раньше catch-all хендлеров регистрации. Вход — `start_walkin` из
`registration.cmd_start` (ленивый импорт, перехват сразу после ветки `vol_`)."""
from __future__ import annotations

import html
import logging
import re

from aiogram import F, Router, types
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    ReplyKeyboardMarkup,
    ReplyKeyboardRemove,
)

from cities import (
    cities_module_on,
    default_city_code,
    enabled_cities,
    get_setting_typed_for_city,
    normalize_city,
)
from database.db import create_onsite_user, get_user, record_user_consent
from handlers import reg_i18n
from reg_engine import is_past_season_row, validate_answer
from services.onsite_reg import onsite_enabled, parse_walkin_arg
from settings_schema import get_setting_typed

logger = logging.getLogger(__name__)

router = Router()

_CONSENT_KEY = "onsite"
_MIN_PHONE_DIGITS = 7
# Слово ФИО — буквы (любого алфавита), допускаются дефис и апостроф внутри: «Мария-Анна», «О'Нил».
_NAME_WORD_RE = re.compile(r"^[^\W\d_]+(?:[-'’][^\W\d_]+)*$")


class OnsiteReg(StatesGroup):
    consent = State()
    name = State()
    phone = State()
    university = State()


async def _say(message, key: str, **kwargs):
    return await reg_i18n.say(message, await get_setting_typed(key), **kwargs)


async def _resolve_city(city_code: str | None) -> tuple[bool, str | None]:
    """(ссылка годится?, город). Модуль городов выключен — бот одногородский, город —
    `default_city_code()`. Включён — код из ссылки должен быть среди включённых городов;
    неизвестный — ссылка не наша, /start идёт обычным путём."""
    if not await cities_module_on():
        return True, default_city_code()
    if city_code is None:
        return True, default_city_code()
    codes = {c["code"] for c in await enabled_cities()}
    if city_code not in codes:
        return False, None
    return True, normalize_city(city_code)


async def try_walkin_start(message: types.Message, state: FSMContext, args: str | None) -> bool:
    """Перехват `cmd_start` (сразу после ветки `vol_`): до вопроса о языке, funnel-лога и
    предотбора — человек стоит у стойки, отсев по таблице его резать не должен. Не walk-in
    ссылка или неизвестный город — False, /start идёт обычным путём."""
    is_walkin, city_code = parse_walkin_arg(args)
    if not is_walkin:
        return False
    return await start_walkin(message, state, city_code)


async def start_walkin(message: types.Message, state: FSMContext, city_code: str | None) -> bool:
    """Вход из `cmd_start`. True — /start дальше не идёт (ответили здесь)."""
    ok, city = await _resolve_city(city_code)
    if not ok:
        return False
    uid = message.from_user.id
    if not await onsite_enabled(city):
        await state.clear()
        await _say(message, "onsite_reg_closed_text")
        return True

    user = await get_user(uid)
    if user:
        # D-41: анкету не трогаем никогда — решение по существующей заявке принимает стойка.
        await state.clear()
        event_season = await get_setting_typed("event_season") or None
        if user.get("status") == "approved" and not is_past_season_row(user, event_season):
            await _say(message, "onsite_reg_already_approved_text")
        else:
            await _say(message, "onsite_reg_existing_text")
        return True

    await state.clear()
    await state.set_state(OnsiteReg.consent)
    await state.update_data(onsite_city=city)
    button = await get_setting_typed("onsite_reg_consent_button_text")
    kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text=button, callback_data="onsite_consent"),
    ]])
    await _say(message, "onsite_reg_intro_text", reply_markup=kb)
    logger.info("onsite_reg: walk-in анкета начата (tid=%s, city=%s)", uid, city)
    return True


# ── Шаг 0: согласие ────────────────────────────────────────────────────────────────────────

@router.callback_query(OnsiteReg.consent, F.data == "onsite_consent")
async def onsite_consent(callback: types.CallbackQuery, state: FSMContext):
    raw_button = await get_setting_typed("onsite_reg_consent_button_text")
    try:
        await record_user_consent(callback.from_user.id, _CONSENT_KEY, raw_button=raw_button)
    except Exception:
        logger.exception("onsite_reg: согласие не записано (tid=%s)", callback.from_user.id)
    await state.set_state(OnsiteReg.name)
    await _say(callback.message, "onsite_reg_name_prompt_text")
    await callback.answer()


@router.message(OnsiteReg.consent)
async def onsite_consent_repeat(message: types.Message, state: FSMContext):
    """Человек пишет текстом вместо кнопки — показать приветствие с кнопкой ещё раз."""
    button = await get_setting_typed("onsite_reg_consent_button_text")
    kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text=button, callback_data="onsite_consent"),
    ]])
    await _say(message, "onsite_reg_intro_text", reply_markup=kb)


# ── Шаг 1: ФИО ─────────────────────────────────────────────────────────────────────────────

def _valid_name(raw: str | None) -> str | None:
    value, error = validate_answer("full_name", raw or "")
    if error is not None or not isinstance(value, str):
        return None
    words = value.split()
    if not all(_NAME_WORD_RE.match(w) for w in words):
        return None
    return " ".join(words)


async def _phone_kb(city: str | None) -> ReplyKeyboardMarkup:
    share = await get_setting_typed_for_city("reg_phone_share_button_text", city)
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text=share, request_contact=True)]],
        resize_keyboard=True, one_time_keyboard=True,
    )


@router.message(OnsiteReg.name)
async def onsite_name(message: types.Message, state: FSMContext):
    name = _valid_name(message.text)
    if name is None:
        await _say(message, "onsite_reg_bad_name_text")
        return
    await state.update_data(onsite_name=name)
    await state.set_state(OnsiteReg.phone)
    city = (await state.get_data()).get("onsite_city")
    await _say(message, "onsite_reg_phone_prompt_text", reply_markup=await _phone_kb(city))


# ── Шаг 2: телефон ─────────────────────────────────────────────────────────────────────────

def _valid_phone(raw: str | None) -> str | None:
    value, error = validate_answer("phone", raw or "")
    if error is not None or not isinstance(value, str) or value == "-":
        return None
    if sum(ch.isdigit() for ch in value) < _MIN_PHONE_DIGITS:
        return None
    return value


def _university_kb(skip_text: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text=skip_text, callback_data="onsite_skip"),
    ]])


async def _ask_university(message: types.Message, state: FSMContext, phone: str) -> None:
    await state.update_data(onsite_phone=phone)
    await state.set_state(OnsiteReg.university)
    # Кнопка контакта одноразовая (one_time_keyboard) и прячется сама; окончательно
    # клавиатуру снимает ReplyKeyboardRemove финального сообщения. Здесь — inline «Пропустить».
    skip = await get_setting_typed("onsite_reg_skip_button_text")
    await _say(message, "onsite_reg_university_prompt_text", reply_markup=_university_kb(skip))


@router.message(OnsiteReg.phone, F.contact)
async def onsite_phone_contact(message: types.Message, state: FSMContext):
    phone = message.contact.phone_number or ""
    if not phone.startswith("+"):
        phone = f"+{phone}"
    await _ask_university(message, state, phone)


@router.message(OnsiteReg.phone)
async def onsite_phone_text(message: types.Message, state: FSMContext):
    phone = _valid_phone(message.text)
    if phone is None:
        city = (await state.get_data()).get("onsite_city")
        await _say(message, "onsite_reg_bad_phone_text", reply_markup=await _phone_kb(city))
        return
    await _ask_university(message, state, phone.strip())


# ── Шаг 3: вуз (можно пропустить) и финал ──────────────────────────────────────────────────

async def _finish(message: types.Message, state: FSMContext, uid: int, username: str | None,
                  university: str | None) -> None:
    data = await state.get_data()
    await state.clear()
    name = data.get("onsite_name") or ""
    city = data.get("onsite_city")
    season = await get_setting_typed("event_season") or None
    created = await create_onsite_user(
        uid, username, name, data.get("onsite_phone"), university, city, season,
    )
    if not created:
        logger.info("onsite_reg: строка users уже есть, walk-in не создан (tid=%s)", uid)
        await _say(message, "onsite_reg_existing_text", reply_markup=ReplyKeyboardRemove())
        return
    logger.info("onsite_reg: walk-in анкета заполнена (tid=%s, city=%s)", uid, city)
    lang, tr_map = await reg_i18n.ctx_for(message)
    template = await get_setting_typed("onsite_reg_done_text")
    text = reg_i18n.tr_fmt(template, lang, tr_map, name=html.escape(name))
    from handlers import registration

    await registration._safe_answer(message, text, reply_markup=ReplyKeyboardRemove())


@router.message(OnsiteReg.university)
async def onsite_university(message: types.Message, state: FSMContext):
    text = (message.text or "").strip()
    skip = await get_setting_typed("onsite_reg_skip_button_text")
    if not text:
        await _say(message, "onsite_reg_university_prompt_text", reply_markup=_university_kb(skip))
        return
    university = None if text == skip else text
    await _finish(message, state, message.from_user.id, message.from_user.username, university)


@router.callback_query(OnsiteReg.university, F.data == "onsite_skip")
async def onsite_skip(callback: types.CallbackQuery, state: FSMContext):
    await callback.answer()
    await _finish(callback.message, state, callback.from_user.id, callback.from_user.username, None)
