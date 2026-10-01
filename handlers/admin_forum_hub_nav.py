"""Хаб «🎪 Форум: функции»: подтверждение общего тумблера «🎟 Вход по QR».

Раньше кнопка хаба сама щёлкала `toggle_checkin_qr_enabled` — один тап молча выключал вход по
QR во ВСЕХ городах и выкидывал в раздел «📋 Заявки». Теперь кнопка открывает экран с текущим
состоянием и словами, что именно произойдёт (тумблер общий — экран прямо перечисляет города и
сколько людей уже получили QR), а после подтверждения возвращает в хаб того же города.

Форма шва — как у соседей (`handlers/admin_forum_ready.py`): своего `Router()` нет,
`from handlers.admin import router`, импорт из хвоста `handlers/admin.py`. Право — `settings`,
то же, что у самого тумблера в «📋 Заявки» (`ADMIN_CAPS["toggle_checkin_qr_enabled"]`)."""
import html
import logging

from aiogram import F, types
from aiogram.exceptions import TelegramBadRequest
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from cities import cities_module_on, city_label, enabled_cities
from database.db import checkin_qr_send_counts
from handlers.admin import router
from handlers.admin_checkin import _CITY_FORBIDDEN_ALERT, _city_allowed, _decode_city, _encode_city
from settings_audit import set_setting_by_admin
from settings_schema import get_setting_typed

logger = logging.getLogger(__name__)


async def _edit(callback: types.CallbackQuery, text: str, kb: InlineKeyboardMarkup) -> None:
    try:
        await callback.message.edit_text(text, parse_mode="HTML", reply_markup=kb)
    except TelegramBadRequest as e:
        if "not modified" not in str(e):
            raise


async def _hub(admin_id: int, code: str | None) -> tuple[str, InlineKeyboardMarkup]:
    from handlers.admin_forum_functions import _render_hub, _resolve_screen_city
    if code is None:
        code = await _resolve_screen_city(admin_id)
    if code is None:
        from handlers.admin_forum_functions import _render_city_picker
        return await _render_city_picker()
    return await _render_hub(admin_id, code)


async def _qr_confirm_screen(code: str | None) -> tuple[str, InlineKeyboardMarkup]:
    on = await get_setting_typed("checkin_qr_enabled") == "on"
    got, _confirmed = await checkin_qr_send_counts()
    if await cities_module_on():
        labels = [await city_label(c["code"]) for c in await enabled_cities()]
        where = "во ВСЕХ городах сразу: " + ", ".join(html.escape(x) for x in labels)
    else:
        where = "для всего мероприятия"
    enc = _encode_city(code)
    lines = ["🎟 <b>Вход по QR</b>", "", f"Сейчас: {'✅ включён' if on else '❌ выключен'}.", ""]
    lines.append(f"Это один общий переключатель — он действует {where}. "
                 "Отдельно для одного города его не выключить.")
    lines.append("")
    if on:
        lines += [
            "Если выключить:",
            "• у делегатов пропадёт кнопка «🎟 Мой QR»;",
            "• рассылка QR накануне и утром в день форума не уйдёт ни в одном городе;",
            "• шпаргалка волонтёрам накануне форума тоже не уйдёт.",
        ]
        if got:
            lines.append(f"QR уже получили {got} чел. — у них картинки останутся, отметка на входе "
                         "по ним продолжит работать, но заново QR из бота они не откроют.")
        lines += ["", "Выключить вход по QR?"]
        go = InlineKeyboardButton(text="❌ Да, выключить во всех городах", callback_data=f"forumfn_qr_set:off:{enc}")
    else:
        lines += [
            "Если включить:",
            "• у одобренных делегатов появится кнопка «🎟 Мой QR»;",
            "• рассылка QR накануне форума встанет по расписанию там, где она включена и задана дата форума.",
            "", "Включить вход по QR?",
        ]
        go = InlineKeyboardButton(text="✅ Да, включить во всех городах", callback_data=f"forumfn_qr_set:on:{enc}")
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [go],
        [InlineKeyboardButton(text="◀️ Не менять, назад", callback_data=f"forumfn_back:{enc}")],
    ])
    return "\n".join(lines), kb


@router.callback_query(F.data.startswith("forumfn_qr:"))
async def forumfn_qr_screen(callback: types.CallbackQuery):
    code = _decode_city(callback.data.split(":", 1)[1])
    if not await _city_allowed(callback.from_user.id, code):
        await callback.answer(_CITY_FORBIDDEN_ALERT, show_alert=True)
        return
    text, kb = await _qr_confirm_screen(code)
    await _edit(callback, text, kb)
    await callback.answer()


@router.callback_query(F.data.startswith("forumfn_qr_set:"))
async def forumfn_qr_set(callback: types.CallbackQuery):
    _, value, raw = callback.data.split(":", 2)
    code = _decode_city(raw)
    if value not in ("on", "off") or not await _city_allowed(callback.from_user.id, code):
        await callback.answer(_CITY_FORBIDDEN_ALERT, show_alert=True)
        return
    # Значение — то, что человек увидел на экране подтверждения, а не «переключить»: двойной тап
    # или старая кнопка не щёлкнут тумблер обратно.
    if await get_setting_typed("checkin_qr_enabled") != value:
        await set_setting_by_admin(callback.from_user.id, "checkin_qr_enabled", value)
        try:  # тот же пересчёт джоб, что у тумблера в «📋 Заявки»; сбой — только в лог
            from services.checkin_broadcast import reconcile_forum_jobs
            await reconcile_forum_jobs()
        except Exception:
            logger.exception("forumfn_qr_set: перепланирование рассылок форума упало")
    text, kb = await _hub(callback.from_user.id, code)
    await _edit(callback, text, kb)
    await callback.answer("🎟 Вход по QR: " + ("✅ включён" if value == "on" else "❌ выключен во всех городах"),
                          show_alert=True)


@router.callback_query(F.data.startswith("forumfn_back:"))
async def forumfn_back(callback: types.CallbackQuery):
    """Возврат в хаб того же города правкой текущего сообщения (кнопка «◀️ К „Форум: функции“»
    на экранах, открытых из хаба)."""
    code = _decode_city(callback.data.split(":", 1)[1])
    if not await _city_allowed(callback.from_user.id, code):
        await callback.answer(_CITY_FORBIDDEN_ALERT, show_alert=True)
        return
    text, kb = await _hub(callback.from_user.id, code)
    await _edit(callback, text, kb)
    await callback.answer()
