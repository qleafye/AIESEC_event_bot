"""Проверка подстановок `{…}` при сохранении текста настройки в боте и превью после сохранения.

Шов к общему `admin.router` (техника 13-02/13-03): импортируется последней строкой
`handlers/admin_settings.py` и зависит от него односторонне (settings_edit_value импортируется
лениво внутри хендлера, чтобы не было цикла).

Поток. `gate` зовётся из `settings_edit_value` после валидации и до записи. Нашлась пропавшая
или неизвестная подстановка -> ничего не пишем, спрашиваем (состояние
`EditSetting.waiting_for_placeholder_confirm`). «Сохранить всё равно» / «Исправить на …» кладут
итоговый текст в FSM-поле `ph_ack` и повторно прогоняют ТОТ ЖЕ `settings_edit_value` с копией
сообщения: аудит, развилка вкладок, per-city права и возврат на экран идут общим путём.
"""
import logging

from aiogram import F, types
from aiogram.fsm.context import FSMContext
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from database.db import get_setting
from handlers.admin import router
from handlers.states import EditSetting
from settings_ops import preview_samples, preview_text
from settings_placeholders import TOKEN_RE, check, hint, problem_text
from settings_schema import SETTINGS_SCHEMA

logger = logging.getLogger(__name__)

_QUESTION = "\n\nСохранить всё равно?"


def hint_line(key: str) -> str:
    """Строка-подсказка для экрана правки (с переводом строки) или пустая."""
    h = hint(key)
    return f"\n\n{h}" if h else ""


async def _previous_text(key: str):
    """Прежний текст: своё сохранённое значение ключа, иначе общее/default базового ключа."""
    from cities import split_per_city_key
    current = await get_setting(key)
    if current:
        return current
    split = split_per_city_key(key)
    base = split[0] if split else key
    if split:
        current = await get_setting(base)
        if current:
            return current
    default = SETTINGS_SCHEMA.get(base, {}).get("default")
    return default if isinstance(default, str) else None


async def gate(message: types.Message, state: FSMContext, key: str, value: str,
               ack: str | None = None) -> bool:
    """True — вопрос задан, запись отложена. False — можно сохранять."""
    if value == "-" or not isinstance(value, str):
        return False
    if ack is not None and ack == value:
        return False
    result = check(key, value, await _previous_text(key))
    if result.ok:
        return False
    fixes = [[n, s] for n, s in result.unknown if s]
    await state.update_data(ph_key=key, ph_value=value, ph_fixes=fixes)
    await state.set_state(EditSetting.waiting_for_placeholder_confirm)
    rows = []
    if fixes:
        label = "Исправить на {" + fixes[0][1] + "}" if len(fixes) == 1 else "Исправить подсказанным"
        rows.append([InlineKeyboardButton(text=label, callback_data="phchk_fix")])
    rows.append([InlineKeyboardButton(
        text="Оставить как есть" if result.unknown else "Сохранить всё равно", callback_data="phchk_save")])
    if result.missing or not fixes:
        rows.append([InlineKeyboardButton(text="✏️ Исправить", callback_data="phchk_retry")])
    await message.answer(problem_text(key, result) + _QUESTION, reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))
    return True


async def _resave(callback: types.CallbackQuery, state: FSMContext, value: str):
    data = await state.get_data()
    key = data.get("ph_key")
    await state.update_data(ph_ack=value)
    await state.set_state(EditSetting.waiting_for_value)
    await callback.answer()
    from handlers.admin_settings import settings_edit_value
    copy = callback.message.model_copy(update={"text": value, "from_user": callback.from_user, "entities": None})
    logger.info(f"admin {callback.from_user.id}: подтверждено сохранение {key} с подстановками")
    await settings_edit_value(copy, state)


@router.callback_query(F.data == "phchk_save", EditSetting.waiting_for_placeholder_confirm)
async def phchk_save(callback: types.CallbackQuery, state: FSMContext):
    data = await state.get_data()
    await _resave(callback, state, data["ph_value"])


@router.callback_query(F.data == "phchk_fix", EditSetting.waiting_for_placeholder_confirm)
async def phchk_fix(callback: types.CallbackQuery, state: FSMContext):
    data = await state.get_data()
    fixes = {n: s for n, s in data.get("ph_fixes") or []}
    fixed = TOKEN_RE.sub(lambda m: "{" + fixes.get(m.group(1), m.group(1)) + "}", data["ph_value"])
    await _resave(callback, state, fixed)


@router.callback_query(F.data == "phchk_retry", EditSetting.waiting_for_placeholder_confirm)
async def phchk_retry(callback: types.CallbackQuery, state: FSMContext):
    await state.update_data(ph_ack=None)
    await state.set_state(EditSetting.waiting_for_value)
    await callback.answer()
    await callback.message.answer("Хорошо, пришлите исправленный текст одним сообщением.")


async def send_preview(message: types.Message, key: str, value: str) -> None:
    """«Так увидит делегат:» — только если в тексте есть подстановки."""
    if not isinstance(value, str) or not TOKEN_RE.search(value):
        return
    shown = preview_text(key, value, samples=await preview_samples())
    await message.answer("Так увидит делегат:\n\n" + shown)
