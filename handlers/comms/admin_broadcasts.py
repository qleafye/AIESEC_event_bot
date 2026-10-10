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
from shared.secret_redact import redact_secrets
import json
import logging
import os
import re
from datetime import datetime

from aiogram import F, types, Bot
from aiogram.exceptions import TelegramRetryAfter
from aiogram.filters import Command, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.types import BufferedInputFile, InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardRemove

from database.ext_forms_db import list_forms as _ext_forms_list
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
    SEASON_CURRENT,
    SEASON_NONE,
    get_season_filter_options,
    # Квик 260911-0fh (RESUME-FILTER-02): поле фильтра «Резюме» — выбор «есть»/«нет».
    RESUME_HAS,
    RESUME_MISSING,
    get_resume_filter_options,
    # Квик 260914-rgr (RGR-01..07): поле фильтра «Чат делегатов» — выбор «в чате»/«не в чате».
    CHAT_IN,
    CHAT_OUT,
    get_chat_filter_options,
    # Phase 31 (31-02/31-07, D-28): поле фильтра «Автоотказ по правилу» — выбор
    # «отклонён»/«не отклонён».
    AUTO_REJECT_YES,
    AUTO_REJECT_NO,
    get_auto_reject_filter_options,
    # Делегации вузов (D-07): поле фильтра «🏫 Делегации» — «делегация вуза»/«не делегация».
    DELEGATION_YES,
    DELEGATION_NO,
    get_delegation_filter_options,
    # Форум-ночь п.6 (D-25, идея №14): поле фильтра «Отметка на форуме» — выбор
    # «пришли»/«не пришли».
    CHECKIN_YES,
    CHECKIN_NO,
    CHECKIN_DAY_TODAY,
    get_checkin_entry_picker_options,
    # «Сессия программы» — свой мастер (город → день → сессия), не входит в generic-пикер.
    any_program_sessions_exist,
    # Quick 260910-okb (BC-01..06): журнал немедленных рассылок + отзыв у получателей.
    create_broadcast,
    get_broadcast,
    list_recent_broadcasts,
    # Квик 260915-twr (Task B2): предупреждение об аудитории «Всем» на экране подтверждения.
    list_staff,
)
from services.scheduler import (
    _parse_schedule_dt,
    _fmt_dt,
    _now_moscow_naive,
    schedule_broadcast_job,
    cancel_broadcast_job,
    # Форум-ночь п.7 («❗ Важное» + «🔕 Не присылать сегодня»): общий хвост доставки, тот же,
    # что у services.scheduler::send_scheduled_broadcast — одна точка правды для пометки
    # важности (встроена в содержимое) и клавиатуры получателя, не вторая копия.
    important_prefix,
    apply_important_prefix,
    load_recipient_langs, recipient_markup,
    # Только альбом — media_group не принимает reply_markup, см. докстринг там же.
    send_mute_offer_if_eligible,
)
from services.access.allowlist import refresh_allowlist, allowlist_size
from services.infra.background import spawn as _spawn
from services.comms.broadcast_run import run_broadcast, request_stop, can_revoke
from services.comms.broadcast_scope import (
    past_season_note, restrict_to_sender_city, season_default_filter, sender_city_note, split_by_sender_city,
)
from services.forum.forum_days import day_cities_suffix  # «не пришли 25.09 — Москва»
from keyboards.builders import get_cancel_kb
from handlers.states import Broadcast
from domain.cities import CITIES, cities_module_on, city_label, city_scope
from handlers.admin import router
from config import config

logger = logging.getLogger(__name__)

# INVARIANT (13-01 cap-test, extended by 13-04 to scan every handlers/admin*.py file): every
# `@router.*` decorator below MUST fit on ONE line.

pending_albums = {}


BROADCAST_TARGET_FILE = "data/broadcast_target.txt"


def _file_broadcast_allowed(user_id: int | None) -> bool:
    """«По файлу в проекте» — рассылка по списку id, который кладут на сервер руками; менеджеру
    без доступа к серверу она бесполезна и опасна, поэтому только суперадмину."""
    return user_id is not None and user_id in config.ADMIN_IDS


