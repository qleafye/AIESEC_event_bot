"""«🕐 Часовой пояс» города — экран хаба «🎪 Форум: функции».

Менеджер выбирает смещение города от Москвы кнопками («МСК», «МСК+1» … «МСК+9»), вводить
ничего не нужно. Что от этого зависит, написано прямо на экране: время сессий программы
считается местным, рассылки QR уходят по местным часам, отметки и карточки показывают
местное время. Сами метки в базе остаются московскими.

Форма шва — как у соседей (`handlers/forum/admin_forum_ready.py`): своего `Router()` нет,
`from handlers.admin import router`, импорт из хвоста `handlers/admin.py`. Право —
`moderate_reg`, как у остальных экранов хаба."""
import html
import logging

from aiogram import F, types
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from domain.cities import cities_module_on, city_label, per_city_key
from handlers.admin import router
from handlers.forum.admin_checkin import _CITY_FORBIDDEN_ALERT, _city_allowed, _decode_city, _encode_city
from services.infra.timeutil import city_offset_hours, offset_label
from services.settings.audit import set_setting_by_admin
from domain.settings.schema import SETTINGS_SCHEMA

logger = logging.getLogger(__name__)

KEY = "city_tz_offset"
_OPTIONS = SETTINGS_SCHEMA[KEY]["options"]
_LABELS = SETTINGS_SCHEMA[KEY]["option_labels"]


async def tz_cfg_text_kb(code: str | None) -> tuple[str, InlineKeyboardMarkup]:
    current = await city_offset_hours(code)
    label = await city_label(code) if code and await cities_module_on() else None
    text = (
        "🕐 <b>Часовой пояс</b>" + (f" — {html.escape(label)}" if label else "") + "\n"
        f"Сейчас: <b>{offset_label(current)}</b>\n\n"
        "Выберите, на сколько часов город опережает Москву. Тюмень — «МСК+2».\n\n"
        "Что от этого зависит: время сессий в программе считается местным («🔴 Идёт сейчас», "
        "отзыв о сессии), рассылки QR накануне и утром уходят по местным часам, «не пришедшим» "
        "не пишем в местные тихие часы, отметки и карточки показывают местное время."
    )
    row: list[InlineKeyboardButton] = []
    rows: list[list[InlineKeyboardButton]] = []
    for opt in _OPTIONS:
        mark = "✅ " if int(opt) == current else ""
        row.append(InlineKeyboardButton(
            text=f"{mark}{_LABELS[opt]}", callback_data=f"forumtz_set:{_encode_city(code)}:{opt}",
        ))
        if len(row) == 3:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    rows.append([InlineKeyboardButton(text="◀️ Назад", callback_data="admin_forum_functions")])
    return text, InlineKeyboardMarkup(inline_keyboard=rows)


@router.callback_query(F.data.startswith("forumtz_cfg:"))
async def forumtz_cfg_screen(callback: types.CallbackQuery):
    code = _decode_city(callback.data.split(":", 1)[1])
    if not await _city_allowed(callback.from_user.id, code):
        await callback.answer(_CITY_FORBIDDEN_ALERT, show_alert=True)
        return
    text, kb = await tz_cfg_text_kb(code)
    await callback.message.answer(text, parse_mode="HTML", reply_markup=kb)
    await callback.answer()


async def _safe_reschedule(code: str | None) -> None:
    """Смена пояса сдвигает местное время рассылок QR — переставить джобы города сразу.
    Fail-soft: недоступный планировщик не должен ронять сохранение."""
    try:
        from services.checkin_broadcast import schedule_city_jobs
        await schedule_city_jobs(code)
    except Exception as e:
        logger.error(f"forum_tz reschedule({code!r}) failed: {e}")


@router.callback_query(F.data.startswith("forumtz_set:"))
async def forumtz_set_go(callback: types.CallbackQuery):
    _, raw_city, value = callback.data.split(":", 2)
    code = _decode_city(raw_city)
    if value not in _OPTIONS:
        await callback.answer("Не понял, какой пояс выбран — нажмите одну из кнопок.", show_alert=True)
        return
    if not await _city_allowed(callback.from_user.id, code):
        await callback.answer(_CITY_FORBIDDEN_ALERT, show_alert=True)
        return
    if code and await cities_module_on():
        composed = per_city_key(KEY, code)
        await set_setting_by_admin(callback.from_user.id, composed, value)
    else:
        await set_setting_by_admin(callback.from_user.id, KEY, value)
    await _safe_reschedule(code)
    text, kb = await tz_cfg_text_kb(code)
    await callback.message.edit_text(text, parse_mode="HTML", reply_markup=kb)
    await callback.answer(f"✅ {_LABELS[value]}")
