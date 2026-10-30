"""Экран «⏰ Напоминания о сессиях» (из «⚙️ Настройки записи»): тумблер «за N минут до сессии»,
минуты кнопками, текст напоминания, тумблер «утром — программа на день», время и шапка утреннего
сообщения, «📨 Прислать себе пример» и строка-статус «сколько запланировано».

Всё кнопками (правило проекта «бот для людей»): коды настроек менеджер не видит, текстом вводятся
только произвольные значения — текст напоминания, шапка и время ЧЧ:ММ (его проверяет общая
проверка настроек `format: time` и объясняет ошибку). Сохранение идёт через `set_setting_by_admin`,
чей хук (`services/settings/reschedule.py`) сам переставляет джобы — отдельный вызов не нужен.

Форма шва — как у `admin_enroll_list.py`: общий `handlers.admin.router`, импорт хвостом
`handlers/admin.py`, право `settings` по префиксу `prog_*`."""
import html as html_module

from aiogram import F, types
from aiogram.fsm.context import FSMContext
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from domain.cities import cities_module_on, city_label, get_setting_typed_for_city, header_snapshot, per_city_key
from domain.settings.placeholders import hint
from domain.settings.schema import SETTINGS_SCHEMA
from handlers.admin import router
from handlers.forum.admin_program import _CITY_FORBIDDEN_ALERT, _city_allowed
from handlers.states import EditSetting
from services.forum import session_remind as sr
from services.settings.audit import set_setting_by_admin

_CANCEL_ROW = [InlineKeyboardButton(text="❌ Отмена", callback_data="settings_cancel")]
_BACK_ROW = "prog_enrset:{code}"


async def _deny(callback: types.CallbackQuery, code: str | None) -> bool:
    if await _city_allowed(callback.from_user.id, code):
        return False
    await callback.answer(_CITY_FORBIDDEN_ALERT, show_alert=True)
    return True


async def _write_key(base_key: str, code: str) -> str:
    """При выключенном модуле городов — голый ключ (составной не подействовал бы)."""
    return per_city_key(base_key, code) if await cities_module_on() else base_key


def _onoff(flag: bool) -> str:
    return "✅ Вкл" if flag else "❌ Выкл"


def _plural_reminders(n: int) -> str:
    n10, n100 = n % 10, n % 100
    if n10 == 1 and n100 != 11:
        return "напоминание"
    if 2 <= n10 <= 4 and not 12 <= n100 <= 14:
        return "напоминания"
    return "напоминаний"


def _day_label(day: str) -> str:
    return f"{day[8:10]}.{day[5:7]}"


async def _status_line(code: str) -> str:
    plan = await sr.planned_summary(code)
    if plan["day"] is None:
        return "Запланировано: пока нет — в программе нет предстоящих сессий."
    parts = [f"{plan['reminders']} {_plural_reminders(plan['reminders'])} из {plan['sessions']} сессий"]
    parts.append("утренняя программа " + ("поставлена" if plan["morning"] else "не запланирована"))
    return f"Запланировано на {_day_label(plan['day'])}: " + ", ".join(parts) + "."


async def render_remind_screen(code: str) -> tuple[str, InlineKeyboardMarkup]:
    remind_on = await sr.remind_enabled(code)
    minutes = await sr.remind_minutes(code)
    morning_on = await sr.morning_enabled(code)
    morning_at = await sr.morning_time(code)
    lines = [
        f"⏰ <b>Напоминания о сессиях</b> — {html_module.escape(await city_label(code))}", "",
        "Напоминание за N минут приходит личным сообщением всем одобренным делегатам города. "
        "Записываться на сессию для этого не нужно. "
        "Утренняя программа — тем, у кого в этот день есть своё расписание.", "",
        f"Напоминать за {minutes} мин до сессии: {_onoff(remind_on)}",
        f"Утром — программа на день: {_onoff(morning_on)}, в {morning_at} по времени города",
        "", await _status_line(code),
    ]
    warning = await sr.morning_time_warning(code)
    if warning:
        lines += ["", html_module.escape(warning)]
    minute_row = [
        InlineKeyboardButton(text=("✅ " if m == minutes else "") + f"{m} мин", callback_data=f"prog_srmmin:{code}:{m}")
        for m in sr.MINUTES_CHOICES
    ]
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(
            text=f"Напоминать за {minutes} мин: " + ("✅ → ❌" if remind_on else "❌ → ✅"),
            callback_data=f"prog_srmon:{code}",
        )],
        minute_row,
        [InlineKeyboardButton(text="✏️ Текст напоминания", callback_data=f"prog_srmtx:{code}:remind")],
        [InlineKeyboardButton(
            text="Утром — программа на день: " + ("✅ → ❌" if morning_on else "❌ → ✅"),
            callback_data=f"prog_srmmo:{code}",
        )],
        [InlineKeyboardButton(text=f"🕗 Время утренней программы: {morning_at}", callback_data=f"prog_srmtm:{code}")],
        [InlineKeyboardButton(text="✏️ Шапка программы", callback_data=f"prog_srmtx:{code}:head")],
        [InlineKeyboardButton(text="📨 Прислать себе пример", callback_data=f"prog_srmex:{code}")],
        [InlineKeyboardButton(text="← Настройки записи", callback_data=_BACK_ROW.format(code=code))],
    ])
    return "\n".join(lines), kb


