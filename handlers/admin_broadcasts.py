"""Phase 13 (13-05, REFAC-01): broadcast seam.

`admin.py:2360-3124` moved byte-for-byte, contiguous slice — immediate/local-file/segment
(unsubscribed/incomplete)/filtered broadcasts, the schedule-a-broadcast wizard, `/scheduled`
management, and the manual allowlist refresh command — onto the SAME shared `admin.router`
(13-02/13-03/13-04/13-05 shared-router seam-import technique).

`pending_albums` (admin.py:167) and `_wait_and_send_album` (CONCERNS.md coupling warning) MOVE
TOGETHER here: the module-level album buffer and its consumer belong to the same file, or album
broadcasts break with no import error.
"""
import asyncio
import csv
import html as html_module
import io
import json
import os
import re

from aiogram import F, types, Bot
from aiogram.exceptions import TelegramRetryAfter
from aiogram.filters import Command, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.types import BufferedInputFile, InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardRemove

from database.db import (
    get_all_users_ids,
    get_non_subscriber_ids,
    get_incomplete_user_ids,
    export_users_csv,
    create_scheduled_broadcast,
    list_pending_broadcasts,
    list_sending_broadcasts,
    count_deliveries,
    cancel_scheduled_broadcast,
    count_and_list_filtered,
    get_distinct_filter_values,
)
from services.scheduler import (
    _parse_schedule_dt,
    _fmt_dt,
    _now_moscow_naive,
    schedule_broadcast_job,
    cancel_broadcast_job,
)
from services.allowlist import refresh_allowlist, allowlist_size
from services.background import spawn as _spawn
from keyboards.builders import get_cancel_kb
from handlers.states import Broadcast
from cities import CITIES, cities_module_on, city_label, city_scope
from handlers.admin import router

# INVARIANT (13-01 cap-test, extended by 13-04 to scan every handlers/admin*.py file): every
# `@router.*` decorator below MUST fit on ONE line.

pending_albums = {}


@router.callback_query(F.data == "admin_broadcast")
async def show_admin_broadcast(callback: types.CallbackQuery, state: FSMContext):
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

@router.message(Command("export"))
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

@router.message(Command("broadcast"))
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
    user_ids = await get_non_subscriber_ids()
    await _start_segment_broadcast(
        callback, state, user_ids,
        f"🚫 {len(set(user_ids))} пользователей не подписаны на канал, давайте пришлём им уведомление.\n"
        "Теперь отправьте сообщение для рассылки.",
    )


@router.callback_query(F.data == "broadcast_incomplete", Broadcast.target_selection)
async def process_broadcast_incomplete(callback: types.CallbackQuery, state: FSMContext):
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

@router.message(Broadcast.message)
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


@router.message(Broadcast.schedule_when)
async def broadcast_schedule_when(message: types.Message, state: FSMContext):
    when = _parse_schedule_dt(message.text)
    if when is None:
        await message.answer("❌ Не понял дату. Формат: ДД.ММ.ГГГГ ЧЧ:ММ (напр. 01.07.2026 14:30)")
        return
    # TZFIX-260816: admin input is Moscow wall-clock — compare against Moscow, not the
    # container clock (UTC), or a past-MSK time can slip through as "future" and fire instantly.
    if when <= _now_moscow_naive():
        await message.answer("❌ Это время уже прошло. Введите будущую дату.")
        return
    await state.update_data(schedule_dt=when)
    await message.answer(
        f"✅ Запланировано на {when.strftime('%d.%m.%Y %H:%M')}.\n"
        "Теперь отправьте сообщение (текст или фото с подписью) для рассылки.",
        reply_markup=get_cancel_kb(),
    )
    await state.set_state(Broadcast.schedule_message)


@router.message(Broadcast.schedule_message)
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


