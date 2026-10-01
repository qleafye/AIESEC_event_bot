"""Хаб «🎪 Форум: функции»: подтверждение общего тумблера «🎟 Вход по QR» и возврат в хаб.

Раньше кнопка хаба сама щёлкала `toggle_checkin_qr_enabled` — один тап молча выключал вход по
QR во ВСЕХ городах и выкидывал в раздел «📋 Заявки». Теперь кнопка открывает экран с текущим
состоянием и словами, что именно произойдёт (тумблер общий — экран прямо перечисляет города и
сколько людей уже получили QR), а после подтверждения возвращает в хаб того же города.

Экраны «✅ Отметки на форуме», «📱 Настройки приложения», «🔘 Кнопки меню», «⭐ Отзывы» живут в
своих разделах, и их «Назад» ведёт туда. Открытые из хаба (`forumfn_open:<экран>:<город>`), они
получают вместо него «◀️ К «Форум: функции»» — кнопку хаба того же города. Признак «пришли из
хаба» хранится в самой клавиатуре сообщения: перерисовка экрана после тумблера переносит кнопку
(`keep_hub_back`), поэтому контекст переживает и тапы, и перезапуск бота.

Форма шва — как у соседей (`handlers/admin_forum_ready.py`): своего `Router()` нет,
`from handlers.admin import router`, импорт из хвоста `handlers/admin.py`. Право — `settings`,
то же, что у самого тумблера в «📋 Заявки» (`ADMIN_CAPS["toggle_checkin_qr_enabled"]`)."""
import html
import logging

from aiogram import F, types
from aiogram.exceptions import TelegramBadRequest
from aiogram.fsm.context import FSMContext
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from cities import cities_module_on, city_label, enabled_cities
from database.db import checkin_qr_send_counts
from handlers.admin import router
from handlers.admin_checkin import _CITY_FORBIDDEN_ALERT, _city_allowed, _decode_city, _encode_city
from settings_audit import set_setting_by_admin
from settings_schema import get_setting_typed

logger = logging.getLogger(__name__)

HUB_BACK_PREFIX = "forumfn_back:"
QR_ALL_CITIES_ALERT = ("«🎟 Вход по QR» — общий переключатель для всех городов сразу, его меняет "
                       "главный менеджер. Если нужно включить или выключить — напишите ему.")


async def sees_all_cities(admin_id: int) -> bool:
    """Общий тумблер «🎟 Вход по QR» действует во всех городах: менять его из хаба может только
    тот, кто видит все города (суперадмин или менеджер без привязки к городу). Менеджер одного
    города с правом «Настройки» иначе выключил бы QR и чужим городам."""
    if not await cities_module_on():
        return True
    import settings_ops
    from cities import city_codes
    return set(city_codes()) <= set(await settings_ops.per_city_visible_codes(admin_id))
HUB_BACK_TEXT = "◀️ К «Форум: функции»"
_OPEN_PREFIX = "forumfn_open:"


def _hub_back_cb(markup: InlineKeyboardMarkup | None) -> str | None:
    """Кнопка возврата в хаб на экране. Вложенный экран (подтверждение «↩️ Все как везде»,
    «🎭 Оформление») несёт вместо неё возврат на родной экран «из хаба»
    (`forumfn_open:<экран>:<город>`) — по нему город хаба тоже известен."""
    for row in getattr(markup, "inline_keyboard", None) or []:
        for b in row:
            cb = b.callback_data or ""
            if cb.startswith(HUB_BACK_PREFIX):
                return cb
            if cb.startswith(_OPEN_PREFIX) and cb.count(":") >= 2:
                return HUB_BACK_PREFIX + cb.split(":", 2)[2]
    return None


