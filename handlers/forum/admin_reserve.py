"""Экран «Позвать резерв»: письмо на сегодня. Статус заявки не меняется."""
from __future__ import annotations

import html
import json
import logging

from aiogram import Bot, F, types
from aiogram.exceptions import TelegramForbiddenError
from aiogram.fsm.context import FSMContext
from aiogram.types import BufferedInputFile, InlineKeyboardButton, InlineKeyboardMarkup

from database import db
from domain.cities import (
    ALL_CITIES,
    admin_selected_city,
    city_label,
    city_scope,
)
from handlers.admin import router
from handlers.states import ReserveInvite
from services.applications.applications import _resolve_rule_label
from services.forum.checkin import build_checkin_qr
from services.forum.reserve import is_live_reserve, today_msk
from services.infra.timeutil import msk_now

logger = logging.getLogger(__name__)

PAGE = 6
READY_LETTER = "{name}, вас зовут на площадку из резерва. Вход — только {date}. Город: {city}."
PICK_CITY = (
    "📮 <b>Позвать резерв</b>\n\n"
    "Сначала выберите конкретный город в шапке. "
    "Пока стоят «Все города», никого не зовём."
)


def confirm_line(n: int, when: str) -> str:
    return f"Позовём {n}. Статус „одобрен“ не появится. Вход — только {when}."


def today_label() -> str:
    return msk_now().strftime("%d.%m.%Y")


def letter_body(template: str | None, *, name: str, date: str, city: str) -> str:
    raw = (template or "").strip() or READY_LETTER
    who = html.escape(name.strip()) if (name or "").strip() else "друг"
    return (
        raw.replace("{name}", who)
        .replace("{date}", html.escape(date))
        .replace("{city}", html.escape(city))
    )


def report_text(*, new: int, already: int, failed: int, blocked: int, qr_miss: list[str]) -> str:
    lines = [
        f"Новым позвали {new}.",
        f"Уже были приглашены сегодня {already}.",
        f"Не доставлено {failed}.",
    ]
    if blocked:
        lines.append("Делегат заблокировал бота — сообщение не дошло.")
    if qr_miss:
        names = ", ".join(html.escape(item) for item in qr_miss)
        lines.append(f"QR не приложился: {names}.")
    return "\n".join(lines)


def user_in_city_scope(event_city: str | None, scope) -> bool:
    if scope is None:
        return True
    code, exclude = scope
    if not exclude:
        return event_city == code
    return event_city is None or event_city not in exclude


def _rule_ids(raw) -> list[int]:
    try:
        data = json.loads(raw or "[]")
    except (TypeError, ValueError):
        return []
    out = []
    for item in data if isinstance(data, list) else []:
        try:
            out.append(int(item))
        except (TypeError, ValueError):
            continue
    return out


def _short(text: str, limit: int = 28) -> str:
    clean = " ".join((text or "без имени").split())
    if len(clean) <= limit:
        return clean
    return clean[: limit - 1] + "…"


async def rows_for_header(admin_id: int, rule_id: int | None = None) -> list[dict] | None:
    """None — шапка «Все города»: выборки нет и слать нечего."""
    code = await admin_selected_city(admin_id)
    if code == ALL_CITIES:
        return None
    return await db.list_live_reserve_rows(city_scope=city_scope(code), rule_id=rule_id)


async def ids_for_action(admin_id: int, *, rule_id: int | None, mode: str,
                          selected: list[int]) -> list[int] | None:
    rows = await rows_for_header(admin_id, rule_id)
    if rows is None:
        return None
    live = [int(row["telegram_id"]) for row in rows]
    if mode == "all":
        return live
    live_set = set(live)
    return [int(item) for item in selected if int(item) in live_set]


async def _city_phrase(code: str | None) -> str:
    if not code or code == ALL_CITIES:
        return "площадка"
    return await city_label(code)