@router.message(Command("scheduled"))
async def cmd_scheduled(message: types.Message):
    rows = await list_pending_broadcasts()
    # Review 260817 §B2: a broadcast that is mid-send (or died mid-send and waits for the boot
    # reclaim) is shown too, with its per-recipient checkpoint count — otherwise the manager
    # sees nothing and re-creates it by hand while the original is still going out.
    sending = await list_sending_broadcasts()
    if not rows and not sending:
        await message.answer("Нет запланированных рассылок.")
        return
    for row in sending:
        ok, failed = await count_deliveries(row["id"])
        preview = re.sub(r"<[^>]+>", "", row.get("text") or "(фото)")[:60]
        tail = f", не доставлено {failed}" if failed else ""
        await message.answer(
            f"#{row['id']} — {row['scheduled_at']}\n{html_module.escape(preview)}\n"
            f"⏳ Отправляется: доставлено {ok}{tail}"
        )
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
    # Phase 07.2 (CITY-02). NOT the same thing as "city": "Город" above — that one is the
    # DELEGATE's own home city (a registration question); this one is the CITY OF THE EVENT.
    # The two labels sit in the same menu and must stay visually distinguishable on screen —
    # confusing them is the most expensive mistake this feature can make.
    "event_city": "Город мероприятия",
}

# Fields whose value is chosen from a DB-distinct picker (buttons pulled from real data).
# Everything in the filter menu except «Дата регистрации» (a before/after threshold).
_PICKER_FIELDS = {
    "city", "university", "source", "status", "payment_status",
    "local_committee", "department", "aiesec_role", "education_status",
    "course", "study_field", "position", "attendance_format",
    "participant_type",  # Phase 5 (D-19) — must ALSO be in db._FILTER_COLUMNS or it's dropped
    # Phase 07.2 (CITY-02) — same двойная регистрация rule: also in db._FILTER_COLUMNS, else
    # the filter shows on screen and never reaches the SQL. No separate handler needed —
    # `filter_pick_field` is subscribed to `filter_f_{fld}` across this whole set, computed at
    # import time. Its values are the ONLY ones not sourced from a DB DISTINCT (see
    # `_show_value_picker`).
    "event_city",
}

# How many value buttons per picker page (long cyrillic values → 1 per row).
_FILTER_PAGE_SIZE = 8


