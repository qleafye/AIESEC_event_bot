"""Quick 260911-805 (W4-03): экран «🌙 Тихие часы» — тумблер и «с»/«до» в одном месте.

УАТ-находка ночи 10-11.09: раздел «📋 Заявки» показывал только тумблер («toggle_quiet_hours»)
— само поле времени лежало внутри обезличенной группы «⚙️ Тексты и настройки», недостижимой
из раздела прямым тапом. Этот экран собирает тумблер (та же кнопка `settings_toggle_rows`,
второй подписи не заводим — D-04) и три существующих редактора ключа (`settings_edit:...`)
на одном месте, плюс состояние: действующее окно, для какого города оно показано, что именно
откладывается и сколько уведомлений уже в очереди.

Форма шва — `handlers/admin_purge.py`: своего `Router()` нет, `from handlers.admin import
router`, декоратор — в одну строку (инвариант cap-теста `tests/test_roles_phase8.py`).
`handlers.admin_settings`/`handlers.admin_sections` импортируются ЛЕНИВО внутри функции —
на уровне модуля они замкнули бы цикл (admin_sections -> admin_settings -> хвост admin_sections
-> этот модуль, D-03)."""
import html as html_module

from aiogram import F, types
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from cities import ALL_CITIES, admin_selected_city, city_label, get_setting_typed_for_city
from handlers.admin import router
from services import quiet_hours
from settings_schema import get_setting_typed


def _fmt_raw(raw: str | None) -> str:
    """Для ТЕКСТА экрана (parse_mode HTML) — значение экранировано."""
    return html_module.escape(str(raw)) if raw else "не задано"


def _button_value(raw: str | None) -> str:
    """Для ПОДПИСИ кнопки — Telegram не рендерит HTML в кнопках, экранировать не нужно (и
    вредно: `&lt;` показался бы менеджеру буквально)."""
    return str(raw) if raw else "не задано"


async def render_quiet_hours_screen(admin_id: int) -> tuple[str, InlineKeyboardMarkup]:
    """Состояние собирается ЯВНО из тумблера + двух разобранных значений, а не через
    `quiet_hours.window_for_city` — та функция возвращает `None` и при выключенном тумблере,
    и при сломанных часах, а этому экрану нужно различить причины и назвать их менеджеру
    словами (T-805-05: один лишний COUNT + четыре чтения настроек на тап — цена, как у
    соседних экранов раздела)."""
    header_code = await admin_selected_city(admin_id)
    enabled = await get_setting_typed("quiet_hours_enabled") == "on"
    start_raw = await get_setting_typed_for_city("quiet_hours_start", header_code)
    end_raw = await get_setting_typed_for_city("quiet_hours_end", header_code)
    notice_raw = await get_setting_typed("quiet_hours_manager_notice_text") or ""
    pending = await quiet_hours.queued_count()

    start = quiet_hours.parse_hhmm(start_raw)
    end = quiet_hours.parse_hhmm(end_raw)

    lines = ["🌙 <b>Тихие часы</b>"]
    # Шапка города рисуется СТРОКОЙ текста, не кнопкой-переключателем — город переключается
    # в разделе, второй точки переключения не заводим.
    if header_code and header_code != ALL_CITIES:
        lines.append(f"Город: {html_module.escape(await city_label(header_code))}")

    if not enabled:
        lines.append(
            "🔇 Тишина выключена — уведомления уходят сразу, в любое время. "
            "Часы можно настроить заранее, до включения."
        )
    elif start is None or end is None:
        lines.append(
            f"⚠️ Не разобрал время: «{_fmt_raw(start_raw)}» / «{_fmt_raw(end_raw)}» — "
            "формат ЧЧ:ММ (например, 22:00). Задайте оба значения кнопками ниже."
        )
    elif start == end:
        lines.append(
            f"⚠️ «С» и «до» совпадают ({start.strftime('%H:%M')}) — окна тишины нет вовсе. "
            "Задайте разные значения кнопками ниже."
        )
    else:
        lines.append(f"✅ Окно: {start.strftime('%H:%M')}–{end.strftime('%H:%M')}")
        lines.append(
            "На это время откладываются решения по заявкам, монеты, напоминания об оплате "
            "и другие ночные уведомления делегатам — уведомления копятся и уходят сразу "
            "после конца окна."
        )

    lines.append("")
    lines.append(f"В очереди уведомлений: {pending}")
    lines.append("")
    lines.append(
        "🌙 Мгновенная рассылка тишину НЕ ждёт — отдельное предупреждение стоит на её "
        "экране подтверждения."
    )
    if notice_raw:
        lines.append("")
        lines.append(f"Текст делегату о переносе: {html_module.escape(notice_raw)}")

    text = "\n".join(lines)

    from handlers.admin_sections import back_button  # ленивый шов (D-03)
    from handlers.admin_settings import settings_toggle_rows  # ленивый шов (D-03)

    toggles = await settings_toggle_rows(admin_id, header_code=header_code)
    buttons: list[list[InlineKeyboardButton]] = [row for row in toggles.get("toggle_quiet_hours", [])]
    buttons.append([InlineKeyboardButton(
        text=f"🕓 С: {_button_value(start_raw)}", callback_data="settings_edit:quiet_hours_start",
    )])
    buttons.append([InlineKeyboardButton(
        text=f"🕓 До: {_button_value(end_raw)}", callback_data="settings_edit:quiet_hours_end",
    )])
    buttons.append([InlineKeyboardButton(
        text="✏️ Текст делегату о переносе",
        callback_data="settings_edit:quiet_hours_manager_notice_text",
    )])
    buttons.append([back_button("admin_quiet_hours")])

    return text, InlineKeyboardMarkup(inline_keyboard=buttons)


@router.callback_query(F.data == "admin_quiet_hours")
async def admin_quiet_hours(callback: types.CallbackQuery):
    text, kb = await render_quiet_hours_screen(callback.from_user.id)
    await callback.message.edit_text(text, parse_mode="HTML", reply_markup=kb)
    await callback.answer()
