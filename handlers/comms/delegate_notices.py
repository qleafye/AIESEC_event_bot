"""Три выключателя уведомлений делегата и кнопка «Не присылать сегодня».

Срок до завтрашней полуночи хранится отдельно от флагов: снятие одного флага
срок не снимает, «Присылать всё» флаги не снимает. Пока отправители не читают
флаги, подпись кнопки на рассылке не обещает новое — тот же текст, что раньше.
"""
from __future__ import annotations

import logging

from aiogram import F, types
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from database.db import (
    NOTICE_KINDS,
    get_user,
    notice_flags,
    set_daily_quiet,
    set_notice_flag,
    tomorrow_moscow_midnight,
)
from domain.settings.schema import get_setting_typed
from handlers.i18n import reg_i18n
from handlers.user_actions import router

logger = logging.getLogger(__name__)

# Совпадают с services.scheduler.MUTE_TODAY_CALLBACK / UNMUTE_TODAY_CALLBACK.
# Литералы продублированы здесь намеренно: импорт scheduler из хендлера рассылки
# на уровне модуля зациклил бы загрузку.
_MUTE_TODAY = "bc_mute_today"
_UNMUTE_TODAY = "bc_unmute_today"

_NOTICE_CALLBACK = {
    "broadcasts": "bc_notice_bc",
    "game": "bc_notice_game",
    "reminders": "bc_notice_rem",
}

# Подписи без имён колонок. «Выключить» — то, что сделает нажатие.
_OFF_LABEL = {
    "broadcasts": "🔕 Не присылать рассылки",
    "game": "🔕 Не беспокоить игрой",
    "reminders": "🔕 Без напоминаний",
}
_ON_LABEL = {
    "broadcasts": "🔔 Снова присылать рассылки",
    "game": "🔔 Снова присылать игру и баллы",
    "reminders": "🔔 Снова присылать напоминания",
}
_TURNED_OFF = {
    "broadcasts": "Рассылки выключены. Включить — кнопка «Снова присылать рассылки».",
    "game": "Игра и баллы выключены. Включить — кнопка «Снова присылать игру и баллы».",
    "reminders": "Напоминания выключены. Включить — кнопка «Снова присылать напоминания».",
}
_TURNED_ON = {
    "broadcasts": "Снова присылаю рассылки.",
    "game": "Снова присылаю игру и баллы.",
    "reminders": "Снова присылаю напоминания.",
}

_SCREEN_TEXT = "Три выключателя. Каждый действует, пока сами не включите обратно."
_SAVE_FAILED = "Не получилось записать. Нажмите кнопку ещё раз."
_UNKNOWN_BUTTON = "Не понял кнопку. Нажмите одну из трёх на этом сообщении."

# Те же дефолты, что реестр broadcast_mute_confirm_text / broadcast_unmute_confirm_text.
# Не расширять, пока отправители не начнут читать все три выключателя.
_MUTE_TODAY_CONFIRM_TEXT = (
    "🔕 Хорошо, сегодня присылаю только важное. Вернуть — кнопка «🔔 Присылать всё»."
)
_UNMUTE_TODAY_CONFIRM_TEXT = "🔔 Хорошо, снова присылаю все рассылки."


def _swap_mute_button(
    markup: InlineKeyboardMarkup | None, old_callback: str, new_button: InlineKeyboardButton,
) -> InlineKeyboardMarkup:
    """Меняет на месте кнопку суточного глушения. Остальные ряды не трогает."""
    if markup is None:
        return InlineKeyboardMarkup(inline_keyboard=[[new_button]])
    rows = []
    for row in markup.inline_keyboard:
        rows.append([new_button if btn.callback_data == old_callback else btn for btn in row])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _switches_markup(flags: dict[str, bool], lang: str, tr_map: dict) -> InlineKeyboardMarkup:
    rows = []
    for kind in NOTICE_KINDS:
        label = _ON_LABEL[kind] if flags.get(kind) else _OFF_LABEL[kind]
        rows.append([InlineKeyboardButton(
            text=reg_i18n.tr_text(label, lang, tr_map),
            callback_data=_NOTICE_CALLBACK[kind],
        )])
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def _say(callback: types.CallbackQuery, text: str) -> None:
    lang, tr_map = await reg_i18n.ctx_for(callback)
    await callback.answer(reg_i18n.tr_text(text, lang, tr_map), show_alert=True)


