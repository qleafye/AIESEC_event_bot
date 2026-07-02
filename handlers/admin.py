import csv
import html as html_module
import io
import asyncio
import json
import logging
import os
import re
from datetime import datetime
from aiogram import Router, F, types, Bot
from aiogram.filters import Command, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.types import BufferedInputFile, InlineKeyboardMarkup, InlineKeyboardButton, ReplyKeyboardRemove
from config import config
from database.db import (
    get_stats,
    get_all_users_ids,
    get_all_users_dicts,
    export_users_csv,
    get_user,
    get_user_by_username,
    get_monthly_registration_stats,
    get_source_stats,
    get_setting,
    set_setting,
    delete_setting,
    add_coins,
    get_balance,
    get_non_subscriber_ids,
    get_incomplete_user_ids,
    get_pending_users,
    get_pending_count,
    approve_user_atomic,
    reject_user,
    approve_all_pending,
    create_scheduled_broadcast,
    list_pending_broadcasts,
    cancel_scheduled_broadcast,
    count_and_list_filtered,
    get_receipt_pending_users,
    get_receipt_pending_count,
    update_payment_status,
)
from aiogram.exceptions import TelegramRetryAfter
from services.sheets import get_existing_sheet_ids, append_rows_to_sheet, ensure_sheet_header
from services.scheduler import (
    _parse_schedule_dt,
    _fmt_dt,
    schedule_broadcast_job,
    cancel_broadcast_job,
)
from services.allowlist import refresh_allowlist, allowlist_size
from handlers.states import Broadcast, EditSetting, Approval, ReceiptReview
from keyboards.builders import get_cancel_kb, MENU_BUTTONS, get_main_menu_kb
from handlers.registration import REG_FLOW, REG_DEFAULTS, REG_LABELS, SHEET_HEADERS, _build_sheet_row, approve_user

router = Router()
logger = logging.getLogger(__name__)

pending_albums = {}

MONTH_NAMES = {
    "01": "Январь",
    "02": "Февраль",
    "03": "Март",
    "04": "Апрель",
    "05": "Май",
    "06": "Июнь",
    "07": "Июль",
    "08": "Август",
    "09": "Сентябрь",
    "10": "Октябрь",
    "11": "Ноябрь",
    "12": "Декабрь",
}

def is_admin(message: types.Message):
    return message.from_user.id in config.ADMIN_IDS


def _parse_coins_amount(token: str) -> int | None:
    """Parse a signed coin amount like '+10', '-3', '10'. None on failure."""
    if not token:
        return None
    token = token.strip()
    body = token[1:] if token[0] in "+-" else token
    if not body.isdigit():
        return None
    value = int(body)
    return -value if token[0] == "-" else value


def build_admin_keyboard():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📊 Статистика регистраций", callback_data="admin_stats")],
        [InlineKeyboardButton(text="🗓 Регистрации по месяцам", callback_data="admin_monthly_stats")],
        [InlineKeyboardButton(text="📈 Источники", callback_data="admin_source_stats")],
        [InlineKeyboardButton(text="📄 Экспорт CSV", callback_data="admin_export_csv")],
        [InlineKeyboardButton(text="📋 Заявки", callback_data="admin_applications")],
        [InlineKeyboardButton(text="🧾 Чеки", callback_data="admin_receipts")],
        [InlineKeyboardButton(text="📢 Рассылка", callback_data="admin_broadcast")],
        [InlineKeyboardButton(text="🔄 Синхронизация таблицы", callback_data="admin_sync_sheet")],
        [InlineKeyboardButton(text="⚙️ Настройки форума", callback_data="admin_settings")],
        [InlineKeyboardButton(text="📖 Справка по настройкам", callback_data="admin_settings_guide")],
    ])


async def render_monthly_stats() -> str:
    rows = await get_monthly_registration_stats()
    if not rows:
        return "📅 <b>Регистрации по месяцам</b>\n\nПока нет ни одной регистрации."

    lines = ["📅 <b>Регистрации по месяцам</b>", ""]
    for month, count in rows:
        if not month or len(month) != 7:
            label = month or "Неизвестно"
        else:
            year, month_num = month.split("-")
            month_name = MONTH_NAMES.get(month_num, month_num)
            label = f"{month_name} {year}"
        lines.append(f"• {label}: {count}")

    return "\n".join(lines)

@router.message(Command("admin"), is_admin)
async def cmd_admin_help(message: types.Message):
    text = (
        "👮‍♂️ <b>Панель администратора</b>\n\n"
        "/stats - Статистика регистраций\n"
        "/stats_monthly - Регистрации по месяцам\n"
        "/create_link &lt;название&gt; - Создать ссылку с меткой\n"
        "/export - Скачать базу пользователей (CSV)\n"
        "/broadcast - Рассылка сообщения всем\n"
        "/find @username - Найти пользователя по юзернейму\n"
        "/coins @username +N [причина] - Начислить/списать монеты\n"
        "/scheduled - Запланированные рассылки\n"
        "/refresh_allowlist - Обновить список отобранных\n"
        "/settings_guide - 📖 Справка по всем настройкам бота"
    )
    await message.answer(text, parse_mode="HTML", reply_markup=build_admin_keyboard())


@router.message(Command("coins"), is_admin)
async def cmd_coins(message: types.Message, bot: Bot):
    args = (message.text or "").split(maxsplit=3)
    hint = "⚠️ Формат: /coins @username +N [причина]"
    if len(args) < 3:
        await message.answer(hint)
        return

    user = await get_user_by_username(args[1])
    if not user:
        await message.answer(f"❌ Пользователь {html_module.escape(args[1])} не найден.")
        return

    amount = _parse_coins_amount(args[2])
    if amount is None:
        await message.answer(hint)
        return

    reason = args[3] if len(args) > 3 else None  # optional free text (D-13)
    await add_coins(user["telegram_id"], amount, reason=reason, changed_by=message.from_user.id)
    balance = await get_balance(user["telegram_id"])

    safe_username = html_module.escape(str(user.get("username") or args[1]))
    sign = "начислено" if amount >= 0 else "списано"
    await message.answer(
        f"🪙 {sign} {abs(amount)} монет(ы) для {safe_username}.\n"
        f"Новый баланс: <b>{balance}</b>.",
        parse_mode="HTML",
    )

@router.message(Command("find"), is_admin)
async def cmd_find_user(message: types.Message):
    args = message.text.split()
    if len(args) < 2:
        await message.answer("⚠️ Используйте формат: /find @username")
        return

    username = args[1]
    user = await get_user_by_username(username)

    if user:
        text = (
            f"👤 <b>Пользователь найден:</b>\n"
            f"ID: <code>{user['telegram_id']}</code>\n"
            f"Имя: {user['full_name']}\n"
            f"Username: {user['username']}\n"
            f"Email: {user['email']}\n"
            f"Регистрация: {user['registration_date']}"
        )
        await message.answer(text, parse_mode="HTML")
    else:
        await message.answer(f"❌ Пользователь {username} не найден в базе данных.")


@router.message(Command("create_link"), is_admin)
async def cmd_create_link(message: types.Message, bot: Bot):
    args = message.text.split(maxsplit=1)
    if len(args) < 2 or not args[1].strip():
        await message.answer("⚠️ Используйте формат: /create_link &lt;название&gt;\nПример: /create_link vk_poster", parse_mode="HTML")
        return

    tag = args[1].strip()
    bot_user = await bot.get_me()
    link = f"https://t.me/{bot_user.username}?start=src_{tag}"
    await message.answer(
        f"🔗 Ссылка с меткой <b>{tag}</b>:\n\n"
        f"<code>{link}</code>\n\n"
        f"Регистрации по этой ссылке появятся в разделе «📈 Источники».",
        parse_mode="HTML",
    )



def is_question_reply(message: types.Message) -> bool:
    if message.from_user.id not in config.ADMIN_IDS:
        return False
    replied = message.reply_to_message
    if not replied or not replied.text:
        return False
    return "🆔" in replied.text and "❓" in replied.text


@router.message(is_question_reply)
async def admin_reply_to_question(message: types.Message, bot: Bot):
    replied = message.reply_to_message
    match = re.search(r"🆔\s*(\d+)", replied.text)
    if not match:
        return

    user_id = int(match.group(1))

    try:
        if message.text:
            reply_text = f"💬 <b>Ответ от организаторов:</b>\n\n{message.html_text}"
            await bot.send_message(user_id, reply_text, parse_mode="HTML")
        else:
            await bot.send_message(
                user_id, "💬 <b>Ответ от организаторов:</b>", parse_mode="HTML"
            )
            await message.send_copy(user_id)
        await message.reply("✅ Ответ отправлен пользователю.")

        admin_name = message.from_user.full_name or message.from_user.username or "Админ"
        for other_admin_id in config.ADMIN_IDS:
            if other_admin_id == message.from_user.id:
                continue
            try:
                await bot.send_message(
                    other_admin_id,
                    f"✅ {admin_name} ответил(а) на вопрос от пользователя <code>{user_id}</code>.",
                    parse_mode="HTML",
                )
            except Exception:
                pass
    except Exception as e:
        logger.error(f"Failed to send reply to user {user_id}: {e}")
        await message.reply("❌ Не удалось отправить ответ пользователю.")


@router.message(Command("stats"), is_admin)
async def cmd_stats(message: types.Message):
    total, top_unis = await get_stats()

    text = (
        f"📊 <b>Статистика:</b>\n"
        f"Всего регистраций: {total}\n"
        f"🏆 <b>Топ-3 ВУЗа:</b>\n"
    )

    for i, (uni, count) in enumerate(top_unis, 1):
        text += f"{i}. {uni} — {count}\n"

    await message.answer(text, parse_mode="HTML")


@router.message(Command("stats_monthly"), is_admin)
async def cmd_stats_monthly(message: types.Message):
    await message.answer(await render_monthly_stats(), parse_mode="HTML")


@router.callback_query(F.data == "admin_stats")
async def show_admin_stats(callback: types.CallbackQuery):
    if callback.from_user.id not in config.ADMIN_IDS:
        await callback.answer("Недостаточно прав", show_alert=True)
        return

    total, top_unis = await get_stats()
    text = (
        f"📊 <b>Статистика:</b>\n"
        f"Всего регистраций: {total}\n"
        f"🏆 <b>Топ-3 ВУЗа:</b>\n"
    )

    for i, (uni, count) in enumerate(top_unis, 1):
        text += f"{i}. {uni} — {count}\n"

    await callback.message.edit_text(text, parse_mode="HTML", reply_markup=build_admin_keyboard())
    await callback.answer()


@router.callback_query(F.data == "admin_monthly_stats")
async def show_admin_monthly_stats(callback: types.CallbackQuery):
    if callback.from_user.id not in config.ADMIN_IDS:
        await callback.answer("Недостаточно прав", show_alert=True)
        return

    await callback.message.edit_text(await render_monthly_stats(), parse_mode="HTML", reply_markup=build_admin_keyboard())
    await callback.answer()


@router.callback_query(F.data == "admin_source_stats")
async def show_admin_source_stats(callback: types.CallbackQuery):
    if callback.from_user.id not in config.ADMIN_IDS:
        await callback.answer("Недостаточно прав", show_alert=True)
        return

    rows = await get_source_stats()
    if not rows:
        text = "📈 <b>Источники регистраций</b>\n\nПока нет данных."
    else:
        lines = ["📈 <b>Источники регистраций</b>", ""]
        for source, count in rows:
            lines.append(f"• {source} — {count}")
        text = "\n".join(lines)

    await callback.message.edit_text(text, parse_mode="HTML", reply_markup=build_admin_keyboard())
    await callback.answer()


# --- Settings ---