async def _show(callback: types.CallbackQuery, code: str, note: str | None = None) -> None:
    from handlers.forum.admin_forum_hub_nav import keep_hub_back  # открыт из хаба форума — «Назад» в хаб

    text, kb = await render_remind_screen(code)
    await callback.message.edit_text(text, parse_mode="HTML", reply_markup=keep_hub_back(callback.message, kb))
    await callback.answer(note)


@router.callback_query(F.data.startswith("prog_srm:"))
async def prog_srm(callback: types.CallbackQuery):
    code = callback.data.split(":", 1)[1]
    if await _deny(callback, code):
        return
    await _show(callback, code)


@router.callback_query(F.data.startswith("prog_srmon:"))
async def prog_srmon(callback: types.CallbackQuery):
    code = callback.data.split(":", 1)[1]
    if await _deny(callback, code):
        return
    new_val = "off" if await sr.remind_enabled(code) else "on"
    await set_setting_by_admin(callback.from_user.id, await _write_key("session_remind_enabled", code), new_val)
    await _show(callback, code, "Сохранено.")


@router.callback_query(F.data.startswith("prog_srmmo:"))
async def prog_srmmo(callback: types.CallbackQuery):
    code = callback.data.split(":", 1)[1]
    if await _deny(callback, code):
        return
    new_val = "off" if await sr.morning_enabled(code) else "on"
    await set_setting_by_admin(callback.from_user.id, await _write_key("session_morning_enabled", code), new_val)
    await _show(callback, code, "Сохранено.")


@router.callback_query(F.data.startswith("prog_srmmin:"))
async def prog_srmmin(callback: types.CallbackQuery):
    _, code, value_s = callback.data.split(":", 2)
    if await _deny(callback, code):
        return
    try:
        value = int(value_s)
    except ValueError:
        value = 0
    if value not in sr.MINUTES_CHOICES:
        await callback.answer("Выберите одну из кнопок: 5, 10, 15 или 30 минут.", show_alert=True)
        return
    await set_setting_by_admin(callback.from_user.id, await _write_key("session_remind_minutes", code), str(value))
    await _show(callback, code, "Сохранено.")


_TEXT_FIELDS = {
    "remind": "session_remind_text",
    "head": "session_morning_head",
}


@router.callback_query(F.data.startswith("prog_srmtx:"))
async def prog_srmtx(callback: types.CallbackQuery, state: FSMContext):
    _, code, field = callback.data.split(":", 2)
    if await _deny(callback, code):
        return
    base = _TEXT_FIELDS.get(field)
    if base is None:
        await callback.answer("Этого текста нет — обновите экран.", show_alert=True)
        return
    entry = SETTINGS_SCHEMA[base]
    current = await get_setting_typed_for_city(base, code)
    prompt = entry["prompt"]
    extra = hint(base)
    lines = [f"✏️ <b>{html_module.escape(entry['label'])}</b>", "",
             f"Сейчас:\n<b>{html_module.escape(str(current))}</b>" if current else "Сейчас: стандартный текст", "",
             html_module.escape(prompt)]
    if extra and "Подстановки" not in prompt:
        lines += ["", html_module.escape(extra)]
    lines += ["", "<i>«-» — вернуть стандартный текст.</i>"]
    await state.set_state(EditSetting.waiting_for_value)
    await state.set_data({"setting_key": await _write_key(base, code),
                          "return_cb": f"prog_srm:{code}", "return_label": "← К напоминаниям",
                          **await header_snapshot(callback.from_user.id)})  # город из кнопки, не из шапки
    await callback.message.edit_text(
        "\n".join(lines), parse_mode="HTML", reply_markup=InlineKeyboardMarkup(inline_keyboard=[_CANCEL_ROW]),
    )
    await callback.answer()


@router.callback_query(F.data.startswith("prog_srmtm:"))
async def prog_srmtm(callback: types.CallbackQuery, state: FSMContext):
    code = callback.data.split(":", 1)[1]
    if await _deny(callback, code):
        return
    await state.set_state(EditSetting.waiting_for_value)
    await state.set_data({"setting_key": await _write_key("session_morning_time", code),
                          "return_cb": f"prog_srm:{code}", "return_label": "← К напоминаниям",
                          **await header_snapshot(callback.from_user.id)})
    await callback.message.edit_text(
        "🕗 <b>Время утренней программы</b>\n\n" + SETTINGS_SCHEMA["session_morning_time"]["prompt"]
        + "\n\n<i>«-» — вернуть 08:30.</i>",
        parse_mode="HTML", reply_markup=InlineKeyboardMarkup(inline_keyboard=[_CANCEL_ROW]),
    )
    await callback.answer()


@router.callback_query(F.data.startswith("prog_srmex:"))
async def prog_srmex(callback: types.CallbackQuery):
    code = callback.data.split(":", 1)[1]
    if await _deny(callback, code):
        return
    examples = await sr.build_examples(code, callback.from_user.id)
    if examples is None:
        await callback.answer(
            "В программе этого города пока нет сессий — добавьте хотя бы одну, и пример соберётся из неё.",
            show_alert=True,
        )
        return
    remind, morning = examples
    await callback.message.answer("📨 Так делегат увидит напоминание перед сессией:")
    await callback.message.answer(remind)
    await callback.message.answer("📨 А так — утреннюю программу (если у вас нет своих записей, показаны все сессии дня):")
    await callback.message.answer(morning)
    await callback.answer("Прислал.")
