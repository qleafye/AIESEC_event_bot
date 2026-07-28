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
from settings_schema import SETTINGS_SCHEMA, get_setting_typed  # REG-02/REG-03: registry + typed accessor
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
    get_incomplete_rows,
    get_dropout_step_stats,
    get_pending_users,
    get_pending_count,
    approve_user_atomic,
    reject_user,
    approve_all_pending,
    create_scheduled_broadcast,
    list_pending_broadcasts,
    cancel_scheduled_broadcast,
    count_and_list_filtered,
    get_distinct_filter_values,
    get_receipt_pending_users,
    get_receipt_pending_count,
    update_payment_status,
)
from aiogram.exceptions import TelegramRetryAfter
from services.sheets import get_existing_sheet_ids, append_rows_to_sheet, ensure_sheet_header, sync_named_worksheet, dedupe_sheet_by_id, update_status_in_sheet, bulk_update_status_in_sheet, rebuild_main_sheet
from services.scheduler import (
    _parse_schedule_dt,
    _fmt_dt,
    schedule_broadcast_job,
    cancel_broadcast_job,
)
from services.allowlist import refresh_allowlist, allowlist_size
from services.background import spawn as _spawn
from handlers.states import Broadcast, EditSetting, Approval, ReceiptReview
from keyboards.builders import get_cancel_kb, MENU_BUTTONS, get_main_menu_kb
from handlers.registration import REG_FLOW, REG_DEFAULTS, REG_LABELS, REG_PRESETS, REG_CATEGORIES, SHEET_HEADERS, STATUS_LABELS, _build_sheet_row, active_sheet_headers, set_sheet_schema, _sheet_value_map, approve_user, dropout_step_label, _apply_party_preset, incomplete_sheet_headers, incomplete_sheet_row

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
    token = (token or "").strip()
    if not token:  # IN-03: check emptiness AFTER strip so a whitespace-only token can't IndexError
        return None
    body = token[1:] if token[0] in "+-" else token
    if not (body.isascii() and body.isdigit()):
        return None
    value = int(body)
    return -value if token[0] == "-" else value


def build_admin_keyboard():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📊 Статистика регистраций", callback_data="admin_stats")],
        [InlineKeyboardButton(text="🗓 Регистрации по месяцам", callback_data="admin_monthly_stats")],
        [InlineKeyboardButton(text="📈 Источники", callback_data="admin_source_stats")],
        [InlineKeyboardButton(text="📄 Экспорт CSV", callback_data="admin_export_csv")],
        [InlineKeyboardButton(text="📝 Незавершённые → таблица", callback_data="admin_export_incomplete")],
        [InlineKeyboardButton(text="📋 Заявки", callback_data="admin_applications")],
        [InlineKeyboardButton(text="🧾 Чеки", callback_data="admin_receipts")],
        [InlineKeyboardButton(text="📢 Рассылка", callback_data="admin_broadcast")],
        [InlineKeyboardButton(text="🔄 Синхронизация таблицы", callback_data="admin_sync_sheet")],
        [InlineKeyboardButton(text="♻️ Пересобрать таблицу", callback_data="admin_rebuild_sheet")],
        [InlineKeyboardButton(text="🧹 Убрать дубли из таблицы", callback_data="admin_dedupe_sheet")],
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
            f"Имя: {html_module.escape(str(user['full_name'] or ''))}\n"
            f"Username: {html_module.escape(str(user['username'] or ''))}\n"
            f"Email: {html_module.escape(str(user['email'] or ''))}\n"
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
        text += f"{i}. {html_module.escape(str(uni))} — {count}\n"

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
        text += f"{i}. {html_module.escape(str(uni))} — {count}\n"

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
            lines.append(f"• {html_module.escape(str(source))} — {count}")
        text = "\n".join(lines)

    await callback.message.edit_text(text, parse_mode="HTML", reply_markup=build_admin_keyboard())
    await callback.answer()


# --- Settings ---

# REG-03: the event-group text/enum entries are GENERATED from settings_schema.SETTINGS_SCHEMA
# (single source of truth, D-13) instead of hand-written literals. Order is pinned explicitly
# (not a dict-order assumption) so the settings screen stays byte-identical to the
# pre-registry literal table. Remaining (unmigrated) groups below stay literal tuples — no
# change — until their own migration wave (coexistence invariant, SC#3).
_EVENT_FIELD_ORDER = [
    "event_date", "event_time", "event_place_name", "event_place_address",
    "contact_person", "contact_vk", "contact_tg", "start_text", "event_name", "event_type",
]
_EVENT_FIELDS = [
    (k, SETTINGS_SCHEMA[k]["label"], SETTINGS_SCHEMA[k]["prompt"])
    for k in _EVENT_FIELD_ORDER
]

# REG-01/REG-03 (06-02): reg/pay/party/consent groups GENERATED from settings_schema.
# SETTINGS_SCHEMA (same computed-view splice as the event pilot, D-13) — every text/list/
# int/date key that used to be a hand-written SETTINGS_FIELDS tuple now lives ONLY in the
# registry; order is pinned per group (not registry dict-insertion order) so the on-screen
# order stays byte-identical to the pre-migration literal tables.
_REG_FIELD_ORDER = [
    "source_options", "reg_complete_text", "approve_text", "reject_text",
    "pending_reminder_interval", "city_options", "study_field_options",
    "goal_options", "formats_options", "university_options",
    "short_sheet_tab",  # Phase 7 (SHORT-02): краткая-форма вкладка Google-таблицы
]
_PAY_FIELD_ORDER = [
    "payment_options", "payment_requisites", "payment_requisites_by_lc",
    "payment_deadline", "payment_reminder_text", "payment_overdue_text", "penalty_schedule",
]
_PARTY_FIELD_ORDER = [
    "party_closed_text", "party_sheet_tab", "approve_text__party",
]
_CONSENT_FIELD_ORDER = [
    "consent_button_text", "consent_list",
]

_REG_FIELDS = [(k, SETTINGS_SCHEMA[k]["label"], SETTINGS_SCHEMA[k]["prompt"]) for k in _REG_FIELD_ORDER]
_PAY_FIELDS = [(k, SETTINGS_SCHEMA[k]["label"], SETTINGS_SCHEMA[k]["prompt"]) for k in _PAY_FIELD_ORDER]
_PARTY_FIELDS = [(k, SETTINGS_SCHEMA[k]["label"], SETTINGS_SCHEMA[k]["prompt"]) for k in _PARTY_FIELD_ORDER]
_CONSENT_FIELDS = [(k, SETTINGS_SCHEMA[k]["label"], SETTINGS_SCHEMA[k]["prompt"]) for k in _CONSENT_FIELD_ORDER]

# NOTE: reg_university_mode и edu_conditional вынесены в кнопки-переключатели (build_settings_keyboard).
# PDF согласий грузятся в разделе «🧾 PDF согласий».
# Phase 5 (D-11a/D-13): party-track text settings (party_enabled/party_fork_question/
# party_approval are toggle buttons in build_settings_keyboard, not here).
SETTINGS_FIELDS = _EVENT_FIELDS + _REG_FIELDS + _PAY_FIELDS + _PARTY_FIELDS + _CONSENT_FIELDS

# Phase 5 (D-11a): default text shown in render_settings_text when a text setting is unset,
# so the manager sees what users actually receive today, not a bare "не указано". REG-01
# (06-02): derived from the registry `default` field instead of a hand-written literal dict
# (T-06-06) — restricted to `type == "text"` entries with a genuinely non-empty default so a
# functional parse-fallback default (e.g. pending_reminder_interval's int default 1800) is
# never mistaken for a display default.
_SETTINGS_DISPLAY_DEFAULTS = {
    k: v["default"] for k, v in SETTINGS_SCHEMA.items()
    if v["type"] == "text" and v.get("default") not in (None, "")
}

# Quick 260724-c0x: group→keys grouping (NOT a per-key metadata registry) so the settings
# landing screen can route into per-group sub-screens instead of dumping every field's value
# inline. Shape mirrors REG_CATEGORIES (handlers/registration.py) — (label, token, [keys]).
# REG-03: the "event" row's key list is generated from SETTINGS_SCHEMA (registry is the
# source, D-13) — same pinned _EVENT_FIELD_ORDER used to build _EVENT_FIELDS above, filtered
# to the text/enum keys (photo/file keys are handled separately by the event branch in
# render_settings_group_text/build_settings_group_keyboard via PHOTO_FIELDS/FILE_FIELDS,
# unchanged per D-10).
_EVENT_GROUP_KEYS = [
    k for k in _EVENT_FIELD_ORDER
    if SETTINGS_SCHEMA[k]["type"] in ("text", "enum")
]

SETTINGS_GROUPS = [
    ("🎪 Событие/Медиа", "event", _EVENT_GROUP_KEYS),
    ("📝 Регистрация", "reg", _REG_FIELD_ORDER),
    ("💳 Оплата", "pay", _PAY_FIELD_ORDER),
    ("🎉 Party", "party", _PARTY_FIELD_ORDER),
    ("📋 Согласия", "consent", _CONSENT_FIELD_ORDER),
]


def _settings_group_keys(token: str) -> list[str]:
    """Keys for a given SETTINGS_GROUPS token, including leftover-safety: any SETTINGS_FIELDS
    key not placed in a declared group lands in the trailing «Прочие»/"misc" group so nothing
    is ever silently hidden (mirrors _categorized_question_keys leftover handling)."""
    for _, tok, keys in SETTINGS_GROUPS:
        if tok == token:
            return list(keys)
    if token == "misc":
        seen = {k for _, __, keys in SETTINGS_GROUPS for k in keys}
        return [k for k, _, _ in SETTINGS_FIELDS if k not in seen]
    return []


def _settings_group_label(token: str) -> str:
    for label, tok, _ in SETTINGS_GROUPS:
        if tok == token:
            return label
    if token == "misc":
        return "📦 Прочие"
    return token