SETTINGS_FIELDS = [
    ("event_date", "🗓 Дата", "Введите дату форума"),
    ("event_time", "⌚ Время", "Введите время проведения"),
    ("event_place_name", "📍 Место", "Введите название площадки"),
    ("event_place_address", "📫 Адрес", "Введите адрес площадки"),
    ("contact_person", "👤 Контакт", "Введите юзернейм контактного лица (например @username)"),
    ("contact_vk", "🔵 VK", "Введите ссылку на группу ВК"),
    ("contact_tg", "🔹 TG", "Введите ссылку на Telegram-канал"),
    ("start_text", "💬 Приветствие", "Введите текст приветствия при /start (поддерживается HTML-разметка)"),
    ("event_name", "🎪 Название меро", "Название мероприятия в родительном падеже — подставляется в вопрос об ожиданиях (например: «конференции RusCo», «форума YouLead», «Годового отчёта»)"),
    ("source_options", "📢 Источники", "Отправьте варианты источников, каждый с новой строки"),
    ("reg_complete_text", "✅ После регистрации", "Текст, который участник увидит СРАЗУ после отправки анкеты (например «Поздравляем, заявка принята! Рассмотрим за 2-3 дня»). Поддерживается HTML."),
    ("approve_text", "🎉 После одобрения", "Отдельный текст, который участник увидит, когда менеджер ОДОБРИТ заявку. Поддерживается HTML."),
    ("consent_button_text", "✅ Текст кнопки согласия", "Надпись на кнопке согласия (по умолчанию «Согласен(-на)»)."),
    ("pending_reminder_interval", "🕒 Тайминг батчей заявок", "Как часто бот присылает админам сводку «Заявок в ожидании: N» (режим «Пачкой»).\n\nВ СЕКУНДАХ. Примеры:\n900 = 15 мин\n1800 = 30 мин (по умолчанию)\n3600 = 1 час\n\nМеняется на лету, перезапуск не нужен."),
    # Phase 4: event modularity + consent + payment config (all default empty/off → live flow unchanged)
    ("event_type", "🎭 Тип события", "Напишите одно слово: forum (форум) / conference (конференция) / custom (вручную).\n\nДля forum и conference бот сам включит/выключит модули оплаты и согласий — потом можно поправить кнопками выше."),
    ("consent_list", "📋 Список согласий", "Согласия, которые участник примет в конце анкеты.\n\nКаждое согласие — отдельной строкой в формате:\nВидимое название | короткий_ключ_латиницей\n\nКлюч нужен, чтобы привязать к согласию PDF. Пример (две строки):\nСогласие на обработку данных|data\nПолитика конфиденциальности|policy\n\nПосле сохранения загрузите PDF в разделе «🧾 PDF согласий»."),
    ("payment_options", "💳 Варианты оплаты", "Варианты участия (билеты/тарифы), каждый — отдельной строкой:\nНазвание | Цена\n\nПример:\nПолный билет|5000\nСтудент|3000\n\nЦена 0 = бесплатно. Если вариант один — участник его не выбирает, сразу видит реквизиты."),
    ("payment_requisites", "💰 Реквизиты оплаты", "Общие реквизиты: банк, номер карты, ФИО получателя. Показываются, если для ЛК участника не задана своя карта (см. «💳 Реквизиты по ЛК»). Обычный текст."),
    ("payment_requisites_by_lc", "💳 Реквизиты по ЛК", "Своя карта для каждого ЛК — каждый комитет собирает на свои реквизиты.\n\nКаждый ЛК — отдельной строкой в формате:\nНазвание ЛК | реквизиты\n\nНазвание ЛК должно совпадать с кнопкой в вопросе про ЛК (EG, SPUEF, Moscow, Tyumen, Ufa, Ekaterinburg).\n\nПример:\nMoscow | Сбер 1234 5678 9012 3456, Иван И.\nSPUEF | Тинькофф 9876 5432, Пётр П.\n\nЕсли ЛК участника нет в списке — покажутся общие «💰 Реквизиты оплаты»."),
    ("payment_deadline", "📅 Дедлайн оплаты", "Крайний срок оплаты в формате ДД.ММ.ГГГГ ЧЧ:ММ.\n\nПример: 15.08.2026 23:59\n\nПо этому сроку бот сам пришлёт участнику напоминания за 3 дня и за 1 день."),
    ("penalty_schedule", "⚠️ Штрафы за отмену", "Необязательно. Каждая строка: дата | сумма возврата/остатка.\n\nПример:\n01.08.2026|3000\n10.08.2026|0\n\nОставьте пустым (отправьте «-»), если штрафов нет."),
    # YL'26: configurable option lists (0 хардкода — всё правит админ)
    ("city_options", "🏙 Города (варианты)", "Города-кнопки для вопроса «Город». Каждый город — на отдельной строке.\n\nКнопка «Другое» добавится сама. Оставьте пустым — будет стандартный список."),
    ("study_field_options", "🎯 Направления обучения (варианты)", "Варианты-кнопки для вопроса «Направление обучения». Каждый — на отдельной строке.\n\nПусто = стандартный список."),
    ("goal_options", "🎯 Цель участия (варианты)", "Варианты для вопроса «Цель участия» — участник сможет выбрать несколько. Каждый — на отдельной строке.\n\nПусто = стандартный список."),
    ("formats_options", "📋 Форматы форума (варианты)", "Варианты для вопроса «Форматы форума» — выбор нескольких. Каждый — на отдельной строке.\n\nПусто = стандартный список."),
    ("university_options", "🏫 Список ВУЗов", "ВУЗы-кнопки для режима «выбор из базы». Каждый ВУЗ — на отдельной строке.\n\nКнопка «Другое» добавится сама. Пусто = встроенный список."),
    # NOTE: reg_university_mode и edu_conditional вынесены в кнопки-переключатели (build_settings_keyboard).
    # PDF согласий грузятся в разделе «🧾 PDF согласий».
]

PHOTO_FIELDS = [
    ("program", "📅 Программа", "Отправьте фото программы (можно с подписью)."),
    ("speakers", "🗣 Спикеры", "Отправьте одно фото со всеми спикерами (можно с подписью)."),
    ("start", "💬 Фото приветствия", "Отправьте фото для приветственного сообщения (/start)."),
    ("venue", "🏢 Площадка", "Отправьте фото площадки (можно с подписью)."),
]

FILE_FIELDS = [
    ("reg_bonus", "🎁 Бонус за регистрацию", "Отправьте файл или фото бонуса (можно с подписью)."),
]


async def render_settings_text() -> str:
    lines = ["⚙️ <b>Настройки форума</b>", ""]

    reg_mode = await get_setting("registration_mode") or "short"
    mode_label = "📋 Полная" if reg_mode == "full" else "⚡ Краткая"
    lines.append(f"📝 Форма регистрации: <b>{mode_label}</b>")

    bonus_enabled = await get_setting("reg_bonus_enabled") or "off"
    bonus_label = "✅ Вкл" if bonus_enabled == "on" else "❌ Выкл"
    lines.append(f"🎁 Бонус за регистрацию: <b>{bonus_label}</b>")

    full_appr = await get_setting("full_approval") or "manual"
    short_appr = await get_setting("short_approval") or "auto"
    notify_mode = await get_setting("pending_notify_mode") or "batched"
    appr_lbl = lambda v: "👮 Ручная" if v == "manual" else "⚡ Авто"
    lines.append(f"✅ Модерация полной формы: <b>{appr_lbl(full_appr)}</b>")
    lines.append(f"✅ Модерация краткой формы: <b>{appr_lbl(short_appr)}</b>")
    notify_lbl = "📨 Сразу" if notify_mode == "instant" else "🕒 Пачкой (напоминалка)"
    lines.append(f"🔔 Уведомление о заявке: <b>{notify_lbl}</b>")

    payment_enabled = await get_setting("payment_enabled") or "off"
    consent_enabled = await get_setting("consent_enabled") or "off"
    lines.append(f"💳 Модуль оплаты: <b>{'✅ Вкл' if payment_enabled == 'on' else '❌ Выкл'}</b>")
    lines.append(f"📋 Модуль согласий: <b>{'✅ Вкл' if consent_enabled == 'on' else '❌ Выкл'}</b>")

    enabled_q = 0
    for _, sk, *_rest in REG_FLOW:
        v = await get_setting(sk)
        is_on = (v == "on") if v is not None else (REG_DEFAULTS.get(sk, "on") == "on")
        if is_on:
            enabled_q += 1
    lines.append(f"📋 Вопросы: <b>{enabled_q} из {len(REG_FLOW)}</b> включено")

    enabled_m = 0
    for key, _ in MENU_BUTTONS:
        v = await get_setting(key)
        if (v == "on") if v is not None else True:
            enabled_m += 1
    lines.append(f"🔘 Меню: <b>{enabled_m} из {len(MENU_BUTTONS)}</b> кнопок")
    lines.append("")

    for key, label, _ in SETTINGS_FIELDS:
        value = await get_setting(key)
        if not value:
            status = "<i>не указано</i>"
        else:
            escaped = html_module.escape(value)
            if len(value) > 60:
                status = html_module.escape(value[:60]) + "…"
            else:
                status = escaped
        lines.append(f"{label}: {status}")

    lines.append("")
    for prefix, label, _ in PHOTO_FIELDS:
        photo = await get_setting(f"{prefix}_photo_file_id")
        lines.append(f"{label}: {'✅ загружена' if photo else '<i>не загружена</i>'}")

    for prefix, label, _ in FILE_FIELDS:
        photo = await get_setting(f"{prefix}_photo_file_id")
        doc = await get_setting(f"{prefix}_doc_file_id")
        if photo or doc:
            lines.append(f"{label}: ✅ загружен")
        else:
            lines.append(f"{label}: <i>не загружен</i>")

    lines.append("")
    lines.append("<i>Отправьте «-» при редактировании текстовых полей, чтобы скрыть.</i>")
    return "\n".join(lines)


