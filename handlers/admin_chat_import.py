"""«🏆 Рейтинг чата» -> «📥 Загрузить историю чата»: перенос истории чата, которая была до того, как
бот начал вести учёт, из экспорта Telegram Desktop в рейтинг чата.

Менеджер выбирает чат кнопкой (из привязанных к городам), присылает файл result.json документом,
видит предпросмотр — сколько сообщений и авторов добавится, за какие даты, сколько уже есть — и
подтверждает кнопкой. Текст сообщений не сохраняется, только длина. Повторная загрузка ничего не
задваивает (запись по id сообщения), поэтому большую историю можно присылать по частям.

Логика — `services.chat_export_import` (она же под `tools/chat_export_import.py`). Разбор и запись
синхронные и идут в потоке (`asyncio.to_thread`), чтобы большой файл не вешал бота.

Шов: своего `Router()` нет, декорирует общий `handlers.admin.router`; подключается хвостовым
импортом `handlers/admin_chat_rating.py`. Право — `settings`, как у экрана рейтинга чата.
"""
from __future__ import annotations

import asyncio
import html
import io
import logging
import sqlite3

from aiogram import Bot, F, types
from aiogram.filters import Command, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardRemove

from cities import city_label
from config import config
from handlers.admin import router
from handlers.admin_chat_rating import _screen_city
from handlers.states import ChatExportImport
from keyboards.builders import get_cancel_kb
from services import chat_export_import as svc
from services import chat_tracking

logger = logging.getLogger(__name__)

# INVARIANT (13-01 cap-test): every `@router.*` decorator below MUST fit on ONE line.

_MAX_BYTES = 20 * 1024 * 1024  # потолок скачивания файлов ботом (Bot API)
_lock = asyncio.Lock()
_STALE = "Кнопка устарела — откройте «🏆 Рейтинг чата» заново."
_BACK = [InlineKeyboardButton(text="⬅️ К рейтингу чата", callback_data="chrate:back")]

HOWTO = (
    "Как получить файл: на компьютере откройте Telegram Desktop → нужный чат → три точки справа "
    "вверху → «Экспорт истории чата». Уберите все галочки (фото, видео, файлы — они не нужны), "
    "в поле «Формат» выберите <b>Machine-readable JSON</b> и нажмите «Экспортировать». Получится "
    "файл <b>result.json</b> — пришлите его сюда <b>документом</b> (скрепкой, а не как фото)."
)
TOO_BIG = (
    "Файл больше 20 МБ — Telegram не позволяет боту принимать такие файлы. Что сделать: в окне "
    "экспорта нажмите «Дата» и выгрузите историю частями (например, по месяцу), затем пришлите "
    "каждую часть отдельно. Повторы бот пропускает сам, так что ничего не задвоится."
)


async def _bound_for_screen(admin_id: int) -> list[dict]:
    """Привязанные чаты; если в шапке админки выбран город — только его чат."""
    code, _header = await _screen_city(admin_id)
    chats = await chat_tracking.bound_chats()
    if code is not None:
        chats = [c for c in chats if c["city"] == code]
    return chats


async def _chat_name(entry: dict) -> str:
    title = (entry.get("title") or "").strip()
    city = await city_label(entry["city"]) if entry.get("city") else ""
    if title and city:
        return f"{title} — {city}"
    return title or city or "чат делегатов"


async def _edit(callback: types.CallbackQuery, text: str, kb: InlineKeyboardMarkup) -> None:
    try:
        await callback.message.edit_text(text, parse_mode="HTML", reply_markup=kb)
    except Exception as e:
        if "not modified" in str(e).lower():
            return
        await callback.message.answer(text, parse_mode="HTML", reply_markup=kb)


async def _ask_file(callback: types.CallbackQuery, state: FSMContext, entry: dict) -> None:
    await state.clear()
    await state.update_data(chat_id=entry["chat_id"])
    await state.set_state(ChatExportImport.waiting_file)
    name = html.escape(await _chat_name(entry))
    await callback.message.answer(
        f"<b>📥 Загрузка истории чата</b>\nЧат: {name}\n\n"
        "Рейтинг чата считается с момента, когда бот начал вести учёт. Всё, что писали раньше, "
        "можно добавить из выгрузки истории.\n\n"
        f"{HOWTO}\n\nФайл до 20 МБ. Текст сообщений бот не сохраняет — только их длину. "
        "Перед записью я покажу, что добавится, и спрошу подтверждение.",
        parse_mode="HTML", reply_markup=get_cancel_kb(),
    )