def build_broadcast_menu_kb(user_id: int | None = None) -> InlineKeyboardMarkup:
    """Меню аудитории рассылки. «По файлу в проекте» показываем только суперадмину и только когда
    файл лежит на месте: кнопка, которая заведомо отвечает ошибкой, менеджера только пугает."""
    rows = [
        [InlineKeyboardButton(text="📢 Все пользователи", callback_data="broadcast_all")],
        [InlineKeyboardButton(text="🚫 Не подписаны на канал", callback_data="broadcast_unsubscribed")],
        [InlineKeyboardButton(text="📝 Не завершили регистрацию", callback_data="broadcast_incomplete")],
        [InlineKeyboardButton(text="🎯 По фильтру", callback_data="broadcast_filter")],
        [InlineKeyboardButton(text="🕓 Запланировать", callback_data="broadcast_schedule")],
        [InlineKeyboardButton(text="🗒 Последние рассылки", callback_data="admin_broadcast_log")],
        # UAT 15.09: раньше отложенные рассылки были видны только скрытой командой /scheduled —
        # кнопка открывает тот же список (_render_scheduled_list), без второго рендера.
        [InlineKeyboardButton(text="⏰ Запланированные", callback_data="admin_broadcast_scheduled")],
        [InlineKeyboardButton(text="❌ Отмена", callback_data="broadcast_cancel")],
    ]
    if _file_broadcast_allowed(user_id) and os.path.exists(BROADCAST_TARGET_FILE):
        rows.insert(1, [InlineKeyboardButton(text="📄 По файлу в проекте", callback_data="broadcast_local")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


@router.callback_query(F.data == "admin_broadcast")
async def show_admin_broadcast(callback: types.CallbackQuery, state: FSMContext):
    kb = build_broadcast_menu_kb(callback.from_user.id)
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
    kb = build_broadcast_menu_kb(message.from_user.id)
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
    if not _file_broadcast_allowed(callback.from_user.id):
        await callback.answer("Рассылка по файлу — только суперадмину. Выберите получателей кнопками выше.", show_alert=True)
        return
    file_path = BROADCAST_TARGET_FILE

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
        await callback.message.edit_text(f"Ошибка при чтении файла: {html_module.escape(redact_secrets(e))}")
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


# Quick 260910-okb (BC-02): FSM хранит альбом как простые dict'ы ({"type","file_id","caption"}),
# не объекты aiogram InputMedia* — на подтверждении (bc_go) их собирают заново этой функцией.
_ALBUM_MEDIA_CLASSES = {
    "photo": types.InputMediaPhoto,
    "video": types.InputMediaVideo,
    "document": types.InputMediaDocument,
    "audio": types.InputMediaAudio,
}


def _media_from_album_dicts(album: list[dict], important_prefix_text: str | None = None) -> list:
    """Форум-ночь п.7 (переделка): `bot.send_media_group` не принимает `reply_markup` — единственное
    место встроить пометку важности альбома — подпись ПЕРВОГО элемента (`important_prefix_text`,
    `None`/пусто — рассылка не важная, ни один элемент не трогается)."""
    media = []
    first = True
    for item in album:
        cls = _ALBUM_MEDIA_CLASSES.get(item.get("type"))
        if cls is None:
            continue
        caption = item.get("caption")
        if first and important_prefix_text:
            caption = f"{important_prefix_text}\n\n{caption}" if caption else important_prefix_text
        media.append(cls(media=item["file_id"], caption=caption, parse_mode="HTML"))
        first = False
    return media


async def _audience_warning(state: FSMContext, users_ids: list[int] | None, sender_id: int) -> str:
    """Квик 260915-twr (Task B2): «Всем» = SELECT telegram_id FROM users — менеджер/админ, не
    регистрировавшийся делегатом, в аудиторию не входит. Аудиторию не расширяем молча, но
    экран подтверждения теперь честно говорит, сколько человек из команды рассылку не получат.
    Пустая строка при K == 0 или аудитории не «Всем» — экран остаётся байт-в-байт прежним.

    `sender_id` (адресат самого экрана подтверждения, личный чат с ботом) исключён из подсчёта
    сознательно: менеджер, читающий это предупреждение прямо сейчас, и так знает, что он не
    зарегистрирован делегатом — предупреждать его о нём самом бессмысленно, речь про ОСТАЛЬНУЮ
    команду."""
    if users_ids is None:
        return ""
    data = await state.get_data()
    if data.get("target_type", "all") != "all":
        return ""
    staff = set(config.ADMIN_IDS) | {s["telegram_id"] for s in await list_staff()}
    missing = staff - set(users_ids) - {sender_id}
    if not missing:
        return ""
    from handlers.comms.admin_broadcast_status import staff_missing_note  # нет анкеты / не одобрены
    return await staff_missing_note(missing)


async def _send_confirm_prompt(
    bot: Bot, chat_id: int, state: FSMContext, total: int, users_ids: list[int] | None = None,
):
    """Экран подтверждения перед стартом рассылки (BC-01) — общий хвост и для обычного
    сообщения, и для альбома.

    Quick 260911-805 (W4-01): в отличие от отложенной ветки (broadcast_schedule_when:622),
    мгновенная рассылка НЕ переносится на конец тихих часов — она копирует уже разрешённый
    список получателей (bc_users) и не умеет сохранить альбом обратно в filter_spec (D-01).
    Попадание «сейчас» в глобальное окно (window_for_city(None)) — только честное
    предупреждение с явным «всё равно сейчас»; тумблер выключен/окна нет/время вне окна —
    экран байт-в-байт прежний (паритет обязателен).

    Квик 260915-twr (Task B2): `users_ids` — опциональный пятый аргумент, только для
    предупреждения об аудитории «Всем»; отсутствие аргумента (`None`) сохраняет экран
    байт-в-байт прежним для любого вызова, который его не передаёт. `chat_id` здесь всегда
    личный чат отправителя с ботом — используется и как адрес доставки, и как исключаемый из
    предупреждения sender_id.

    Форум-ночь п.7: строка-тумблер «❗ Отметить как важное» — читает `bc_important` из FSM
    (по умолчанию выкл, D-01), состояние переживает перерисовку (bc_important_toggle зовёт
    эту же функцию заново)."""
    dropped = int((await state.get_data()).get("bc_scope_dropped") or 0)
    warning = await sender_city_note(chat_id, dropped)
    from handlers.comms.admin_broadcast_status import confirm_extra  # сезон + статусы заявки
    season_text, season_rows = await confirm_extra(state, users_ids)
    warning += season_text + await _audience_warning(state, users_ids, chat_id)
    important = bool((await state.get_data()).get("bc_important"))
    important_btn = InlineKeyboardButton(
        text="✅ Отмечено как важное" if important else "❗ Отметить как важное",
        callback_data="bc_important_toggle",
    )
    from services.comms import quiet_hours
    now = _now_moscow_naive()
    window = await quiet_hours.window_for_city(None)
    if window is not None and quiet_hours.is_quiet(now, *window):
        start, end = window
        window_end = quiet_hours.next_window_end(now, start, end)
        text = (
            f"🌙 Сейчас тихие часы ({start.strftime('%H:%M')}–{end.strftime('%H:%M')}) — "
            "делегаты получат сообщение ночью. Мгновенная рассылка тишину не ждёт.\n"
            "Если хотите подождать — отмените и отправьте через «🕓 Запланировать» "
            f"на время после {window_end.strftime('%H:%M')}.\n\n"
            f"{warning}Отправить это {total} пользователям?"
        )
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text=f"🌙 Всё равно отправить сейчас ({total})", callback_data="bc_go")],
            *season_rows, [important_btn],
            [InlineKeyboardButton(text="❌ Отмена", callback_data="bc_no")],
        ])
        await bot.send_message(chat_id, text, reply_markup=kb)
        await state.set_state(Broadcast.confirm)
        return
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=f"✅ Отправить {total} пользователям", callback_data="bc_go")],
        *season_rows, [important_btn],
        [InlineKeyboardButton(text="❌ Отмена", callback_data="bc_no")],
    ])
    await bot.send_message(chat_id, f"{warning}Отправить это {total} пользователям?", reply_markup=kb)
    await state.set_state(Broadcast.confirm)


@router.callback_query(F.data == "bc_important_toggle", Broadcast.confirm)
async def bc_important_toggle(callback: types.CallbackQuery, state: FSMContext, bot: Bot):
    """Форум-ночь п.7: тумблер на экране подтверждения — флаг в FSM, экран перерисовывается
    той же `_send_confirm_prompt` (новым сообщением: предыдущая карточка подтверждения могла
    нести отдельную «тихие часы»-ветку текста, редактирование на месте рискует расходиться)."""
    data = await state.get_data()
    important = not bool(data.get("bc_important"))
    await state.update_data(bc_important=important)
    users_ids = data.get("bc_users", [])
    await callback.answer("❗ Отмечено как важное" if important else "Пометка снята")
    try:
        await callback.message.delete()
    except Exception:
        pass
    await _send_confirm_prompt(bot, callback.message.chat.id, state, len(users_ids), users_ids)


async def _collect_album_and_preview(media_group_id: str, users_ids: list, bot: Bot, state: FSMContext, admin_id: int):
    """Бывшая `_wait_and_send_album` (Phase 3, COMM-04) — теперь НЕ шлёт получателям, а копит
    альбом в FSM и показывает превью+подтверждение (BC-01/02), как и путь одного сообщения."""
    await asyncio.sleep(0.8)
    album_data = pending_albums.pop(media_group_id, None)
    if not album_data:
        return

    messages = album_data["messages"]
    media = []
    album_dicts = []

    for msg in messages:
        caption_text = msg.html_text if getattr(msg, "html_text", None) else None

        if msg.photo:
            file_id = msg.photo[-1].file_id
            media.append(types.InputMediaPhoto(media=file_id, caption=caption_text, parse_mode="HTML"))
            album_dicts.append({"type": "photo", "file_id": file_id, "caption": caption_text})
        elif msg.video:
            file_id = msg.video.file_id
            media.append(types.InputMediaVideo(media=file_id, caption=caption_text, parse_mode="HTML"))
            album_dicts.append({"type": "video", "file_id": file_id, "caption": caption_text})
        elif msg.document:
            file_id = msg.document.file_id
            media.append(types.InputMediaDocument(media=file_id, caption=caption_text, parse_mode="HTML"))
            album_dicts.append({"type": "document", "file_id": file_id, "caption": caption_text})
        elif msg.audio:
            file_id = msg.audio.file_id
            media.append(types.InputMediaAudio(media=file_id, caption=caption_text, parse_mode="HTML"))
            album_dicts.append({"type": "audio", "file_id": file_id, "caption": caption_text})

    if not media:
        # WR-04: unsupported-only media group — notify the admin and clear state instead of
        # silently leaving the FSM parked in Broadcast.message with no feedback.
        try:
            await bot.send_message(admin_id, "⚠️ Рассылка отменена: неподдерживаемый тип вложения.")
        except Exception:
            pass
        await state.clear()
        return

    await bot.send_media_group(admin_id, media)
    await state.update_data(
        bc_users=users_ids,
        bc_album=album_dicts,
        bc_preview=_album_preview_text(album_dicts),
    )
    await _send_confirm_prompt(bot, admin_id, state, len(users_ids), users_ids)


def _album_preview_text(album_dicts: list[dict]) -> str:
    """Ревью 470ce5e..3703ba4 (находка 🟡): раньше `full_text` альбома был заглушкой
    `«[альбом x N]»` — в экране делегата «❗ Важное» (handlers/user_actions.py::
    show_important_today) он видел эту заглушку вместо реального текста. Склейка подписей
    элементов (html, порядок как в альбоме) — если хоть одна есть; иначе честное «Альбом из N
    фото/видео» вместо технического литерала со «x»."""
    captions = [c for c in (item.get("caption") for item in album_dicts) if c]
    if captions:
        return "\n\n".join(captions)
    return f"Альбом из {len(album_dicts)} фото/видео"