async def build_settings_keyboard():
    reg_mode = await get_setting("registration_mode") or "short"
    toggle_text = "📝 Регистрация: ⚡ Краткая → 📋 Полная" if reg_mode == "short" else "📝 Регистрация: 📋 Полная → ⚡ Краткая"

    bonus_enabled = await get_setting("reg_bonus_enabled") or "off"
    bonus_toggle_text = "🎁 Бонус: ❌ Выкл → ✅ Вкл" if bonus_enabled == "off" else "🎁 Бонус: ✅ Вкл → ❌ Выкл"

    full_appr = await get_setting("full_approval") or "manual"
    short_appr = await get_setting("short_approval") or "auto"
    notify_mode = await get_setting("pending_notify_mode") or "batched"
    full_txt = "✅ Полная форма: 👮 Ручная → ⚡ Авто" if full_appr == "manual" else "✅ Полная форма: ⚡ Авто → 👮 Ручная"
    short_txt = "✅ Краткая форма: 👮 Ручная → ⚡ Авто" if short_appr == "manual" else "✅ Краткая форма: ⚡ Авто → 👮 Ручная"
    notify_txt = "🔔 Уведомление: 📨 Сразу → 🕒 Пачкой" if notify_mode == "instant" else "🔔 Уведомление: 🕒 Пачкой → 📨 Сразу"

    payment_enabled = await get_setting("payment_enabled") or "off"
    consent_enabled = await get_setting("consent_enabled") or "off"
    payment_toggle_text = "💳 Оплата: ❌ Выкл → ✅ Вкл" if payment_enabled != "on" else "💳 Оплата: ✅ Вкл → ❌ Выкл"
    consent_toggle_text = "📋 Согласия: ❌ Выкл → ✅ Вкл" if consent_enabled != "on" else "📋 Согласия: ✅ Вкл → ❌ Выкл"

    uni_mode = await get_setting("reg_university_mode") or "text"
    uni_mode_text = ("🏫 ВУЗ: выбор из списка → свободный ввод" if uni_mode == "list"
                     else "🏫 ВУЗ: свободный ввод → выбор из списка")
    edu_cond = await get_setting("edu_conditional") or "on"
    edu_cond_text = ("🎓 ВУЗ/курс только у студентов: ✅ Вкл → ❌ Выкл" if edu_cond == "on"
                     else "🎓 ВУЗ/курс только у студентов: ❌ Выкл → ✅ Вкл")
    show_progress = await get_setting("reg_show_progress") or "off"
    show_progress_text = ("🔢 Нумерация вопросов: ✅ Вкл → ❌ Выкл" if show_progress == "on"
                          else "🔢 Нумерация вопросов: ❌ Выкл → ✅ Вкл")

    buttons = [
        [InlineKeyboardButton(text=toggle_text, callback_data="settings_toggle_reg")],
        [InlineKeyboardButton(text=bonus_toggle_text, callback_data="settings_toggle_bonus")],
        [InlineKeyboardButton(text=full_txt, callback_data="settings_toggle_full_approval")],
        [InlineKeyboardButton(text=short_txt, callback_data="settings_toggle_short_approval")],
        [InlineKeyboardButton(text=notify_txt, callback_data="settings_toggle_notify")],
        [InlineKeyboardButton(text=payment_toggle_text, callback_data="toggle_payment_enabled")],
        [InlineKeyboardButton(text=consent_toggle_text, callback_data="toggle_consent_enabled")],
        [InlineKeyboardButton(text="🧾 PDF согласий", callback_data="admin_consent_pdfs")],
        [InlineKeyboardButton(text=uni_mode_text, callback_data="toggle_uni_mode")],
        [InlineKeyboardButton(text=edu_cond_text, callback_data="toggle_edu_conditional")],
        [InlineKeyboardButton(text=show_progress_text, callback_data="toggle_show_progress")],
        [InlineKeyboardButton(text="📋 Вопросы регистрации", callback_data="admin_reg_questions")],
        [InlineKeyboardButton(text="✏️ Тексты вопросов", callback_data="admin_reg_prompts")],
        [InlineKeyboardButton(text="🔘 Кнопки меню", callback_data="admin_menu_buttons")],
    ]
    for key, label, _ in SETTINGS_FIELDS:
        buttons.append([InlineKeyboardButton(text=f"✏️ {label}", callback_data=f"settings_edit:{key}")])
    for prefix, label, _ in PHOTO_FIELDS:
        buttons.append([InlineKeyboardButton(text=f"📷 {label}", callback_data=f"settings_photo:{prefix}")])
    for prefix, label, _ in FILE_FIELDS:
        buttons.append([InlineKeyboardButton(text=f"📎 {label}", callback_data=f"settings_file:{prefix}")])
    buttons.append([InlineKeyboardButton(text="← Назад", callback_data="settings_back")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


@router.callback_query(F.data == "admin_settings")
async def show_admin_settings(callback: types.CallbackQuery):
    if callback.from_user.id not in config.ADMIN_IDS:
        await callback.answer("Недостаточно прав", show_alert=True)
        return

    text = await render_settings_text()
    await callback.message.edit_text(text, parse_mode="HTML", reply_markup=await build_settings_keyboard())
    await callback.answer()


@router.callback_query(F.data == "settings_toggle_reg")
async def toggle_registration_mode(callback: types.CallbackQuery):
    if callback.from_user.id not in config.ADMIN_IDS:
        await callback.answer("Недостаточно прав", show_alert=True)
        return

    current = await get_setting("registration_mode") or "short"
    new_mode = "full" if current == "short" else "short"
    await set_setting("registration_mode", new_mode)

    label = "📋 Полная" if new_mode == "full" else "⚡ Краткая"
    await callback.answer(f"Форма регистрации: {label}", show_alert=True)

    text = await render_settings_text()
    await callback.message.edit_text(text, parse_mode="HTML", reply_markup=await build_settings_keyboard())


async def _toggle_approval_setting(callback: types.CallbackQuery, key: str, default: str, title: str):
    if callback.from_user.id not in config.ADMIN_IDS:
        await callback.answer("Недостаточно прав", show_alert=True)
        return
    current = await get_setting(key) or default
    new_val = "auto" if current == "manual" else "manual"
    await set_setting(key, new_val)
    await callback.answer(f"{title}: {'👮 Ручная' if new_val == 'manual' else '⚡ Авто'}", show_alert=True)
    text = await render_settings_text()
    await callback.message.edit_text(text, parse_mode="HTML", reply_markup=await build_settings_keyboard())


@router.callback_query(F.data == "settings_toggle_full_approval")
async def toggle_full_approval(callback: types.CallbackQuery):
    await _toggle_approval_setting(callback, "full_approval", "manual", "Модерация полной формы")


@router.callback_query(F.data == "settings_toggle_short_approval")
async def toggle_short_approval(callback: types.CallbackQuery):
    await _toggle_approval_setting(callback, "short_approval", "auto", "Модерация краткой формы")


# ── Phase 4: module on/off toggles (payment, consent) + event-type preset ────

async def _toggle_module_setting(callback: types.CallbackQuery, key: str, title: str):
    """On/off toggle for a Phase 4 module flag (fail-safe default OFF, D-15)."""
    if callback.from_user.id not in config.ADMIN_IDS:
        await callback.answer("Недостаточно прав", show_alert=True)
        return
    current = await get_setting(key) or "off"
    new_val = "off" if current == "on" else "on"
    await set_setting(key, new_val)
    label = "✅ Вкл" if new_val == "on" else "❌ Выкл"
    await callback.answer(f"{title}: {label}", show_alert=True)
    text = await render_settings_text()
    await callback.message.edit_text(text, parse_mode="HTML", reply_markup=await build_settings_keyboard())


@router.callback_query(F.data == "toggle_payment_enabled")
async def toggle_payment_enabled(callback: types.CallbackQuery):
    await _toggle_module_setting(callback, "payment_enabled", "💳 Оплата")


@router.callback_query(F.data == "toggle_consent_enabled")
async def toggle_consent_enabled(callback: types.CallbackQuery):
    await _toggle_module_setting(callback, "consent_enabled", "📋 Согласия")


async def _toggle_value_setting(callback, key, val_a, val_b, default, title_a, title_b):
    """Generic two-value toggle (e.g. list↔text, on↔off) with a friendly alert."""
    if callback.from_user.id not in config.ADMIN_IDS:
        await callback.answer("Недостаточно прав", show_alert=True)
        return
    current = await get_setting(key) or default
    new_val = val_b if current == val_a else val_a
    await set_setting(key, new_val)
    await callback.answer(title_a if new_val == val_a else title_b, show_alert=True)
    text = await render_settings_text()
    await callback.message.edit_text(text, parse_mode="HTML", reply_markup=await build_settings_keyboard())


@router.callback_query(F.data == "toggle_uni_mode")
async def toggle_uni_mode(callback: types.CallbackQuery):
    await _toggle_value_setting(
        callback, "reg_university_mode", "list", "text", "text",
        "🏫 ВУЗ: выбор из списка", "🏫 ВУЗ: свободный ввод",
    )


@router.callback_query(F.data == "toggle_edu_conditional")
async def toggle_edu_conditional(callback: types.CallbackQuery):
    await _toggle_value_setting(
        callback, "edu_conditional", "on", "off", "on",
        "🎓 ВУЗ/курс спрашиваются только у студентов", "🎓 ВУЗ/курс спрашиваются у всех",
    )


@router.callback_query(F.data == "toggle_show_progress")
async def toggle_show_progress(callback: types.CallbackQuery):
    await _toggle_value_setting(
        callback, "reg_show_progress", "on", "off", "off",
        "🔢 Нумерация вопросов включена", "🔢 Нумерация вопросов выключена",
    )


async def _apply_event_type_preset(event_type: str):
    """D-05: event type presets module flags; each is still manually overridable after.
    conference → payment+consent ON; forum → both OFF; custom → no change."""
    if event_type == "conference":
        await set_setting("payment_enabled", "on")
        await set_setting("consent_enabled", "on")
    elif event_type == "forum":
        await set_setting("payment_enabled", "off")
        await set_setting("consent_enabled", "off")
    # "custom" → no change (manual control)


@router.callback_query(F.data == "settings_toggle_notify")
async def toggle_notify_mode(callback: types.CallbackQuery):
    if callback.from_user.id not in config.ADMIN_IDS:
        await callback.answer("Недостаточно прав", show_alert=True)
        return
    current = await get_setting("pending_notify_mode") or "batched"
    new_val = "batched" if current == "instant" else "instant"
    await set_setting("pending_notify_mode", new_val)
    await callback.answer(f"Уведомление: {'📨 Сразу' if new_val == 'instant' else '🕒 Пачкой'}", show_alert=True)
    text = await render_settings_text()
    await callback.message.edit_text(text, parse_mode="HTML", reply_markup=await build_settings_keyboard())


@router.callback_query(F.data == "settings_toggle_bonus")
async def toggle_bonus(callback: types.CallbackQuery):
    if callback.from_user.id not in config.ADMIN_IDS:
        await callback.answer("Недостаточно прав", show_alert=True)
        return

    current = await get_setting("reg_bonus_enabled") or "off"
    new_val = "on" if current == "off" else "off"
    await set_setting("reg_bonus_enabled", new_val)

    label = "✅ Вкл" if new_val == "on" else "❌ Выкл"
    await callback.answer(f"Бонус за регистрацию: {label}", show_alert=True)

    text = await render_settings_text()
    await callback.message.edit_text(text, parse_mode="HTML", reply_markup=await build_settings_keyboard())


@router.callback_query(F.data.startswith("settings_file:"))
async def settings_file_start(callback: types.CallbackQuery, state: FSMContext):
    if callback.from_user.id not in config.ADMIN_IDS:
        await callback.answer("Недостаточно прав", show_alert=True)
        return

    prefix = callback.data.split(":", 1)[1]
    prompts = {p: (label, prompt) for p, label, prompt in FILE_FIELDS}
    label, prompt = prompts.get(prefix, ("Файл", "Отправьте файл."))

    photo = await get_setting(f"{prefix}_photo_file_id")
    doc = await get_setting(f"{prefix}_doc_file_id")
    status = "✅ загружен" if (photo or doc) else "<i>не загружен</i>"
    text = f"{label}: {status}\n\n{prompt}"

    cancel_kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="❌ Отмена", callback_data="settings_cancel")],
    ])
    await callback.message.edit_text(text, parse_mode="HTML", reply_markup=cancel_kb)
    await state.set_state(EditSetting.waiting_for_file)
    await state.update_data(file_setting=prefix)
    await callback.answer()


@router.callback_query(F.data.startswith("settings_edit:"))
async def settings_edit_start(callback: types.CallbackQuery, state: FSMContext):
    if callback.from_user.id not in config.ADMIN_IDS:
        await callback.answer("Недостаточно прав", show_alert=True)
        return

    key = callback.data.split(":", 1)[1]
    prompts = {k: prompt for k, _, prompt in SETTINGS_FIELDS}
    prompt = prompts.get(key, "Введите значение")
    current = await get_setting(key)

    # Escape both the field description (may contain literal <b>/<code> examples) and the
    # current value (admin may have stored raw HTML) — otherwise parse_mode=HTML breaks.
    text = f"{html_module.escape(prompt)}"
    if current:
        text = f"Сейчас задано:\n<b>{html_module.escape(current)}</b>\n\n{text}"
    text += "\n\n<i>Пришлите новое значение сообщением. Чтобы очистить поле — отправьте «-».</i>"

    cancel_kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="❌ Отмена", callback_data="settings_cancel")],
    ])
    await callback.message.edit_text(text, parse_mode="HTML", reply_markup=cancel_kb)
    await state.set_state(EditSetting.waiting_for_value)
    await state.update_data(setting_key=key)
    await callback.answer()


@router.callback_query(F.data.startswith("settings_photo:"))
async def settings_photo_start(callback: types.CallbackQuery, state: FSMContext):
    if callback.from_user.id not in config.ADMIN_IDS:
        await callback.answer("Недостаточно прав", show_alert=True)
        return

    prefix = callback.data.split(":", 1)[1]
    prompts = {p: (label, prompt) for p, label, prompt in PHOTO_FIELDS}
    label, prompt = prompts.get(prefix, ("Фото", "Отправьте фото."))

    current = await get_setting(f"{prefix}_photo_file_id")
    text = f"{label}: {'✅ загружена' if current else '<i>не загружена</i>'}\n\n{prompt}"

    cancel_kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="❌ Отмена", callback_data="settings_cancel")],
    ])
    await callback.message.edit_text(text, parse_mode="HTML", reply_markup=cancel_kb)
    await state.set_state(EditSetting.waiting_for_photo)
    await state.update_data(photo_setting=prefix)
    await callback.answer()


@router.callback_query(F.data == "settings_cancel")
async def cancel_edit_setting_callback(callback: types.CallbackQuery, state: FSMContext):
    await state.clear()
    text = await render_settings_text()
    await callback.message.edit_text(text, parse_mode="HTML", reply_markup=await build_settings_keyboard())
    await callback.answer()