def hub_return(message, kb: InlineKeyboardMarkup, native_cb: str, target: str) -> InlineKeyboardMarkup:
    """Вложенный экран родного экрана, открытого из хаба: его кнопку «назад на родной экран»
    (`native_cb`) ведём на тот же экран «из хаба» (`forumfn_open:<target>:<город>`), иначе
    после неё «Назад» снова вёл бы в раздел, а не в хаб."""
    back = _hub_back_cb(getattr(message, "reply_markup", None))
    if back is None:
        return kb
    new_cb = f"{_OPEN_PREFIX}{target}:{back[len(HUB_BACK_PREFIX):]}"
    rows = [[b.model_copy(update={"callback_data": new_cb}) if b.callback_data == native_cb else b
             for b in row] for row in kb.inline_keyboard]
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _swap_back(kb: InlineKeyboardMarkup, back_cb: str) -> InlineKeyboardMarkup:
    """Последняя строка родного экрана — его «Назад»; заменяем её возвратом в хаб."""
    rows = list(kb.inline_keyboard)[:-1]
    rows.append([InlineKeyboardButton(text=HUB_BACK_TEXT, callback_data=back_cb)])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def keep_hub_back(message, new_kb: InlineKeyboardMarkup) -> InlineKeyboardMarkup:
    """Перерисовка родного экрана: если он был открыт из хаба, «Назад» по-прежнему ведёт в хаб."""
    cb = _hub_back_cb(getattr(message, "reply_markup", None))
    return _swap_back(new_kb, cb) if cb else new_kb


async def edit_or_answer(callback: types.CallbackQuery, text: str, kb: InlineKeyboardMarkup) -> None:
    """Экран хаба — правкой того сообщения, где нажата кнопка (а не новым сообщением, иначе в
    чате копятся «Выберите город.»); сообщение без текста (фото) или слишком старое — новым."""
    try:
        await callback.message.edit_text(text, parse_mode="HTML", reply_markup=kb)
    except TelegramBadRequest as e:
        if "not modified" not in str(e):
            await callback.message.answer(text, parse_mode="HTML", reply_markup=kb)


_edit = edit_or_answer


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
        [InlineKeyboardButton(text="◀️ Не менять, назад", callback_data=f"{HUB_BACK_PREFIX}{enc}")],
    ])
    return "\n".join(lines), kb


@router.callback_query(F.data.startswith("forumfn_qr:"))
async def forumfn_qr_screen(callback: types.CallbackQuery):
    code = _decode_city(callback.data.split(":", 1)[1])
    if not await _city_allowed(callback.from_user.id, code):
        await callback.answer(_CITY_FORBIDDEN_ALERT, show_alert=True)
        return
    if not await sees_all_cities(callback.from_user.id):
        await callback.answer(QR_ALL_CITIES_ALERT, show_alert=True)
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
    if not await sees_all_cities(callback.from_user.id):
        await callback.answer(QR_ALL_CITIES_ALERT, show_alert=True)
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


async def _native_screen(target: str, admin_id: int, code: str | None):
    if target == "chk":
        from handlers.admin_checkin import render_admin_checkin
        return await render_admin_checkin(admin_id, code)  # экран города хаба, не шапки
    if target == "app":
        from handlers.admin_miniapp import build_miniapp_settings_keyboard, render_miniapp_settings_text
        return await render_miniapp_settings_text(), await build_miniapp_settings_keyboard()
    if target == "menu":
        # Экран кнопок меню читает город из шапки — ставим шапку на город хаба (как
        # `asos_city`), иначе хаб Тюмени при шапке «Все города» правил бы общие кнопки.
        if code and await cities_module_on():
            from cities import set_admin_city
            await set_admin_city(admin_id, code)
        from handlers.admin_reg_config import build_menu_keyboard, render_menu_text
        return await render_menu_text(admin_id), await build_menu_keyboard(admin_id)
    if target == "fb" and code:
        from handlers.session_feedback import render_feedback_settings_screen
        return await render_feedback_settings_screen(code)
    return None


@router.callback_query(F.data.startswith("forumfn_open:"))
async def forumfn_open(callback: types.CallbackQuery, state: FSMContext):
    """«forumfn_open:<chk|app|menu|fb>:<город>» — родной экран функции с возвратом в хаб."""
    _, target, raw = callback.data.split(":", 2)
    code = _decode_city(raw)
    if not await _city_allowed(callback.from_user.id, code):
        await callback.answer(_CITY_FORBIDDEN_ALERT, show_alert=True)
        return
    if target == "app":  # тот же сброс, что у родного входа: зависшая правка оформления не нужна
        await state.clear()
    screen = await _native_screen(target, callback.from_user.id, code)
    if screen is None:
        await callback.answer("Эта кнопка устарела — откройте «🎪 Форум: функции» заново.", show_alert=True)
        return
    text, kb = screen
    await _edit(callback, text, _swap_back(kb, f"{HUB_BACK_PREFIX}{_encode_city(code)}"))
    await callback.answer()