@router.callback_query(F.data == "chimp:open")
async def chat_import_open(callback: types.CallbackQuery, state: FSMContext):
    await state.clear()
    chats = await _bound_for_screen(callback.from_user.id)
    if not chats:
        await _edit(
            callback,
            "<b>📥 Загрузка истории чата</b>\n\nНет чата, в который можно загрузить историю: чат "
            "делегатов ещё не привязан. Добавьте бота в чат делегатов администратором — бот сам "
            "напишет вам в личку и спросит, к какому городу относится чат. Ответьте кнопкой и "
            "вернитесь сюда.",
            InlineKeyboardMarkup(inline_keyboard=[_BACK]),
        )
        await callback.answer()
        return
    if len(chats) == 1:
        await _ask_file(callback, state, chats[0])
        await callback.answer()
        return
    rows = [
        [InlineKeyboardButton(text=await _chat_name(c), callback_data=f"chimp:chat:{c['chat_id']}")]
        for c in chats
    ]
    rows.append(_BACK)
    await _edit(
        callback,
        "<b>📥 Загрузка истории чата</b>\n\nВ какой чат загрузить историю?",
        InlineKeyboardMarkup(inline_keyboard=rows),
    )
    await callback.answer()


@router.callback_query(F.data.startswith("chimp:chat:"))
async def chat_import_pick(callback: types.CallbackQuery, state: FSMContext):
    raw = callback.data.split(":", 2)[2]
    try:
        chat_id = int(raw)
    except ValueError:
        await callback.answer(_STALE, show_alert=True)
        return
    entry = next((c for c in await _bound_for_screen(callback.from_user.id)
                  if c["chat_id"] == chat_id), None)
    if entry is None:
        await callback.answer(_STALE, show_alert=True)
        return
    await _ask_file(callback, state, entry)
    await callback.answer()


@router.message(StateFilter(ChatExportImport), Command("cancel"))
@router.message(StateFilter(ChatExportImport), F.text == "Отмена")
async def chat_import_cancel(message: types.Message, state: FSMContext):
    await state.clear()
    await message.answer("Отменено. Ничего не загружено.", reply_markup=ReplyKeyboardRemove())


@router.callback_query(F.data == "chimp:cancel")
async def chat_import_cancel_button(callback: types.CallbackQuery, state: FSMContext):
    await state.clear()
    await callback.message.answer("Отменено. Ничего не загружено.", reply_markup=ReplyKeyboardRemove())
    await callback.answer()


def _db_plan(raw: bytes, label: str, chat_id: int) -> dict:
    """В потоке: разбор файла и предпросмотр. ExportError/UnicodeDecodeError — наверх."""
    data = svc.parse_export(raw.decode("utf-8-sig"), label)
    conn = sqlite3.connect(config.DB_PATH, timeout=30)
    try:
        return svc.make_plan(conn, data, chat_id)
    finally:
        conn.close()


def _db_apply(plan: dict) -> tuple[int, int]:
    conn = sqlite3.connect(config.DB_PATH, timeout=30)
    try:
        return svc.apply_plan(conn, plan)
    finally:
        conn.close()


def _range(span) -> str:
    return f"{span[0]} — {span[1]}" if span else "—"


def _export_chat_matches(plan: dict) -> bool:
    export_id = plan.get("export_id")
    if export_id is None:
        return True
    try:
        n = int(export_id)
    except (TypeError, ValueError):
        return True
    return plan["chat_id"] in (int(f"-100{n}"), -n)


def render_preview(plan: dict, chat_name: str) -> tuple[str, InlineKeyboardMarkup]:
    stats = plan["stats"]
    lines = [
        "<b>📥 Загрузка истории чата</b>",
        f"Чат: {html.escape(chat_name)}",
        "",
        f"В файле: «{html.escape(str(plan['export_name'] or 'без названия'))}», сообщений "
        f"{len(plan['messages'])}, период {_range(plan['days'])}.",
        f"Уже есть в базе: {plan['already']} — повторно не добавятся.",
        f"Добавится новых сообщений: {plan['to_add']}"
        + (f", авторов: {plan['to_add_authors']}, за {_range(plan['to_add_days'])}."
           if plan["to_add"] else "."),
    ]
    if plan["too_old"]:
        lines.append(
            f"Старше срока хранения истории ({plan['keep_days']} дн.): {plan['too_old']} — их не "
            "запишем, бот удалил бы их при ближайшей ночной чистке. Нужна история длиннее — "
            "увеличьте «Сколько дней хранить историю чата» в настройках и загрузите файл снова."
        )
    if stats["no_unixtime"]:
        lines.append(
            f"Пропущено сообщений без точного времени: {stats['no_unixtime']}. Это бывает у "
            "старых выгрузок — выгрузите историю заново в свежем Telegram Desktop."
        )
    lines += [
        "",
        "Текст сообщений не сохраняется, только длина. Ников в файле нет: у тех, кого бот ещё не "
        "видел в чате, в рейтинге будет имя из Telegram.",
    ]
    back = [InlineKeyboardButton(text="❌ Отмена", callback_data="chimp:cancel")]
    rows = []
    if not _export_chat_matches(plan):
        lines += [
            "",
            f"⚠️ В файле история чата «{html.escape(str(plan['export_name'] or '?'))}», а вы "
            f"выбрали «{html.escape(chat_name)}». Если это тот же чат — продолжайте, если нет — "
            "отмените и пришлите другой файл.",
        ]
    if plan["to_add"]:
        label = "✅ Загрузить" if _export_chat_matches(plan) else "⚠️ Это тот же чат — загрузить"
        rows.append([InlineKeyboardButton(text=f"{label} ({plan['to_add']})", callback_data="chimp:go")])
    else:
        lines += ["", "Добавлять нечего: всё из файла уже есть в базе или старше срока хранения."]
    rows.append(back)
    return "\n".join(lines), InlineKeyboardMarkup(inline_keyboard=rows)