@router.callback_query(F.data == "admin_sync_sheet")
async def sync_sheet(callback: types.CallbackQuery):
    if callback.from_user.id not in config.ADMIN_IDS:
        await callback.answer("Недостаточно прав", show_alert=True)
        return

    await callback.answer("🔄 Синхронизация...")
    await callback.message.edit_text("🔄 Получаю данные из таблицы...", parse_mode="HTML")

    try:
        await ensure_sheet_header(SHEET_HEADERS)  # шапка таблицы, если её ещё нет
        existing_ids = await get_existing_sheet_ids()
        all_users = await get_all_users_dicts()

        missing = [u for u in all_users if u["telegram_id"] not in existing_ids]

        if not missing:
            await callback.message.edit_text(
                "✅ Таблица синхронизирована, пропущенных записей нет.",
                parse_mode="HTML",
                reply_markup=build_admin_keyboard(),
            )
            return

        rows = [_build_sheet_row(u) for u in missing]
        count = await append_rows_to_sheet(rows)

        await callback.message.edit_text(
            f"✅ Синхронизация завершена!\n\n"
            f"Добавлено записей: <b>{count}</b>",
            parse_mode="HTML",
            reply_markup=build_admin_keyboard(),
        )
    except Exception as e:
        logger.error(f"Sheet sync failed: {e}")
        await callback.message.edit_text(
            f"❌ Ошибка синхронизации:\n<code>{html_module.escape(str(e))}</code>",
            parse_mode="HTML",
            reply_markup=build_admin_keyboard(),
        )


@router.callback_query(F.data == "settings_back")
async def settings_back_to_admin(callback: types.CallbackQuery):
    if callback.from_user.id not in config.ADMIN_IDS:
        await callback.answer("Недостаточно прав", show_alert=True)
        return

    await callback.message.edit_text(
        "👮‍♂️ <b>Панель администратора</b>",
        parse_mode="HTML",
        reply_markup=build_admin_keyboard(),
    )
    await callback.answer()


def _parse_consent_list(raw: str) -> list[tuple[str, str]]:
    """consent_list ('Название|ключ' per line) → [(label, key)]."""
    items = []
    for line in (raw or "").strip().splitlines():
        line = line.strip()
        if not line or "|" not in line:
            continue
        label, key = line.split("|", 1)
        key = key.strip()
        if key:
            items.append((label.strip() or key, key))
    return items


@router.callback_query(F.data == "admin_consent_pdfs")
async def admin_consent_pdfs(callback: types.CallbackQuery):
    if callback.from_user.id not in config.ADMIN_IDS:
        await callback.answer("Недостаточно прав", show_alert=True)
        return
    items = _parse_consent_list(await get_setting("consent_list") or "")
    if not items:
        await callback.answer()
        await callback.message.edit_text(
            "🧾 <b>PDF согласий</b>\n\n"
            "Здесь пусто, потому что ещё не задан список согласий.\n\n"
            "<b>Что сделать:</b>\n"
            "1. Зайди в «📋 Список согласий» и добавь согласия (каждое строкой "
            "<i>Название|ключ</i>).\n"
            "2. Вернись сюда — у каждого согласия появится кнопка для загрузки PDF.",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="📋 К списку согласий", callback_data="settings_edit:consent_list")],
                [InlineKeyboardButton(text="← Назад", callback_data="admin_settings")],
            ]),
        )
        return
    buttons = []
    for label, key in items:
        has_pdf = bool(await get_setting(f"consent_pdf_{key}"))
        mark = "✅" if has_pdf else "📎"
        buttons.append([InlineKeyboardButton(text=f"{mark} {label}", callback_data=f"consent_pdf_set:{key}")])
    buttons.append([InlineKeyboardButton(text="← Назад", callback_data="admin_settings")])
    await callback.message.edit_text(
        "🧾 <b>PDF согласий</b>\n\n"
        "Нажми на согласие и пришли PDF-файл — участник увидит его прикреплённым к этому согласию.\n\n"
        "✅ — PDF уже загружен · 📎 — ещё нет.",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons),
    )
    await callback.answer()


@router.callback_query(F.data.startswith("consent_pdf_set:"))
async def consent_pdf_set(callback: types.CallbackQuery, state: FSMContext):
    if callback.from_user.id not in config.ADMIN_IDS:
        await callback.answer("Недостаточно прав", show_alert=True)
        return
    key = callback.data.split(":", 1)[1]
    cancel_kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="❌ Отмена", callback_data="settings_cancel")],
    ])
    await callback.message.edit_text(
        "📎 Пришли сюда <b>PDF-файл</b> этого согласия одним сообщением "
        "(перетащи файл или прикрепи через скрепку).\n\n"
        "Просто фото или ссылка не подойдут — нужен именно PDF-документ.",
        parse_mode="HTML",
        reply_markup=cancel_kb,
    )
    await state.set_state(EditSetting.waiting_for_file)
    await state.update_data(raw_file_key=f"consent_pdf_{key}")
    await callback.answer()


@router.message(StateFilter(EditSetting), Command("cancel"))
@router.message(StateFilter(EditSetting), F.text == "Отмена")
async def cancel_edit_setting(message: types.Message, state: FSMContext):
    await state.clear()
    text = await render_settings_text()
    await message.answer(text, parse_mode="HTML", reply_markup=await build_settings_keyboard())


@router.message(EditSetting.waiting_for_photo, is_admin, F.photo)
async def settings_receive_photo(message: types.Message, state: FSMContext):
    data = await state.get_data()
    prefix = data.get("photo_setting", "program")

    file_id = message.photo[-1].file_id
    await set_setting(f"{prefix}_photo_file_id", file_id)

    if message.caption:
        await set_setting(f"{prefix}_caption", message.html_text)
    else:
        await delete_setting(f"{prefix}_caption")

    await state.clear()
    await message.answer("✅ Фото обновлено!")
    text = await render_settings_text()
    await message.answer(text, parse_mode="HTML", reply_markup=await build_settings_keyboard())


@router.message(EditSetting.waiting_for_photo, is_admin)
async def settings_receive_photo_invalid(message: types.Message):
    await message.answer("Отправьте именно фото (не файлом).")


@router.message(EditSetting.waiting_for_file, is_admin, F.photo)
async def settings_receive_file_photo(message: types.Message, state: FSMContext):
    data = await state.get_data()
    if data.get("raw_file_key"):
        await message.answer("Согласие принимается только PDF-документом, не фото.")
        return
    prefix = data.get("file_setting", "reg_bonus")

    file_id = message.photo[-1].file_id
    await set_setting(f"{prefix}_photo_file_id", file_id)
    await delete_setting(f"{prefix}_doc_file_id")

    if message.caption:
        await set_setting(f"{prefix}_caption", message.html_text)
    else:
        await delete_setting(f"{prefix}_caption")

    await state.clear()
    await message.answer("✅ Файл обновлён!")
    text = await render_settings_text()
    await message.answer(text, parse_mode="HTML", reply_markup=await build_settings_keyboard())


@router.message(EditSetting.waiting_for_file, is_admin, F.document)
async def settings_receive_file_doc(message: types.Message, state: FSMContext):
    data = await state.get_data()

    # Consent PDF: store the document file_id directly into an arbitrary settings key.
    raw_key = data.get("raw_file_key")
    if raw_key:
        if (message.document.mime_type or "") != "application/pdf":
            await message.answer("Принимается только PDF-документ. Пришли PDF.")
            return
        await set_setting(raw_key, message.document.file_id)
        await state.clear()
        await message.answer("✅ PDF согласия сохранён!")
        text = await render_settings_text()
        await message.answer(text, parse_mode="HTML", reply_markup=await build_settings_keyboard())
        return

    prefix = data.get("file_setting", "reg_bonus")

    file_id = message.document.file_id
    await set_setting(f"{prefix}_doc_file_id", file_id)
    await delete_setting(f"{prefix}_photo_file_id")

    if message.caption:
        await set_setting(f"{prefix}_caption", message.html_text)
    else:
        await delete_setting(f"{prefix}_caption")

    await state.clear()
    await message.answer("✅ Файл обновлён!")
    text = await render_settings_text()
    await message.answer(text, parse_mode="HTML", reply_markup=await build_settings_keyboard())


@router.message(EditSetting.waiting_for_file, is_admin)
async def settings_receive_file_invalid(message: types.Message):
    await message.answer("Отправьте фото или документ.")


HTML_SETTINGS = {"start_text", "reg_complete_text", "approve_text"}

@router.message(EditSetting.waiting_for_value, is_admin)
async def settings_edit_value(message: types.Message, state: FSMContext):
    data = await state.get_data()
    key = data["setting_key"]

    if key in HTML_SETTINGS:
        value = (message.html_text or message.text or "").strip()
    else:
        value = (message.text or "").strip()

    if value == "-":
        await delete_setting(key)
    else:
        await set_setting(key, value)
        # Phase 4 (D-05): saving event_type applies the module-toggle preset.
        if key == "event_type":
            await _apply_event_type_preset(value.strip().lower())

    await state.clear()
    text = await render_settings_text()
    await message.answer(text, parse_mode="HTML", reply_markup=await build_settings_keyboard())


@router.callback_query(F.data == "admin_export_csv")
async def show_admin_export(callback: types.CallbackQuery):
    if callback.from_user.id not in config.ADMIN_IDS:
        await callback.answer("Недостаточно прав", show_alert=True)
        return

    headers, rows = await export_users_csv()
    output = io.StringIO()
    writer = csv.writer(output, delimiter=';', quotechar='"', quoting=csv.QUOTE_MINIMAL)
    writer.writerow(headers)
    writer.writerows(rows)
    file_bytes = output.getvalue().encode('utf-8-sig')
    document = BufferedInputFile(file_bytes, filename="users.csv")
    await callback.message.answer_document(document, caption="База данных пользователей")
    await callback.answer()


@router.callback_query(F.data == "admin_broadcast")
async def show_admin_broadcast(callback: types.CallbackQuery, state: FSMContext):
    if callback.from_user.id not in config.ADMIN_IDS:
        await callback.answer("Недостаточно прав", show_alert=True)
        return

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📢 Все пользователи", callback_data="broadcast_all")],
        [InlineKeyboardButton(text="📄 По файлу в проекте", callback_data="broadcast_local")],
        [InlineKeyboardButton(text="🚫 Не подписаны на канал", callback_data="broadcast_unsubscribed")],
        [InlineKeyboardButton(text="📝 Не завершили регистрацию", callback_data="broadcast_incomplete")],
        [InlineKeyboardButton(text="🎯 По фильтру", callback_data="broadcast_filter")],
        [InlineKeyboardButton(text="🕓 Запланировать", callback_data="broadcast_schedule")],
        [InlineKeyboardButton(text="❌ Отмена", callback_data="broadcast_cancel")],
    ])
    await callback.message.edit_text("Выберите целевую аудиторию рассылки:", reply_markup=kb)
    await state.set_state(Broadcast.target_selection)
    await callback.answer()

@router.message(Command("export"), is_admin)
async def cmd_export(message: types.Message):
    headers, rows = await export_users_csv()

    output = io.StringIO()
    writer = csv.writer(output, delimiter=';', quotechar='"', quoting=csv.QUOTE_MINIMAL)
    writer.writerow(headers)
    writer.writerows(rows)

    output.seek(0)
    file_bytes = output.getvalue().encode('utf-8-sig')
    document = BufferedInputFile(file_bytes, filename="users.csv")

    await message.answer_document(document, caption="База данных пользователей")

@router.message(Command("broadcast"), is_admin)
async def cmd_broadcast(message: types.Message, state: FSMContext):
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📢 Все пользователи", callback_data="broadcast_all")],
        [InlineKeyboardButton(text="📄 По файлу в проекте", callback_data="broadcast_local")],
        [InlineKeyboardButton(text="🚫 Не подписаны на канал", callback_data="broadcast_unsubscribed")],
        [InlineKeyboardButton(text="📝 Не завершили регистрацию", callback_data="broadcast_incomplete")],
        [InlineKeyboardButton(text="🎯 По фильтру", callback_data="broadcast_filter")],
        [InlineKeyboardButton(text="🕓 Запланировать", callback_data="broadcast_schedule")],
        [InlineKeyboardButton(text="❌ Отмена", callback_data="broadcast_cancel")],
    ])
    await message.answer("Выберите целевую аудиторию рассылки:", reply_markup=kb)
    await state.set_state(Broadcast.target_selection)