def _settings_nav_groups() -> list[tuple[str, str]]:
    """(label, token) rows for the landing keyboard nav buttons — declared groups plus a
    trailing «Прочие» group ONLY if leftover keys exist."""
    rows = [(label, tok) for label, tok, _ in SETTINGS_GROUPS]
    if _settings_group_keys("misc"):
        rows.append(("📦 Прочие", "misc"))
    return rows

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

    # REG-02 (06-07): final-coverage sweep — closes the 06-05-flagged boundary; byte-
    # identical to the prior `get_setting(k) or "<literal>"` idiom (enum falsy->default).
    reg_mode = await get_setting_typed("registration_mode")
    mode_label = "📋 Полная" if reg_mode == "full" else "⚡ Краткая"
    lines.append(f"📝 Форма регистрации: <b>{mode_label}</b>")

    # REG-02 (06-05): feature-switch reads resolved via the registry's enum default,
    # byte-identical to the prior `get_setting(k) or "<literal>"` idiom (get_setting_typed's
    # enum branch is `raw if raw else default`, matching falsy-to-default on empty-string).
    bonus_enabled = await get_setting_typed("reg_bonus_enabled")
    bonus_label = "✅ Вкл" if bonus_enabled == "on" else "❌ Выкл"
    lines.append(f"🎁 Бонус за регистрацию: <b>{bonus_label}</b>")

    full_appr = await get_setting_typed("full_approval")
    short_appr = await get_setting_typed("short_approval")
    notify_mode = await get_setting_typed("pending_notify_mode")
    appr_lbl = lambda v: "👮 Ручная" if v == "manual" else "⚡ Авто"
    lines.append(f"✅ Модерация полной формы: <b>{appr_lbl(full_appr)}</b>")
    lines.append(f"✅ Модерация краткой формы: <b>{appr_lbl(short_appr)}</b>")
    notify_lbl = "📨 Сразу" if notify_mode == "instant" else "🕒 Пачкой (напоминалка)"
    lines.append(f"🔔 Уведомление о заявке: <b>{notify_lbl}</b>")

    payment_enabled = await get_setting_typed("payment_enabled")
    consent_enabled = await get_setting_typed("consent_enabled")
    lines.append(f"💳 Модуль оплаты: <b>{'✅ Вкл' if payment_enabled == 'on' else '❌ Выкл'}</b>")
    lines.append(f"📋 Модуль согласий: <b>{'✅ Вкл' if consent_enabled == 'on' else '❌ Выкл'}</b>")
    pay_rem_enabled = await get_setting_typed("payment_reminders_enabled")
    lines.append(f"⏰ Автонапоминания об оплате: <b>{'✅ Вкл' if pay_rem_enabled == 'on' else '❌ Выкл'}</b>")

    # Phase 5 (D-13): party settings always read as off/manual when unset — new-capability
    # default-OFF posture (Phase-4 D-15 lineage), independent of full_approval/short_approval.
    party_enabled = await get_setting_typed("party_enabled")
    party_fork_question = await get_setting_typed("party_fork_question")
    party_approval = await get_setting_typed("party_approval")
    lines.append(f"🎉 Трек вечеринки: <b>{'✅ Вкл' if party_enabled == 'on' else '❌ Выкл'}</b>")
    lines.append(f"🔀 Вопрос-развилка формата: <b>{'✅ Вкл' if party_fork_question == 'on' else '❌ Выкл'}</b>")
    lines.append(f"✅ Модерация вечеринки: <b>{appr_lbl(party_approval)}</b>")

    enabled_q = 0
    for _, sk, *_rest in REG_FLOW:
        # REG-02 (06-04): resolves via the registry's toggle default, byte-identical to the
        # prior REG_DEFAULTS.get(sk, "on") == "on" fallback (get_setting_typed's toggle
        # branch is (raw == "on") if raw is not None else (default == "on")).
        is_on = await get_setting_typed(sk)
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

    lines.append("✏️ Тексты и медиа — по кнопкам групп ниже.")

    lines.append("")
    lines.append("<i>Отправьте «-» при редактировании текстовых полей, чтобы скрыть.</i>")
    return "\n".join(lines)