async def deliver(bot, admin_id: int, ids: list[int], *, city_code: str | None,
                  rule_id: int | None, attachment: dict | None = None) -> dict:
    """Письмо + тот же QR, что «Мой QR». Повтор в тот же день молчит. status не трогаем."""
    empty = {"new": 0, "already": 0, "failed": 0, "blocked": 0, "qr_miss": []}
    if city_code == ALL_CITIES:
        return empty
    day = today_msk()
    when = today_label()
    from domain.cities import get_setting_typed_for_city
    template = await get_setting_typed_for_city("reserve_invite_text", city_code)
    place = await _city_phrase(city_code)
    scope = city_scope(city_code) if city_code else None
    new = already = failed = blocked = 0
    qr_miss: list[str] = []
    for tid in ids:
        user = await db.get_user(int(tid))
        if not await is_live_reserve(user):
            continue
        if not user_in_city_scope(user.get("event_city"), scope):
            continue
        if rule_id is not None:
            entry = await db.get_live_auto_reject_log_entry(int(tid))
            if not entry or rule_id not in _rule_ids(entry.get("rule_ids")):
                continue
        stored_city = city_code or user.get("event_city")
        inserted = await db.invite_reserve([int(tid)], day, city=stored_city, invited_by=admin_id)
        if not inserted:
            already += 1
            continue
        who = user.get("full_name") or ""
        text = letter_body(template, name=who, date=when, city=place)
        try:
            await bot.send_message(int(tid), text, parse_mode="HTML")
        except TelegramForbiddenError:
            failed += 1
            blocked += 1
            continue
        except Exception:
            logger.warning("reserve invite: письмо не ушло %s", tid, exc_info=True)
            failed += 1
            continue
        new += 1
        if attachment:
            try:
                await _send_attachment(bot, int(tid), attachment)
            except Exception:
                logger.warning("reserve invite: файл не ушёл %s", tid, exc_info=True)
        try:
            png, caption = await build_checkin_qr(user)
            await bot.send_photo(
                int(tid), BufferedInputFile(png, filename="checkin_qr.png"), caption=caption,
            )
        except Exception:
            logger.warning("reserve invite: QR не собрался %s", tid, exc_info=True)
            qr_miss.append(who.strip() or str(tid))
    return {"new": new, "already": already, "failed": failed, "blocked": blocked, "qr_miss": qr_miss}


async def _send_attachment(bot, chat_id: int, attachment: dict) -> None:
    kind = attachment.get("kind")
    file_id = attachment.get("file_id")
    if kind == "photo":
        await bot.send_photo(chat_id, file_id)
    elif kind == "video":
        await bot.send_video(chat_id, file_id)
    elif kind == "document":
        await bot.send_document(chat_id, file_id)
    elif kind == "audio":
        await bot.send_audio(chat_id, file_id)
    elif kind == "animation":
        await bot.send_animation(chat_id, file_id)


def _one_file(message: types.Message) -> dict | None:
    if message.photo:
        return {"kind": "photo", "file_id": message.photo[-1].file_id}
    if message.video:
        return {"kind": "video", "file_id": message.video.file_id}
    if message.document:
        return {"kind": "document", "file_id": message.document.file_id}
    if message.audio:
        return {"kind": "audio", "file_id": message.audio.file_id}
    if message.animation:
        return {"kind": "animation", "file_id": message.animation.file_id}
    return None


def confirm_body(n: int, when: str, *, attached: bool) -> str:
    file_note = (
        "К письму приложен один файл."
        if attached else
        "Файл не приложен. Можно прислать один — фото, видео или документ."
    )
    return f"{confirm_line(n, when)}\n\n{file_note}"


async def _flow(state: FSMContext) -> dict:
    data = await state.get_data()
    rule = data.get("rsv_rule")
    return {
        "origin": data.get("rsv_from") or "apps",
        "rule": int(rule) if rule is not None else None,
        "page": int(data.get("rsv_page") or 0),
        "sel": [int(item) for item in (data.get("rsv_sel") or [])],
        "file": data.get("rsv_file"),
        "mode": data.get("rsv_mode") or "sel",
    }