@router.callback_query(F.data == "broadcast_all", Broadcast.target_selection)
async def process_broadcast_all(callback: types.CallbackQuery, state: FSMContext):
    await callback.answer()
    try:
        await callback.message.delete()
    except Exception:
        pass
    await callback.message.answer(
        "Отправьте сообщение (текст или фото с подписью) для рассылки всем пользователям.",
        reply_markup=get_cancel_kb(),
    )
    await state.update_data(target_type="all")
    await state.set_state(Broadcast.message)

@router.callback_query(F.data == "broadcast_local", Broadcast.target_selection)
async def process_broadcast_local_file(callback: types.CallbackQuery, state: FSMContext):
    file_path = "data/broadcast_target.txt"

    if not os.path.exists(file_path):
        await callback.message.edit_text(f"❌ Файл {file_path} не найден! Создайте его и добавьте ID пользователей.")
        await state.clear()
        return

    try:
        with open(file_path, 'r', encoding='utf-8') as f:
            content = f.read()

        user_ids = []
        for line in content.splitlines():
            line = line.strip()
            clean_line = line.replace(',', '').replace(';', '')

            if clean_line.isdigit():
                user_ids.append(int(clean_line))

        if not user_ids:
            await callback.message.edit_text("⚠️ Файл пуст или не содержит корректных ID.")
            await state.clear()
            return

        user_ids = list(set(user_ids))

        await state.update_data(target_type="list", target_users=user_ids)
        await callback.answer()
        try:
            await callback.message.delete()
        except Exception:
            pass
        await callback.message.answer(
            f"✅ Найдено {len(user_ids)} пользователей в файле.\nТеперь отправьте сообщение для рассылки.",
            reply_markup=get_cancel_kb(),
        )
        await state.set_state(Broadcast.message)

    except Exception as e:
        await callback.message.edit_text(f"Ошибка при чтении файла: {e}")
        await state.clear()

async def _start_segment_broadcast(callback: types.CallbackQuery, state: FSMContext, user_ids: list, prompt: str):
    # Callbacks are not covered by the message-level is_admin filter — re-check here (D-06 / T-04-03).
    if callback.from_user.id not in config.ADMIN_IDS:
        await callback.answer("Недостаточно прав", show_alert=True)
        return
    user_ids = list(set(user_ids))
    if not user_ids:
        await callback.message.edit_text("В этом сегменте сейчас нет пользователей.")
        await state.clear()
        await callback.answer()
        return
    await state.update_data(target_type="list", target_users=user_ids)
    await callback.answer()
    try:
        await callback.message.delete()
    except Exception:
        pass
    await callback.message.answer(prompt, reply_markup=get_cancel_kb())
    await state.set_state(Broadcast.message)


# ── Phase 3 (COMM-04): pure flood-safe send helpers ──────────────────────────

def _retry_delay(retry_after: int) -> int:
    """Wait Telegram's told delay plus 1s of slack before retrying (D-07)."""
    return retry_after + 1


def _classify_outcome(first_ok: bool, retried_ok) -> tuple[int, int]:
    """(delivered_inc, blocked_inc). A 429 that succeeds on retry is delivered, NOT
    blocked (D-08); only a genuine/failed-retry outcome increments blocked."""
    if first_ok or retried_ok is True:
        return (1, 0)
    return (0, 1)


@router.callback_query(F.data == "broadcast_unsubscribed", Broadcast.target_selection)
async def process_broadcast_unsubscribed(callback: types.CallbackQuery, state: FSMContext):
    if callback.from_user.id not in config.ADMIN_IDS:
        await callback.answer("Недостаточно прав", show_alert=True)
        return
    user_ids = await get_non_subscriber_ids()
    await _start_segment_broadcast(
        callback, state, user_ids,
        f"🚫 {len(set(user_ids))} пользователей не подписаны на канал, давайте пришлём им уведомление.\n"
        "Теперь отправьте сообщение для рассылки.",
    )


@router.callback_query(F.data == "broadcast_incomplete", Broadcast.target_selection)
async def process_broadcast_incomplete(callback: types.CallbackQuery, state: FSMContext):
    if callback.from_user.id not in config.ADMIN_IDS:
        await callback.answer("Недостаточно прав", show_alert=True)
        return
    user_ids = await get_incomplete_user_ids()
    await _start_segment_broadcast(
        callback, state, user_ids,
        f"📝 {len(set(user_ids))} пользователей не завершили регистрацию.\n"
        "Теперь отправьте сообщение для рассылки.",
    )


@router.callback_query(F.data == "broadcast_cancel")
async def cancel_broadcast_callback(callback: types.CallbackQuery, state: FSMContext):
    await state.clear()
    await callback.message.edit_text("Рассылка отменена.")
    await callback.answer()


@router.message(StateFilter(Broadcast), Command("cancel"))
@router.message(StateFilter(Broadcast), F.text == "Отмена")
async def cancel_broadcast(message: types.Message, state: FSMContext):
    await state.clear()
    await message.answer("Рассылка отменена.", reply_markup=ReplyKeyboardRemove())


async def _wait_and_send_album(media_group_id: str, users_ids: list, bot: Bot, state: FSMContext, admin_id: int):
    await asyncio.sleep(0.8)
    album_data = pending_albums.pop(media_group_id, None)
    if not album_data:
        return

    messages = album_data["messages"]
    media = []

    for msg in messages:
        caption_text = msg.html_text if getattr(msg, "html_text", None) else None

        if msg.photo:
            media.append(types.InputMediaPhoto(
                media=msg.photo[-1].file_id,
                caption=caption_text,
                parse_mode="HTML"
            ))
        elif msg.video:
            media.append(types.InputMediaVideo(
                media=msg.video.file_id,
                caption=caption_text,
                parse_mode="HTML"
            ))
        elif msg.document:
            media.append(types.InputMediaDocument(
                media=msg.document.file_id,
                caption=caption_text,
                parse_mode="HTML"
            ))
        elif msg.audio:
            media.append(types.InputMediaAudio(
                media=msg.audio.file_id,
                caption=caption_text,
                parse_mode="HTML"
            ))

    if not media:
        return

    count = 0
    blocked = 0
    for chat_id in users_ids:
        retried_ok = None
        try:
            await bot.send_media_group(chat_id, media=media)
            first_ok = True
        except TelegramRetryAfter as e:
            first_ok = False
            await asyncio.sleep(_retry_delay(e.retry_after))
            try:
                await bot.send_media_group(chat_id, media=media)
                retried_ok = True
            except Exception:
                retried_ok = "error"
        except Exception:
            first_ok = False
        delivered_inc, blocked_inc = _classify_outcome(first_ok, retried_ok)
        count += delivered_inc
        blocked += blocked_inc
        await asyncio.sleep(0.05)

    try:
        await bot.send_message(
            admin_id,
            f"Рассылка альбома завершена.\n✅ Успешно: {count}\n❌ Недоступно: {blocked}"
        )
    except Exception:
        pass

    await state.clear()

@router.message(Broadcast.message, is_admin)
async def process_broadcast(message: types.Message, state: FSMContext, bot: Bot):
    data = await state.get_data()
    target_type = data.get("target_type", "all")

    if target_type == "list":
        users_ids = data.get("target_users", [])
        if not users_ids:
             await message.answer("Список пользователей пуст. Рассылка отменена.")
             await state.clear()
             return
    else:
        users_ids = await get_all_users_ids()

    mgid = message.media_group_id
    if mgid:
        if mgid not in pending_albums:
            pending_albums[mgid] = {"messages": [message]}
            await message.answer(f"Альбом получен. Начинаю рассылку на {len(users_ids)} пользователей...")
            asyncio.create_task(_wait_and_send_album(mgid, users_ids, bot, state, message.from_user.id))
        else:
            pending_albums[mgid]["messages"].append(message)
        return

    count = 0
    blocked = 0

    status_msg = await message.answer(f"Начинаю рассылку на {len(users_ids)} пользователей...")

    for chat_id in users_ids:
        retried_ok = None
        try:
            await message.send_copy(chat_id)
            first_ok = True
        except TelegramRetryAfter as e:
            first_ok = False
            await asyncio.sleep(_retry_delay(e.retry_after))
            try:
                await message.send_copy(chat_id)
                retried_ok = True
            except Exception:
                retried_ok = "error"
        except Exception:
            first_ok = False
        delivered_inc, blocked_inc = _classify_outcome(first_ok, retried_ok)
        count += delivered_inc
        blocked += blocked_inc
        await asyncio.sleep(0.05)

    await message.answer(
        f"Рассылка завершена.\n"
        f"✅ Успешно: {count}\n"
        f"❌ Недоступно: {blocked}"
    )
    await state.clear()


# ── Phase 3 (SCHED-01): schedule-a-broadcast UI ──────────────────────────────

@router.callback_query(F.data == "broadcast_schedule", Broadcast.target_selection)
async def broadcast_schedule_start(callback: types.CallbackQuery, state: FSMContext):
    if callback.from_user.id not in config.ADMIN_IDS:
        await callback.answer("Недостаточно прав", show_alert=True)
        return
    await callback.answer()
    try:
        await callback.message.delete()
    except Exception:
        pass
    await callback.message.answer(
        "🕓 Введите дату и время рассылки в формате <b>ДД.ММ.ГГГГ ЧЧ:ММ</b>\n"
        "Например: 01.07.2026 14:30",
        reply_markup=get_cancel_kb(),
    )
    await state.set_state(Broadcast.schedule_when)


@router.message(Broadcast.schedule_when, is_admin)
async def broadcast_schedule_when(message: types.Message, state: FSMContext):
    when = _parse_schedule_dt(message.text)
    if when is None:
        await message.answer("❌ Не понял дату. Формат: ДД.ММ.ГГГГ ЧЧ:ММ (напр. 01.07.2026 14:30)")
        return
    if when <= datetime.now():
        await message.answer("❌ Это время уже прошло. Введите будущую дату.")
        return
    await state.update_data(schedule_dt=when)
    await message.answer(
        f"✅ Запланировано на {when.strftime('%d.%m.%Y %H:%M')}.\n"
        "Теперь отправьте сообщение (текст или фото с подписью) для рассылки.",
        reply_markup=get_cancel_kb(),
    )
    await state.set_state(Broadcast.schedule_message)


@router.message(Broadcast.schedule_message, is_admin)
async def broadcast_schedule_message(message: types.Message, state: FSMContext):
    data = await state.get_data()
    when = data.get("schedule_dt")
    if not when:
        await message.answer("Сессия истекла, начните заново через /broadcast.")
        await state.clear()
        return

    photo = message.photo[-1].file_id if message.photo else None
    if message.text:
        text = message.html_text
    elif message.caption:
        text = message.caption
    else:
        text = None

    filters = data.get("filters")
    filter_spec = json.dumps(filters, ensure_ascii=False) if filters else None

    bid = await create_scheduled_broadcast(text, photo, filter_spec, _fmt_dt(when), message.from_user.id)
    schedule_broadcast_job(bid, when)

    scope = "по фильтру" if filters else "всем пользователям"
    await message.answer(
        f"✅ Рассылка #{bid} запланирована на {when.strftime('%d.%m.%Y %H:%M')} ({scope}).\n"
        "Управление: /scheduled"
    )
    await state.clear()


@router.message(Command("scheduled"), is_admin)
async def cmd_scheduled(message: types.Message):
    rows = await list_pending_broadcasts()
    if not rows:
        await message.answer("Нет запланированных рассылок.")
        return
    for row in rows:
        preview = (row.get("text") or "(фото)")[:60]
        kb = InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="❌ Отменить", callback_data=f"sched_cancel_{row['id']}")
        ]])
        await message.answer(
            f"#{row['id']} — {row['scheduled_at']}\n{html_module.escape(preview)}",
            reply_markup=kb,
        )


@router.callback_query(F.data.startswith("sched_cancel_"))
async def sched_cancel(callback: types.CallbackQuery):
    if callback.from_user.id not in config.ADMIN_IDS:
        await callback.answer("Недостаточно прав", show_alert=True)
        return
    bid = int(callback.data.rsplit("_", 1)[1])
    await cancel_scheduled_broadcast(bid)
    cancel_broadcast_job(bid)
    await callback.answer("Отменено")
    try:
        await callback.message.edit_text(f"#{bid} — отменено ❌")
    except Exception:
        pass


# ── Phase 3 (COMM-01/02/03): filtered-broadcast builder ──────────────────────