async def build_settings_keyboard():
    # REG-02 (06-05): feature-switch reads resolved via the registry's enum default,
    # byte-identical to the prior `get_setting(k) or "<literal>"` idiom — button TEXT
    # ternaries and callback_data strings are intentionally untouched (D-12).
    reg_mode = await get_setting_typed("registration_mode")
    toggle_text = "📝 Регистрация: ⚡ Краткая → 📋 Полная" if reg_mode == "short" else "📝 Регистрация: 📋 Полная → ⚡ Краткая"

    bonus_enabled = await get_setting_typed("reg_bonus_enabled")
    bonus_toggle_text = "🎁 Бонус: ❌ Выкл → ✅ Вкл" if bonus_enabled == "off" else "🎁 Бонус: ✅ Вкл → ❌ Выкл"

    full_appr = await get_setting_typed("full_approval")
    short_appr = await get_setting_typed("short_approval")
    notify_mode = await get_setting_typed("pending_notify_mode")
    full_txt = "✅ Полная форма: 👮 Ручная → ⚡ Авто" if full_appr == "manual" else "✅ Полная форма: ⚡ Авто → 👮 Ручная"
    short_txt = "✅ Краткая форма: 👮 Ручная → ⚡ Авто" if short_appr == "manual" else "✅ Краткая форма: ⚡ Авто → 👮 Ручная"
    notify_txt = "🔔 Уведомление: 📨 Сразу → 🕒 Пачкой" if notify_mode == "instant" else "🔔 Уведомление: 🕒 Пачкой → 📨 Сразу"

    payment_enabled = await get_setting_typed("payment_enabled")
    consent_enabled = await get_setting_typed("consent_enabled")
    payment_toggle_text = "💳 Оплата: ❌ Выкл → ✅ Вкл" if payment_enabled != "on" else "💳 Оплата: ✅ Вкл → ❌ Выкл"
    consent_toggle_text = "📋 Согласия: ❌ Выкл → ✅ Вкл" if consent_enabled != "on" else "📋 Согласия: ✅ Вкл → ❌ Выкл"
    pay_rem_enabled = await get_setting_typed("payment_reminders_enabled")
    pay_rem_toggle_text = ("⏰ Автонапоминания оплаты: ✅ Вкл → ❌ Выкл" if pay_rem_enabled == "on"
                           else "⏰ Автонапоминания оплаты: ❌ Выкл → ✅ Вкл")

    uni_mode = await get_setting_typed("reg_university_mode")
    uni_mode_text = ("🏫 ВУЗ: выбор из списка → свободный ввод" if uni_mode == "list"
                     else "🏫 ВУЗ: свободный ввод → выбор из списка")
    edu_cond = await get_setting_typed("edu_conditional")
    edu_cond_text = ("🎓 ВУЗ/курс только у студентов: ✅ Вкл → ❌ Выкл" if edu_cond == "on"
                     else "🎓 ВУЗ/курс только у студентов: ❌ Выкл → ✅ Вкл")
    show_progress = await get_setting_typed("reg_show_progress")
    show_progress_text = ("🔢 Нумерация вопросов: ✅ Вкл → ❌ Выкл" if show_progress == "on"
                          else "🔢 Нумерация вопросов: ❌ Выкл → ✅ Вкл")

    # Phase 5 (D-13): party_enabled / party_fork_question default OFF; party_approval
    # default "manual" — resolved via the registry's enum default (REG-02, 06-05).
    party_enabled = await get_setting_typed("party_enabled")
    party_toggle_text = ("🎉 Трек вечеринки: ❌ Выкл → ✅ Вкл" if party_enabled != "on"
                         else "🎉 Трек вечеринки: ✅ Вкл → ❌ Выкл")
    party_fork_question = await get_setting_typed("party_fork_question")
    party_fork_toggle_text = ("🔀 Вопрос-развилка формата: ❌ Выкл → ✅ Вкл" if party_fork_question != "on"
                              else "🔀 Вопрос-развилка формата: ✅ Вкл → ❌ Выкл")
    party_approval = await get_setting_typed("party_approval")
    party_appr_txt = ("✅ Модерация вечеринки: 👮 Ручная → ⚡ Авто" if party_approval == "manual"
                      else "✅ Модерация вечеринки: ⚡ Авто → 👮 Ручная")

    buttons = [
        [InlineKeyboardButton(text=toggle_text, callback_data="settings_toggle_reg")],
        [InlineKeyboardButton(text=bonus_toggle_text, callback_data="settings_toggle_bonus")],
        [InlineKeyboardButton(text=full_txt, callback_data="settings_toggle_full_approval")],
        [InlineKeyboardButton(text=short_txt, callback_data="settings_toggle_short_approval")],
        [InlineKeyboardButton(text=notify_txt, callback_data="settings_toggle_notify")],
        [InlineKeyboardButton(text=payment_toggle_text, callback_data="toggle_payment_enabled")],
        [InlineKeyboardButton(text=pay_rem_toggle_text, callback_data="toggle_payment_reminders")],
        [InlineKeyboardButton(text=consent_toggle_text, callback_data="toggle_consent_enabled")],
        [InlineKeyboardButton(text="🧾 PDF согласий", callback_data="admin_consent_pdfs")],
        [InlineKeyboardButton(text=uni_mode_text, callback_data="toggle_uni_mode")],
        [InlineKeyboardButton(text=edu_cond_text, callback_data="toggle_edu_conditional")],
        [InlineKeyboardButton(text=show_progress_text, callback_data="toggle_show_progress")],
        [InlineKeyboardButton(text=party_toggle_text, callback_data="toggle_party_enabled")],
        [InlineKeyboardButton(text=party_fork_toggle_text, callback_data="toggle_party_fork_question")],
        [InlineKeyboardButton(text=party_appr_txt, callback_data="settings_toggle_party_approval")],
        [InlineKeyboardButton(text="🎛 Тип события (пресет)", callback_data="admin_event_preset")],
        [InlineKeyboardButton(text="📋 Вопросы регистрации", callback_data="admin_reg_questions")],
        [InlineKeyboardButton(text="✏️ Тексты вопросов", callback_data="admin_reg_prompts")],
        [InlineKeyboardButton(text="🔘 Кнопки меню", callback_data="admin_menu_buttons")],
    ]
    for label, token in _settings_nav_groups():
        buttons.append([InlineKeyboardButton(text=label, callback_data=f"settings_group:{token}")])
    buttons.append([InlineKeyboardButton(text="← Назад", callback_data="settings_back")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


async def render_settings_group_text(token: str) -> str:
    """Quick 260724-c0x: per-group sub-screen — status FLAGS only («задано»/«не задано»/
    «по умолчанию»), never the raw value inline (that stays behind the existing
    settings_edit tap-through, unchanged)."""
    group_label = _settings_group_label(token)
    lines = [f"⚙️ <b>Настройки → {group_label}</b>", ""]

    field_labels = {k: lbl for k, lbl, _ in SETTINGS_FIELDS}
    for key in _settings_group_keys(token):
        label = field_labels.get(key, key)
        value = await get_setting(key)
        if value:
            flag = "✏️ задано"
        elif key in _SETTINGS_DISPLAY_DEFAULTS:
            flag = "<i>по умолчанию</i>"
        else:
            flag = "<i>— не задано</i>"
        lines.append(f"{label}: {flag}")

    if token == "event":
        for prefix, label, _ in PHOTO_FIELDS:
            photo = await get_setting(f"{prefix}_photo_file_id")
            lines.append(f"{label}: {'✅ загружена' if photo else '<i>— не задано</i>'}")
        for prefix, label, _ in FILE_FIELDS:
            photo = await get_setting(f"{prefix}_photo_file_id")
            doc = await get_setting(f"{prefix}_doc_file_id")
            lines.append(f"{label}: {'✅ загружен' if (photo or doc) else '<i>— не задано</i>'}")

    return "\n".join(lines)


async def build_settings_group_keyboard(token: str):
    """Reuses the existing settings_edit/settings_photo/settings_file callbacks unchanged —
    only the button placement changes. Configured fields first, then a noop section-header
    button (req #2: collapse unconfigured fields), then unconfigured fields."""
    field_labels = {k: lbl for k, lbl, _ in SETTINGS_FIELDS}
    configured: list[InlineKeyboardButton] = []
    unconfigured: list[InlineKeyboardButton] = []

    for key in _settings_group_keys(token):
        label = field_labels.get(key, key)
        btn = InlineKeyboardButton(text=f"✏️ {label}", callback_data=f"settings_edit:{key}")
        value = await get_setting(key)
        (configured if value else unconfigured).append(btn)

    if token == "event":
        for prefix, label, _ in PHOTO_FIELDS:
            btn = InlineKeyboardButton(text=f"📷 {label}", callback_data=f"settings_photo:{prefix}")
            photo = await get_setting(f"{prefix}_photo_file_id")
            (configured if photo else unconfigured).append(btn)
        for prefix, label, _ in FILE_FIELDS:
            btn = InlineKeyboardButton(text=f"📎 {label}", callback_data=f"settings_file:{prefix}")
            photo = await get_setting(f"{prefix}_photo_file_id")
            doc = await get_setting(f"{prefix}_doc_file_id")
            (configured if (photo or doc) else unconfigured).append(btn)

    buttons = [[b] for b in configured]
    if unconfigured:
        buttons.append([InlineKeyboardButton(text="── не настроено ──", callback_data="settings_group_noop")])
        buttons.extend([[b] for b in unconfigured])
    buttons.append([InlineKeyboardButton(text="← Назад к настройкам", callback_data="admin_settings")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


@router.callback_query(F.data == "admin_settings")
async def show_admin_settings(callback: types.CallbackQuery):
    if callback.from_user.id not in config.ADMIN_IDS:
        await callback.answer("Недостаточно прав", show_alert=True)
        return

    text = await render_settings_text()
    await callback.message.edit_text(text, parse_mode="HTML", reply_markup=await build_settings_keyboard())
    await callback.answer()


@router.callback_query(F.data.startswith("settings_group:"))
async def show_settings_group(callback: types.CallbackQuery):
    if callback.from_user.id not in config.ADMIN_IDS:
        await callback.answer("Недостаточно прав", show_alert=True)
        return

    token = callback.data.split(":", 1)[1]
    text = await render_settings_group_text(token)
    await callback.message.edit_text(text, parse_mode="HTML", reply_markup=await build_settings_group_keyboard(token))
    await callback.answer()


@router.callback_query(F.data == "settings_group_noop")
async def settings_group_noop(callback: types.CallbackQuery):
    # Section-header button in the collapsed «не настроено» view — not actionable.
    await callback.answer()


@router.callback_query(F.data == "settings_toggle_reg")
async def toggle_registration_mode(callback: types.CallbackQuery):
    if callback.from_user.id not in config.ADMIN_IDS:
        await callback.answer("Недостаточно прав", show_alert=True)
        return

    # REG-02 (06-05): current-value read migrated to the registry; write path unchanged.
    current = await get_setting_typed("registration_mode")
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
    # REG-02 (06-07): final-coverage sweep — key is always in SETTINGS_SCHEMA (full_approval/
    # short_approval/party_approval), registry default byte-identical to the `default` param.
    current = await get_setting_typed(key)
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


@router.callback_query(F.data == "settings_toggle_party_approval")
async def toggle_party_approval(callback: types.CallbackQuery):
    # D-13: independent setting — never reads/writes/derives from full_approval or
    # short_approval, no fallback chain between them.
    await _toggle_approval_setting(callback, "party_approval", "manual", "Модерация вечеринки")


# ── Phase 4: module on/off toggles (payment, consent) + event-type preset ────

async def _toggle_module_setting(callback: types.CallbackQuery, key: str, title: str):
    """On/off toggle for a Phase 4 module flag (fail-safe default OFF, D-15)."""
    if callback.from_user.id not in config.ADMIN_IDS:
        await callback.answer("Недостаточно прав", show_alert=True)
        return
    # REG-02 (06-07): final-coverage sweep — key is always in SETTINGS_SCHEMA
    # (payment_enabled/consent_enabled/party_enabled/party_fork_question), all default "off".
    current = await get_setting_typed(key)
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


@router.callback_query(F.data == "toggle_party_enabled")
async def toggle_party_enabled(callback: types.CallbackQuery):
    # D-11a master gate: default OFF (fail-safe, Phase-4 D-15 lineage).
    await _toggle_module_setting(callback, "party_enabled", "🎉 Трек вечеринки")


@router.callback_query(F.data == "toggle_party_fork_question")
async def toggle_party_fork_question(callback: types.CallbackQuery):
    # D-10: default OFF — an ordinary delegate sees no extra screen until an admin opts in.
    await _toggle_module_setting(callback, "party_fork_question", "🔀 Вопрос-развилка формата")


@router.callback_query(F.data == "toggle_payment_reminders")
async def toggle_payment_reminders(callback: types.CallbackQuery):
    # Default ON — preserves prior behaviour (reminders fired whenever a deadline was set).
    await _toggle_value_setting(
        callback, "payment_reminders_enabled", "on", "off", "on",
        "⏰ Автонапоминания об оплате включены", "⏰ Автонапоминания об оплате выключены",
    )


async def _toggle_value_setting(callback, key, val_a, val_b, default, title_a, title_b):
    """Generic two-value toggle (e.g. list↔text, on↔off) with a friendly alert."""
    if callback.from_user.id not in config.ADMIN_IDS:
        await callback.answer("Недостаточно прав", show_alert=True)
        return
    # REG-02 (06-07): final-coverage sweep — every key routed through this helper
    # (reg_university_mode/edu_conditional/reg_show_progress/payment_reminders_enabled) is
    # in SETTINGS_SCHEMA with a registry default byte-identical to the `default` param.
    current = await get_setting_typed(key)
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
    # REG-02 (06-05): current-value read migrated to the registry; write path unchanged.
    current = await get_setting_typed("pending_notify_mode")
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

    # REG-02 (06-05): current-value read migrated to the registry; write path unchanged.
    current = await get_setting_typed("reg_bonus_enabled")
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
    # WR-01: callbacks aren't covered by the message-level is_admin filter — re-check (D-06).
    if callback.from_user.id not in config.ADMIN_IDS:
        await callback.answer("Недостаточно прав", show_alert=True)
        return
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
        headers = await active_sheet_headers()  # only enabled columns
        await ensure_sheet_header(headers)  # шапка таблицы, если её ещё нет
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

        # Align each row to the active header order so columns match the sheet exactly.
        rows = [[_sheet_value_map(u).get(h, "-") for h in headers] for u in missing]
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


@router.callback_query(F.data == "admin_rebuild_sheet")
async def rebuild_sheet(callback: types.CallbackQuery):
    """Полная пересборка листа данных: перезаписать шапку + ВСЕ строки в текущем порядке
    колонок, применить выпадашку/цвета к «Статус». Выравнивает старые строки после смены
    порядка колонок (Таня п.1/п.5). Внимание: перезаписывает ручные правки на листе."""
    if callback.from_user.id not in config.ADMIN_IDS:
        await callback.answer("Недостаточно прав", show_alert=True)
        return

    await callback.answer("♻️ Пересборка...")
    await callback.message.edit_text("♻️ Пересобираю таблицу (перезапись всех строк)…", parse_mode="HTML")

    try:
        headers = await active_sheet_headers()  # only enabled columns
        all_users = await get_all_users_dicts()
        rows = [[_sheet_value_map(u).get(h, "-") for h in headers] for u in all_users]
        count = await rebuild_main_sheet(headers, rows)
        if count < 0:
            await callback.message.edit_text(
                "❌ Пересборка не выполнена (таблица не настроена или ошибка API). Смотри логи.",
                parse_mode="HTML",
                reply_markup=build_admin_keyboard(),
            )
            return
        # CR-9: rebuild is the re-sync point — freeze the snapshot to the header just written
        # so subsequent registrations align to the rebuilt physical header.
        await set_sheet_schema(headers)
        await callback.message.edit_text(
            f"✅ Таблица пересобрана!\n\n"
            f"Строк записано: <b>{count}</b>\n"
            f"Колонки выстроены в порядке анкеты, «Статус» с выпадашкой и цветами.",
            parse_mode="HTML",
            reply_markup=build_admin_keyboard(),
        )
    except Exception as e:
        logger.error(f"Sheet rebuild failed: {e}")
        await callback.message.edit_text(
            f"❌ Ошибка пересборки:\n<code>{html_module.escape(str(e))}</code>",
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
    # ';' works as a separator too — see _consent_entries in registration.py: the
    # Telegram Enter=send trap can split multi-line input, so admins may enter all
    # consents on one line joined by ';'. Existing newline data still parses.
    for line in (raw or "").replace(";", "\n").strip().splitlines():
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


HTML_SETTINGS = {"start_text", "reg_complete_text", "approve_text", "approve_text__party"}

@router.message(EditSetting.waiting_for_value, is_admin)
async def settings_edit_value(message: types.Message, state: FSMContext):
    data = await state.get_data()
    key = data["setting_key"]

    if key in HTML_SETTINGS:
        value = (message.html_text or message.text or "").strip()
    else:
        value = (message.text or "").strip()

    warning = ""
    if value == "-":
        await delete_setting(key)
    else:
        await set_setting(key, value)
        # Phase 4 (D-05): saving event_type applies the module-toggle preset.
        if key == "event_type":
            await _apply_event_type_preset(value.strip().lower())
        # WR-02: "Отмена"/"Другое"/"Пропустить" are reserved control words in the registration
        # flow — an option list whose line equals one becomes unreachable (it triggers cancel /
        # "type your own" instead of being recorded). Warn the admin (the value is still saved).
        if key.endswith("_options"):
            reserved = {"отмена", "другое", "пропустить"}
            clashes = sorted({
                ln.strip() for ln in value.splitlines() if ln.strip().lower() in reserved
            })
            if clashes:
                warning = (
                    "\n\n⚠️ Внимание: варианты "
                    + ", ".join(f"«{html_module.escape(c)}»" for c in clashes)
                    + " совпадают со служебными словами бота и будут недоступны для выбора. "
                    "Переименуйте их."
                )

    await state.clear()
    text = await render_settings_text()
    await message.answer(text + warning, parse_mode="HTML", reply_markup=await build_settings_keyboard())


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


@router.callback_query(F.data == "admin_export_incomplete")
async def export_incomplete(callback: types.CallbackQuery):
    if callback.from_user.id not in config.ADMIN_IDS:
        await callback.answer("Недостаточно прав", show_alert=True)
        return
    await callback.answer("📝 Выгружаю…")
    rows = await get_incomplete_rows()  # (id, username, started_at, last_step, partial_data)
    # Quick k4y: headers computed ONCE (Google Sheets quota), rows projected via the shared
    # helper — services/scheduler.py:sync_incomplete_sheet_job MUST build identical
    # headers/rows via the same helpers (WR-01 parity), otherwise the 2h auto-sync silently
    # narrows the tab back down.
    headers = await incomplete_sheet_headers()
    sheet_rows = [
        incomplete_sheet_row(tid, username, started_at, last_step, partial_data, headers)
        for tid, username, started_at, last_step, partial_data in rows
    ]
    written = await sync_named_worksheet("Незавершённые", headers, sheet_rows)

    # Aggregate: on which question do dropouts stall most? (works even if the sheet write failed)
    stats = await get_dropout_step_stats()
    total = sum(c for _s, c in stats) or 1
    top = "\n".join(
        f"• {dropout_step_label(step)} — <b>{cnt}</b> ({round(cnt * 100 / total)}%)"
        for step, cnt in stats[:8]
    )
    summary = f"\n\n📊 <b>Где отваливаются:</b>\n{top}" if stats else ""

    if written < 0:
        await callback.message.answer(
            "⚠️ Не удалось записать в таблицу (проверь доступ к Google Sheets). "
            f"Незавершённых регистраций в базе: <b>{len(rows)}</b>.{summary}",
            parse_mode="HTML",
        )
        return
    await callback.message.answer(
        f"✅ Вкладка «Незавершённые» обновлена: <b>{written}</b> записей.{summary}",
        parse_mode="HTML",
    )


@router.callback_query(F.data == "admin_menu")
async def admin_menu_root(callback: types.CallbackQuery):
    """Back to the admin panel keyboard (also fixes the previously dead «Отмена» buttons
    that pointed at admin_menu without a handler)."""
    if callback.from_user.id not in config.ADMIN_IDS:
        await callback.answer("Недостаточно прав", show_alert=True)
        return
    await callback.message.edit_text(
        "👮‍♂️ <b>Панель администратора</b>", parse_mode="HTML", reply_markup=build_admin_keyboard()
    )
    await callback.answer()


@router.callback_query(F.data == "admin_dedupe_sheet")
async def dedupe_sheet_confirm(callback: types.CallbackQuery):
    if callback.from_user.id not in config.ADMIN_IDS:
        await callback.answer("Недостаточно прав", show_alert=True)
        return
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🧹 Да, убрать дубли", callback_data="admin_dedupe_sheet_go")],
        [InlineKeyboardButton(text="← Отмена", callback_data="admin_menu")],
    ])
    await callback.message.edit_text(
        "🧹 <b>Убрать дубли из таблицы?</b>\n\n"
        "Удалю повторные строки с одинаковым Telegram ID (от повторных регистраций / "
        "тестов админов), оставлю <b>самую свежую</b> по каждому.\n\n"
        "⚠️ Удаляются целые строки — если на старой строке-дубле были ручные заметки, "
        "они пропадут (на оставленной строке всё цело).",
        parse_mode="HTML",
        reply_markup=kb,
    )
    await callback.answer()


@router.callback_query(F.data == "admin_dedupe_sheet_go")
async def dedupe_sheet_run(callback: types.CallbackQuery):
    if callback.from_user.id not in config.ADMIN_IDS:
        await callback.answer("Недостаточно прав", show_alert=True)
        return
    await callback.answer("🧹 Убираю дубли…")
    logger.info(f"admin={callback.from_user.id} action=dedupe_sheet start")
    removed = await dedupe_sheet_by_id()
    if removed < 0:
        text = "⚠️ Не удалось (проверь доступ к Google Sheets, подробности в логах)."
    elif removed == 0:
        text = "✅ Дублей не найдено — таблица чистая."
    else:
        text = f"✅ Удалено дублей: <b>{removed}</b>. Оставлены свежие строки."
    logger.info(f"admin={callback.from_user.id} action=dedupe_sheet removed={removed}")
    await callback.message.edit_text(text, parse_mode="HTML", reply_markup=build_admin_keyboard())


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
    # WR-01: callbacks aren't covered by the message-level is_admin filter — re-check (D-06).
    if callback.from_user.id not in config.ADMIN_IDS:
        await callback.answer("Недостаточно прав", show_alert=True)
        return
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
    # WR-01: callbacks aren't covered by the message-level is_admin filter — re-check (D-06).
    if callback.from_user.id not in config.ADMIN_IDS:
        await callback.answer("Недостаточно прав", show_alert=True)
        return
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
    # WR-01: callbacks aren't covered by the message-level is_admin filter — re-check (D-06).
    if callback.from_user.id not in config.ADMIN_IDS:
        await callback.answer("Недостаточно прав", show_alert=True)
        return
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
        # WR-04: unsupported-only media group — notify the admin and clear state instead of
        # silently leaving the FSM parked in Broadcast.message with no feedback.
        try:
            await bot.send_message(admin_id, "⚠️ Рассылка отменена: неподдерживаемый тип вложения.")
        except Exception:
            pass
        await state.clear()
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
            _spawn(_wait_and_send_album(mgid, users_ids, bot, state, message.from_user.id))
        else:
            pending_albums[mgid]["messages"].append(message)
        return

    count = 0
    blocked = 0

    await message.answer(f"Начинаю рассылку на {len(users_ids)} пользователей...")  # IN-01: was assigned to unused status_msg

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
    # WR-03: html_text already falls back to caption+caption_entities when .text is empty, so
    # it preserves bold/italic/link formatting for BOTH text and photo-caption broadcasts.
    # Using raw message.caption here silently stripped entities the admin applied.
    if message.text or message.caption:
        text = message.html_text
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
        # IN-02: row["text"] was stored as HTML (message.html_text at schedule time). Strip the
        # tags for a clean plain-text preview instead of escaping them into visible &lt;b&gt; noise.
        preview = re.sub(r"<[^>]+>", "", row.get("text") or "(фото)")[:60]
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
    # WR-02: guard the int() parse like _parse_appr/_parse_rcpt do — malformed callback_data
    # (empty suffix) must degrade gracefully, not raise and leave the button spinning.
    try:
        bid = int(callback.data.rsplit("_", 1)[1])
    except ValueError:
        await callback.answer("Некорректные данные.", show_alert=True)
        return
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
    "payment_status": "Оплата",
    "local_committee": "Комитет AIESEC", "department": "Департамент",
    "aiesec_role": "Роль AIESEC", "education_status": "Образование",
    "course": "Курс", "study_field": "Направление",
    "position": "Позиция", "attendance_format": "Формат участия",
    "participant_type": "Трек",  # Phase 5 (D-19)
}

# Fields whose value is chosen from a DB-distinct picker (buttons pulled from real data).
# Everything in the filter menu except «Дата регистрации» (a before/after threshold).
_PICKER_FIELDS = {
    "city", "university", "source", "status", "payment_status",
    "local_committee", "department", "aiesec_role", "education_status",
    "course", "study_field", "position", "attendance_format",
    "participant_type",  # Phase 5 (D-19) — must ALSO be in db._FILTER_COLUMNS or it's dropped
}

# How many value buttons per picker page (long cyrillic values → 1 per row).
_FILTER_PAGE_SIZE = 8


def _value_picker_kb(field: str, options: list[str], page: int) -> InlineKeyboardMarkup:
    """Paginated value picker. The value itself never goes in callback_data (cyrillic values
    blow past Telegram's 64-byte limit) — buttons carry the option INDEX; the full list lives
    in FSM state. payment_status shows human labels."""
    total = len(options)
    pages = max(1, (total + _FILTER_PAGE_SIZE - 1) // _FILTER_PAGE_SIZE)
    page = max(0, min(page, pages - 1))
    start = page * _FILTER_PAGE_SIZE
    rows = []
    for i, v in enumerate(options[start:start + _FILTER_PAGE_SIZE], start=start):
        label = _PAYMENT_STATUS_LABELS.get(v, v) if field == "payment_status" else v
        rows.append([InlineKeyboardButton(text=label[:60], callback_data=f"filter_opt:{i}")])
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton(text="◀", callback_data=f"filter_optpage:{page - 1}"))
    if page < pages - 1:
        nav.append(InlineKeyboardButton(text="▶", callback_data=f"filter_optpage:{page + 1}"))
    if nav:
        rows.append(nav)
    rows.append([InlineKeyboardButton(text="← Назад", callback_data="filter_back")])
    return InlineKeyboardMarkup(inline_keyboard=rows)

# Human labels for payment_status values (shown in the filter summary / value picker).
_PAYMENT_STATUS_LABELS = {
    "not_paid": "Не оплатил", "overdue": "Просрочил",
    "receipt_sent": "Чек на проверке", "paid": "Оплатил",
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
        elif f["field"] == "payment_status":
            parts.append(f"{label} = {_PAYMENT_STATUS_LABELS.get(f.get('value'), val)}")
        else:
            parts.append(f"{label} = {val}")
    return " И ".join(parts)


def _filter_menu_kb(filters: list[dict]) -> InlineKeyboardMarkup:
    kb = [
        [InlineKeyboardButton(text="Комитет AIESEC", callback_data="filter_f_local_committee"),
         InlineKeyboardButton(text="Департамент", callback_data="filter_f_department")],
        [InlineKeyboardButton(text="Роль AIESEC", callback_data="filter_f_aiesec_role"),
         InlineKeyboardButton(text="Позиция", callback_data="filter_f_position")],
        [InlineKeyboardButton(text="Город", callback_data="filter_f_city"),
         InlineKeyboardButton(text="ВУЗ", callback_data="filter_f_university")],
        [InlineKeyboardButton(text="Образование", callback_data="filter_f_education_status"),
         InlineKeyboardButton(text="Курс", callback_data="filter_f_course")],
        [InlineKeyboardButton(text="Направление", callback_data="filter_f_study_field"),
         InlineKeyboardButton(text="Формат участия", callback_data="filter_f_attendance_format")],
        [InlineKeyboardButton(text="Статус", callback_data="filter_f_status"),
         InlineKeyboardButton(text="Источник", callback_data="filter_f_source")],
        [InlineKeyboardButton(text="Дата регистрации", callback_data="filter_f_date"),
         InlineKeyboardButton(text="💰 Оплата", callback_data="filter_f_payment_status")],
        [InlineKeyboardButton(text="🎉 Трек", callback_data="filter_f_participant_type")],
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


async def _show_value_picker(callback: types.CallbackQuery, state: FSMContext, field: str, prompt: str):
    """Load distinct DB values for `field`, stash them in FSM, render the paginated picker."""
    options = await get_distinct_filter_values(field)
    if not options:
        await callback.answer("В базе нет значений для этого поля.", show_alert=True)
        return
    await state.update_data(filter_options=options, filter_page=0)
    await callback.answer()
    await callback.message.edit_text(prompt, reply_markup=_value_picker_kb(field, options, 0))


@router.callback_query(F.data.in_({f"filter_f_{fld}" for fld in _PICKER_FIELDS}), Broadcast.filter_field)
async def filter_pick_field(callback: types.CallbackQuery, state: FSMContext):
    """Every attribute field → a DB-distinct value picker (no free-text typing)."""
    if callback.from_user.id not in config.ADMIN_IDS:
        await callback.answer("Недостаточно прав", show_alert=True)
        return
    field = callback.data[len("filter_f_"):]
    await state.update_data(filter_pending_field=field, filter_pending_op=None)
    await _show_value_picker(callback, state, field, f"Выберите значение — «{_FILTER_FIELD_LABELS.get(field, field)}»:")


@router.callback_query(F.data == "filter_f_date", Broadcast.filter_field)
async def filter_pick_date(callback: types.CallbackQuery, state: FSMContext):
    if callback.from_user.id not in config.ADMIN_IDS:
        await callback.answer("Недостаточно прав", show_alert=True)
        return
    await callback.answer()
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="После", callback_data="filter_d_after"),
         InlineKeyboardButton(text="До", callback_data="filter_d_before")],
        [InlineKeyboardButton(text="← Назад", callback_data="filter_back")],
    ])
    await callback.message.edit_text("Зарегистрированы…", reply_markup=kb)


