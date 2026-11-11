"""«Перенести сканы с тренировки» на экране «✅ Отметки на форуме».

Город — из шапки админки, как у соседних экранов чек-ина. Сессию менеджер выбирает
кнопкой (зал и слот), не вводом id. Подтверждение говорит, что люди будут отмечены,
повтор их второй раз не отметит и статус заявки не изменится.
"""
from __future__ import annotations

import html

from aiogram import F, types
from aiogram.exceptions import TelegramBadRequest
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from handlers.admin import router
from services.forum import checkin_training_move as move

_PREVIEW = 25
_NO_RIGHTS = "Недостаточно прав"
_BAD_SESSION = "Эта сессия не сегодняшнего дня этого города. Откройте список заново."
_PICK_CITY = "Сначала выберите один город в шапке админки — перенос идёт по одному городу."
_EMPTY = "Переносить некого."
_NOT_TODAY = "На сегодня сессий нет — выбрать некуда."


def _people(n: int) -> str:
    n10, n100 = n % 10, n % 100
    if n10 == 1 and n100 != 11:
        word = "человек"
    elif n10 in (2, 3, 4) and n100 not in (12, 13, 14):
        word = "человека"
    else:
        word = "человек"
    return f"{n} {word}"


def _back_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="◀️ Назад", callback_data="admin_checkin"),
    ]])


def _session_id(data: str) -> int | None:
    raw = data.split(":", 1)[1]
    return int(raw) if raw.isdigit() else None


async def _manager(callback: types.CallbackQuery) -> bool:
    from handlers.access.admin_caps import _holds, resolve_capabilities

    if _holds(await resolve_capabilities(callback.from_user.id), "moderate_reg"):
        return True
    await callback.answer(_NO_RIGHTS, show_alert=True)
    return False


async def _move_city(admin_id: int) -> str | None:
    """Город шапки. Тот же разбор, что у экрана приложения."""
    return await move.header_city(admin_id)


async def _allowed_city(callback: types.CallbackQuery) -> str | None:
    from handlers.forum.admin_checkin import _CITY_FORBIDDEN_ALERT, _city_allowed

    code = await _move_city(callback.from_user.id)
    if code is None:
        await callback.message.answer(_PICK_CITY, reply_markup=_back_kb())
        await callback.answer()
        return None
    if not await _city_allowed(callback.from_user.id, code):
        await callback.answer(_CITY_FORBIDDEN_ALERT, show_alert=True)
        return None
    return code


async def _clear_kb(message) -> None:
    edit = getattr(message, "edit_reply_markup", None)
    if edit is None:
        return
    try:
        await edit(reply_markup=None)
    except TelegramBadRequest:
        return


def _list_text(city_label: str, rows: list[dict], sessions: list[dict]) -> str:
    shown = rows[:_PREVIEW]
    lines = [
        f"Перенос сканов с тренировки — {html.escape(city_label)}",
        "",
        f"Сегодня на тренировке {_people(len(rows))}:",
    ]
    lines.extend(f"• {html.escape(move.person_name(row))}" for row in shown)
    if len(rows) > _PREVIEW:
        lines.append(f"… и ещё {len(rows) - _PREVIEW}")
    lines.append("")
    if sessions:
        lines.append("Куда перенести — выберите сессию:")
    else:
        lines.append(_NOT_TODAY)
    return "\n".join(lines)


def _list_kb(sessions: list[dict]) -> InlineKeyboardMarkup:
    rows = [[InlineKeyboardButton(
        text=move.choice_label(session), callback_data=f"ctm_pick:{session['id']}",
    )] for session in sessions]
    rows.append([InlineKeyboardButton(text="◀️ Назад", callback_data="admin_checkin")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _result_text(label: str, counts: dict) -> str:
    return (
        f"Готово. Отмечены на сессии «{html.escape(label)}».\n\n"
        f"Новые: {counts['new']}\n"
        f"Уже были на этой сессии: {counts['duplicate']}\n"
        f"Перенесены с параллельной сессии того же слота: {counts['moved']}"
    )


@router.callback_query(F.data == "checkin_training_move")
async def training_move_open(callback: types.CallbackQuery):
    if not await _manager(callback):
        return
    code = await _allowed_city(callback)
    if code is None:
        return
    from domain.cities import city_label

    rows, sessions = await move.training_move_screen(code)
    if not rows:
        await callback.message.answer(_EMPTY, reply_markup=_back_kb())
        await callback.answer()
        return
    await callback.message.answer(
        _list_text(await city_label(code), rows, sessions),
        parse_mode="HTML", reply_markup=_list_kb(sessions),
    )
    await callback.answer()


@router.callback_query(F.data.startswith("ctm_pick:"))
async def training_move_pick(callback: types.CallbackQuery):
    if not await _manager(callback):
        return
    code = await _allowed_city(callback)
    if code is None:
        return
    session_id = _session_id(callback.data)
    rows, sessions = await move.training_move_screen(code)
    if not rows:
        await callback.message.answer(_EMPTY, reply_markup=_back_kb())
        await callback.answer()
        return
    session = next((s for s in sessions if session_id is not None and s["id"] == session_id), None)
    if session is None:
        await callback.message.answer(_BAD_SESSION, reply_markup=_back_kb())
        await callback.answer()
        return
    label = html.escape(move.choice_label(session))
    text = (
        f"Эти люди ({_people(len(rows))}) будут отмечены на сессии «{label}».\n\n"
        "Повторное нажатие их второй раз не отметит. Статус заявки не меняется."
    )
    kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="✅ Отметить", callback_data=f"ctm_go:{session['id']}"),
        InlineKeyboardButton(text="Отмена", callback_data="ctm_no"),
    ]])
    await callback.message.answer(text, parse_mode="HTML", reply_markup=kb)
    await callback.answer()


@router.callback_query(F.data.startswith("ctm_go:"))
async def training_move_go(callback: types.CallbackQuery):
    if not await _manager(callback):
        return
    code = await _allowed_city(callback)
    if code is None:
        return
    session_id = _session_id(callback.data)
    if session_id is None:
        await callback.message.answer(_BAD_SESSION, reply_markup=_back_kb())
        await callback.answer()
        return
    await _clear_kb(callback.message)
    result = await move.transfer_training_scans(
        code, session_id, staff_id=callback.from_user.id,
    )
    if result["kind"] == "empty":
        await callback.message.answer(_EMPTY, reply_markup=_back_kb())
    elif result["kind"] == "bad_session":
        await callback.message.answer(_BAD_SESSION, reply_markup=_back_kb())
    else:
        await callback.message.answer(
            _result_text(result["label"], result["counts"]), parse_mode="HTML",
            reply_markup=_back_kb(),
        )
    await callback.answer()


@router.callback_query(F.data == "ctm_no")
async def training_move_cancel(callback: types.CallbackQuery):
    if not await _manager(callback):
        return
    await _clear_kb(callback.message)
    await callback.message.answer("Отменено. Никого не отметили.", reply_markup=_back_kb())
    await callback.answer()