_FILTER_FIELD_LABELS = {
    "city": "Город", "university": "ВУЗ", "status": "Статус",
    "source": "Источник", "registration_date": "Дата регистрации",
}


def _filter_summary(filters: list[dict]) -> str:
    if not filters:
        return "Фильтры пока не выбраны."
    parts = []
    for f in filters:
        label = _FILTER_FIELD_LABELS.get(f["field"], f["field"])
        val = html_module.escape(str(f.get("value")))
        if f["field"] == "registration_date":
            opl = "после" if f.get("op") == "after" else "до"
            parts.append(f"{label} {opl} {val}")
        else:
            parts.append(f"{label} = {val}")
    return " И ".join(parts)


def _filter_menu_kb(filters: list[dict]) -> InlineKeyboardMarkup:
    kb = [
        [InlineKeyboardButton(text="Город", callback_data="filter_f_city"),
         InlineKeyboardButton(text="ВУЗ", callback_data="filter_f_university")],
        [InlineKeyboardButton(text="Статус", callback_data="filter_f_status"),
         InlineKeyboardButton(text="Источник", callback_data="filter_f_source")],
        [InlineKeyboardButton(text="Дата регистрации", callback_data="filter_f_date")],
    ]
    if filters:
        kb.append([InlineKeyboardButton(text="📊 Показать и отправить", callback_data="filter_count")])
    kb.append([InlineKeyboardButton(text="❌ Отмена", callback_data="broadcast_cancel")])
    return InlineKeyboardMarkup(inline_keyboard=kb)


async def _render_filter_menu(target, filters: list[dict], *, edit: bool):
    text = (
        "🎯 <b>Рассылка по фильтру</b>\n"
        f"Текущие условия (AND): {_filter_summary(filters)}\n\n"
        "Добавьте поле фильтра или покажите количество."
    )
    kb = _filter_menu_kb(filters)
    if edit:
        await target.edit_text(text, reply_markup=kb)
    else:
        await target.answer(text, reply_markup=kb)


@router.callback_query(F.data == "broadcast_filter", Broadcast.target_selection)
async def broadcast_filter_start(callback: types.CallbackQuery, state: FSMContext):
    if callback.from_user.id not in config.ADMIN_IDS:
        await callback.answer("Недостаточно прав", show_alert=True)
        return
    await state.update_data(filters=[])
    await callback.answer()
    await _render_filter_menu(callback.message, [], edit=True)
    await state.set_state(Broadcast.filter_field)


@router.callback_query(F.data.in_({"filter_f_city", "filter_f_university", "filter_f_source"}), Broadcast.filter_field)
async def filter_pick_text_field(callback: types.CallbackQuery, state: FSMContext):
    if callback.from_user.id not in config.ADMIN_IDS:
        await callback.answer("Недостаточно прав", show_alert=True)
        return
    field = callback.data[len("filter_f_"):]
    await state.update_data(filter_pending_field=field, filter_pending_op=None)
    await callback.answer()
    await callback.message.edit_text(f"Введите значение для поля «{_FILTER_FIELD_LABELS[field]}»:")
    await state.set_state(Broadcast.filter_value)


@router.callback_query(F.data == "filter_f_status", Broadcast.filter_field)
async def filter_pick_status(callback: types.CallbackQuery, state: FSMContext):
    if callback.from_user.id not in config.ADMIN_IDS:
        await callback.answer("Недостаточно прав", show_alert=True)
        return
    await callback.answer()
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="approved", callback_data="filter_v_status_approved")],
        [InlineKeyboardButton(text="pending", callback_data="filter_v_status_pending")],
        [InlineKeyboardButton(text="rejected", callback_data="filter_v_status_rejected")],
    ])
    await callback.message.edit_text("Выберите статус:", reply_markup=kb)


@router.callback_query(F.data.startswith("filter_v_status_"), Broadcast.filter_field)
async def filter_value_status(callback: types.CallbackQuery, state: FSMContext):
    if callback.from_user.id not in config.ADMIN_IDS:
        await callback.answer("Недостаточно прав", show_alert=True)
        return
    value = callback.data.rsplit("_", 1)[1]
    data = await state.get_data()
    filters = data.get("filters", [])
    filters.append({"field": "status", "value": value})
    await state.update_data(filters=filters)
    await callback.answer()
    await _render_filter_menu(callback.message, filters, edit=True)


@router.callback_query(F.data == "filter_f_date", Broadcast.filter_field)
async def filter_pick_date(callback: types.CallbackQuery, state: FSMContext):
    if callback.from_user.id not in config.ADMIN_IDS:
        await callback.answer("Недостаточно прав", show_alert=True)
        return
    await callback.answer()
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="После", callback_data="filter_d_after"),
         InlineKeyboardButton(text="До", callback_data="filter_d_before")],
    ])
    await callback.message.edit_text("Зарегистрированы…", reply_markup=kb)


@router.callback_query(F.data.in_({"filter_d_after", "filter_d_before"}), Broadcast.filter_field)
async def filter_pick_date_op(callback: types.CallbackQuery, state: FSMContext):
    if callback.from_user.id not in config.ADMIN_IDS:
        await callback.answer("Недостаточно прав", show_alert=True)
        return
    op = "after" if callback.data.endswith("after") else "before"
    await state.update_data(filter_pending_field="registration_date", filter_pending_op=op)
    await callback.answer()
    await callback.message.edit_text("Введите дату в формате ДД.ММ.ГГГГ:")
    await state.set_state(Broadcast.filter_value)


@router.message(Broadcast.filter_value, is_admin)
async def filter_capture_value(message: types.Message, state: FSMContext):
    data = await state.get_data()
    field = data.get("filter_pending_field")
    op = data.get("filter_pending_op")
    filters = data.get("filters", [])
    raw = (message.text or "").strip()
    if not field or not raw:
        await message.answer("Пустое значение. Попробуйте ещё раз.")
        return
    if field == "registration_date":
        try:
            normalized = datetime.strptime(raw, "%d.%m.%Y").strftime("%Y-%m-%d")
        except ValueError:
            await message.answer("❌ Дата в формате ДД.ММ.ГГГГ, например 01.06.2026")
            return
        filters.append({"field": "registration_date", "op": op, "value": normalized})
    else:
        filters.append({"field": field, "value": raw})
    await state.update_data(filters=filters, filter_pending_field=None, filter_pending_op=None)
    await _render_filter_menu(message, filters, edit=False)
    await state.set_state(Broadcast.filter_field)


@router.callback_query(F.data == "filter_count", Broadcast.filter_field)
async def filter_count(callback: types.CallbackQuery, state: FSMContext):
    if callback.from_user.id not in config.ADMIN_IDS:
        await callback.answer("Недостаточно прав", show_alert=True)
        return
    data = await state.get_data()
    filters = data.get("filters", [])
    ids = await count_and_list_filtered(filters)
    await callback.answer()
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📨 Отправить сейчас", callback_data="filter_send_now")],
        [InlineKeyboardButton(text="🕓 Запланировать", callback_data="filter_schedule")],
        [InlineKeyboardButton(text="❌ Отмена", callback_data="broadcast_cancel")],
    ])
    await callback.message.edit_text(
        f"🎯 Условия: {_filter_summary(filters)}\n"
        f"Под фильтр попадает <b>{len(ids)}</b> пользователей.",
        reply_markup=kb,
    )


@router.callback_query(F.data == "filter_send_now", Broadcast.filter_field)
async def filter_send_now(callback: types.CallbackQuery, state: FSMContext):
    if callback.from_user.id not in config.ADMIN_IDS:
        await callback.answer("Недостаточно прав", show_alert=True)
        return
    data = await state.get_data()
    filters = data.get("filters", [])
    ids = await count_and_list_filtered(filters)
    await _start_segment_broadcast(
        callback, state, ids,
        f"🎯 {len(set(ids))} получателей по фильтру.\nТеперь отправьте сообщение для рассылки.",
    )


@router.callback_query(F.data == "filter_schedule", Broadcast.filter_field)
async def filter_schedule(callback: types.CallbackQuery, state: FSMContext):
    if callback.from_user.id not in config.ADMIN_IDS:
        await callback.answer("Недостаточно прав", show_alert=True)
        return
    # filters stay in FSM state; the schedule flow reads them as filter_spec
    await callback.answer()
    await callback.message.edit_text(
        "🕓 Введите дату и время рассылки в формате ДД.ММ.ГГГГ ЧЧ:ММ (напр. 01.07.2026 14:30):"
    )
    await state.set_state(Broadcast.schedule_when)


# ── Phase 3 (VERIF): manual allowlist refresh ────────────────────────────────

@router.message(Command("refresh_allowlist"), is_admin)
async def cmd_refresh_allowlist(message: types.Message):
    await refresh_allowlist()
    size = allowlist_size()
    if size == 0:
        await message.answer(
            "⚠️ Allowlist пуст. Если предотбор включён — сейчас впускаются ВСЕ (fail-open). "
            "Проверьте Google-таблицу (вкладка «Отобранные»)."
        )
    else:
        await message.answer(f"✅ Allowlist обновлён: {size} username в списке.")


# --- Registration Question Toggles ---

async def render_questions_text() -> str:
    lines = ["📋 <b>Вопросы регистрации</b>", ""]
    lines.append("<i>Действуют в режиме «📋 Полная регистрация».</i>")
    lines.append("")

    for _, setting_key, *_rest in REG_FLOW:
        label = REG_LABELS.get(setting_key, setting_key)
        val = await get_setting(setting_key)
        is_on = (val == "on") if val is not None else (REG_DEFAULTS.get(setting_key, "on") == "on")
        status = "✅" if is_on else "❌"
        lines.append(f"{status} {label}")

    return "\n".join(lines)


async def build_questions_keyboard():
    buttons = []
    for _, setting_key, *_rest in REG_FLOW:
        label = REG_LABELS.get(setting_key, setting_key)
        val = await get_setting(setting_key)
        is_on = (val == "on") if val is not None else (REG_DEFAULTS.get(setting_key, "on") == "on")
        toggle_text = f"{'✅' if is_on else '❌'} {label}"
        buttons.append([InlineKeyboardButton(text=toggle_text, callback_data=f"reg_q_toggle:{setting_key}")])
    buttons.append([InlineKeyboardButton(text="← Назад к настройкам", callback_data="reg_q_back")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


@router.callback_query(F.data == "admin_reg_questions")
async def show_reg_questions(callback: types.CallbackQuery):
    if callback.from_user.id not in config.ADMIN_IDS:
        await callback.answer("Недостаточно прав", show_alert=True)
        return

    text = await render_questions_text()
    await callback.message.edit_text(text, parse_mode="HTML", reply_markup=await build_questions_keyboard())
    await callback.answer()


@router.callback_query(F.data.startswith("reg_q_toggle:"))
async def toggle_reg_question(callback: types.CallbackQuery):
    if callback.from_user.id not in config.ADMIN_IDS:
        await callback.answer("Недостаточно прав", show_alert=True)
        return

    setting_key = callback.data.split(":", 1)[1]

    val = await get_setting(setting_key)
    current_on = (val == "on") if val is not None else (REG_DEFAULTS.get(setting_key, "on") == "on")

    new_val = "off" if current_on else "on"
    await set_setting(setting_key, new_val)

    label = REG_LABELS.get(setting_key, setting_key)
    status = "✅ Вкл" if new_val == "on" else "❌ Выкл"
    await callback.answer(f"{label}: {status}", show_alert=True)

    text = await render_questions_text()
    await callback.message.edit_text(text, parse_mode="HTML", reply_markup=await build_questions_keyboard())


@router.callback_query(F.data == "reg_q_back")
async def reg_questions_back(callback: types.CallbackQuery):
    if callback.from_user.id not in config.ADMIN_IDS:
        await callback.answer("Недостаточно прав", show_alert=True)
        return

    text = await render_settings_text()
    await callback.message.edit_text(text, parse_mode="HTML", reply_markup=await build_settings_keyboard())
    await callback.answer()


# --- Editable question prompts (YL'26: per-event wording, 0 хардкода) ---

def _prompt_steps() -> list[tuple[str, str]]:
    """(step_key, human label) for every question whose wording can be overridden."""
    steps = [("full_name", "🪪 Фамилия и Имя")]
    for step_key, setting_key, *_ in REG_FLOW:
        steps.append((step_key, REG_LABELS.get(setting_key, step_key)))
    return steps


@router.callback_query(F.data == "admin_reg_prompts")
async def admin_reg_prompts(callback: types.CallbackQuery):
    if callback.from_user.id not in config.ADMIN_IDS:
        await callback.answer("Недостаточно прав", show_alert=True)
        return
    buttons = []
    for step_key, label in _prompt_steps():
        custom = await get_setting(f"reg_prompt_{step_key}")
        mark = "✅" if custom else "✏️"
        buttons.append([InlineKeyboardButton(text=f"{mark} {label}", callback_data=f"reg_prompt_edit:{step_key}")])
    buttons.append([InlineKeyboardButton(text="← Назад к настройкам", callback_data="admin_settings")])
    await callback.message.edit_text(
        "✏️ <b>Тексты вопросов</b>\n\nВыбери вопрос и пришли свой текст. ✅ — текст переопределён, "
        "✏️ — стандартный. Чтобы вернуть стандартный, отправь «-».",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons),
    )
    await callback.answer()


@router.callback_query(F.data.startswith("reg_prompt_edit:"))
async def reg_prompt_edit(callback: types.CallbackQuery, state: FSMContext):
    if callback.from_user.id not in config.ADMIN_IDS:
        await callback.answer("Недостаточно прав", show_alert=True)
        return
    step_key = callback.data.split(":", 1)[1]
    key = f"reg_prompt_{step_key}"
    current = await get_setting(key)
    text = "Пришли новый текст вопроса."
    if current:
        text = f"Текущий текст: <b>{html_module.escape(current)}</b>\n\n{text}"
    text += "\n\n<i>«-» — вернуть стандартный текст.</i>"
    cancel_kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="❌ Отмена", callback_data="settings_cancel")],
    ])
    await callback.message.edit_text(text, parse_mode="HTML", reply_markup=cancel_kb)
    await state.set_state(EditSetting.waiting_for_value)
    await state.update_data(setting_key=key)
    await callback.answer()


