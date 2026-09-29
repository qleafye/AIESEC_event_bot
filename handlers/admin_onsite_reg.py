"""Экран менеджера «📝 Регистрация на месте» (FORUM-CHECKIN.md D-41, D-36).

Тумблер per_city `onsite_reg_enabled` (дефолт выключен) и кнопка «📎 QR для стойки» — картинка
ссылки `?start=walkin_<город>`, которую волонтёр печатает или показывает с телефона. Человек
сканирует её и проходит короткую анкету (handlers/onsite_reg.py), а решение о входе принимает
волонтёр у стойки. Статусная строка — хаб «🎪 Форум: функции» (handlers/admin_forum_functions.py).

Своего `Router()` нет — декорирует `handlers.admin.router`, тот же приём, что
handlers/admin_lost_found.py; импортирован в ХВОСТЕ handlers/admin.py (golden snapshot — чистое
добавление). Капа — «moderate_reg», как у соседних тумблеров хаба (handlers/admin_caps.py);
городская привязка менеджера (D-26) — `_city_allowed`, как у бюро находок."""
import html
import logging

from aiogram import Bot, F, types
from aiogram.types import BufferedInputFile, InlineKeyboardButton, InlineKeyboardMarkup

from core.cities import cities_module_on, city_label, per_city_key
from database.db import get_staff_city
from handlers.admin import router
from handlers.admin_checkin import _CITY_FORBIDDEN_ALERT, _city_allowed, _decode_city, _encode_city
from services.onsite_reg import onsite_enabled, walkin_link, walkin_qr_png
from core.settings_audit import set_setting_by_admin

logger = logging.getLogger(__name__)

_KEY = "onsite_reg_enabled"

_EXPLAIN = (
    "Для тех, кого нет в базе или чья заявка не одобрена, — прямо на площадке.\n\n"
    "• Человек сканирует QR у стойки и отвечает в боте на 3 вопроса: имя, телефон, вуз.\n"
    "• Волонтёр находит его в сканере по фамилии и одной кнопкой пропускает — решение "
    "записывается на волонтёра.\n"
    "• После этого человеку приходит его QR для сессий.\n"
    "• Пакет делегата таким участникам выдают по решению DXP — бот его не выдаёт.\n\n"
    "Пока выключено, ссылка отвечает «регистрация на месте не открыта»."
)

_QR_CAPTION = (
    "📎 QR регистрации на месте{city}\n\n{link}\n\n"
    "Распечатайте и поставьте на стойку или показывайте с телефона. Работает, только пока "
    "регистрация на месте включена."
)

_NO_USERNAME_ALERT = "Не получилось узнать имя бота — попробуйте ещё раз."
_PICK_CITY_ALERT = (
    "Регистрация на месте включается для каждого города отдельно — выберите город в "
    "«🎪 Форум: функции» и откройте этот экран оттуда."
)


async def _city_gate(callback: types.CallbackQuery, code: str | None) -> bool:
    """Право на город (D-26) + «без города нельзя» при включённом модуле городов: общий ключ
    включил бы регистрацию на месте во всех городах, а привязанный к городу менеджер обошёл бы
    свою привязку, собрав callback без города. False — ответ уже показан."""
    if not await _city_allowed(callback.from_user.id, code):
        await callback.answer(_CITY_FORBIDDEN_ALERT, show_alert=True)
        return False
    if code is None and await cities_module_on():
        if await get_staff_city(callback.from_user.id):
            await callback.answer(_CITY_FORBIDDEN_ALERT, show_alert=True)
        else:
            await callback.answer(_PICK_CITY_ALERT, show_alert=True)
        return False
    return True


def _onoff(enabled: bool) -> str:
    return "✅ Вкл" if enabled else "❌ Выкл"


async def _onsitereg_cfg_text_kb(code: str | None) -> tuple[str, InlineKeyboardMarkup]:
    enabled = await onsite_enabled(code)
    label = await city_label(code) if code else None
    lines = [
        "📝 <b>Регистрация на месте</b>" + (f" — {html.escape(label)}" if label else ""),
        f"Сейчас: {_onoff(enabled)}",
        "",
        _EXPLAIN,
    ]
    enc = _encode_city(code)
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(
            text=f"Регистрация на месте: {_onoff(enabled)}", callback_data=f"onsitereg_toggle:{enc}",
        )],
        [InlineKeyboardButton(text="📎 QR для стойки", callback_data=f"onsitereg_qr:{enc}")],
        [InlineKeyboardButton(text="◀️ Назад", callback_data="admin_forum_functions")],
    ])
    return "\n".join(lines), kb


@router.callback_query(F.data.startswith("onsitereg_cfg:"))
async def onsitereg_cfg_screen(callback: types.CallbackQuery):
    code = _decode_city(callback.data.split(":", 1)[1])
    if not await _city_allowed(callback.from_user.id, code):
        await callback.answer(_CITY_FORBIDDEN_ALERT, show_alert=True)
        return
    text, kb = await _onsitereg_cfg_text_kb(code)
    await callback.message.answer(text, parse_mode="HTML", reply_markup=kb)
    await callback.answer()


@router.callback_query(F.data.startswith("onsitereg_toggle:"))
async def onsitereg_toggle_go(callback: types.CallbackQuery):
    code = _decode_city(callback.data.split(":", 1)[1])
    if not await _city_gate(callback, code):
        return
    new_val = "off" if await onsite_enabled(code) else "on"
    if code and await cities_module_on():
        key = per_city_key(_KEY, code)
        if key is None:
            await callback.answer(_CITY_FORBIDDEN_ALERT, show_alert=True)
            return
        await set_setting_by_admin(callback.from_user.id, key, new_val)
    else:
        await set_setting_by_admin(callback.from_user.id, _KEY, new_val)
    text, kb = await _onsitereg_cfg_text_kb(code)
    await callback.message.edit_text(text, parse_mode="HTML", reply_markup=kb)
    await callback.answer(_onoff(new_val == "on"), show_alert=True)


@router.callback_query(F.data.startswith("onsitereg_qr:"))
async def onsitereg_qr_send(callback: types.CallbackQuery, bot: Bot):
    code = _decode_city(callback.data.split(":", 1)[1])
    if not await _city_gate(callback, code):
        return
    try:
        me = await bot.me()
        username = getattr(me, "username", None)
    except Exception:
        logger.warning("onsitereg_qr: не удалось получить имя бота", exc_info=True)
        username = None
    link = await walkin_link(username, code)
    if not link:
        await callback.answer(_NO_USERNAME_ALERT, show_alert=True)
        return
    label = await city_label(code) if code and await cities_module_on() else None
    caption = _QR_CAPTION.format(city=f" — {label}" if label else "", link=link)
    photo = BufferedInputFile(walkin_qr_png(link), filename="walkin_qr.png")
    await callback.message.answer_photo(photo, caption=caption, parse_mode=None)
    await callback.answer()