def _value_picker_kb(field: str, options: list[str], page: int,
                     labels: dict | None = None) -> InlineKeyboardMarkup:
    """Paginated value picker. The value itself never goes in callback_data (cyrillic values
    blow past Telegram's 64-byte limit) — buttons carry the option INDEX; the full list lives
    in FSM state. payment_status shows human labels.

    `labels` (Phase 07.2, CITY-02) is an optional {option: display_text} map for fields whose
    stored value is a machine code — event_city stores "spb", the button must read
    "Санкт-Петербург…". When omitted, behavior is byte-identical to before.
    """
    total = len(options)
    pages = max(1, (total + _FILTER_PAGE_SIZE - 1) // _FILTER_PAGE_SIZE)
    page = max(0, min(page, pages - 1))
    start = page * _FILTER_PAGE_SIZE
    rows = []
    for i, v in enumerate(options[start:start + _FILTER_PAGE_SIZE], start=start):
        if labels:
            label = str(labels.get(v, v))
        else:
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
# Phase 19 (Mini App): словарь переехал в корневой `reg_labels.py` — профиль Mini App
# показывает тот же статус оплаты теми же словами.
from reg_labels import PAYMENT_STATUS_LABELS as _PAYMENT_STATUS_LABELS  # noqa: E402


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
        elif f.get("label"):
            # Phase 07.2 (CITY-02): a filter may carry its own human display text (a city code
            # is unreadable in the summary). Generic — not a city special case. Escaped like
            # every other value; the label comes from bot_settings and is admin-editable.
            parts.append(f"{label} = {html_module.escape(str(f['label']))}")
        else:
            parts.append(f"{label} = {val}")
    return " И ".join(parts)


def _filter_menu_kb(filters: list[dict], *, show_city: bool = False) -> InlineKeyboardMarkup:
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
    # Phase 07.2 (CITY-02): only with the cities module on. Default False keeps the keyboard
    # byte-identical to the pre-phase one when the module is off.
    if show_city:
        kb.append([InlineKeyboardButton(text="🏙 Город мероприятия",
                                        callback_data="filter_f_event_city")])
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
    # The menu deliberately opens EMPTY — the admin's selected city does NOT pre-fill this
    # filter (07.2-04 decision). A broadcast has an asymmetric risk: a condition the manager
    # never set is exactly how a message reaches a third of the base while they believe it
    # reached everyone. Moderation/export can't fail that way (an empty screen is visible).
    kb = _filter_menu_kb(filters, show_city=await cities_module_on())
    if edit:
        await target.edit_text(text, reply_markup=kb)
    else:
        await target.answer(text, reply_markup=kb)


@router.callback_query(F.data == "broadcast_filter", Broadcast.target_selection)
async def broadcast_filter_start(callback: types.CallbackQuery, state: FSMContext):
    await state.update_data(filters=[])
    await callback.answer()
    await _render_filter_menu(callback.message, [], edit=True)
    await state.set_state(Broadcast.filter_field)


# Phase 14 (CFG-02, IN-01): human RU labels for the «Трек» filter picker's buttons. The
# callback_data / value stored in FSM (and in the resulting filter, sent to the DB query) is
# still the raw code — this dict ONLY changes what the button text says. Deliberately separate
# from `_render_application_card`'s own `track_label` dict a few hundred lines down: that one
# renders a pending-application card and its literals are pinned by existing tests — not
# touched here.
_TRACK_LABELS = {
    "full": "Полный",
    "party_overnight": "🎉 Вечеринка с ночёвкой",
    "party_noovernight": "🎉 Вечеринка без ночёвки",
    "short": "⚡ Краткая анкета (акция)",
}


async def _show_value_picker(callback: types.CallbackQuery, state: FSMContext, field: str, prompt: str):
    """Load distinct DB values for `field`, stash them in FSM, render the paginated picker."""
    if field == "event_city":
        # WR-04: гейт живёт В ХЭНДЛЕРЕ, а не только в отрисовке клавиатуры. _render_filter_menu
        # лишь ПРЯЧЕТ кнопку при выключенном модуле, но `filter_pick_field` подписан на
        # множество, вычисленное из _PICKER_FIELDS на импорте, а инлайн-кнопки не истекают:
        # меню фильтров, нарисованное при включённом модуле, после выключения тумблера
        # оставалось рабочим, и рассылка молча сужалась по городу. Контракт module-off
        # («фаза не изменила поведение, пока менеджер не включил модуль») требует отказа здесь.
        if not await cities_module_on():
            await callback.answer("Модуль городов выключен.", show_alert=True)
            return
        # Phase 07.2 (CITY-02): the ONE field whose values come from the REGISTRY, not from a
        # DISTINCT over the column. `get_distinct_filter_values` filters out NULLs by
        # construction — and every application registered before the cities module has
        # event_city NULL, so the DEFAULT city (where all of them live) would simply not be
        # offered. A city with zero applications so far would be unofferable too.
        options = [c["code"] for c in CITIES]
        labels = {code: await city_label(code) for code in options}
    elif field == "participant_type":
        # Phase 14 (CFG-02, IN-01): RU labels instead of raw codes (party_noovernight etc.);
        # fail-soft for a value not in _TRACK_LABELS — falls back to the raw code as the label
        # (dict.get default), never crashes the picker.
        options = await get_distinct_filter_values(field)
        labels = {code: _TRACK_LABELS.get(code, code) for code in options}
    else:
        options = await get_distinct_filter_values(field)
        labels = None
    if not options:
        await callback.answer("В базе нет значений для этого поля.", show_alert=True)
        return
    # `filter_option_labels` rides along in FSM so pagination redraws keep the human labels.
    await state.update_data(filter_options=options, filter_page=0, filter_option_labels=labels)
    await callback.answer()
    await callback.message.edit_text(
        prompt, reply_markup=_value_picker_kb(field, options, 0, labels)
    )


@router.callback_query(F.data.in_({f"filter_f_{fld}" for fld in _PICKER_FIELDS}), Broadcast.filter_field)
async def filter_pick_field(callback: types.CallbackQuery, state: FSMContext):
    """Every attribute field → a DB-distinct value picker (no free-text typing)."""
    field = callback.data[len("filter_f_"):]
    await state.update_data(filter_pending_field=field, filter_pending_op=None)
    await _show_value_picker(callback, state, field, f"Выберите значение — «{_FILTER_FIELD_LABELS.get(field, field)}»:")


@router.callback_query(F.data == "filter_f_date", Broadcast.filter_field)
async def filter_pick_date(callback: types.CallbackQuery, state: FSMContext):
    await callback.answer()
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="После", callback_data="filter_d_after"),
         InlineKeyboardButton(text="До", callback_data="filter_d_before")],
        [InlineKeyboardButton(text="← Назад", callback_data="filter_back")],
    ])
    await callback.message.edit_text("Зарегистрированы…", reply_markup=kb)