# --- Menu Button Toggles ---

async def render_menu_text() -> str:
    lines = ["🔘 <b>Кнопки главного меню</b>", ""]
    for key, text in MENU_BUTTONS:
        val = await get_setting(key)
        is_on = (val == "on") if val is not None else True
        status = "✅" if is_on else "❌"
        lines.append(f"{status} {text}")
    return "\n".join(lines)


async def build_menu_keyboard():
    buttons = []
    for key, text in MENU_BUTTONS:
        val = await get_setting(key)
        is_on = (val == "on") if val is not None else True
        toggle_text = f"{'✅' if is_on else '❌'} {text}"
        buttons.append([InlineKeyboardButton(text=toggle_text, callback_data=f"menu_toggle:{key}")])
    buttons.append([InlineKeyboardButton(text="← Назад к настройкам", callback_data="menu_back")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


@router.callback_query(F.data == "admin_menu_buttons")
async def show_menu_buttons(callback: types.CallbackQuery):
    if callback.from_user.id not in config.ADMIN_IDS:
        await callback.answer("Недостаточно прав", show_alert=True)
        return

    text = await render_menu_text()
    await callback.message.edit_text(text, parse_mode="HTML", reply_markup=await build_menu_keyboard())
    await callback.answer()


@router.callback_query(F.data.startswith("menu_toggle:"))
async def toggle_menu_button(callback: types.CallbackQuery):
    if callback.from_user.id not in config.ADMIN_IDS:
        await callback.answer("Недостаточно прав", show_alert=True)
        return

    key = callback.data.split(":", 1)[1]
    val = await get_setting(key)
    current_on = (val == "on") if val is not None else True

    new_val = "off" if current_on else "on"
    await set_setting(key, new_val)

    label = dict(MENU_BUTTONS).get(key, key)
    status = "✅ Вкл" if new_val == "on" else "❌ Выкл"
    await callback.answer(f"{label}: {status}", show_alert=True)

    text = await render_menu_text()
    await callback.message.edit_text(text, parse_mode="HTML", reply_markup=await build_menu_keyboard())


@router.callback_query(F.data == "menu_back")
async def menu_buttons_back(callback: types.CallbackQuery):
    if callback.from_user.id not in config.ADMIN_IDS:
        await callback.answer("Недостаточно прав", show_alert=True)
        return

    text = await render_settings_text()
    await callback.message.edit_text(text, parse_mode="HTML", reply_markup=await build_settings_keyboard())
    await callback.answer()


# ── Phase 2: application review queue ("Заявки", tinder UI) ───────────────────

def _parse_appr(data: str) -> tuple[str, int | None]:
    """'appr_approve:123' -> ('appr_approve', 123); 'appr_all' -> ('appr_all', None)."""
    if ":" in data:
        prefix, tid = data.split(":", 1)
        try:
            return prefix, int(tid)
        except ValueError:
            return prefix, None
    return data, None


def _render_application_card(user: dict, position: int, total: int) -> str:
    """HTML card for one pending application; all free-text escaped."""
    def esc(v):
        return html_module.escape(str(v)) if v not in (None, "", "-") else None

    lines = [f"📋 <b>Заявка {position}/{total}</b>", ""]
    name = esc(user.get("full_name")) or "—"
    uname = esc(user.get("username"))
    lines.append(f"👤 {name}" + (f" ({uname})" if uname else ""))
    edu = esc(user.get("university")) or esc(user.get("education_status"))
    if edu:
        course = esc(user.get("course"))
        lines.append(f"🎓 {edu}" + (f", {course}" if course else ""))
    if esc(user.get("city")):
        lines.append(f"📍 {esc(user.get('city'))}")
    if esc(user.get("local_committee")):
        lines.append(f"🏢 {esc(user.get('local_committee'))}")
    if esc(user.get("position")):
        lines.append(f"👔 {esc(user.get('position'))}")
    if user.get("age"):
        lines.append(f"🎂 {esc(user.get('age'))}")
    lines.append("📎 Резюме: " + ("загружено" if user.get("resume_file_id") else "нет"))
    return "\n".join(lines)


def _appr_card_kb(tid: int, has_resume: bool, total: int) -> InlineKeyboardMarkup:
    rows = [
        [
            InlineKeyboardButton(text="✅ Одобрить", callback_data=f"appr_approve:{tid}"),
            InlineKeyboardButton(text="❌ Отклонить", callback_data=f"appr_reject:{tid}"),
        ],
    ]
    third = []
    if has_resume:
        third.append(InlineKeyboardButton(text="📎 Резюме", callback_data=f"appr_resume:{tid}"))
    third.append(InlineKeyboardButton(text="⏭ Пропустить", callback_data=f"appr_skip:{tid}"))
    rows.append(third)
    rows.append([InlineKeyboardButton(text=f"✅ Одобрить все ({total})", callback_data="appr_all")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def _show_current_card(target: types.Message, state: FSMContext):
    """Render the oldest non-skipped pending card (DB-driven, restart-safe)."""
    pending = await get_pending_users(limit=50)
    skipped = set((await state.get_data()).get("appr_skipped", []))
    visible = [u for u in pending if u["telegram_id"] not in skipped]
    total = await get_pending_count()
    if not visible:
        await target.answer("✅ Заявок нет.", reply_markup=build_admin_keyboard())
        return
    current = visible[0]
    position = total - len(visible) + 1
    await target.answer(
        _render_application_card(current, position, total),
        parse_mode="HTML",
        reply_markup=_appr_card_kb(current["telegram_id"], bool(current.get("resume_file_id")), total),
    )


@router.callback_query(F.data == "admin_applications")
async def show_applications(callback: types.CallbackQuery, state: FSMContext):
    if callback.from_user.id not in config.ADMIN_IDS:
        await callback.answer("Недостаточно прав", show_alert=True)
        return
    await state.update_data(appr_skipped=[])  # session-only skip set (D-07)
    await callback.answer()
    await _show_current_card(callback.message, state)


@router.callback_query(F.data.startswith("appr_skip:"))
async def appr_skip(callback: types.CallbackQuery, state: FSMContext):
    if callback.from_user.id not in config.ADMIN_IDS:
        await callback.answer("Недостаточно прав", show_alert=True)
        return
    _, tid = _parse_appr(callback.data)
    data = await state.get_data()
    skipped = list(data.get("appr_skipped", []))
    if tid is not None and tid not in skipped:
        skipped.append(tid)
    await state.update_data(appr_skipped=skipped)
    try:
        await callback.message.delete()
    except Exception:
        pass
    await callback.answer("Пропущено")
    await _show_current_card(callback.message, state)


@router.callback_query(F.data.startswith("appr_resume:"))
async def appr_resume(callback: types.CallbackQuery):
    if callback.from_user.id not in config.ADMIN_IDS:
        await callback.answer("Недостаточно прав", show_alert=True)
        return
    _, tid = _parse_appr(callback.data)
    user = await get_user(tid) if tid is not None else None
    if user and user.get("resume_file_id"):
        try:
            await callback.message.answer_document(user["resume_file_id"])
        except Exception as e:
            logger.error(f"Failed to re-send resume for {tid}: {e}")
            await callback.message.answer("Не удалось открыть резюме.")
    elif user and user.get("resume_text"):
        await callback.message.answer(
            f"📄 Резюме (текст):\n\n{html_module.escape(str(user['resume_text']))}",
            parse_mode="HTML",
        )
    else:
        await callback.message.answer("Резюме не приложено.")
    await callback.answer()


@router.callback_query(F.data.startswith("appr_approve:"))
async def appr_approve(callback: types.CallbackQuery, state: FSMContext):
    if callback.from_user.id not in config.ADMIN_IDS:
        await callback.answer("Недостаточно прав", show_alert=True)
        return
    _, tid = _parse_appr(callback.data)
    won = await approve_user_atomic(tid) if tid is not None else False
    if won:
        await approve_user(callback.bot, tid)  # welcome exactly once (D-10)
        await callback.answer("Одобрено")
    else:
        await callback.answer("Уже обработано")
    try:
        await callback.message.delete()
    except Exception:
        pass
    await _show_current_card(callback.message, state)


@router.callback_query(F.data.startswith("appr_reject:"))
async def appr_reject_start(callback: types.CallbackQuery, state: FSMContext):
    if callback.from_user.id not in config.ADMIN_IDS:
        await callback.answer("Недостаточно прав", show_alert=True)
        return
    _, tid = _parse_appr(callback.data)
    await state.update_data(appr_reject_id=tid)
    await callback.message.answer("Укажи причину отклонения:", reply_markup=get_cancel_kb())
    await state.set_state(Approval.reason)
    await callback.answer()


@router.message(Approval.reason, is_admin, F.text.in_({"Отмена", "/cancel"}))
async def appr_reject_cancel(message: types.Message, state: FSMContext):
    await state.set_state(None)
    await message.answer("Отклонение отменено.", reply_markup=ReplyKeyboardRemove())
    await _show_current_card(message, state)


@router.message(Approval.reason, is_admin)
async def appr_reject_reason(message: types.Message, state: FSMContext):
    data = await state.get_data()
    tid = data.get("appr_reject_id")
    reason = message.text or "-"
    ok = await reject_user(tid) if tid is not None else False
    if ok:
        try:
            prefix = await get_setting("reject_text") or "К сожалению, твоя заявка отклонена."
            await message.bot.send_message(
                tid, f"{prefix}\n\n{html_module.escape(reason)}", parse_mode="HTML"
            )
        except Exception as e:
            logger.error(f"Failed to notify rejected user {tid}: {e}")
        await message.answer("Заявка отклонена.", reply_markup=ReplyKeyboardRemove())
    else:
        await message.answer("Заявка уже обработана.", reply_markup=ReplyKeyboardRemove())
    await state.set_state(None)
    await _show_current_card(message, state)


@router.callback_query(F.data == "appr_all")
async def appr_all_confirm(callback: types.CallbackQuery, state: FSMContext):
    if callback.from_user.id not in config.ADMIN_IDS:
        await callback.answer("Недостаточно прав", show_alert=True)
        return
    total = await get_pending_count()
    if total == 0:
        await callback.answer("Заявок нет")
        await _show_current_card(callback.message, state)
        return
    kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="✅ Да", callback_data="appr_all_yes"),
        InlineKeyboardButton(text="❌ Отмена", callback_data="appr_all_no"),
    ]])
    await callback.message.edit_text(f"Одобрить все {total} заявок?", reply_markup=kb)
    await callback.answer()