@router.message(Broadcast.message)
async def process_broadcast(message: types.Message, state: FSMContext, bot: Bot):
    """BC-01: сообщение в состоянии рассылки больше НЕ уходит получателям — складывает превью
    в FSM и переводит на экран подтверждения (Broadcast.confirm); сама отправка стартует только
    по нажатию «✅ Отправить N пользователям» (bc_go)."""
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
    users_ids, scope_dropped = await split_by_sender_city(message.from_user.id, list(set(users_ids)))
    await state.update_data(bc_scope_dropped=scope_dropped)
    if not users_ids and scope_dropped:
        await message.answer(
            f"Рассылать некому: все {scope_dropped} выбранных — из другого города или их нет "
            "в базе бота, а ваши рассылки уходят только вашему городу. Рассылка отменена.",
            reply_markup=ReplyKeyboardRemove(),
        )
        await state.clear()
        return

    mgid = message.media_group_id
    if mgid:
        if mgid not in pending_albums:
            pending_albums[mgid] = {"messages": [message]}
            _spawn(_collect_album_and_preview(mgid, users_ids, bot, state, message.from_user.id))
        else:
            pending_albums[mgid]["messages"].append(message)
        return

    preview = message.html_text if (message.text or message.caption) else "[фото]"
    # Форум-ночь п.7 (переделка): «текст» и «медиа» уходят РАЗНЫМИ методами Bot API
    # (bc_go::send_one ниже) — `bot.copy_message` умеет подменить CAPTION медиа-сообщения
    # (нужно для пометки важности), но не .text чисто текстового, поэтому чисто текстовая
    # рассылка идёт `bot.send_message` с готовым текстом, а не копией. `bc_kind` — какой метод
    # использовать, `bc_content_html` — то, что реально пойдёт в текст/подпись (без пометки —
    # она приклеивается в bc_go по факту тумблера «❗»). `bc_reply_markup` — собственная
    # клавиатура менеджера (например, пересланный пост с кнопками-ссылками), если Telegram её
    # прислал вместе с сообщением — `recipient_markup` добавит строку «🔕» ПОСЛЕДНЕЙ поверх неё,
    # не заменяя (MemoryStorage хранит объект в памяти как есть, без сериализации).
    bc_kind = "text" if message.text else "media"
    content_html = message.html_text if (bc_kind == "text" or message.caption) else None
    await state.update_data(
        bc_chat_id=message.chat.id,
        bc_message_id=message.message_id,
        bc_users=users_ids,
        bc_preview=preview,
        bc_kind=bc_kind,
        bc_content_html=content_html,
        # getattr, не message.reply_markup напрямую: тестовые FakeMessage-дублёры соседних
        # тестов (COMM-04/BC-01..06 и т.д.) этот атрибут не заводят вовсе — реальный
        # aiogram.types.Message его несёт всегда (None у обычного сообщения делегата).
        bc_reply_markup=getattr(message, "reply_markup", None),
    )
    await message.send_copy(message.chat.id)
    await _send_confirm_prompt(bot, message.chat.id, state, len(users_ids), users_ids)


@router.callback_query(F.data == "bc_go", Broadcast.confirm)
async def bc_go(callback: types.CallbackQuery, state: FSMContext, bot: Bot):
    """BC-01/03: подтверждение запускает фоновый прогон (services/broadcast_run.run_broadcast)
    и переводит экран в прогресс с кнопкой «⛔ Остановить».

    Квик 260915-twr (Task B3): раньше `state.clear()` уходил ДО перерисовки экрана, а сама
    перерисовка была под `except Exception: pass` — сбой edit (сообщение удалено, слишком
    старое, сеть) оставлял в чате живую карточку «Отправить/Отмена», на которую уже никто не
    отвечал (`bc_no` больше не ловил её — состояние очищено). Теперь `state.clear()` — ПОСЛЕ
    отрисовки прогресса, а сбой edit чинится новым сообщением с той же клавиатурой.

    Форум-ночь п.7: `important` (тумблер экрана подтверждения) идёт ВСЕМ из `bc_users` —
    «🔕» касается только НЕважных рассылок (D-XX), поэтому мут-фильтр применяется ТОЛЬКО когда
    `important` выключен; `mute_skipped` — отдельная цифра отчёта, не входит в `total`."""
    data = await state.get_data()
    users_ids = data.get("bc_users", [])
    preview = data.get("bc_preview") or ""
    bc_chat_id = data.get("bc_chat_id")
    bc_message_id = data.get("bc_message_id")
    bc_album = data.get("bc_album")
    # "media" — не "text" — дефолт НАРОЧНО: старое поведение (до этой переделки) было
    # универсальным copy_message для ЛЮБОГО типа сообщения; "media"-ветка ближе всего к нему
    # (copy_message остаётся основным механизмом доставки), "text" требует явного bc_kind из
    # process_broadcast.
    bc_kind = data.get("bc_kind", "media")
    bc_content_html = data.get("bc_content_html")
    bc_base_markup = data.get("bc_reply_markup")
    important = bool(data.get("bc_important"))
    admin_id = callback.from_user.id

    mute_skipped = 0
    if not important:
        from database.db import get_muted_today_ids
        muted = await get_muted_today_ids(_now_moscow_naive().strftime("%Y-%m-%d"))
        before_count = len(users_ids)
        users_ids = [tid for tid in users_ids if tid not in muted]
        mute_skipped = before_count - len(users_ids)
    total = len(users_ids)

    bid = await create_broadcast(
        admin_id, preview[:80], total, important=important, full_text=preview,
    )
    logger.info(
        "broadcast %s started by %s: total=%s important=%s mute_skipped=%s preview=%r",
        bid, admin_id, total, important, mute_skipped, preview[:80],
    )

    # Форум-ночь п.7 (переделка): пометка важности — ОДИН раз, до цикла получателей (не зависит
    # от получателя, в отличие от клавиатуры ниже, которая зависит от гейта дня форума ГОРОДА
    # получателя и строится внутри send_one). 1 API-вызов на получателя для text/media, до 2 —
    # только для альбома в день, когда предложение «🔕» ещё не показывалось (send_media_group не
    # принимает reply_markup, см. докстринг services/scheduler.py).
    important_prefix_text = await important_prefix() if important else ""
    langs = await load_recipient_langs() if not important else None  # язык всех — одним чтением

    if bc_album:
        async def send_one(chat_id):
            media = _media_from_album_dicts(bc_album, important_prefix_text if important else None)
            results = await bot.send_media_group(chat_id, media)
            message_ids = [m.message_id for m in results]
            extra_mid = await send_mute_offer_if_eligible(bot, chat_id, important, langs)
            if extra_mid is not None:
                message_ids.append(extra_mid)
                # Единственный случай двух API-вызовов на получателя — вторая пауза здесь же,
                # не в базовой asyncio.sleep(0.05) run_broadcast (та рассчитана на 1 вызов и
                # осталась байт-в-байт прежней для куда более частого одного вызова).
                await asyncio.sleep(0.05)
            return message_ids
    elif bc_kind == "text":
        async def send_one(chat_id):
            markup = await recipient_markup(chat_id, important, bc_base_markup, langs)
            text = apply_important_prefix(bc_content_html, important, important_prefix_text)
            result = await bot.send_message(chat_id, text, reply_markup=markup)
            return [result.message_id]
    else:
        async def send_one(chat_id):
            markup = await recipient_markup(chat_id, important, bc_base_markup, langs)
            # `caption=None`, когда рассылка НЕ важная — Telegram сохраняет исходную подпись
            # копируемого сообщения байт-в-байт (никакого риска расхождения форматирования на
            # самой частой, неважной ветке); подмена нужна ТОЛЬКО чтобы вписать пометку важности.
            caption = apply_important_prefix(bc_content_html, important, important_prefix_text) if important else None
            result = await bot.copy_message(
                chat_id, from_chat_id=bc_chat_id, message_id=bc_message_id,
                caption=caption, reply_markup=markup,
            )
            return [result.message_id]

    stop_kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="⛔ Остановить", callback_data=f"bc_stop:{bid}")
    ]])
    progress_msg = callback.message
    try:
        await progress_msg.edit_text(f"📨 Отправлено 0 из {total}…", reply_markup=stop_kb)
    except Exception as e:
        logger.warning(
            "broadcast %s: не удалось перерисовать экран подтверждения (%s: %s) — "
            "шлю новое сообщение с прогрессом", bid, type(e).__name__, e,
        )
        try:
            await callback.message.delete()
        except Exception:
            pass  # зависшая карточка могла не удалиться по той же причине — это нормально
        try:
            progress_msg = await bot.send_message(
                callback.message.chat.id, f"📨 Отправлено 0 из {total}…", reply_markup=stop_kb,
            )
        except Exception as e3:
            logger.error("broadcast %s: не удалось отправить экран прогресса: %s", bid, e3)
            progress_msg = None
    await callback.answer()
    await state.clear()

    async def on_progress(delivered, blocked, total_n):
        if progress_msg is None:
            return
        try:
            await progress_msg.edit_text(f"📨 Отправлено {delivered} из {total_n}…", reply_markup=stop_kb)
        except Exception:
            pass  # "message is not modified" и подобные не должны ронять рассылку

    async def on_finish(status, delivered, blocked):
        if progress_msg is None:
            return
        row = await get_broadcast(bid)
        if row:
            text, kb2 = _broadcast_card(row)
        else:
            text, kb2 = (
                f"Рассылка завершена. ✅ Отправлено {delivered}, ❌ недоступно {blocked}", None
            )
        try:
            await progress_msg.edit_text(text, reply_markup=kb2)
        except Exception:
            pass

    _spawn(run_broadcast(
        bid, users_ids, send_one, on_progress=on_progress, on_finish=on_finish,
        mute_skipped=mute_skipped,
    ))


