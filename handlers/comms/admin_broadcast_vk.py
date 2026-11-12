"""Рассылка ВК отдельно от Telegram. Аудитория — кому сейчас пишем во ВК.

Картинку Telegram сюда не берём: у сообщества уходит только текст.
Шов на общий admin.router, импорт — хвостом admin_broadcasts.py.
"""
from aiogram import F, types
from aiogram.fsm.context import FSMContext
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardRemove

from handlers.admin import router
from handlers.states import Broadcast
from keyboards.builders import get_cancel_kb
from services.vk_broadcast import broadcast_vk, list_vk_broadcast_ids

_VK_LIMIT = 4096
_OPEN_STATES = frozenset({
    Broadcast.target_selection.state,
    Broadcast.vk_text.state,
    Broadcast.vk_confirm.state,
})


def _has_attachment(message: types.Message) -> bool:
    return bool(
        message.photo or message.video or message.document
        or message.animation or message.sticker or message.audio or message.voice
    )


@router.callback_query(F.data == "broadcast_vk")
async def vk_broadcast_start(callback: types.CallbackQuery, state: FSMContext):
    """Кнопка в меню рассылок. Чужую недописанную рассылку Telegram не затирает."""
    current = await state.get_state()
    if current and current not in _OPEN_STATES:
        await callback.answer(
            "Сначала закончите или отмените текущую рассылку.", show_alert=True,
        )
        return
    ids = await list_vk_broadcast_ids()
    if not ids:
        from handlers.comms.admin_broadcasts import build_broadcast_menu_kb

        await callback.message.edit_text(
            "Сейчас во ВК никому не пишем. Рассылка не начнётся.",
            reply_markup=build_broadcast_menu_kb(callback.from_user.id),
        )
        await state.set_state(Broadcast.target_selection)
        await callback.answer()
        return
    await callback.answer()
    try:
        await callback.message.delete()
    except Exception:
        pass
    await callback.message.answer(
        f"Рассылка ВК. Получателей: {len(ids)}. Пришлите текст.\n"
        "Картинка не уйдёт: во ВК отправляется только текст.",
        reply_markup=get_cancel_kb(),
    )
    await state.set_state(Broadcast.vk_text)
    await state.update_data(vk_text=None)


@router.message(Broadcast.vk_text)
async def vk_broadcast_text(message: types.Message, state: FSMContext):
    """Текст для ВК. Вложение не прячем в подпись: менеджер думал бы, что уйдёт картинка."""
    if _has_attachment(message):
        await message.answer(
            "Во ВК уходит только текст. Пришлите сообщение без картинки и файла."
        )
        return
    body = (message.text or "").strip()
    if not body:
        await message.answer("Пришлите текст рассылки.")
        return
    if len(body) > _VK_LIMIT:
        await message.answer(
            "Во ВК помещается 4096 символов. Сократите текст и пришлите ещё раз."
        )
        return
    ids = await list_vk_broadcast_ids()
    if not ids:
        await state.clear()
        await message.answer(
            "Сейчас во ВК никому не пишем. Ничего не отправлено.",
            reply_markup=ReplyKeyboardRemove(),
        )
        return
    await state.update_data(vk_text=body)
    await state.set_state(Broadcast.vk_confirm)
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=f"✅ Отправить ({len(ids)})", callback_data="bcvk_go")],
        [InlineKeyboardButton(text="Не отправлять", callback_data="bcvk_no")],
    ])
    await message.answer(
        f"Рассылка ВК: получателей {len(ids)}. "
        "Уйдёт текст тем, кому сейчас пишем во ВК. Отправить?",
        reply_markup=keyboard,
    )


@router.callback_query(F.data == "bcvk_go")
async def vk_broadcast_go(callback: types.CallbackQuery, state: FSMContext):
    """Список читается снова: кто ушёл в Telegram между вопросом и кнопкой, письмо не получит."""
    if await state.get_state() != Broadcast.vk_confirm.state:
        await callback.answer(
            "Это подтверждение уже не действует. Откройте «Рассылка ВК» снова.",
            show_alert=True,
        )
        return
    data = await state.get_data()
    text = (data.get("vk_text") or "").strip()
    ids = await list_vk_broadcast_ids()
    await state.clear()
    if not text or not ids:
        await callback.message.edit_text(
            "Сейчас отправлять некому или текст не найден. Ничего не ушло."
        )
        await callback.answer()
        return
    success, failed = await broadcast_vk(text, None, ids)
    await callback.message.edit_text(
        f"Рассылка ВК: {success} доставлено, {failed} ошибок."
    )
    await callback.answer()


@router.callback_query(F.data == "bcvk_no")
async def vk_broadcast_no(callback: types.CallbackQuery, state: FSMContext):
    if await state.get_state() != Broadcast.vk_confirm.state:
        await callback.answer(
            "Это подтверждение уже не действует. Откройте «Рассылка ВК» снова.",
            show_alert=True,
        )
        return
    await state.clear()
    await callback.message.edit_text("Рассылка ВК не отправлена. Ничего не ушло.")
    await callback.answer()