@router.callback_query(F.data.in_({"filter_d_after", "filter_d_before"}), Broadcast.filter_field)
async def filter_pick_date_op(callback: types.CallbackQuery, state: FSMContext):
    if callback.from_user.id not in config.ADMIN_IDS:
        await callback.answer("Недостаточно прав", show_alert=True)
        return
    op = "after" if callback.data.endswith("after") else "before"
    await state.update_data(filter_pending_field="registration_date", filter_pending_op=op)
    opl = "после" if op == "after" else "до"
    await _show_value_picker(callback, state, "registration_date", f"Зарегистрированы {opl} даты:")


@router.callback_query(F.data.startswith("filter_optpage:"), Broadcast.filter_field)
async def filter_page_nav(callback: types.CallbackQuery, state: FSMContext):
    if callback.from_user.id not in config.ADMIN_IDS:
        await callback.answer("Недостаточно прав", show_alert=True)
        return
    data = await state.get_data()
    field = data.get("filter_pending_field")
    options = data.get("filter_options", [])
    if not field or not options:
        await callback.answer("Список устарел, начните заново.", show_alert=True)
        return
    try:
        page = int(callback.data.split(":", 1)[1])
    except ValueError:
        await callback.answer()
        return
    await state.update_data(filter_page=page)
    await callback.answer()
    await callback.message.edit_reply_markup(reply_markup=_value_picker_kb(field, options, page))


