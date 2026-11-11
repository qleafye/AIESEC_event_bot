"""Экран «Позвать резерв»: письмо на сегодня. Статус заявки не меняется."""
from __future__ import annotations

import html
import logging

from aiogram import Bot, F, types
from aiogram.fsm.context import FSMContext
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

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
from services.forum.reserve_invite import (
    PICK_CITY,
    _rule_ids,
    confirm_line,
    deliver,
    ids_for_action,
    report_text,
    rows_for_header,
    today_label,
)

logger = logging.getLogger(__name__)

PAGE = 6


def _short(text: str, limit: int = 28) -> str:
    clean = " ".join((text or "без имени").split())
    if len(clean) <= limit:
        return clean
    return clean[: limit - 1] + "…"


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