def _back_button(origin: str) -> InlineKeyboardButton:
    if origin == "hub":
        return InlineKeyboardButton(text="← Назад", callback_data="admin_forum_functions")
    from handlers.settings.admin_sections import back_button
    return back_button("rsv")


async def _edit(message, text: str, kb: InlineKeyboardMarkup) -> None:
    try:
        await message.edit_text(text, reply_markup=kb, parse_mode="HTML")
    except Exception as exc:
        if "not modified" in str(exc).lower():
            return
        await message.answer(text, reply_markup=kb, parse_mode="HTML")


async def _rule_choices(rows: list[dict]) -> list[tuple[int, str]]:
    seen: list[int] = []
    for row in rows:
        for rid in _rule_ids(row.get("rule_ids")):
            if rid not in seen:
                seen.append(rid)
    out = []
    for rid in seen:
        out.append((rid, await _resolve_rule_label(rid)))
    return out


async def _list_view(admin_id: int, flow: dict) -> tuple[str, InlineKeyboardMarkup]:
    code = await admin_selected_city(admin_id)
    if code == ALL_CITIES:
        kb = InlineKeyboardMarkup(inline_keyboard=[[_back_button(flow["origin"])]])
        return PICK_CITY, kb
    rows = await db.list_live_reserve_rows(
        city_scope=city_scope(code), rule_id=flow["rule"],
    )
    catalog = await db.list_live_reserve_rows(city_scope=city_scope(code), rule_id=None)
    title = "📮 <b>Позвать резерв</b>"
    if code:
        title += f" — {html.escape(await city_label(code))}"
    if flow["rule"] is None:
        filt = "все живые автоотказы"
    else:
        filt = html.escape(await _resolve_rule_label(flow["rule"]))
    pages = max(1, (len(rows) + PAGE - 1) // PAGE)
    page = min(max(flow["page"], 0), pages - 1)
    chunk = rows[page * PAGE:(page + 1) * PAGE]
    live_ids = {int(row["telegram_id"]) for row in rows}
    chosen = [item for item in flow["sel"] if item in live_ids]
    lines = [
        title, "",
        f"Фильтр: {filt}",
        f"В списке: {len(rows)}. Отмечено: {len(chosen)}.",
        "",
        "Отметьте, кого звать сегодня. «Всех по этому фильтру» берёт весь фильтр, "
        "не только эту страницу.",
    ]
    if not rows:
        lines.append("\nЖивых автоотказов по этому фильтру нет.")
    buttons: list[list[InlineKeyboardButton]] = []
    filter_row = [InlineKeyboardButton(
        text=("✅ Все" if flow["rule"] is None else "Все"), callback_data="rsv:a",
    )]
    for rid, label in await _rule_choices(catalog):
        mark = "✅ " if flow["rule"] == rid else ""
        filter_row.append(InlineKeyboardButton(
            text=f"{mark}{_short(label, 24)}", callback_data=f"rsv:r:{rid}",
        ))
        if len(filter_row) == 2:
            buttons.append(filter_row)
            filter_row = []
    if filter_row:
        buttons.append(filter_row)
    for row in chunk:
        tid = int(row["telegram_id"])
        mark = "✅" if tid in chosen else "☐"
        buttons.append([InlineKeyboardButton(
            text=f"{mark} {_short(row.get('full_name') or '')}",
            callback_data=f"rsv:t:{tid}",
        )])
    if pages > 1:
        nav = []
        if page > 0:
            nav.append(InlineKeyboardButton(text="◀", callback_data=f"rsv:p:{page - 1}"))
        nav.append(InlineKeyboardButton(text=f"{page + 1}/{pages}", callback_data=f"rsv:p:{page}"))
        if page + 1 < pages:
            nav.append(InlineKeyboardButton(text="▶", callback_data=f"rsv:p:{page + 1}"))
        buttons.append(nav)
    if rows:
        buttons.append([InlineKeyboardButton(text="Всех по этому фильтру", callback_data="rsv:all")])
    if chosen:
        buttons.append([InlineKeyboardButton(
            text=f"Позвать выбранных ({len(chosen)})", callback_data="rsv:ok",
        )])
    buttons.append([_back_button(flow["origin"])])
    return "\n".join(lines), InlineKeyboardMarkup(inline_keyboard=buttons)


async def _confirm_view(admin_id: int, flow: dict) -> tuple[str, InlineKeyboardMarkup] | None:
    """None — шапка «Все города». Пустой кортеж смысла нет: пустой список зовёт вызывающий."""
    ids = await ids_for_action(
        admin_id, rule_id=flow["rule"], mode=flow["mode"], selected=flow["sel"],
    )
    if ids is None:
        kb = InlineKeyboardMarkup(inline_keyboard=[[_back_button(flow["origin"])]])
        return PICK_CITY, kb
    text = confirm_body(len(ids), today_label(), attached=bool(flow["file"]))
    buttons = [[InlineKeyboardButton(text="Приложить файл", callback_data="rsv:at")]]
    if flow["file"]:
        buttons.append([InlineKeyboardButton(text="Убрать файл", callback_data="rsv:cl")])
    if ids:
        buttons.append([InlineKeyboardButton(text="Позвать", callback_data="rsv:go")])
    buttons.append([InlineKeyboardButton(text="← К списку", callback_data="rsv:no")])
    return text, InlineKeyboardMarkup(inline_keyboard=buttons)


async def _render_list(callback: types.CallbackQuery, state: FSMContext) -> None:
    flow = await _flow(state)
    try:
        text, kb = await _list_view(callback.from_user.id, flow)
    except Exception:
        logger.exception("reserve list")
        text, kb = (
            "Не получилось открыть список. Попробуйте ещё раз.",
            InlineKeyboardMarkup(inline_keyboard=[[_back_button(flow["origin"])]]),
        )
    await _edit(callback.message, text, kb)


async def _open(callback: types.CallbackQuery, state: FSMContext, origin: str) -> None:
    await state.set_state(None)
    await state.update_data(
        rsv_from=origin, rsv_rule=None, rsv_page=0, rsv_sel=[], rsv_file=None, rsv_mode=None,
    )
    await callback.answer()
    await _render_list(callback, state)


async def _open_confirm(callback: types.CallbackQuery, state: FSMContext, mode: str) -> None:
    await state.set_state(None)
    await state.update_data(rsv_mode=mode)
    flow = await _flow(state)
    ids = await ids_for_action(
        callback.from_user.id, rule_id=flow["rule"], mode=mode, selected=flow["sel"],
    )
    if ids is None:
        await callback.answer()
        await _edit(callback.message, PICK_CITY, InlineKeyboardMarkup(
            inline_keyboard=[[_back_button(flow["origin"])]],
        ))
        return
    if not ids:
        await callback.answer("Некого звать по этому фильтру.", show_alert=True)
        return
    await callback.answer()
    view = await _confirm_view(callback.from_user.id, flow)
    await _edit(callback.message, view[0], view[1])


@router.callback_query(F.data == "rsv")
async def rsv_open(callback: types.CallbackQuery, state: FSMContext):
    await _open(callback, state, "apps")


@router.callback_query(F.data.startswith("rsv:"))
async def rsv_act(callback: types.CallbackQuery, state: FSMContext, bot: Bot):
    data = callback.data or ""
    if data == "rsv:h":
        await _open(callback, state, "hub")
        return
    if data == "rsv:a":
        await state.update_data(rsv_rule=None, rsv_sel=[], rsv_page=0)
        await state.set_state(None)
        await callback.answer()
        await _render_list(callback, state)
        return
    if data.startswith("rsv:r:"):
        try:
            rule_id = int(data.split(":")[2])
        except (IndexError, ValueError):
            await callback.answer("Кнопка устарела. Откройте список заново.", show_alert=True)
            return
        await state.update_data(rsv_rule=rule_id, rsv_sel=[], rsv_page=0)
        await state.set_state(None)
        await callback.answer()
        await _render_list(callback, state)
        return
    if data.startswith("rsv:p:"):
        try:
            page = int(data.split(":")[2])
        except (IndexError, ValueError):
            page = 0
        await state.update_data(rsv_page=page)
        await callback.answer()
        await _render_list(callback, state)
        return
    if data.startswith("rsv:t:"):
        try:
            tid = int(data.split(":")[2])
        except (IndexError, ValueError):
            await callback.answer("Кнопка устарела. Откройте список заново.", show_alert=True)
            return
        flow = await _flow(state)
        selected = list(flow["sel"])
        if tid in selected:
            selected.remove(tid)
        else:
            selected.append(tid)
        await state.update_data(rsv_sel=selected)
        await callback.answer()
        await _render_list(callback, state)
        return
    if data == "rsv:all":
        await _open_confirm(callback, state, "all")
        return
    if data == "rsv:ok":
        await _open_confirm(callback, state, "sel")
        return
    if data == "rsv:no":
        await state.set_state(None)
        await callback.answer()
        await _render_list(callback, state)
        return
    if data == "rsv:at":
        await state.set_state(ReserveInvite.file)
        await callback.answer()
        await callback.message.answer(
            "Пришлите один файл: фото, видео или документ. Альбом не подойдёт.\n"
            "Напишите «Отмена», чтобы вернуться без файла.",
        )
        return
    if data == "rsv:cl":
        await state.update_data(rsv_file=None)
        await state.set_state(None)
        await callback.answer("Файл убран")
        flow = await _flow(state)
        view = await _confirm_view(callback.from_user.id, flow)
        if view:
            await _edit(callback.message, view[0], view[1])
        return
    if data == "rsv:go":
        await _send(callback, state, bot)
        return
    await callback.answer("Кнопка устарела. Откройте список заново.", show_alert=True)


async def _send(callback: types.CallbackQuery, state: FSMContext, bot: Bot) -> None:
    await callback.answer("Зовём…")
    await state.set_state(None)
    flow = await _flow(state)
    code = await admin_selected_city(callback.from_user.id)
    if code == ALL_CITIES:
        await _edit(callback.message, PICK_CITY, InlineKeyboardMarkup(
            inline_keyboard=[[_back_button(flow["origin"])]],
        ))
        return
    ids = await ids_for_action(
        callback.from_user.id, rule_id=flow["rule"], mode=flow["mode"], selected=flow["sel"],
    )
    if not ids:
        await _edit(
            callback.message,
            "Некого звать по этому фильтру.",
            InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(
                text="← К списку", callback_data="rsv:no",
            )]]),
        )
        return
    result = await deliver(
        bot, callback.from_user.id, ids,
        city_code=code, rule_id=flow["rule"], attachment=flow["file"],
    )
    await state.update_data(rsv_sel=[], rsv_mode=None)
    text = report_text(
        new=result["new"], already=result["already"], failed=result["failed"],
        blocked=result["blocked"], qr_miss=result["qr_miss"],
    )
    await _edit(callback.message, text, InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="← К списку", callback_data="rsv:no")]],
    ))


async def _answer_confirm(message: types.Message, state: FSMContext) -> None:
    flow = await _flow(state)
    ids = await ids_for_action(
        message.from_user.id, rule_id=flow["rule"], mode=flow["mode"], selected=flow["sel"],
    )
    if not ids:
        await message.answer("Некого звать по этому фильтру.")
        return
    view = await _confirm_view(message.from_user.id, flow)
    if view:
        await message.answer(view[0], reply_markup=view[1], parse_mode="HTML")


@router.message(ReserveInvite.file)
async def rsv_file(message: types.Message, state: FSMContext):
    if message.media_group_id:
        await message.answer("Нужен один файл, не несколько сразу.")
        return
    word = (message.text or "").strip().casefold()
    if word in {"отмена", "назад"}:
        await state.set_state(None)
        await message.answer("Файл не приложен.")
        await _answer_confirm(message, state)
        return
    found = _one_file(message)
    if not found:
        await message.answer("Не понял. Пришлите один файл или напишите «Отмена».")
        return
    await state.update_data(rsv_file=found)
    await state.set_state(None)
    await message.answer("Файл приложим к письму.")
    await _answer_confirm(message, state)