@router.callback_query(F.data.startswith("filter_opt:"), Broadcast.filter_field)
async def filter_pick_value(callback: types.CallbackQuery, state: FSMContext):
    if callback.from_user.id not in config.ADMIN_IDS:
        await callback.answer("Недостаточно прав", show_alert=True)
        return
    data = await state.get_data()
    field = data.get("filter_pending_field")
    options = data.get("filter_options", [])
    try:
        idx = int(callback.data.split(":", 1)[1])
    except ValueError:
        await callback.answer("Некорректный выбор.", show_alert=True)
        return
    if not field or not (0 <= idx < len(options)):
        await callback.answer("Значение больше не доступно, начните заново.", show_alert=True)
        return
    value = options[idx]
    filters = data.get("filters", [])
    if field == "registration_date":
        filters.append({"field": field, "op": data.get("filter_pending_op"), "value": value})
    else:
        filters.append({"field": field, "value": value})
    await state.update_data(
        filters=filters, filter_pending_field=None, filter_pending_op=None,
        filter_options=[], filter_page=0,
    )
    await callback.answer()
    await _render_filter_menu(callback.message, filters, edit=True)


@router.callback_query(F.data == "filter_back", Broadcast.filter_field)
async def filter_back(callback: types.CallbackQuery, state: FSMContext):
    """Abandon the in-progress field pick, return to the filter menu."""
    if callback.from_user.id not in config.ADMIN_IDS:
        await callback.answer("Недостаточно прав", show_alert=True)
        return
    data = await state.get_data()
    await state.update_data(filter_pending_field=None, filter_pending_op=None, filter_options=[], filter_page=0)
    await callback.answer()
    await _render_filter_menu(callback.message, data.get("filters", []), edit=True)


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

async def _is_question_on(setting_key: str) -> bool:
    # REG-02 (06-04): delegates entirely to the registry's typed toggle accessor — byte-
    # identical to the prior manual (val == "on") if val is not None else REG_DEFAULTS.get(...)
    # idiom (get_setting_typed's toggle branch reproduces it exactly), single get_setting call.
    return await get_setting_typed(setting_key)


# Phase 5 (D-04): party question tri-state helpers. These are PURE — they operate on the
# raw get_setting(f"{key}__party") return value (None | "on" | "off") and must never route
# through _is_question_on, which collapses None into a resolved boolean and would make
# "inherit" indistinguishable from "off".
def _party_tri_state_label(raw: str | None) -> str:
    if raw is None:
        return "➕ Наследует"
    return "✅ Вкл" if raw == "on" else "❌ Выкл"


def _party_tri_state_advance(raw: str | None) -> str | None:
    """Cycle: None (inherit) -> "on" -> "off" -> None (inherit). The None return value
    signals the caller to delete_setting (key-absence IS the inherit state, D-04)."""
    if raw is None:
        return "on"
    if raw == "on":
        return "off"
    return None


def _track_switcher_row(active: str) -> list[InlineKeyboardButton]:
    """D-06: first row of the questions keyboard — switches the whole screen between the
    full-track view (existing 2-state toggles) and the party-track view (tri-state)."""
    return [
        InlineKeyboardButton(text=("• " if active == "full" else "") + "Полный", callback_data="reg_q_track:full"),
        InlineKeyboardButton(text=("• " if active == "party" else "") + "Party", callback_data="reg_q_track:party"),
    ]


def _categorized_question_keys() -> list[tuple[str, str]]:
    """(header, setting_key) rows in category order. Any REG_FLOW key not placed in a
    REG_CATEGORIES bucket lands in a trailing «Прочие» group so nothing is ever hidden."""
    seen = set()
    rows: list[tuple[str, str]] = []
    for header, keys in REG_CATEGORIES:
        for k in keys:
            rows.append((header, k))
            seen.add(k)
    leftover = [sk for _, sk, *_ in REG_FLOW if sk not in seen]
    for k in leftover:
        rows.append(("📦 Прочие", k))
    return rows


async def render_questions_text(track: str = "full") -> str:
    lines = ["📋 <b>Вопросы регистрации</b>", ""]
    if track == "party":
        lines.append(
            "<i>Действуют в режиме «🎉 Party». ➕ Наследует — берётся общая настройка, "
            "✅/❌ — переопределено для этого трека.</i>"
        )
    else:
        lines.append("<i>Действуют в режиме «📋 Полная регистрация». Сгруппированы по типу события.</i>")
    lines.append("")
    current = None
    for header, setting_key in _categorized_question_keys():
        if header != current:
            lines.append(f"\n<b>{header}</b>")
            current = header
        label = REG_LABELS.get(setting_key, setting_key)
        if track == "party":
            raw = await get_setting(f"{setting_key}__party")
            status = _party_tri_state_label(raw)
        else:
            status = "✅" if await _is_question_on(setting_key) else "❌"
        lines.append(f"{status} {label}")
    return "\n".join(lines)


async def build_questions_keyboard(track: str = "full"):
    buttons = [_track_switcher_row(track)]
    current = None
    for header, setting_key in _categorized_question_keys():
        if header != current:
            # Non-actionable section header (noop callback).
            buttons.append([InlineKeyboardButton(text=f"── {header} ──", callback_data="reg_q_noop")])
            current = header
        label = REG_LABELS.get(setting_key, setting_key)
        if track == "party":
            raw = await get_setting(f"{setting_key}__party")
            toggle_text = f"{_party_tri_state_label(raw)} {label}"
            buttons.append([InlineKeyboardButton(text=toggle_text, callback_data=f"reg_q_ptoggle:{setting_key}")])
        else:
            toggle_text = f"{'✅' if await _is_question_on(setting_key) else '❌'} {label}"
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


async def _refresh_sheet_header() -> None:
    """Regenerate the Google-sheet header after a question toggle so newly enabled
    questions show up as columns right away. The header is otherwise built only at
    startup (main.py), so mid-session toggles left the sheet missing enabled columns —
    the reported bug. Fail-soft (ensure_sheet_header swallows API/credential errors) and
    backgrounded so the admin UI stays snappy.

    NOTE: a column inserted mid-list only aligns rows appended AFTER the toggle; rows
    already in the sheet keep their original positions. Set the event type before
    delegates start registering to avoid mid-event drift."""
    try:
        headers = await active_sheet_headers()
    except Exception as e:
        logger.warning(f"_refresh_sheet_header: could not compute headers: {e}")
        return
    _spawn(ensure_sheet_header(headers))