@router.callback_query(F.data.in_({"filter_d_after", "filter_d_before"}), Broadcast.filter_field)
async def filter_pick_date_op(callback: types.CallbackQuery, state: FSMContext):
    op = "after" if callback.data.endswith("after") else "before"
    await state.update_data(filter_pending_field="registration_date", filter_pending_op=op)
    opl = "после" if op == "after" else "до"
    await _show_value_picker(callback, state, "registration_date", f"Зарегистрированы {opl} даты:")


@router.callback_query(F.data.startswith("filter_optpage:"), Broadcast.filter_field)
async def filter_page_nav(callback: types.CallbackQuery, state: FSMContext):
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
    await callback.message.edit_reply_markup(
        reply_markup=_value_picker_kb(field, options, page, data.get("filter_option_labels"))
    )


@router.callback_query(F.data.startswith("filter_opt:"), Broadcast.filter_field)
async def filter_pick_value(callback: types.CallbackQuery, state: FSMContext):
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
    elif field == "event_city":
        # `exclude` is computed by cities.city_scope — the SAME function that scopes both
        # moderation queues and the CSV export, so "which codes count as this city" has
        # exactly one definition. It travels inside the filter dict because database/db.py
        # may not import cities (cycle), and it must survive the JSON round-trip a scheduled
        # broadcast's spec goes through. `label` renders the summary in human words.
        labels = data.get("filter_option_labels") or {}
        scope = city_scope(value)
        filters.append({
            "field": field,
            "value": value,
            "exclude": list(scope[1]) if scope else [],
            "label": labels.get(value, value),
        })
    else:
        filters.append({"field": field, "value": value})
    await state.update_data(
        filters=filters, filter_pending_field=None, filter_pending_op=None,
        filter_options=[], filter_page=0, filter_option_labels=None,
    )
    await callback.answer()
    await _render_filter_menu(callback.message, filters, edit=True)


@router.callback_query(F.data == "filter_back", Broadcast.filter_field)
async def filter_back(callback: types.CallbackQuery, state: FSMContext):
    """Abandon the in-progress field pick, return to the filter menu."""
    data = await state.get_data()
    await state.update_data(filter_pending_field=None, filter_pending_op=None, filter_options=[],
                            filter_page=0, filter_option_labels=None)
    await callback.answer()
    await _render_filter_menu(callback.message, data.get("filters", []), edit=True)


@router.callback_query(F.data == "filter_count", Broadcast.filter_field)
async def filter_count(callback: types.CallbackQuery, state: FSMContext):
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
    data = await state.get_data()
    filters = data.get("filters", [])
    ids = await count_and_list_filtered(filters)
    await _start_segment_broadcast(
        callback, state, ids,
        f"🎯 {len(set(ids))} получателей по фильтру.\nТеперь отправьте сообщение для рассылки.",
    )


@router.callback_query(F.data == "filter_schedule", Broadcast.filter_field)
async def filter_schedule(callback: types.CallbackQuery, state: FSMContext):
    # filters stay in FSM state; the schedule flow reads them as filter_spec
    await callback.answer()
    await callback.message.edit_text(
        "🕓 Введите дату и время рассылки в формате ДД.ММ.ГГГГ ЧЧ:ММ (напр. 01.07.2026 14:30):"
    )
    await state.set_state(Broadcast.schedule_when)


# ── Phase 3 (VERIF): manual allowlist refresh ────────────────────────────────

@router.message(Command("refresh_allowlist"))
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
