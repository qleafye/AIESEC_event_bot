"""Подтверждение надстроек раздела «🤝 Амбассадоры».

Тумблер, который меняет экран делегата, сначала показывает, что изменится, и только
потом пишет настройку. Коды ключей менеджеру не показываем. Волны — индикатор, не тумблер.
"""
from __future__ import annotations

import logging

from aiogram import F, types
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from handlers.admin import router
from handlers.amb.amb_panel import addon_on, confirm_title, effect_text, header_city, setting_key, write_target
from services.settings.audit import set_setting_by_admin

logger = logging.getLogger(__name__)

_SAVED = {
    ("offer", "on"): "Сохранено. После анкеты делегат увидит предложение взять свою ссылку.",
    ("offer", "off"): "Сохранено. Предложение после анкеты исчезнет. Уже выданные ссылки останутся.",
    ("team", "on"): "Сохранено. «Хочу свою ссылку» — заявка в команду, на «Моя ссылка» появится статус.",
    ("team", "off"): "Сохранено. В команду снова сразу по кнопке. Кандидаты и настройки на месте.",
    ("steps", "on"): "Сохранено. Амбассадоры начнут получать сообщения ступеней.",
    ("steps", "off"): "Сохранено. Новые ступени не придут. Уже выданные и настройки лестницы остаются.",
    ("stats", "on"): "Сохранено. На «Моя ссылка» появятся баллы и место в волне.",
    ("stats", "off"): "Сохранено. Строки баллов и места скроются. Баллы и волна остаются.",
}


async def _edit(message: types.Message, text: str, kb: InlineKeyboardMarkup) -> None:
    try:
        await message.edit_text(text, parse_mode="HTML", reply_markup=kb)
    except Exception as e:
        if "not modified" not in str(e).lower():
            await message.answer(text, parse_mode="HTML", reply_markup=kb)


async def _back_to_section(callback: types.CallbackQuery) -> None:
    from handlers.settings.admin_sections import section_screen
    screen = await section_screen(callback.from_user.id, "amb")
    if screen is None:
        return
    text, kb = screen
    await _edit(callback.message, text, kb)


@router.callback_query(F.data.startswith("amb_addon_ask:"))
async def amb_addon_ask(callback: types.CallbackQuery):
    code = (callback.data or "").split(":", 1)[1]
    city = await header_city(callback.from_user.id)
    on = await addon_on(code, city)
    target = "off" if on else "on"
    effect = effect_text(code, target)
    title = confirm_title(code)
    if not effect or not title:
        await callback.answer("Кнопка устарела. Откройте раздел заново.", show_alert=True)
        return
    now = "включено" if on else "выключено"
    text = f"<b>{title}</b>\n\nСейчас: {now}.\n\nЕсли переключить: {effect}"
    verb = "Выключить" if on else "Включить"
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=f"✅ {verb}", callback_data=f"amb_addon_go:{code}:{target}")],
        [InlineKeyboardButton(text="← Не менять", callback_data="admin_sec:amb")],
    ])
    await _edit(callback.message, text, kb)
    await callback.answer()


@router.callback_query(F.data.startswith("amb_addon_go:"))
async def amb_addon_go(callback: types.CallbackQuery):
    parts = (callback.data or "").split(":")
    if len(parts) != 3:
        await callback.answer("Кнопка устарела. Откройте раздел заново.", show_alert=True)
        return
    _prefix, code, value = parts
    base = setting_key(code)
    alert = _SAVED.get((code, value))
    if base is None or value not in ("on", "off") or alert is None:
        await callback.answer("Кнопка устарела. Откройте раздел заново.", show_alert=True)
        return
    key = await write_target(callback.from_user.id, base)
    await set_setting_by_admin(callback.from_user.id, key, value)
    logger.info("admin=%s addon %s=%s", callback.from_user.id, code, value)
    await callback.answer(alert, show_alert=True)
    await _back_to_section(callback)


@router.callback_query(F.data == "amb_off_note")
async def amb_off_note(callback: types.CallbackQuery):
    await callback.answer(
        "Выключено. Данные и настройки сохранены — включите кнопкой выше.",
        show_alert=True,
    )


@router.callback_query(F.data == "amb_addon_look")
async def amb_addon_look(callback: types.CallbackQuery):
    await callback.answer(
        "Это включает тот, у кого есть право «⚙️ Настройки».",
        show_alert=True,
    )
