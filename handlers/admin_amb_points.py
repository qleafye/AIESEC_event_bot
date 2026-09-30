"""Раздел «🤝 Амбассадоры» → «💰 Баллы и приватность» (право moderate_game).

Всё про то, сколько получает амбассадор за приглашённого и что он видит:
- «💰 Баллов за одобренного приглашённого» — число вводом (0 — не начислять). Баллы идут амбассадору
  и в общий зачёт, и в текущую волну; уже начисленные не снимаются, кроме исключения за накрутку;
- «🙈 Скрывать имена приглашённых» — амбассадор видит только цифры;
- «🏅 Имена в рейтинге волны» — показывать ли топ с именами.

Тумблеры здесь — это настройки амбассадорки, поэтому право moderate_game. Те же ключи остаются
в «⚙️ Настройки» под правом settings: запись идёт через `set_setting_by_admin`, значение общее.

Шов: своего `Router()` нет, декорирует общий `handlers.admin.router`; подключается хвостовым
импортом `handlers/admin_amb_tier_ladder.py`.
"""
from __future__ import annotations

import logging

from aiogram import F, types
from aiogram.filters import StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from handlers.admin import router
from handlers.states import AmbPointsEdit
from settings_audit import set_setting_by_admin
from settings_schema import get_setting_typed

logger = logging.getLogger(__name__)

COINS_KEY = "ambassador_referral_coins"

_TOGGLES = {
    # суффикс callback -> (ключ, алерт при on, алерт при off)
    "hide": (
        "amb_hide_invitee_names",
        "🙈 Имена скрыты: амбассадор видит у приглашённых только цифры.",
        "🙈 Имена видны: амбассадор снова видит, кто пришёл по его ссылке.",
    ),
    "wavenames": (
        "wave_rating_show_names",
        "🏅 Имена в рейтинге волны показываются: амбассадор видит топ с именами.",
        "🏅 Имена в рейтинге скрыты: амбассадор видит только своё место и отрыв от призового.",
    ),
}

_PROMPT = (
    "💰 <b>Баллов за одобренного приглашённого</b>\n\n"
    "Пришлите число баллов, например <code>10</code>. <code>0</code> — не начислять."
)
_NOT_NUMBER = "Не понял. Пришлите целое число, например 10, или 0."
_NEGATIVE = "Баллы не могут быть меньше нуля. Пришлите 0 или больше."


def _yes_no(flag: bool) -> str:
    return "да" if flag else "нет"


def _cancel_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="❌ Отмена", callback_data="ambpt_coins_cancel")],
    ])


async def _edit_or_send(message: types.Message, text: str, kb: InlineKeyboardMarkup) -> None:
    try:
        await message.edit_text(text, parse_mode="HTML", reply_markup=kb)
    except Exception as e:
        if "not modified" in str(e).lower():
            return
        await message.answer(text, parse_mode="HTML", reply_markup=kb)


async def render_points_screen() -> tuple[str, InlineKeyboardMarkup]:
    from handlers.admin_sections import back_button

    coins = int(await get_setting_typed(COINS_KEY) or 0)
    hide = await get_setting_typed("amb_hide_invitee_names") == "on"
    names = await get_setting_typed("wave_rating_show_names") == "on"
    coins_label = f"{coins} (0 — выключено)" if coins else "0 (выключено)"
    text = "\n".join([
        "<b>🤝 Амбассадоры → 💰 Баллы и приватность</b>",
        "",
        f"💰 Баллов за одобренного приглашённого: <b>{coins_label}</b>",
        "Баллы идут амбассадору и в общий зачёт, и в текущую волну. Уже начисленные не "
        "снимаются, кроме исключения за накрутку.",
        "",
        f"🙈 Скрывать имена приглашённых: <b>{_yes_no(hide)}</b>",
        f"🏅 Имена в рейтинге волны: <b>{_yes_no(names)}</b>",
    ])
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=f"💰 Баллов за приглашённого: {coins}", callback_data="ambpt_coins")],
        [InlineKeyboardButton(text=f"🙈 Скрывать имена приглашённых: {_yes_no(hide)}",
                              callback_data="ambpt_toggle:hide")],
        [InlineKeyboardButton(text=f"🏅 Имена в рейтинге волны: {_yes_no(names)}",
                              callback_data="ambpt_toggle:wavenames")],
        [back_button("admin_amb_points")],
    ])
    return text, kb


@router.callback_query(F.data == "admin_amb_points")
async def show_amb_points(callback: types.CallbackQuery, state: FSMContext):
    await state.clear()
    text, kb = await render_points_screen()
    await _edit_or_send(callback.message, text, kb)
    await callback.answer()


@router.callback_query(F.data == "ambpt_coins")
async def amb_points_start(callback: types.CallbackQuery, state: FSMContext):
    await state.set_state(AmbPointsEdit.waiting_for_value)
    await callback.message.answer(_PROMPT, parse_mode="HTML", reply_markup=_cancel_kb())
    await callback.answer()


@router.callback_query(F.data == "ambpt_coins_cancel")
async def amb_points_cancel(callback: types.CallbackQuery, state: FSMContext):
    await state.clear()
    text, kb = await render_points_screen()
    await callback.message.answer("Отменено, баллы не менялись.")
    await callback.message.answer(text, parse_mode="HTML", reply_markup=kb)
    await callback.answer()


@router.message(StateFilter(AmbPointsEdit.waiting_for_value))
async def amb_points_value(message: types.Message, state: FSMContext):
    body = (message.text or "").strip()
    if body.startswith("/") or body.lower() in {"отмена", "❌ отмена"}:
        await state.clear()
        await message.answer("Отменено, баллы не менялись.")
        return
    digits = body.lstrip("-")
    if not (digits.isascii() and digits.isdigit()):
        await message.answer(_NOT_NUMBER, reply_markup=_cancel_kb())
        return
    value = int(body)
    if value < 0:
        await message.answer(_NEGATIVE, reply_markup=_cancel_kb())
        return
    await set_setting_by_admin(message.from_user.id, COINS_KEY, str(value))
    logger.info("admin=%s %s=%s", message.from_user.id, COINS_KEY, value)
    await state.clear()
    text, kb = await render_points_screen()
    await message.answer("✅ Сохранено\n\n" + text, parse_mode="HTML", reply_markup=kb)


@router.callback_query(F.data.startswith("ambpt_toggle:"))
async def amb_points_toggle(callback: types.CallbackQuery):
    spec = _TOGGLES.get(callback.data.split(":", 1)[1])
    if spec is None:
        await callback.answer("Кнопка устарела — откройте экран заново", show_alert=True)
        return
    key, on_note, off_note = spec
    new_val = "off" if await get_setting_typed(key) == "on" else "on"
    await set_setting_by_admin(callback.from_user.id, key, new_val)
    logger.info("admin=%s %s=%s", callback.from_user.id, key, new_val)
    await callback.answer(on_note if new_val == "on" else off_note, show_alert=True)
    text, kb = await render_points_screen()
    await _edit_or_send(callback.message, text, kb)