async def _refresh_party_sheet_header() -> None:
    """MEDIUM-01: resync the party tab's physical header after a party-question toggle/preset so
    party rows appended afterwards align to the same columns as row 1. party_sheet_row recomputes
    party_sheet_headers() live per append, so a mid-event __party override otherwise shifts every
    subsequent row against the once-written startup header — a silent column misalignment.
    Mirrors _refresh_sheet_header for the main tab. GATED on party_enabled='on' (like the startup
    _maybe_ensure_party_sheet_header) so toggling a party override while the track is OFF never
    materializes the tab (D-15). Fail-soft + backgrounded."""
    from handlers.registration import party_sheet_headers, PARTY_SHEET_TAB_DEFAULT
    from services.sheets import ensure_named_sheet_header
    try:
        # REG-02 (06-05): gate read migrated to the registry; behavior unchanged.
        if (await get_setting_typed("party_enabled")) != "on":
            return
        tab = await get_setting("party_sheet_tab") or PARTY_SHEET_TAB_DEFAULT
        headers = await party_sheet_headers()
    except Exception as e:
        logger.warning(f"_refresh_party_sheet_header: could not compute headers: {e}")
        return
    _spawn(ensure_named_sheet_header(tab, headers))


@router.callback_query(F.data.startswith("reg_q_toggle:"))
async def toggle_reg_question(callback: types.CallbackQuery):
    if callback.from_user.id not in config.ADMIN_IDS:
        await callback.answer("Недостаточно прав", show_alert=True)
        return

    setting_key = callback.data.split(":", 1)[1]

    # REG-02 (06-04): registry-driven resolution, byte-identical to the prior manual
    # (val == "on") if val is not None else REG_DEFAULTS.get(setting_key, "on") == "on" idiom.
    current_on = await get_setting_typed(setting_key)

    new_val = "off" if current_on else "on"
    await set_setting(setting_key, new_val)

    label = REG_LABELS.get(setting_key, setting_key)
    status = "✅ Вкл" if new_val == "on" else "❌ Выкл"
    await callback.answer(f"{label}: {status}", show_alert=True)

    text = await render_questions_text()
    await callback.message.edit_text(text, parse_mode="HTML", reply_markup=await build_questions_keyboard())
    await _refresh_sheet_header()  # keep the sheet header in sync with enabled questions


@router.callback_query(F.data.startswith("reg_q_track:"))
async def reg_q_track_switch(callback: types.CallbackQuery):
    """D-06: track switcher row — re-renders the SAME «📋 Вопросы регистрации» message in
    the requested track context. No FSM state — the requested track lives entirely in the
    callback_data of the tapped button."""
    if callback.from_user.id not in config.ADMIN_IDS:
        await callback.answer("Недостаточно прав", show_alert=True)
        return
    track = callback.data.split(":", 1)[1]
    if track not in ("full", "party"):
        track = "full"
    text = await render_questions_text(track)
    await callback.message.edit_text(text, parse_mode="HTML", reply_markup=await build_questions_keyboard(track))
    await callback.answer()


@router.callback_query(F.data.startswith("reg_q_ptoggle:"))
async def toggle_party_question(callback: types.CallbackQuery):
    """D-04: tri-state cycle inherit(absent) -> on -> off -> inherit for the party-track
    override of one question. Reads/writes the RAW f"{setting_key}__party" value — never
    routes through _is_question_on, which would collapse None and make "inherit"
    indistinguishable from "off". delete_setting is the "back to inherit" primitive."""
    if callback.from_user.id not in config.ADMIN_IDS:
        await callback.answer("Недостаточно прав", show_alert=True)
        return
    setting_key = callback.data.split(":", 1)[1]
    # T-05-03-02: validate setting_key against REG_FLOW before ever suffixing/writing it —
    # an unknown key from a crafted callback is rejected, never turned into a bot_settings write.
    valid_keys = {sk for _, sk, *_ in REG_FLOW}
    if setting_key not in valid_keys:
        await callback.answer("Неизвестный вопрос.", show_alert=True)
        return

    party_key = f"{setting_key}__party"
    current = await get_setting(party_key)  # None | "on" | "off" — do NOT collapse
    new_val = _party_tri_state_advance(current)
    if new_val is None:
        await delete_setting(party_key)  # back to inherit — key ABSENCE is the inherit state
    else:
        await set_setting(party_key, new_val)
    label = _party_tri_state_label(new_val)

    await _refresh_party_sheet_header()  # MEDIUM-01: keep the party tab header aligned with the toggle
    await callback.answer(f"{REG_LABELS.get(setting_key, setting_key)} (party): {label}", show_alert=True)
    text = await render_questions_text("party")
    await callback.message.edit_text(text, parse_mode="HTML", reply_markup=await build_questions_keyboard("party"))


@router.callback_query(F.data == "reg_q_noop")
async def reg_q_noop(callback: types.CallbackQuery):
    # Section-header button in the categorized question view — not actionable.
    await callback.answer()


@router.callback_query(F.data == "reg_q_back")
async def reg_questions_back(callback: types.CallbackQuery):
    if callback.from_user.id not in config.ADMIN_IDS:
        await callback.answer("Недостаточно прав", show_alert=True)
        return

    text = await render_settings_text()
    await callback.message.edit_text(text, parse_mode="HTML", reply_markup=await build_settings_keyboard())
    await callback.answer()


# --- Event-type presets (one-tap bulk toggle) ---

async def _apply_event_preset(preset_key: str) -> None:
    """Bulk-write reg_q_* + payment_enabled for the chosen preset. Every REG_DEFAULTS
    key is set explicitly (on if in the preset's list, else off) so the result is
    deterministic regardless of prior per-question overrides."""
    preset = REG_PRESETS[preset_key]
    on_set = set(preset["on"])
    for key in REG_DEFAULTS:
        await set_setting(key, "on" if key in on_set else "off")
    await set_setting("payment_enabled", preset["payment_enabled"])


@router.callback_query(F.data == "admin_event_preset")
async def admin_event_preset(callback: types.CallbackQuery):
    if callback.from_user.id not in config.ADMIN_IDS:
        await callback.answer("Недостаточно прав", show_alert=True)
        return
    buttons = [
        [InlineKeyboardButton(text=p["label"], callback_data=f"preset_apply:{key}")]
        for key, p in REG_PRESETS.items()
    ]
    buttons.append([InlineKeyboardButton(text="← Назад к настройкам", callback_data="reg_q_back")])
    await callback.message.edit_text(
        "🎛 <b>Тип события</b>\n\n"
        "Выбери пресет — он одним махом включит нужные вопросы и выключит остальные "
        "(+ настроит модуль оплаты). Экстра-вопросы потом докинешь вручную в «📋 Вопросы регистрации».\n\n"
        "⚠️ Перезатрёт текущие тумблеры вопросов.",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons),
    )
    await callback.answer()


@router.callback_query(F.data.startswith("preset_apply:"))
async def preset_apply(callback: types.CallbackQuery):
    if callback.from_user.id not in config.ADMIN_IDS:
        await callback.answer("Недостаточно прав", show_alert=True)
        return
    key = callback.data.split(":", 1)[1]
    preset = REG_PRESETS.get(key)
    if not preset:
        await callback.answer("Неизвестный пресет.", show_alert=True)
        return
    on_labels = ", ".join(REG_LABELS.get(k, k) for k in preset["on"])
    # D-07: the party preset carries no "payment_enabled" key (party pricing is plan 05-05's
    # concern, not this preset's). preset.get(...) avoids a KeyError that the global
    # @dp.errors() handler would otherwise swallow silently (the admin sees nothing happen).
    payment_enabled = preset.get("payment_enabled")
    pay_line = ""
    if payment_enabled is not None:
        pay = "включится" if payment_enabled == "on" else "выключится"
        pay_line = f" Модуль оплаты <b>{pay}</b>."
    # D-07: __party keys never overlap the globals a live full-form admin is looking at, so
    # the party preset does not need the "перезатрёт текущие настройки" warning the
    # forum/conf presets carry — nothing existing gets overwritten.
    warn = "" if key == "party" else "\n\n⚠️ Текущие настройки вопросов будут перезаписаны."
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Применить", callback_data=f"preset_confirm:{key}")],
        [InlineKeyboardButton(text="← Отмена", callback_data="admin_event_preset")],
    ])
    await callback.message.edit_text(
        f"Применить пресет <b>{preset['label']}</b>?\n\n"
        f"<b>Включатся:</b> {on_labels}\n"
        f"Остальные вопросы выключатся.{pay_line}"
        f"{warn}",
        parse_mode="HTML",
        reply_markup=kb,
    )
    await callback.answer()


@router.callback_query(F.data.startswith("preset_confirm:"))
async def preset_confirm(callback: types.CallbackQuery):
    if callback.from_user.id not in config.ADMIN_IDS:
        await callback.answer("Недостаточно прав", show_alert=True)
        return
    key = callback.data.split(":", 1)[1]
    preset = REG_PRESETS.get(key)
    if not preset:
        await callback.answer("Неизвестный пресет.", show_alert=True)
        return
    if key == "party":
        # D-07: route to the isolated __party-only bulk writer. _apply_event_preset writes
        # GLOBAL reg_q_* keys for every REG_DEFAULTS entry — routing the party key there
        # would erase the live full-delegate question set, exactly what D-07 exists to prevent.
        await _apply_party_preset()
        await callback.answer(f"Пресет применён: {preset['label']}", show_alert=True)
        text = await render_questions_text("party")
        await callback.message.edit_text(
            text, parse_mode="HTML", reply_markup=await build_questions_keyboard("party")
        )
        # No _refresh_sheet_header(): the party preset changes no global setting, so the main
        # sheet header cannot have drifted. MEDIUM-01: but the party preset DOES change the
        # __party question set, so resync the PARTY tab's own header (plan 05-06).
        await _refresh_party_sheet_header()
        return
    await _apply_event_preset(key)
    await callback.answer(f"Пресет применён: {preset['label']}", show_alert=True)
    text = await render_questions_text()
    await callback.message.edit_text(text, parse_mode="HTML", reply_markup=await build_questions_keyboard())
    await _refresh_sheet_header()  # preset flips many questions → resync the sheet header


# --- Editable question prompts (YL'26: per-event wording, 0 хардкода) ---

def _prompt_steps() -> list[tuple[str, str]]:
    """(step_key, human label) for every question whose wording can be overridden."""
    steps = [("full_name", "🪪 Фамилия и Имя")]
    for step_key, setting_key, *_ in REG_FLOW:
        steps.append((step_key, REG_LABELS.get(setting_key, step_key)))
    return steps