@router.callback_query(F.data == "bc_no", Broadcast.confirm)
async def bc_no(callback: types.CallbackQuery, state: FSMContext):
    await state.clear()
    await callback.message.edit_text("Рассылка отменена.")
    await callback.answer()


@router.callback_query(F.data == "bc_no")
async def bc_no_after_start(callback: types.CallbackQuery):
    """Квик 260915-twr (Task B3): страховочный хендлер БЕЗ фильтра состояния — регистрируется
    ПОСЛЕ `bc_no` выше, aiogram отдаёт событие первому подошедшему, поэтому порядок объявления
    здесь и есть контракт. Ловит повторный тап «❌ Отмена» по карточке, которая не была
    перерисована в прогресс (см. bc_go) — состояние уже очищено, рассылка уже идёт. Ничего не
    редактирует, только отвечает алертом: право на callback_data `bc_no` уже объявлено в
    ADMIN_CAPS, новое право заводить не нужно."""
    await callback.answer(
        "Рассылка уже запущена. Остановить её можно кнопкой «⛔ Остановить» "
        "в сообщении с прогрессом.",
        show_alert=True,
    )


@router.callback_query(F.data.startswith("bc_stop:"))
async def bc_stop(callback: types.CallbackQuery):
    """Стоп идущей рассылки/отзыва — тот же адресный флаг для обоих (BC-03)."""
    try:
        bid = int(callback.data.split(":", 1)[1])
    except ValueError:
        await callback.answer("Некорректные данные.", show_alert=True)
        return
    row = await get_broadcast(bid)
    if not row:
        await callback.answer("Рассылка не найдена.", show_alert=True)
        return
    # Стоп нажимает ЛЮБОЙ админ с capability раздела, не только автор: 09.09 остановить
    # чужую рассылку хотел второй менеджер — сбежавшая рассылка общая беда, не личная.
    request_stop(bid)
    logger.info("broadcast %s stop requested by %s (author %s)", bid, callback.from_user.id, row["admin_id"])
    await callback.answer("Останавливаю…")


# ── Quick 260910-okb (BC-05/06): отзыв у получателей + «Последние рассылки» ─────

_BROADCAST_STATUS_LABELS = {
    "sending": "отправляется",
    "done": "завершена",
    "stopped": "остановлена",
    "revoked": "удалена у получателей",
}


def _broadcast_card(row: dict) -> tuple[str, InlineKeyboardMarkup | None]:
    """Один рендер и для итога рассылки (bc_go/bc_revgo on_finish), и для строки списка
    «Последние рассылки» (BC-05). Заголовок несёт статус+счётчики словами, которые уже видел
    менеджер на экране прогресса — единый рендер не значит новую формулировку.

    Форум-ночь п.7: строка «🔕 не отправлено (тихий режим): N» — только когда `mute_skipped` > 0
    (старые строки до миграции читают 0 через `_ensure_column`-дефолт, экран для них не
    меняется ни байтом)."""
    status = row.get("status")
    delivered = row.get("delivered") or 0
    blocked = row.get("blocked") or 0
    total = row.get("total") or 0
    mute_skipped = row.get("mute_skipped") or 0
    preview = html_module.escape(re.sub(r"<[^>]+>", "", row.get("text_preview") or ""))
    started_at = row.get("started_at") or "—"
    important_prefix = "❗ " if row.get("important") else ""

    if status == "sending":
        headline = f"📨 Отправляется: {delivered} из {total}…"
    elif status == "stopped":
        headline = f"⛔ Остановлено: отправлено {delivered}, недоступно {blocked}"
    elif status == "revoked":
        headline = "🗑 Удалена у получателей."
    else:
        headline = f"Рассылка завершена. ✅ Отправлено {delivered}, ❌ недоступно {blocked}"
    if mute_skipped:
        headline += f"\n🔕 Не отправлено (выключили уведомления на сегодня): {mute_skipped}"

    text = (
        f"{important_prefix}#{row.get('id')} — {started_at}\n"
        f"Автор: {row.get('admin_id')}\n"
        f"{preview}\n"
        f"Статус: {_BROADCAST_STATUS_LABELS.get(status, status or '—')}\n"
        f"{headline}"
    )

    kb = None
    if status == "revoked":
        pass
    elif can_revoke(started_at):
        kb = InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="🗑 Удалить у получателей", callback_data=f"bc_rev:{row.get('id')}")
        ]])
    else:
        text += "\nУдалить нельзя: прошло больше 48 часов."
    return text, kb


# Удаление у получателей (bc_rev/bc_revno/bc_revgo) — шов admin_broadcast_revoke; импорт здесь
# держит прежний порядок регистрации хендлеров.
from handlers.comms.admin_broadcast_revoke import bc_rev, bc_revgo, bc_revno, revoked_line  # noqa: E402,F401


async def _render_broadcast_log(target):
    """Общий рендер для кнопки «🗒 Последние рассылки» и команды /broadcasts (BC-05) — до 10
    карточек, каждая — тот же `_broadcast_card`, что и итог рассылки."""
    rows = await list_recent_broadcasts(10)
    if not rows:
        await target.answer("Рассылок пока не было.")
        return
    for row in rows:
        if row.get("status") == "revoked":
            # Удалённая — одной строкой: кнопок у неё нет, счётчики доставки уже неправда.
            await target.answer(revoked_line(row))
            continue
        text, kb = _broadcast_card(row)
        await target.answer(text, reply_markup=kb)



@router.callback_query(F.data == "admin_broadcast_log")
async def admin_broadcast_log(callback: types.CallbackQuery):
    await callback.answer()
    await _render_broadcast_log(callback.message)


@router.message(Command("broadcasts"))
async def cmd_broadcasts(message: types.Message):
    await _render_broadcast_log(message)