@router.callback_query(F.data == "appr_all_no")
async def appr_all_no(callback: types.CallbackQuery, state: FSMContext):
    if callback.from_user.id not in config.ADMIN_IDS:
        await callback.answer("Недостаточно прав", show_alert=True)
        return
    await callback.answer("Отменено")
    await _show_current_card(callback.message, state)


async def _welcome_flipped(bot, ids: list):
    """Drain welcome sends for a mass approval, handling Telegram 429 (D-11)."""
    for tid in ids:
        try:
            await approve_user(bot, tid)
        except TelegramRetryAfter as e:
            await asyncio.sleep(e.retry_after + 1)
            try:
                await approve_user(bot, tid)
            except Exception as e2:
                logger.error(f"Mass-approve welcome retry failed for {tid}: {e2}")
        except Exception as e:
            logger.error(f"Mass-approve welcome failed for {tid}: {e}")
        await asyncio.sleep(0.05)


@router.callback_query(F.data == "appr_all_yes")
async def appr_all_yes(callback: types.CallbackQuery, state: FSMContext):
    if callback.from_user.id not in config.ADMIN_IDS:
        await callback.answer("Недостаточно прав", show_alert=True)
        return
    ids = await approve_all_pending()  # atomic flip first (D-11)
    await callback.message.edit_text(
        f"✅ Одобрено: {len(ids)}. Рассылаю приветствия…",
        reply_markup=build_admin_keyboard(),
    )
    asyncio.create_task(_welcome_flipped(callback.bot, ids))  # drain sends in background
    await callback.answer()


# ── Phase 4: receipt verification queue ("Чеки", tinder UI, D-12) ─────────────

def _parse_rcpt(data: str) -> tuple[str, int | None]:
    """'rcpt_confirm:123' -> ('rcpt_confirm', 123)."""
    if ":" in data:
        prefix, uid = data.split(":", 1)
        try:
            return prefix, int(uid)
        except ValueError:
            return prefix, None
    return data, None


def _render_receipt_card(user: dict, position: int, total: int) -> str:
    lines = [f"🧾 <b>Чек {position}/{total}</b>", ""]
    lines.append(f"👤 {html_module.escape(str(user.get('full_name') or '—'))}")
    lines.append(f"💳 Вариант: {html_module.escape(str(user.get('payment_option') or '—'))}")
    lines.append(f"📎 Чек: {'загружен' if user.get('receipt_file_id') else 'нет'}")
    return "\n".join(lines)


def _rcpt_card_kb(uid: int, has_receipt: bool, total: int) -> InlineKeyboardMarkup:
    rows = [[
        InlineKeyboardButton(text="✅ Подтвердить", callback_data=f"rcpt_confirm:{uid}"),
        InlineKeyboardButton(text="❌ Отклонить", callback_data=f"rcpt_reject:{uid}"),
    ]]
    third = []
    if has_receipt:
        third.append(InlineKeyboardButton(text="🧾 Чек", callback_data=f"rcpt_view:{uid}"))
    third.append(InlineKeyboardButton(text="⏭ Следующий", callback_data=f"rcpt_skip:{uid}"))
    rows.append(third)
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def _show_current_receipt_card(target: types.Message, state: FSMContext):
    pending = await get_receipt_pending_users(limit=50)
    skipped = set((await state.get_data()).get("rcpt_skipped", []))
    visible = [u for u in pending if u["telegram_id"] not in skipped]
    total = await get_receipt_pending_count()
    if not visible:
        await target.answer("✅ Чеков на проверке нет.", reply_markup=build_admin_keyboard())
        return
    current = visible[0]
    position = total - len(visible) + 1
    await target.answer(
        _render_receipt_card(current, position, total),
        parse_mode="HTML",
        reply_markup=_rcpt_card_kb(current["telegram_id"], bool(current.get("receipt_file_id")), total),
    )


@router.callback_query(F.data == "admin_receipts")
async def show_receipts(callback: types.CallbackQuery, state: FSMContext):
    if callback.from_user.id not in config.ADMIN_IDS:
        await callback.answer("Недостаточно прав", show_alert=True)
        return
    await state.update_data(rcpt_skipped=[])
    await callback.answer()
    await _show_current_receipt_card(callback.message, state)


@router.callback_query(F.data.startswith("rcpt_confirm:"))
async def rcpt_confirm(callback: types.CallbackQuery, state: FSMContext):
    if callback.from_user.id not in config.ADMIN_IDS:
        await callback.answer("Недостаточно прав", show_alert=True)
        return
    _, uid = _parse_rcpt(callback.data)
    rows = await update_payment_status(uid, "paid") if uid is not None else 0
    if rows == 0:
        # Atomic guard (T-04-05-02): another manager already confirmed.
        await callback.answer("Чек уже обработан.")
        await _show_current_receipt_card(callback.message, state)
        return
    from services.scheduler import cancel_payment_reminders
    cancel_payment_reminders(uid)  # cancel BEFORE notifying — no reminder after paid
    try:
        await callback.bot.send_message(
            uid,
            "✅ <b>Оплата подтверждена!</b>\n\nСпасибо, ваш взнос получен.",
            parse_mode="HTML",
            reply_markup=await get_main_menu_kb(uid),  # first menu after the payment journey
        )
        # WR-04: payment-confirm must mirror the non-payment approval path — deliver the
        # configured completion text + registration bonus. Menu already sent above.
        from handlers.registration import send_completion_and_bonus
        await send_completion_and_bonus(callback.bot, uid, with_menu=False)
    except Exception as e:
        logger.error(f"Failed to notify user {uid} of payment confirmation: {e}")
    await callback.answer("Оплата подтверждена")
    await _show_current_receipt_card(callback.message, state)


@router.callback_query(F.data.startswith("rcpt_reject:"))
async def rcpt_reject_start(callback: types.CallbackQuery, state: FSMContext):
    if callback.from_user.id not in config.ADMIN_IDS:
        await callback.answer("Недостаточно прав", show_alert=True)
        return
    _, uid = _parse_rcpt(callback.data)
    await state.update_data(rcpt_reject_uid=uid)
    await state.set_state(ReceiptReview.reject_reason)
    await callback.message.answer("Укажи причину отклонения (или «-» без объяснений):", reply_markup=get_cancel_kb())
    await callback.answer()


@router.message(ReceiptReview.reject_reason, is_admin, F.text.in_({"Отмена", "/cancel"}))
async def rcpt_reject_cancel(message: types.Message, state: FSMContext):
    await state.set_state(None)
    await message.answer("Отклонение отменено.", reply_markup=ReplyKeyboardRemove())
    await _show_current_receipt_card(message, state)


@router.message(ReceiptReview.reject_reason, is_admin)
async def rcpt_reject_reason(message: types.Message, state: FSMContext):
    data = await state.get_data()
    uid = data.get("rcpt_reject_uid")
    if uid is not None:
        await update_payment_status(uid, "not_paid")  # reset → user can re-upload
        reason_text = (message.text or "").strip()
        user_msg = "❌ Чек отклонён."
        if reason_text and reason_text != "-":
            user_msg += f" Причина: {html_module.escape(reason_text)}"
        user_msg += "\n\nЗагрузи чек повторно через бота."
        try:
            await message.bot.send_message(uid, user_msg, parse_mode="HTML")
        except Exception as e:
            logger.error(f"Failed to notify user {uid} of receipt rejection: {e}")
    await state.set_state(None)
    await message.answer("Готово.", reply_markup=ReplyKeyboardRemove())
    await _show_current_receipt_card(message, state)


@router.callback_query(F.data.startswith("rcpt_skip:"))
async def rcpt_skip(callback: types.CallbackQuery, state: FSMContext):
    if callback.from_user.id not in config.ADMIN_IDS:
        await callback.answer("Недостаточно прав", show_alert=True)
        return
    _, uid = _parse_rcpt(callback.data)
    data = await state.get_data()
    skipped = list(data.get("rcpt_skipped", []))
    if uid is not None and uid not in skipped:
        skipped.append(uid)
    await state.update_data(rcpt_skipped=skipped)
    try:
        await callback.message.delete()
    except Exception:
        pass
    await callback.answer()
    await _show_current_receipt_card(callback.message, state)


@router.callback_query(F.data.startswith("rcpt_view:"))
async def rcpt_view(callback: types.CallbackQuery):
    if callback.from_user.id not in config.ADMIN_IDS:
        await callback.answer("Недостаточно прав", show_alert=True)
        return
    _, uid = _parse_rcpt(callback.data)
    user = await get_user(uid) if uid is not None else None
    if user and user.get("receipt_file_id"):
        file_id = user["receipt_file_id"]
        try:
            await callback.message.answer_document(file_id, caption=f"Чек пользователя {uid}")
        except Exception:
            # receipt may be a photo file_id, not a document — fall back to photo send
            try:
                await callback.message.answer_photo(file_id, caption=f"Чек пользователя {uid}")
            except Exception as e:
                logger.error(f"Failed to show receipt for {uid}: {e}")
                await callback.answer("Не удалось открыть чек.", show_alert=True)
                return
        await callback.answer()
    else:
        await callback.answer("Чек не найден.", show_alert=True)


# ── Phase 2: self-documenting settings command (D-16) ────────────────────────

APPROVAL_SETTINGS_DOC = [
    ("registration_mode", "Режим формы регистрации (full/short)", "short"),
    ("short_approval", "Модерация короткой формы (auto/manual)", "auto"),
    ("full_approval", "Модерация полной формы (auto/manual)", "manual"),
    ("reject_text", "Текст пользователю при отклонении заявки", "(стандартный текст)"),
    ("pending_notify_mode", "Уведомление о новой заявке: instant/batched", "batched"),
    ("pending_reminder_enabled", "Периодическая напоминалка о заявках (on/off)", "on"),
    ("pending_reminder_interval", "Интервал напоминалки, сек", "1800"),
    ("reg_q_resume", "Запрос резюме в полной форме (on/off)", "off"),
    # Phase 3 — dropout nudge (SCHED-03)
    ("nudge_enabled", "Авто-напоминание о дорегистрации (on/off)", "on"),
    ("nudge_after_minutes", "Через сколько минут бездействия слать напоминание", "120"),
    ("nudge_scan_minutes", "Период сканирования дропаутов, мин (вступает в силу после перезапуска)", "15"),
    ("nudge_text", "Текст напоминания о дорегистрации", "(стандартный)"),
    # Phase 3 — pre-selection gate (VERIF-01/02)
    ("preselect_enabled", "Предотбор по Google-таблице (on/off)", "off"),
    ("preselect_tab", "Название вкладки со списком отобранных", "Отобранные"),
    ("preselect_link", "Ссылка для не прошедших отбор", "(нет)"),
    ("preselect_fail_text", "Текст не прошедшим отбор", "Отбор не пройден."),
    ("preselect_no_username_text", "Текст пользователю без @username", "(стандартный)"),
    ("preselect_manual_ids", "Ручной allowlist по telegram_id (CSV)", "(пусто)"),
    ("allowlist_refresh_minutes", "Период обновления allowlist, мин", "60"),
]


def _render_settings_guide(rows: list, current: dict) -> str:
    out = ["⚙️ <b>Настройки модерации</b>", ""]
    for key, desc, default in rows:
        val = current.get(key)
        shown = val if val is not None else f"{default} (по умолчанию)"
        out.append(f"<b>{key}</b> — {html_module.escape(desc)}\nТекущее: {html_module.escape(str(shown))}")
        out.append("")
    return "\n".join(out).rstrip()


@router.message(Command("settings_guide"), is_admin)
async def cmd_settings_guide(message: types.Message):
    current = {key: await get_setting(key) for key, _, _ in APPROVAL_SETTINGS_DOC}
    await message.answer(_render_settings_guide(APPROVAL_SETTINGS_DOC, current), parse_mode="HTML")


@router.callback_query(F.data == "admin_settings_guide")
async def show_admin_settings_guide(callback: types.CallbackQuery):
    if callback.from_user.id not in config.ADMIN_IDS:
        await callback.answer("Недостаточно прав", show_alert=True)
        return
    current = {key: await get_setting(key) for key, _, _ in APPROVAL_SETTINGS_DOC}
    await callback.message.answer(
        _render_settings_guide(APPROVAL_SETTINGS_DOC, current), parse_mode="HTML"
    )
    await callback.answer()