def _prompt_track_switcher_row(active: str) -> list[InlineKeyboardButton]:
    """Quick 260724-cfn (WR-02b): mirrors _track_switcher_row for the «Тексты вопросов»
    screen — switches between editing the global (full) prompt overrides and the
    party-track (__party) prompt overrides. Own callback namespace (reg_prompt_track:)
    so it never collides with the questions-toggle screen's reg_q_track: switcher."""
    return [
        InlineKeyboardButton(text=("• " if active == "full" else "") + "Полный", callback_data="reg_prompt_track:full"),
        InlineKeyboardButton(text=("• " if active == "party" else "") + "Party", callback_data="reg_prompt_track:party"),
    ]


async def render_prompts_text(track: str = "full") -> str:
    text = (
        "✏️ <b>Тексты вопросов</b>\n\nВыбери вопрос и пришли свой текст. ✅ — текст переопределён, "
        "✏️ — стандартный. Чтобы вернуть стандартный, отправь «-»."
    )
    if track == "party":
        text += (
            "\n\n<i>Действуют в режиме 🎉 Party. ✏️ — берётся общий текст вопроса, "
            "✅ — переопределено для party. «-» — сброс к общему.</i>"
        )
    return text


async def build_prompts_keyboard(track: str = "full"):
    buttons = [_prompt_track_switcher_row(track)]
    for step_key, label in _prompt_steps():
        if track == "party":
            key = f"reg_prompt_{step_key}__party"
            callback_data = f"reg_prompt_edit:{step_key}:party"
        else:
            key = f"reg_prompt_{step_key}"
            callback_data = f"reg_prompt_edit:{step_key}"
        custom = await get_setting(key)
        mark = "✅" if custom else "✏️"
        buttons.append([InlineKeyboardButton(text=f"{mark} {label}", callback_data=callback_data)])
    buttons.append([InlineKeyboardButton(text="← Назад к настройкам", callback_data="admin_settings")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


@router.callback_query(F.data == "admin_reg_prompts")
async def admin_reg_prompts(callback: types.CallbackQuery):
    if callback.from_user.id not in config.ADMIN_IDS:
        await callback.answer("Недостаточно прав", show_alert=True)
        return
    text = await render_prompts_text("full")
    await callback.message.edit_text(text, parse_mode="HTML", reply_markup=await build_prompts_keyboard("full"))
    await callback.answer()


@router.callback_query(F.data.startswith("reg_prompt_track:"))
async def reg_prompt_track_switch(callback: types.CallbackQuery):
    """Quick 260724-cfn (WR-02b): re-renders the SAME «✏️ Тексты вопросов» message in the
    requested track context. No FSM state — mirrors reg_q_track_switch."""
    if callback.from_user.id not in config.ADMIN_IDS:
        await callback.answer("Недостаточно прав", show_alert=True)
        return
    track = callback.data.split(":", 1)[1]
    if track not in ("full", "party"):
        track = "full"
    text = await render_prompts_text(track)
    await callback.message.edit_text(text, parse_mode="HTML", reply_markup=await build_prompts_keyboard(track))
    await callback.answer()


@router.callback_query(F.data.startswith("reg_prompt_edit:"))
async def reg_prompt_edit(callback: types.CallbackQuery, state: FSMContext):
    if callback.from_user.id not in config.ADMIN_IDS:
        await callback.answer("Недостаточно прав", show_alert=True)
        return
    # Quick 260724-cfn (WR-02b): optional trailing ":party" track suffix. step_keys (full_name
    # + REG_FLOW) never contain ":", so this split is safe. Any suffix other than the literal
    # "party" falls back to "full" (closed whitelist, mirrors reg_q_track_switch).
    parts = callback.data.split(":")
    step_key = parts[1]
    track = "party" if len(parts) > 2 and parts[2] == "party" else "full"
    key = f"reg_prompt_{step_key}__party" if track == "party" else f"reg_prompt_{step_key}"
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
    # Phase 5 (D-14): shared queue — one extra line for a non-full track, no second queue,
    # no track predicate anywhere in get_pending_users/get_pending_count. Unrecognised values
    # are HTML-escaped (T-05-03-03): the raw DB column value can never inject markup here.
    track = user.get("participant_type") or "full"
    if track != "full":
        track_label = {
            "party_overnight": "🎉 Трек: вечеринка с ночёвкой",
            "party_noovernight": "🎉 Трек: вечеринка без ночёвки",
        }.get(track, f"🎉 Трек: {html_module.escape(str(track))}")
        lines.append(track_label)
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
    if esc(user.get("alumni_status")):
        lines.append(f"🏷 {esc(user.get('alumni_status'))}")
    if user.get("age"):
        lines.append(f"🎂 {esc(user.get('age'))}")
    # Резюме: файлом, текстом или нет. Текст показываем прямо в карточке (Таня п.4),
    # обрезая длинные — полный текст доступен по кнопке «📎 Резюме».
    if user.get("resume_file_id"):
        lines.append("📎 Резюме: файлом (кнопка ниже)")
    elif esc(user.get("resume_text")):
        rt = str(user.get("resume_text"))
        preview = html_module.escape(rt[:300] + ("…" if len(rt) > 300 else ""))
        lines.append(f"📎 Резюме (текст): {preview}")
    else:
        lines.append("📎 Резюме: нет")
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
    skipped = set((await state.get_data()).get("appr_skipped", []))
    total = await get_pending_count()
    offset = 0
    visible: list[dict] = []
    while not visible and offset < total:
        batch = await get_pending_users(limit=50, offset=offset)
        if not batch:
            break
        visible = [u for u in batch if u["telegram_id"] not in skipped]
        offset += len(batch)
    if not visible:
        await target.answer("✅ Заявок нет.", reply_markup=build_admin_keyboard())
        return
    current = visible[0]
    # M-02: position = how many the admin has already skipped + 1 (the shown card is the first
    # not-yet-skipped pending item). The old total - len(visible) + 1 returned e.g. 51/100 for
    # the first card whenever a full 50-row batch was unskipped. Cap at total for safety.
    position = min(len(skipped) + 1, total)
    await target.answer(
        _render_application_card(current, position, total),
        parse_mode="HTML",
        reply_markup=_appr_card_kb(
            current["telegram_id"],
            bool(current.get("resume_file_id") or current.get("resume_text")),
            total,
        ),
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
        # Автосинк статуса в таблицу (Таня п.5), fire-and-forget fail-soft.
        _spawn(update_status_in_sheet(tid, STATUS_LABELS["approved"]))
        logger.info(f"admin={callback.from_user.id} action=approve user={tid}")
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


# WR-03: admin.router is first, so appr_reject_reason (the Approval.reason catch-all below)
# would otherwise SWALLOW any /command typed mid-rejection as the rejection reason — the
# rejection fires with a garbage reason and the command never runs. Catch «Отмена» AND any
# «/...» command here first, aborting the rejection cleanly so the admin can re-issue it.
@router.message(Approval.reason, is_admin, F.text.in_({"Отмена"}) | F.text.startswith("/"))
async def appr_reject_cancel(message: types.Message, state: FSMContext):
    await state.set_state(None)
    text = (message.text or "").strip()
    if text not in ("Отмена", "/cancel"):
        note = "Отклонение отменено (введена команда). При необходимости повторите её."
    else:
        note = "Отклонение отменено."
    await message.answer(note, reply_markup=ReplyKeyboardRemove())
    await _show_current_card(message, state)


@router.message(Approval.reason, is_admin)
async def appr_reject_reason(message: types.Message, state: FSMContext):
    data = await state.get_data()
    tid = data.get("appr_reject_id")
    reason = message.text or "-"
    ok = await reject_user(tid) if tid is not None else False
    if ok:
        _spawn(update_status_in_sheet(tid, STATUS_LABELS["rejected"]))
        logger.info(f"admin={message.from_user.id} action=reject user={tid} reason={reason!r}")
        try:
            prefix = await get_setting("reject_text") or "К сожалению, твоя заявка отклонена."
            # WR-05: escape the admin-set prefix symmetrically with reason — an unescaped
            # &/< in the setting would otherwise break every rejection message under HTML mode.
            await message.bot.send_message(
                tid, f"{html_module.escape(prefix)}\n\n{html_module.escape(reason)}", parse_mode="HTML"
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
    # WR-04: a stale confirm dialog re-clicked (buttons never expire) hits approve_all_pending
    # again — atomic, so it returns [] the second time. Don't run the drain or claim a count;
    # tell the admin it's already done and refresh the card.
    if not ids:
        try:
            await callback.message.edit_text("Заявки уже обработаны.", reply_markup=build_admin_keyboard())
        except Exception:
            pass
        await callback.answer("Уже обработано")
        await _show_current_card(callback.message, state)
        return
    # WR-01: schedule the welcome drain (and status sync) BEFORE the fragile edit_text. Inline
    # buttons never expire, but Telegram rejects editing a message >48h old, and the card may
    # have been deleted — if the edit threw first, the N just-approved users would be left
    # `approved` in DB with no welcome/menu/payment requisites (violates D-11 "welcome exactly
    # once"). Ordering the background sends first makes delivery independent of the edit.
    _spawn(_welcome_flipped(callback.bot, ids))  # drain sends in background
    # Массовый автосинк статуса в таблицу (Таня п.5) — один batch, fail-soft.
    if ids:
        _spawn(
            bulk_update_status_in_sheet({str(t): STATUS_LABELS["approved"] for t in ids})
        )
    try:
        await callback.message.edit_text(
            f"✅ Одобрено: {len(ids)}. Рассылаю приветствия…",
            reply_markup=build_admin_keyboard(),
        )
    except Exception as e:
        logger.warning(f"appr_all_yes: confirm edit failed (welcome drain already scheduled): {e}")
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
    skipped = set((await state.get_data()).get("rcpt_skipped", []))
    total = await get_receipt_pending_count()
    offset = 0
    visible: list[dict] = []
    while not visible and offset < total:
        batch = await get_receipt_pending_users(limit=50, offset=offset)
        if not batch:
            break
        visible = [u for u in batch if u["telegram_id"] not in skipped]
        offset += len(batch)
    if not visible:
        await target.answer("✅ Чеков на проверке нет.", reply_markup=build_admin_keyboard())
        return
    current = visible[0]
    # M-02: position = skipped-so-far + 1 (the shown card is the first not-yet-skipped receipt).
    # The old total - len(visible) + 1 returned e.g. 51/100 for the first card on a >50 queue.
    position = min(len(skipped) + 1, total)
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
    # H-01: disable this card's buttons now that it's confirmed, so scrolling back and
    # tapping ❌ Отклонить on it can't fire a stale reject (the db guard also blocks it).
    try:
        await callback.message.edit_reply_markup(reply_markup=None)
    except Exception:
        pass
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
        # WR-01: resolve the track before calling send_completion_and_bonus, mirroring the
        # pattern in approve_user (registration.py) — this is the primary PAID party path, so
        # without this a paying party delegate always got the full-track approve_text.
        from handlers.registration import send_completion_and_bonus
        try:
            user_row = await get_user(uid)
            participant_type = (user_row or {}).get("participant_type") or "full"
        except Exception as e2:
            logger.error(f"rcpt_confirm: failed to resolve participant_type for {uid}, defaulting to 'full': {e2}")
            participant_type = "full"
        await send_completion_and_bonus(callback.bot, uid, with_menu=False, participant_type=participant_type)
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
        # H-01: guard the reset — a stale/already-confirmed card tapped ❌ must NOT flip a
        # 'paid' user back to 'not_paid'. Only a row still in 'receipt_sent' is rejectable.
        rows = await update_payment_status(uid, "not_paid", require_status="receipt_sent")
        if rows == 0:
            await state.set_state(None)
            await message.answer(
                "Чек уже обработан (оплата подтверждена или чек не в очереди) — отклонение пропущено.",
                reply_markup=ReplyKeyboardRemove(),
            )
            await _show_current_receipt_card(message, state)
            return
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
# Quick 260726-0bc: the guide used to dump raw bot_settings keys ("pending_notify_mode —
# instant/batched"). Managers do not read keys — they read screens. It now renders as
# grouped plain-Russian cards: what the setting does, what it is set to RIGHT NOW in words,
# and which admin button changes it. `entries` carry the raw key only for the DB read.

_GUIDE_ONOFF = {"on": "✅ включено", "off": "❌ выключено"}


def _guide_text_value(raw):
    """Human 'current value' for free-text settings — managers care whether a custom text is
    set at all, not about its full body (which can be several screens long)."""
    if raw is None or not str(raw).strip():
        return None
    text = " ".join(str(raw).split())
    return f"свой текст («{text[:60]}…»)" if len(text) > 60 else f"свой текст («{text}»)"


# Each entry: key + label + what it does + how to read its value + where to change it.
# `values` maps a stored value to a human phrase; missing/unknown values fall through to
# `default` (shown with the «по умолчанию» marker) or to _guide_text_value for text settings.
SETTINGS_GUIDE_SECTIONS = [
    (
        "📝 Приём заявок",
        "Какая анкета показывается и нужно ли одобрение менеджера.",
        [
            {
                "key": "registration_mode",
                "label": "Форма регистрации",
                "what": "Краткая — бот спрашивает только имя. Полная — вся анкета.",
                "values": {"short": "⚡ краткая (только имя)", "full": "📋 полная анкета"},
                "default": "short",
                "where": "⚙️ Настройки → «📝 Регистрация»",
            },
            {
                "key": "short_approval",
                "label": "Одобрение для краткой формы",
                "what": "Авто — участник сразу попадает в меню. Вручную — ждёт менеджера.",
                "values": {"auto": "автоматически", "manual": "вручную (через «📋 Заявки»)"},
                "default": "auto",
                "where": "⚙️ Настройки → «✅ Модерация (краткая форма)»",
            },
            {
                "key": "full_approval",
                "label": "Одобрение для полной формы",
                "what": "Авто — участник сразу попадает в меню. Вручную — ждёт менеджера.",
                "values": {"auto": "автоматически", "manual": "вручную (через «📋 Заявки»)"},
                "default": "manual",
                "where": "⚙️ Настройки → «✅ Модерация (полная форма)»",
            },
            {
                "key": "reg_q_resume",
                "label": "Просить резюме",
                "what": "Участник прикладывает PDF/DOCX или пишет резюме текстом.",
                "values": _GUIDE_ONOFF,
                "default": "off",
                "where": "⚙️ Настройки → «📋 Вопросы регистрации» → «📄 Резюме»",
            },
            {
                "key": "reject_text",
                "label": "Текст при отклонении заявки",
                "what": "Что участник прочитает, если менеджер нажал «❌ Отклонить».",
                "default": "стандартный «К сожалению, твоя заявка отклонена»",
                "where": "⚙️ Настройки → «📝 Регистрация» → «🚫 При отклонении»",
            },
        ],
    ),
    (
        "🔔 Уведомления менеджерам о заявках",
        "Как часто бот дёргает вас, когда приходят новые заявки.",
        [
            {
                "key": "pending_notify_mode",
                "label": "Когда сообщать о новой заявке",
                "what": "Сразу — сообщение на каждую заявку. Пачкой — одна сводка «Заявок: N».",
                "values": {"instant": "сразу по каждой заявке", "batched": "пачкой, сводкой"},
                "default": "batched",
                "where": "⚙️ Настройки → «🔔 Уведомление о заявке»",
            },
            {
                "key": "pending_reminder_enabled",
                "label": "Напоминать о нерассмотренных заявках",
                "what": "Бот периодически пишет, сколько заявок ждёт решения.",
                "values": _GUIDE_ONOFF,
                "default": "on",
                "where": "просите разработчика — кнопки нет",
            },
            {
                "key": "pending_reminder_interval",
                "label": "Как часто напоминать",
                "what": "Интервал сводки. 900 = 15 мин, 1800 = 30 мин, 3600 = 1 час.",
                "default": "1800 (30 минут)",
                "unit": " сек.",
                "where": "⚙️ Настройки → «📝 Регистрация» → «🕒 Тайминг батчей заявок»",
            },
        ],
    ),
    (
        "⏰ Напоминания тем, кто бросил анкету",
        "Человек начал регистрацию и не дошёл до конца — бот сам его вернёт.",
        [
            {
                "key": "nudge_enabled",
                "label": "Догонять брошенные анкеты",
                "what": "Одно напоминание на человека, повторно бот не пишет.",
                "values": _GUIDE_ONOFF,
                "default": "on",
                "where": "просите разработчика — кнопки нет",
            },
            {
                "key": "nudge_after_minutes",
                "label": "Через сколько напомнить",
                "what": "Сколько минут молчания участника ждать до напоминания.",
                "default": "120 (2 часа)",
                "unit": " мин.",
                "where": "меняет разработчик по вашей просьбе",
            },
            {
                "key": "nudge_text",
                "label": "Текст напоминания",
                "what": "Что получит человек, бросивший анкету.",
                "default": "стандартный текст",
                "where": "меняет разработчик по вашей просьбе",
            },
        ],
    ),
    (
        "🎯 Предотбор по Google-таблице",
        "Пускать в бота только тех, кто уже отобран вручную (список @username в таблице). "
        "Если таблица недоступна — бот пускает всех и пишет админу, регистрация не встаёт.",
        [
            {
                "key": "preselect_enabled",
                "label": "Предотбор",
                "what": "Проверять @username по вкладке с отобранными на входе.",
                "values": _GUIDE_ONOFF,
                "default": "off",
                "where": "просите разработчика — кнопки нет",
            },
            {
                "key": "preselect_tab",
                "label": "Вкладка со списком отобранных",
                "what": "Имя вкладки в вашей Google-таблице, где лежат @username.",
                "default": "Отобранные",
                "where": "меняет разработчик по вашей просьбе",
            },
            {
                "key": "preselect_fail_text",
                "label": "Текст не прошедшим отбор",
                "what": "Что увидит человек, которого нет в списке.",
                "default": "«Отбор не пройден.»",
                "where": "меняет разработчик по вашей просьбе",
            },
            {
                "key": "preselect_link",
                "label": "Ссылка не прошедшим отбор",
                "what": "Куда отправить человека, если он не в списке (канал, сайт).",
                "default": "нет",
                "where": "меняет разработчик по вашей просьбе",
            },
        ],
    ),
]

# Every key the guide needs to read from bot_settings (single source for the DB fetch).
SETTINGS_GUIDE_KEYS = [
    entry["key"] for _, __, entries in SETTINGS_GUIDE_SECTIONS for entry in entries
]

_GUIDE_CHUNK_LIMIT = 3500  # Telegram hard limit is 4096; leave room for HTML tags


def _render_guide_entry(entry: dict, raw) -> str:
    """One settings card: label, what it does, what it is set to now, where to change it."""
    values = entry.get("values") or {}
    if raw is not None and str(raw).strip():
        stored = str(raw).strip()
        if values:
            shown = values.get(stored, stored)
        else:
            unit = entry.get("unit")
            shown = f"{stored}{unit}" if unit and stored.isdigit() else _guide_text_value(raw)
    else:
        # Unset key → show what the bot actually does today, in the same human wording.
        default = entry["default"]
        shown = f"{values.get(default, default)} — по умолчанию"

    return (
        f"<b>{html_module.escape(entry['label'])}</b>\n"
        f"{html_module.escape(entry['what'])}\n"
        f"Сейчас: <b>{html_module.escape(str(shown))}</b>\n"
        f"Где менять: {html_module.escape(entry['where'])}"
    )


def _render_settings_guide(sections: list, current: dict) -> list[str]:
    """Render the guide as a list of Telegram-sized messages (one or more sections each)."""
    blocks = ["📖 <b>Справка: что где настраивается</b>"]
    for title, subtitle, entries in sections:
        block = [f"<b>{html_module.escape(title)}</b>\n{html_module.escape(subtitle)}"]
        block += [_render_guide_entry(e, current.get(e["key"])) for e in entries]
        blocks.append("\n\n".join(block).rstrip())
    blocks.append(
        "Остальное — кнопками в ⚙️ Настройки форума: тексты, даты, фото, вопросы анкеты, "
        "оплата, согласия, Party.\nПодробный гайд — файл ADMIN_GUIDE.md, короткая "
        "версия — ADMIN_CHEATSHEET.md."
    )

    messages, buf = [], ""
    for block in blocks:
        candidate = f"{buf}\n\n{block}" if buf else block
        if len(candidate) > _GUIDE_CHUNK_LIMIT and buf:
            messages.append(buf)
            buf = block
        else:
            buf = candidate
    if buf:
        messages.append(buf)
    return messages


async def _send_settings_guide(target: types.Message):
    current = {key: await get_setting(key) for key in SETTINGS_GUIDE_KEYS}
    for chunk in _render_settings_guide(SETTINGS_GUIDE_SECTIONS, current):
        await target.answer(chunk, parse_mode="HTML")


@router.message(Command("settings_guide"), is_admin)
async def cmd_settings_guide(message: types.Message):
    await _send_settings_guide(message)


@router.callback_query(F.data == "admin_settings_guide")
async def show_admin_settings_guide(callback: types.CallbackQuery):
    if callback.from_user.id not in config.ADMIN_IDS:
        await callback.answer("Недостаточно прав", show_alert=True)
        return
    await _send_settings_guide(callback.message)
    await callback.answer()