@router.callback_query(F.data == "admin_broadcast_scheduled")
async def admin_broadcast_scheduled(callback: types.CallbackQuery):
    """UAT 15.09: кнопка «⏰ Запланированные» на экране рассылок открывает тот же список,
    что и раньше была видна только скрытой командой /scheduled — рендерит его через
    `_render_scheduled_list` (см. ниже, тот же список для команды и кнопки), не заводя
    второй копии рендера. Список — это N отдельных карточек, а не один экран, поэтому
    «Назад» отдельным сообщением с той же клавиатурой и для пустого, и для непустого списка."""
    await callback.answer()
    back_kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="◀️ Назад", callback_data="admin_broadcast")
    ]])
    shown = await _render_scheduled_list(callback.message)
    if not shown:
        await callback.message.answer("Запланированных рассылок нет.", reply_markup=back_kb)
        return
    await callback.message.answer("Вернуться в меню рассылок:", reply_markup=back_kb)


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
    # Quick 260904-dq1: рассылка не привязана к одному городу — окно ГЛОБАЛЬНОЕ
    # (window_for_city(None)). Тумблер выключен / окна нет / время вне окна — шаг работает
    # БЕЗ единого лишнего сообщения, байт-в-байт как раньше.
    from services.comms import quiet_hours
    window = await quiet_hours.window_for_city(None)
    if window is not None and quiet_hours.is_quiet(when, *window):
        start, end = window
        window_end = quiet_hours.next_window_end(when, start, end)
        await state.update_data(
            schedule_dt_pending=_fmt_dt(when),
            schedule_dt_shift=_fmt_dt(window_end),
        )
        kb = InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(
                text=f"📨 Отправить в {window_end.strftime('%H:%M')}",
                callback_data="bcast_quiet:shift",
            ),
            InlineKeyboardButton(text="🌙 Всё равно в это время", callback_data="bcast_quiet:keep"),
        ]])
        await message.answer(
            f"🌙 Это время попадёт в тихие часы ({start.strftime('%H:%M')}–"
            f"{end.strftime('%H:%M')}) — делегаты получат рассылку ночью.",
            reply_markup=kb,
        )
        return
    await _confirm_broadcast_schedule(message, state, when)


async def _confirm_broadcast_schedule(target, state: FSMContext, when: datetime) -> None:
    """Хвост шага «когда» — общий для прямого ввода и обеих кнопок `bcast_quiet:*`."""
    await state.update_data(schedule_dt=when)
    await target.answer(
        f"✅ Запланировано на {when.strftime('%d.%m.%Y %H:%M')}.\n"
        "Теперь отправьте сообщение (текст или фото с подписью) для рассылки.",
        reply_markup=get_cancel_kb(),
    )
    await state.set_state(Broadcast.schedule_message)


@router.callback_query(F.data.startswith("bcast_quiet:"), Broadcast.schedule_when)
async def broadcast_schedule_quiet_choice(callback: types.CallbackQuery, state: FSMContext):
    action = callback.data.split(":", 1)[1]
    data = await state.get_data()
    pending_raw = data.get("schedule_dt_pending")
    if not pending_raw:
        await callback.answer()
        return
    shift_raw = data.get("schedule_dt_shift")
    raw = shift_raw if action == "shift" and shift_raw else pending_raw
    when = datetime.strptime(raw, "%Y-%m-%d %H:%M:%S")
    await callback.answer()
    try:
        await callback.message.delete()
    except Exception:
        pass
    await _confirm_broadcast_schedule(callback.message, state, when)


@router.message(Broadcast.schedule_message)
async def broadcast_schedule_message(message: types.Message, state: FSMContext):
    """Форум-ночь п.7: сообщение больше НЕ создаёт отложенную рассылку сразу — копит текст/
    фото в FSM и показывает экран подтверждения с тумблером «❗ Отметить как важное» (тот же
    приём, что BC-01/02 у мгновенной рассылки, `_send_confirm_prompt`/`bc_important_toggle`).
    Создание строки переехало в `sched_go`."""
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

    await state.update_data(sched_text=text, sched_photo=photo, bc_important=False)
    await message.send_copy(message.chat.id)
    await _send_schedule_confirm_prompt(message, state)


async def _send_schedule_confirm_prompt(target, state: FSMContext) -> None:
    """Экран подтверждения отложенной рассылки — общий хвост для первого показа
    (`broadcast_schedule_message`) и перерисовки после тумблера (`sched_important_toggle`)."""
    data = await state.get_data()
    when = data.get("schedule_dt")
    important = bool(data.get("bc_important"))
    important_btn = InlineKeyboardButton(
        text="✅ Отмечено как важное" if important else "❗ Отметить как важное",
        callback_data="sched_important_toggle",
    )
    from handlers.comms.admin_broadcast_season import schedule_season_extra
    season_text, season_rows = await schedule_season_extra(state)
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🗓 Запланировать", callback_data="sched_go")],
        *season_rows, [important_btn],
        [InlineKeyboardButton(text="❌ Отмена", callback_data="sched_no")],
    ])
    await target.answer(
        f"{season_text}Запланировать эту рассылку на {when.strftime('%d.%m.%Y %H:%M')}?", reply_markup=kb,
    )
    await state.set_state(Broadcast.schedule_confirm)


@router.callback_query(F.data == "sched_important_toggle", Broadcast.schedule_confirm)
async def sched_important_toggle(callback: types.CallbackQuery, state: FSMContext):
    data = await state.get_data()
    important = not bool(data.get("bc_important"))
    await state.update_data(bc_important=important)
    await callback.answer("❗ Отмечено как важное" if important else "Пометка снята")
    try:
        await callback.message.delete()
    except Exception:
        pass
    await _send_schedule_confirm_prompt(callback.message, state)


@router.callback_query(F.data == "sched_no", Broadcast.schedule_confirm)
async def sched_no(callback: types.CallbackQuery, state: FSMContext):
    await state.clear()
    await callback.answer()
    await callback.message.edit_text("Рассылка отменена.")


@router.callback_query(F.data == "sched_go", Broadcast.schedule_confirm)
async def sched_go(callback: types.CallbackQuery, state: FSMContext):
    data = await state.get_data()
    when = data.get("schedule_dt")
    text = data.get("sched_text")
    photo = data.get("sched_photo")
    important = bool(data.get("bc_important"))
    if not when:
        await callback.answer()
        await callback.message.edit_text("Сессия истекла, начните заново через /broadcast.")
        await state.clear()
        return

    filters = data.get("filters")
    filter_spec = json.dumps(filters, ensure_ascii=False) if filters else None

    bid = await create_scheduled_broadcast(
        text, photo, filter_spec, _fmt_dt(when), callback.from_user.id, important=important,
    )
    schedule_broadcast_job(bid, when)
    await state.clear()
    await callback.answer()

    scope = "по фильтру" if filters else "всем пользователям"
    await callback.message.edit_text(
        f"✅ Рассылка #{bid} запланирована на {when.strftime('%d.%m.%Y %H:%M')} ({scope}).\n"
        "Управление: /scheduled"
    )