async def _offer_screen(callback: types.CallbackQuery) -> None:
    """Отдельное сообщение: клавиатуру рассылки не раздуваем."""
    message = getattr(callback, "message", None)
    answer = getattr(message, "answer", None)
    if answer is None:
        return
    try:
        user = await get_user(callback.from_user.id)
        lang, tr_map = await reg_i18n.ctx_for(callback)
        await answer(
            reg_i18n.tr_text(_SCREEN_TEXT, lang, tr_map),
            reply_markup=_switches_markup(notice_flags(user), lang, tr_map),
        )
    except Exception:
        logger.warning("экран выключателей не отправился", exc_info=True)


async def apply_mute_today(callback: types.CallbackQuery, now) -> None:
    from services import scheduler as sched

    wrote = await set_daily_quiet(
        callback.from_user.id, now.strftime("%Y-%m-%d"), tomorrow_moscow_midnight(now),
    )
    if not wrote:
        await _say(callback, _SAVE_FAILED)
        return
    text = await get_setting_typed("broadcast_mute_confirm_text") or _MUTE_TODAY_CONFIRM_TEXT
    await _say(callback, text)
    try:
        new_button = await sched.unmute_button(callback.from_user.id)
        new_markup = _swap_mute_button(
            getattr(callback.message, "reply_markup", None), _MUTE_TODAY, new_button,
        )
        await callback.message.edit_reply_markup(reply_markup=new_markup)
    except Exception:
        pass
    await _offer_screen(callback)


async def apply_unmute_today(callback: types.CallbackQuery) -> None:
    from services import scheduler as sched

    wrote = await set_daily_quiet(callback.from_user.id, None, None)
    if not wrote:
        await _say(callback, _SAVE_FAILED)
        return
    text = await get_setting_typed("broadcast_unmute_confirm_text") or _UNMUTE_TODAY_CONFIRM_TEXT
    await _say(callback, text)
    try:
        new_button = await sched.mute_button(callback.from_user.id)
        new_markup = _swap_mute_button(
            getattr(callback.message, "reply_markup", None), _UNMUTE_TODAY, new_button,
        )
        await callback.message.edit_reply_markup(reply_markup=new_markup)
    except Exception:
        pass
    await _offer_screen(callback)


async def _toggle(callback: types.CallbackQuery, kind: str) -> None:
    if kind not in NOTICE_KINDS:
        await _say(callback, _UNKNOWN_BUTTON)
        return
    user = await get_user(callback.from_user.id)
    if user is None:
        await _say(callback, _SAVE_FAILED)
        return
    turn_on = not notice_flags(user)[kind]
    if not await set_notice_flag(callback.from_user.id, kind, turn_on):
        await _say(callback, _SAVE_FAILED)
        return
    await _say(callback, _TURNED_OFF[kind] if turn_on else _TURNED_ON[kind])
    try:
        lang, tr_map = await reg_i18n.ctx_for(callback)
        flags = notice_flags(await get_user(callback.from_user.id))
        await callback.message.edit_reply_markup(
            reply_markup=_switches_markup(flags, lang, tr_map),
        )
    except Exception:
        pass


@router.callback_query(F.data == "bc_notice_bc")
async def toggle_notice_broadcasts(callback: types.CallbackQuery):
    await _toggle(callback, "broadcasts")


@router.callback_query(F.data == "bc_notice_game")
async def toggle_notice_game(callback: types.CallbackQuery):
    await _toggle(callback, "game")


@router.callback_query(F.data == "bc_notice_rem")
async def toggle_notice_reminders(callback: types.CallbackQuery):
    await _toggle(callback, "reminders")
