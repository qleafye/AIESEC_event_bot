import csv
import io
import asyncio
import logging
import os
import re
from aiogram import Router, F, types, Bot
from aiogram.filters import Command, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.types import BufferedInputFile, InlineKeyboardMarkup, InlineKeyboardButton, ReplyKeyboardRemove
from config import config
from database.db import (
    get_stats,
    get_all_users_ids,
    export_users_csv,
    get_user_by_username,
    get_monthly_registration_stats,
    get_source_stats,
    get_setting,
    set_setting,
    delete_setting,
)
from handlers.states import Broadcast, EditSetting
from keyboards.builders import get_cancel_kb, MENU_BUTTONS
from handlers.registration import REG_FLOW, REG_DEFAULTS, REG_LABELS

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


def build_admin_keyboard():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📊 Статистика регистраций", callback_data="admin_stats")],
        [InlineKeyboardButton(text="🗓 Регистрации по месяцам", callback_data="admin_monthly_stats")],
        [InlineKeyboardButton(text="📈 Источники", callback_data="admin_source_stats")],
        [InlineKeyboardButton(text="📄 Экспорт CSV", callback_data="admin_export_csv")],
        [InlineKeyboardButton(text="📢 Рассылка", callback_data="admin_broadcast")],
        [InlineKeyboardButton(text="⚙️ Настройки форума", callback_data="admin_settings")],
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
        "/find @username - Найти пользователя по юзернейму"
    )
    await message.answer(text, parse_mode="HTML", reply_markup=build_admin_keyboard())

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
    ("source_options", "📢 Источники", "Отправьте варианты источников, каждый с новой строки"),
    ("reg_complete_text", "✅ После регистрации", "Введите текст, который увидит пользователь после регистрации (поддерживается HTML-разметка)"),
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

    enabled_q = 0
    for _, sk in REG_FLOW:
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
        elif len(value) > 60:
            status = value[:60] + "…"
        else:
            status = value
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

    buttons = [
        [InlineKeyboardButton(text=toggle_text, callback_data="settings_toggle_reg")],
        [InlineKeyboardButton(text=bonus_toggle_text, callback_data="settings_toggle_bonus")],
        [InlineKeyboardButton(text="📋 Вопросы регистрации", callback_data="admin_reg_questions")],
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

    text = f"{prompt}:"
    if current:
        text = f"Текущее значение: <b>{current}</b>\n\n{text}"
    text += "\n\n<i>Отправьте «-» чтобы скрыть это поле.</i>"

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


@router.message(EditSetting.waiting_for_value, is_admin)
async def settings_edit_value(message: types.Message, state: FSMContext):
    data = await state.get_data()
    key = data["setting_key"]
    value = (message.text or "").strip()

    if value == "-":
        await delete_setting(key)
    else:
        await set_setting(key, value)

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
        [InlineKeyboardButton(text="❌ Отмена", callback_data="broadcast_cancel")],
    ])
    await callback.message.edit_text("Выберите целевую аудиторию рассылки:", reply_markup=kb)
    await state.set_state(Broadcast.target_selection)
    await callback.answer()

@router.message(Command("export"), is_admin)
async def cmd_export(message: types.Message):
    headers, rows = await export_users_csv()
    
    output = io.StringIO()
    # Используем разделитель ;, так как Excel в РФ часто его ждет
    # quotechar='"' нужен чтобы экранировать поля с кавычками или разделителями
    writer = csv.writer(output, delimiter=';', quotechar='"', quoting=csv.QUOTE_MINIMAL)
    writer.writerow(headers)
    writer.writerows(rows)
    
    output.seek(0)
    # Используем utf-8-sig, чтобы добавить BOM (Byte Order Mark). 
    # Это подскажет Excel, что файл в кодировке UTF-8 и починит кракозябры.
    file_bytes = output.getvalue().encode('utf-8-sig')
    document = BufferedInputFile(file_bytes, filename="users.csv")
    
    await message.answer_document(document, caption="База данных пользователей")

@router.message(Command("broadcast"), is_admin)
async def cmd_broadcast(message: types.Message, state: FSMContext):
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📢 Все пользователи", callback_data="broadcast_all")],
        [InlineKeyboardButton(text="📄 По файлу в проекте", callback_data="broadcast_local")],
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
            # Handle potential common delimiters
            clean_line = line.replace(',', '').replace(';', '')
            
            if clean_line.isdigit():
                user_ids.append(int(clean_line))
        
        if not user_ids:
            await callback.message.edit_text("⚠️ Файл пуст или не содержит корректных ID.")
            await state.clear()
            return

        # Unique IDs
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
        # Получаем caption_html для сохранения всех гиперссылок и форматирования
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
        try:
            await bot.send_media_group(chat_id, media=media)
            count += 1
            await asyncio.sleep(0.05)
        except Exception:
            blocked += 1

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
        try:
            await message.send_copy(chat_id)
            count += 1
            await asyncio.sleep(0.05) # Prevent flood wait
        except Exception:
            blocked += 1
            
    await message.answer(
        f"Рассылка завершена.\n"
        f"✅ Успешно: {count}\n"
        f"❌ Недоступно: {blocked}"
    )
    await state.clear()


# --- Registration Question Toggles ---

async def render_questions_text() -> str:
    lines = ["📋 <b>Вопросы регистрации</b>", ""]
    lines.append("<i>Действуют в режиме «📋 Полная регистрация».</i>")
    lines.append("")

    for _, setting_key in REG_FLOW:
        label = REG_LABELS.get(setting_key, setting_key)
        val = await get_setting(setting_key)
        is_on = (val == "on") if val is not None else (REG_DEFAULTS.get(setting_key, "on") == "on")
        status = "✅" if is_on else "❌"
        lines.append(f"{status} {label}")

    return "\n".join(lines)


async def build_questions_keyboard():
    buttons = []
    for _, setting_key in REG_FLOW:
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