async def _render_scheduled_list(target) -> bool:
    """Общий рендер списка отложенных рассылок — для скрытой команды /scheduled и для кнопки
    «⏰ Запланированные» на экране рассылок (UAT 15.09). Возвращает False, если список пуст —
    вызывающий сам решает, чем заменить пустой экран (команда и кнопка делают это разными
    словами), поэтому пустая ветка здесь НЕ отправляет никакого сообщения."""
    rows = await list_pending_broadcasts()
    # Review 260817 §B2: a broadcast that is mid-send (or died mid-send and waits for the boot
    # reclaim) is shown too, with its per-recipient checkpoint count — otherwise the manager
    # sees nothing and re-creates it by hand while the original is still going out.
    sending = await list_sending_broadcasts()
    if not rows and not sending:
        return False
    for row in sending:
        ok, failed = await count_deliveries(row["id"])
        preview = re.sub(r"<[^>]+>", "", row.get("text") or "(фото)")[:60]
        tail = f", не доставлено {failed}" if failed else ""
        await target.answer(
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
        await target.answer(
            f"#{row['id']} — {row['scheduled_at']}\n{html_module.escape(preview)}",
            reply_markup=kb,
        )
    return True


@router.message(Command("scheduled"))
async def cmd_scheduled(message: types.Message):
    shown = await _render_scheduled_list(message)
    if not shown:
        await message.answer("Нет запланированных рассылок.")


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
    "ext_form": "Внешняя форма",
    "session_enroll": "Запись на сессии", "quiz": "Тест",
    "city": "Город", "university": "ВУЗ", "status": "Статус",
    "source": "Источник", "registration_date": "Дата регистрации",
    "payment_status": "Оплата",
    "local_committee": "Комитет АЙСЕК", "department": "Департамент",
    "aiesec_role": "Роль АЙСЕК", "education_status": "Образование",
    "course": "Курс", "study_field": "Направление",
    "position": "Позиция", "attendance_format": "Формат участия",
    "participant_type": "Трек",  # Phase 5 (D-19)
    # Phase 07.2 (CITY-02). NOT the same thing as "city": "Город" above — that one is the
    # DELEGATE's own home city (a registration question); this one is the CITY OF THE EVENT.
    # The two labels sit in the same menu and must stay visually distinguishable on screen —
    # confusing them is the most expensive mistake this feature can make.
    "event_city": "Город мероприятия",
    # Квик 260910-vfl (SEASON-FILTER-02). Это сезон СОБЫТИЯ, которым помечена регистрация
    # (настройка «🎉 Сезон события», `settings_schema.event_season`) — не «Статус» и не «Трек».
    "season": "Сезон",
    # Квик 260911-0fh (RESUME-FILTER-02): это НАЛИЧИЕ резюме в любом виде (файл / текст /
    # ссылка Nextcloud / ссылка на профиль), а не какое-то одно поле анкеты — набор колонок
    # это db.RESUME_COLUMNS, второй карты здесь нет.
    "resume": "Резюме",
    # Квик 260914-rgr (RGR-01..07): это членство в ЧАТЕ мероприятия (Telegram-группа
    # делегатов), а не «Город»/«Статус» — подписи двух полей должны различаться на экране.
    "delegate_chat": "Чат делегатов",
    # Phase 31 (31-02/31-07, D-28): попал ли делегат под срабатывание правила автоотказа —
    # значение хранится в `users.auto_reject_rule_ids`, отдельной колонки `auto_reject` нет.
    "auto_reject": "Автоотказ по правилу",
    # Форум-ночь п.6 (D-25, идея №14): отметка «Вход» в `checkins` — пришёл ли делегат на
    # форум (не «одобрен», это отдельное поле «Статус» выше).
    "checkin_entry": "Отметка на форуме",
    # Делегации вузов (D-07): «вуз делегации» — из ФОРМЫ (users.delegation), не «ВУЗ» анкеты выше.
    "delegation_any": "Делегация вуза", "delegation": "Вуз делегации",
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
    # Квик 260910-vfl (SEASON-FILTER-01) — same двойная регистрация rule: also in
    # `db._FILTER_COLUMNS`, see there. No separate handler needed for the same reason as
    # `event_city` above.
    "season",
    # Квик 260911-0fh (RESUME-FILTER-02) — same двойная регистрация rule: also in
    # `db._FILTER_COLUMNS` (see there — `resume` is virtual there). No separate handler
    # needed for the same reason as `event_city`/`season` above.
    "resume",
    # Квик 260914-rgr (RGR-01..07) — same двойная регистрация rule: also in
    # `db._FILTER_COLUMNS` (see there — `delegate_chat` is virtual there). No separate
    # handler needed for the same reason as `event_city`/`season`/`resume` above.
    "delegate_chat",
    # Phase 31 (31-02/31-07, D-28) — same двойная регистрация rule: ОБЯЗАНО быть
    # зарегистрировано И здесь, И в `db._FILTER_COLUMNS` (see there — `auto_reject` is
    # virtual there), иначе фильтр виден на экране и молча не доходит до SQL — менеджер
    # уверен, что шлёт сегменту, а рассылка уходит всем (тот же прецедент D-19, что у
    # `event_city`/`season`/`resume`/`delegate_chat` выше). No separate handler needed for
    # the same reason as those fields.
    "auto_reject",
    # Форум-ночь п.6 (D-25, идея №14) — same двойная регистрация rule: also in
    # `db._FILTER_COLUMNS`/`db._FILTER_VIRTUAL_FIELDS` (see there). No separate handler
    # needed for the same reason as the fields above.
    "checkin_entry",
    # Делегации вузов (D-07) — та же двойная регистрация с `db._FILTER_COLUMNS` (там
    # `delegation_any` виртуальное, `delegation` — обычная колонка через общий DISTINCT-пикер).
    "delegation_any", "delegation",
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
            label = _FILTER_VALUE_LABELS.get(field, {}).get(v, v)
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
# Phase 19 (Mini App): словарь переехал в корневой `domain/regform/labels.py` — профиль Mini App
# показывает тот же статус оплаты теми же словами.
from domain.regform.labels import PAYMENT_STATUS_LABELS as _PAYMENT_STATUS_LABELS, STATUS_LABELS as _APP_STATUS_LABELS  # noqa: E402

# Поля фильтра, у которых в базе лежит служебный код, а менеджеру нужны слова (pending -> «Новая»).
_FILTER_VALUE_LABELS = {"payment_status": _PAYMENT_STATUS_LABELS, "status": _APP_STATUS_LABELS}


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
        elif f["field"] in _FILTER_VALUE_LABELS:
            parts.append(f"{label} = {_FILTER_VALUE_LABELS[f['field']].get(f.get('value'), val)}")
        elif f.get("label"):
            # Phase 07.2 (CITY-02) / квик 260910-vfl: a filter may carry its own human display
            # text (a city code / the SEASON_NONE sentinel is unreadable in the summary).
            # Generic — not a city special case. Escaped like every other value.
            parts.append(f"{label} = {html_module.escape(str(f['label']))}")
        else:
            parts.append(f"{label} = {val}")
    return " И ".join(parts)


def _filter_menu_kb(filters: list[dict], *, show_city: bool = False,
                     show_season: bool = False, show_resume: bool = False,
                     show_chat: bool = False, show_auto_reject: bool = False,
                     show_checkin: bool = False, show_sessions: bool = False,
                     show_ext_form: bool = False, show_delegations: bool = False,
                     extra_rows: list | None = None) -> InlineKeyboardMarkup:
    kb = [
        [InlineKeyboardButton(text="Комитет АЙСЕК", callback_data="filter_f_local_committee"),
         InlineKeyboardButton(text="Департамент", callback_data="filter_f_department")],
        [InlineKeyboardButton(text="Роль АЙСЕК", callback_data="filter_f_aiesec_role"),
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
    # Квик 260910-vfl (SEASON-FILTER-02): кнопка только когда сезонов больше одного — на одном
    # сезоне фильтровать не по чему, кнопка была бы шумом. Дефолт False держит клавиатуру
    # байт-в-байт прежней.
    if show_season:
        kb.append([InlineKeyboardButton(text="Сезон", callback_data="filter_f_season")])
    # Квик 260911-0fh (RESUME-FILTER-02): кнопка только когда в базе есть и делегаты с
    # резюме, и без — фильтровать не по чему, когда все по одну сторону (тот же довод, что
    # у «Сезона»). Дефолт False держит клавиатуру байт-в-байт прежней.
    if show_resume:
        kb.append([InlineKeyboardButton(text="📄 Резюме", callback_data="filter_f_resume")])
    # Квик 260914-rgr (RGR-01..07): кнопка только когда по обе стороны реально есть люди —
    # фильтровать не по чему, когда все по одну сторону (тот же довод, что у «Резюме»/
    # «Сезона»). Дефолт False держит клавиатуру байт-в-байт прежней.
    if show_chat:
        kb.append([InlineKeyboardButton(text="💬 Чат делегатов", callback_data="filter_f_delegate_chat")])
    # Phase 31 (31-02/31-07, D-28): кнопка только когда в базе реально есть и автоотклонённые,
    # и нет — фильтровать не по чему, когда все по одну сторону (тот же довод, что у
    # «Резюме»/«Чата делегатов»/«Сезона»). Дефолт False держит клавиатуру байт-в-байт прежней.
    if show_auto_reject:
        kb.append([InlineKeyboardButton(text="🤖 Автоотказ по правилу", callback_data="filter_f_auto_reject")])
    # Делегации вузов (D-07): кнопка только когда в базе есть и делегаты вузов, и остальные —
    # тот же довод, что у соседей выше. Вуз конкретный — вторая кнопка того же ряда.
    if show_delegations:
        kb.append([InlineKeyboardButton(text="🏫 Делегации", callback_data="filter_f_delegation_any"),
                   InlineKeyboardButton(text="🏫 Вуз делегации", callback_data="filter_f_delegation")])
    # Форум-ночь п.6 (D-25, идея №14): кнопка только когда в `checkins` реально есть и
    # пришедшие, и (approved текущего сезона) не пришедшие — тот же довод, что у соседей выше.
    if show_checkin:
        kb.append([InlineKeyboardButton(text="🚪 Отметка на форуме", callback_data="filter_f_checkin_entry")])
    # Свой мастер (город → день → сессия, handlers/comms/admin_broadcast_session_filter.py) — не
    # входит в generic-пикер `_show_value_picker` (значение — конкретная сессия, а не пара
    # сентинелов). Кнопка только когда менеджер завёл хотя бы одну сессию программы.
    if show_sessions:
        kb.append([
            InlineKeyboardButton(text="🎤 Были на сессии…", callback_data="cksf_start:attended"),
            InlineKeyboardButton(text="🚫 Не были на сессии…", callback_data="cksf_start:not_attended"),
        ])
    kb.extend(extra_rows or [])  # сезон + запись на сессии/тест (шов admin_broadcast_season)
    if show_ext_form:
        kb.append([InlineKeyboardButton(text="📝 Внешняя форма", callback_data="extff_start")])
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
    # Квик 260910-vfl (SEASON-FILTER-02): порог считается по тому же списку, который потом
    # покажет пикер (get_season_filter_options) — второй карты значений нет.
    season_options = await get_season_filter_options()
    # Квик 260911-0fh (RESUME-FILTER-02): порог считается по тому же списку, который потом
    # покажет пикер (get_resume_filter_options) — второй карты значений нет; когда все
    # делегаты по одну сторону, фильтровать не по чему и кнопка была бы шумом.
    resume_options = await get_resume_filter_options()
    # Квик 260914-rgr (RGR-01..07): ленивый импорт — `services` в `handlers` на уровне модуля
    # не тянем. `chats` — карта из `bound_chats()`, порог считается ТЕМ ЖЕ списком, который
    # потом покажет пикер (второй карты значений нет).
    from services import chat_tracking

    chats = await chat_tracking.bound_chats()
    chat_options = await get_chat_filter_options(chats)
    # Phase 31 (31-02/31-07, D-28): порог считается ТЕМ ЖЕ списком, который потом покажет
    # пикер (get_auto_reject_filter_options) — второй карты значений нет.
    auto_reject_options = await get_auto_reject_filter_options()
    delegation_options = await get_delegation_filter_options()  # делегации вузов (D-07), тот же порог
    # Форум-ночь п.6 (D-25, идея №14): та же роль порога, что у auto_reject/chat выше.
    # Вход каждый день: порог — хоть один вариант (за форум / сегодня / день) с людьми.
    checkin_options = await get_checkin_entry_picker_options()
    show_sessions = await any_program_sessions_exist()
    from handlers.comms.admin_broadcast_season import menu_extra_rows
    kb = _filter_menu_kb(filters, show_city=await cities_module_on(),
                         show_season=len(season_options) > 1,
                         show_resume=len(resume_options) > 1,
                         show_chat=len(chat_options) > 1,
                         show_auto_reject=len(auto_reject_options) > 1,
                         show_checkin=bool(checkin_options),
                         show_sessions=show_sessions,
                         show_ext_form=bool(await _ext_forms_list()),
                         show_delegations=len(delegation_options) > 1,
                         extra_rows=await menu_extra_rows(filters))
    if edit:
        await target.edit_text(text, reply_markup=kb)
    else:
        await target.answer(text, reply_markup=kb)


@router.callback_query(F.data == "broadcast_filter", Broadcast.target_selection)
async def broadcast_filter_start(callback: types.CallbackQuery, state: FSMContext):
    filters = [f] if (f := await season_default_filter()) else []  # по умолчанию — текущий сезон
    await state.update_data(filters=filters)
    await callback.answer()
    await _render_filter_menu(callback.message, filters, edit=True)
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
    elif field == "season":
        # Квик 260910-vfl (SEASON-FILTER-04): гейт живёт В ХЭНДЛЕРЕ, а не только в отрисовке
        # клавиатуры — тот же довод, что WR-04 у event_city: инлайн-кнопки не истекают, меню,
        # нарисованное вчера при двух сезонах, живо и сегодня, когда сезон снова один.
        options = await get_season_filter_options()
        if len(options) < 2:
            await callback.answer("В базе один сезон — фильтровать не по чему.", show_alert=True)
            return
        labels = {SEASON_NONE: "Без сезона"}
    elif field == "resume":
        # Квик 260911-0fh (RESUME-FILTER-05): гейт живёт В ХЭНДЛЕРЕ, а не только в отрисовке
        # клавиатуры — тот же довод WR-04, что у event_city/season: инлайн-кнопки не
        # истекают, вчерашнее меню с кнопкой «Резюме» живо и сегодня, когда все делегаты
        # снова по одну сторону.
        options = await get_resume_filter_options()
        if len(options) < 2:
            await callback.answer(
                "У всех делегатов резюме в одном состоянии — фильтровать не по чему.",
                show_alert=True,
            )
            return
        # Человеку показываем только эти два слова — коды (has/none) не показываем и ввести
        # не просим (правило «бот для людей»).
        labels = {RESUME_HAS: "есть", RESUME_MISSING: "нет"}
    elif field == "delegate_chat":
        # Квик 260914-rgr (RGR-01..07): гейт живёт В ХЭНДЛЕРЕ — тот же довод WR-04, что у
        # event_city/season/resume выше: инлайн-кнопки не истекают, вчерашнее меню с кнопкой
        # «Чат делегатов» живо и сегодня, когда чат уже отвязан. Ленивый импорт — `services`
        # в `handlers` на уровне модуля не тянем.
        from services import chat_tracking

        chats = await chat_tracking.bound_chats()
        if not chats:
            await callback.answer(
                "Чат делегатов не подключён — фильтровать не по чему.", show_alert=True,
            )
            return
        options = await get_chat_filter_options(chats)
        if len(options) < 2:
            await callback.answer(
                "У всех делегатов чат в одном состоянии — фильтровать не по чему.",
                show_alert=True,
            )
            return
        # Человеку показываем только эти два слова — коды (in/out) не показываем (правило
        # «бот для людей»).
        labels = {CHAT_IN: "в чате", CHAT_OUT: "не в чате"}
    elif field == "auto_reject":
        # Phase 31 (31-02/31-07, D-28): гейт живёт В ХЭНДЛЕРЕ — тот же довод WR-04, что у
        # event_city/season/resume/delegate_chat выше: инлайн-кнопки не истекают, вчерашнее
        # меню с кнопкой «Автоотказ» живо и сегодня, когда автоотказов в базе больше нет.
        options = await get_auto_reject_filter_options()
        if len(options) < 2:
            await callback.answer(
                "Автоотказов в базе пока нет — фильтровать не по чему.", show_alert=True,
            )
            return
        # Человеку показываем только эти два слова — коды (yes/no) не показываем (правило
        # «бот для людей»).
        labels = {AUTO_REJECT_YES: "Отклонён правилом", AUTO_REJECT_NO: "Не отклонён правилом"}
    elif field == "delegation_any":
        # Делегации вузов (D-07): гейт живёт В ХЭНДЛЕРЕ — тот же довод WR-04, что у соседей
        # выше (инлайн-кнопки не истекают). Человеку — слова, коды yes/no не показываем.
        options = await get_delegation_filter_options()
        if len(options) < 2:
            await callback.answer("Делегаций в базе пока нет — фильтровать не по чему.", show_alert=True)
            return
        labels = {DELEGATION_YES: "Делегация вуза", DELEGATION_NO: "Не делегация"}
    elif field == "checkin_entry":
        # Форум-ночь п.6 (D-25, идея №14): гейт живёт В ХЭНДЛЕРЕ — тот же довод WR-04, что у
        # соседей выше: инлайн-кнопки не истекают, вчерашнее меню с кнопкой «Отметка на
        # форуме» живо и сегодня, когда все делегаты снова по одну сторону.
        # Вход каждый день: варианты за форум, сегодня и каждый день со входами — только те, где
        # есть люди. «сегодня» пересчитывается на момент отправки (отложенная рассылка на утро
        # второго дня берёт второй день). День едет в значении через «@», в запись фильтра —
        # отдельным ключом `day`.
        options = await get_checkin_entry_picker_options()
        if not options:
            await callback.answer(
                "Отметок входа ещё не было — фильтровать не по чему.", show_alert=True,
            )
            return
        # Человеку — только слова, коды (yes/no/@день) не показываем (правило «бот для людей»).
        labels = {}
        for opt in options:
            base, _, day = opt.partition("@")
            if not day:
                labels[opt] = "пришли на форум" if base == CHECKIN_YES else "не пришли ни разу"
                continue
            when = "сегодня" if day == CHECKIN_DAY_TODAY else f"{day[8:10]}.{day[5:7]}"
            labels[opt] = f"{'пришли' if base == CHECKIN_YES else 'не пришли'} {when}{await day_cities_suffix(day)}"
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
        # may not import domain.cities as cities (cycle), and it must survive the JSON round-trip a scheduled
        # broadcast's spec goes through. `label` renders the summary in human words.
        labels = data.get("filter_option_labels") or {}
        scope = city_scope(value)
        filters.append({
            "field": field,
            "value": value,
            "exclude": list(scope[1]) if scope else [],
            "label": labels.get(value, value),
        })
    elif field == "season":
        # Квик 260910-vfl (SEASON-FILTER-04): «label» только для сентинела SEASON_NONE — у
        # настоящих сезонов подписи нет (значение и есть подпись, «YL 26/2»). Сводку уже
        # рисует существующая ветка `f.get("label")` в `_filter_summary`.
        entry = {"field": field, "value": value}
        # конкретный сезон заменяет «Текущий сезон» по умолчанию, иначе AND даёт 0 получателей
        filters[:] = [f for f in filters
                      if not (f.get("field") == "season" and f.get("value") == SEASON_CURRENT)]
        labels = data.get("filter_option_labels") or {}
        if value in labels:
            entry["label"] = labels[value]
        filters.append(entry)
    elif field == "resume":
        # Квик 260911-0fh (RESUME-FILTER-06): `label` есть ВСЕГДА — в отличие от «Сезона»,
        # оба значения (RESUME_HAS/RESUME_MISSING) — сентинелы, без подписи сводка читалась
        # бы «Резюме = none».
        labels = data.get("filter_option_labels") or {}
        filters.append({"field": field, "value": value, "label": labels.get(value, value)})
    elif field == "delegate_chat":
        # Квик 260914-rgr (RGR-01..07, D-5): `label` есть ВСЕГДА — оба значения (CHAT_IN/
        # CHAT_OUT) сентинелы, та же причина, что у «Резюме» выше. Карта `chats` едет ВНУТРИ
        # записи фильтра — `database/db.py` не может импортировать `cities`, `exclude`
        # каждого чата берётся из `cities.city_scope`, та же функция, что у `event_city`
        # (`city is None` -> пустой список — глобальная привязка).
        from services import chat_tracking

        bound = await chat_tracking.bound_chats()
        chats_payload = []
        for entry in bound:
            scope = city_scope(entry["city"]) if entry["city"] else None
            chats_payload.append({
                "city": entry["city"],
                "chat_id": entry["chat_id"],
                "exclude": list(scope[1]) if scope else [],
            })
        labels = data.get("filter_option_labels") or {}
        filters.append({
            "field": field, "value": value, "label": labels.get(value, value),
            "chats": chats_payload,
        })
    elif field in ("auto_reject", "delegation_any"):
        # Phase 31 (31-02/31-07, D-28): `label` есть ВСЕГДА — оба значения (AUTO_REJECT_YES/
        # AUTO_REJECT_NO) сентинелы, та же причина, что у «Резюме»/«Чата делегатов» выше.
        # Делегации вузов (D-07): `delegation_any` — те же два сентинела, та же запись.
        labels = data.get("filter_option_labels") or {}
        filters.append({"field": field, "value": value, "label": labels.get(value, value)})
    elif field == "checkin_entry":
        # Форум-ночь п.6 (D-25, идея №14): `label` есть ВСЕГДА — оба значения (CHECKIN_YES/
        # CHECKIN_NO) сентинелы, та же причина, что у соседей выше. `event_season` НЕ кладём
        # сюда — он резолвится заново на КАЖДЫЙ вызов `count_and_list_filtered`
        # (`database.db._resolve_checkin_entry_season`), а не замораживается на момент выбора.
        labels = data.get("filter_option_labels") or {}
        base, _, day = str(value).partition("@")
        entry = {"field": field, "value": base, "label": labels.get(value, value)}
        if day:
            entry["day"] = day
        filters.append(entry)
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
    # Менеджер города — то же сужение, что у «✅ Отправить N»: числа на экранах совпадают.
    ids = await restrict_to_sender_city(callback.from_user.id, await count_and_list_filtered(filters))
    await callback.answer()
    from handlers.comms.admin_broadcast_status import status_block  # разбивка по статусу заявки
    st_text, st_rows = await status_block(ids, filters, "bcstatus_filter")
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📨 Отправить сейчас", callback_data="filter_send_now")], *st_rows,
        [InlineKeyboardButton(text="🕓 Запланировать", callback_data="filter_schedule")],
        [InlineKeyboardButton(text="❌ Отмена", callback_data="broadcast_cancel")],
    ])
    from services.forum.forum_days import not_arrived_city_note  # «не пришли» — по городам форума
    await callback.message.edit_text(
        f"{await sender_city_note(callback.from_user.id)}🎯 Условия: {_filter_summary(filters)}\n"
        f"Под фильтр попадает <b>{len(ids)}</b> пользователей.{await past_season_note(ids)}"
        f"{html_module.escape(await not_arrived_city_note(filters, ids))}\n\n{st_text}".rstrip(),
        reply_markup=kb,
    )


@router.callback_query(F.data == "filter_send_now", Broadcast.filter_field)
async def filter_send_now(callback: types.CallbackQuery, state: FSMContext):
    data = await state.get_data()
    filters = data.get("filters", [])
    ids = await restrict_to_sender_city(callback.from_user.id, await count_and_list_filtered(filters))
    await _start_segment_broadcast(
        callback, state, ids,
        f"🎯 {len(set(ids))} получателей по фильтру.\nТеперь отправьте сообщение для рассылки.",
    )


@router.callback_query(F.data == "filter_schedule", Broadcast.filter_field)
async def filter_schedule(callback: types.CallbackQuery, state: FSMContext):
    # filters stay in FSM state; the schedule flow reads them as filter_spec
    data = await state.get_data()
    await callback.answer()
    text = "🕓 Введите дату и время рассылки в формате ДД.ММ.ГГГГ ЧЧ:ММ (напр. 01.07.2026 14:30):"
    if data.get("filters"):
        # Форум-ночь п.6 (D-25): список по фильтру резолвится ЗАНОВО в момент отправки
        # (`services.scheduler.send_scheduled_broadcast` -> `count_and_list_filtered`), не
        # замораживается сейчас — за время ожидания состав может измениться (кто-то отметился
        # на входе, заявку одобрили/отклонили). Менеджер должен знать это ДО того, как нажмёт
        # «Запланировать», а не догадываться по факту отправки.
        text = (
            "⚠️ Список получателей пересчитается заново в момент отправки (если кто-то за это "
            "время отметится на входе или сменит статус — письмо уйдёт актуальному списку).\n\n"
            f"{text}"
        )
    await callback.message.edit_text(text)
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


# Форум-ночь п.6 (D-25, идея №14): мастер «Были/Не были на сессии …» — свой шов, декорирует
# тот же `handlers.admin.router` (см. докстринг handlers/comms/admin_broadcast_session_filter.py).
from handlers.comms import admin_broadcast_session_filter  # noqa: E402,F401
from handlers.comms import admin_broadcast_ext_form_filter  # noqa: E402,F401
from handlers.comms import admin_broadcast_season  # noqa: E402,F401  # сезон по умолчанию, «из них прошлого сезона»
from handlers.comms import admin_broadcast_enroll_filter  # noqa: E402,F401  # фильтры записи на сессии и теста
from handlers.comms import admin_broadcast_status  # noqa: E402,F401  # разбивка по статусу заявки, «Только одобренные»