@router.message(ChatExportImport.waiting_file, F.document)
async def chat_import_file(message: types.Message, state: FSMContext, bot: Bot):
    doc = message.document
    if (doc.file_size or 0) > _MAX_BYTES:
        await message.answer(TOO_BIG, parse_mode="HTML", reply_markup=get_cancel_kb())
        return
    name = doc.file_name or "файл"
    if not name.lower().endswith(".json"):
        await message.answer(
            f"Файл «{html.escape(name)}» — не result.json. Нужен именно JSON, а не архив или "
            f"таблица.\n\n{HOWTO}",
            parse_mode="HTML", reply_markup=get_cancel_kb(),
        )
        return
    data = await state.get_data()
    chat_id = data.get("chat_id")
    entry = next((c for c in await _bound_for_screen(message.from_user.id)
                  if c["chat_id"] == chat_id), None)
    if entry is None:
        await state.clear()
        await message.answer(
            "Чат больше не привязан к городу — откройте «🏆 Рейтинг чата» и начните заново.",
            reply_markup=ReplyKeyboardRemove(),
        )
        return
    buf = io.BytesIO()
    try:
        await bot.download(doc.file_id, destination=buf)
    except Exception as e:  # лимит Bot API и сетевые сбои
        logger.warning("chat_import: не скачал файл: %s", e)
        await message.answer(
            "Не получилось скачать файл. Если он больше 20 МБ, выгрузите историю частями по датам. "
            "Иначе пришлите файл ещё раз.\n\n" + TOO_BIG,
            reply_markup=get_cancel_kb(),
        )
        return
    try:
        plan = await asyncio.to_thread(_db_plan, buf.getvalue(), name, chat_id)
    except svc.ExportError as e:
        await message.answer(f"{e}\n\nПришлите правильный файл или нажмите «Отмена».",
                             reply_markup=get_cancel_kb())
        return
    except UnicodeDecodeError:
        await message.answer(
            f"Файл «{name}» не читается как текст. Нужен JSON из экспорта Telegram Desktop.\n\n"
            + svc_howto_plain(),
            reply_markup=get_cancel_kb(),
        )
        return
    await state.update_data(plan=plan, chat_name=await _chat_name(entry))
    await state.set_state(ChatExportImport.confirm)
    text, kb = render_preview(plan, await _chat_name(entry))
    await message.answer("Файл разобран.", reply_markup=ReplyKeyboardRemove())
    await message.answer(text, parse_mode="HTML", reply_markup=kb)


@router.message(ChatExportImport.waiting_file)
async def chat_import_not_document(message: types.Message):
    await message.answer(
        "Жду файл result.json документом (скрепкой, не как фото). Если передумали — нажмите «Отмена».",
        reply_markup=get_cancel_kb(),
    )


@router.callback_query(F.data == "chimp:go")
async def chat_import_go(callback: types.CallbackQuery, state: FSMContext):
    if _lock.locked():
        await callback.answer("Загрузка уже идёт — дождитесь итога.", show_alert=True)
        return
    async with _lock:
        data = await state.get_data()
        plan = data.get("plan")
        if (await state.get_state()) != ChatExportImport.confirm.state or not plan:
            await callback.answer(_STALE, show_alert=True)
            return
        await state.clear()
        await callback.answer()
        await callback.message.edit_reply_markup(reply_markup=None)
        added_messages, added_reactions = await asyncio.to_thread(_db_apply, plan)
    logger.info(
        "chat_import: by=%s chat=%s сообщений=%s реакций=%s",
        callback.from_user.id, plan["chat_id"], added_messages, added_reactions,
    )
    lines = [f"Готово. Добавлено сообщений: {added_messages}, реакций: {added_reactions}."]
    if added_messages < plan["to_add"]:
        lines.append("Часть сообщений уже появилась в базе, пока вы смотрели предпросмотр — их пропустил.")
    lines.append("Рейтинг на дашборде пересчитается сам. Повторная загрузка того же файла ничего не задвоит.")
    await callback.message.answer("\n".join(lines), reply_markup=InlineKeyboardMarkup(inline_keyboard=[_BACK]))


__all__ = [
    "chat_import_open", "chat_import_pick", "chat_import_file", "chat_import_go",
    "chat_import_cancel", "chat_import_cancel_button", "chat_import_not_document",
]
