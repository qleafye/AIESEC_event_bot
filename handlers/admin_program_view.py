"""D-29 (FORUM-CHECKIN.md, «Решения владельца 24.09»): циклический тумблер «Таблица/Фото»
(`program_miniapp_view`) — что делегат видит по кнопке «📅 Программа» в Mini App.

Отдельный шов, а не тело `handlers/admin_program.py` (тот на потолке размера,
`tests/test_module_size_convention_260816.py`, KNOWN_OVERAGES) и не тело
`handlers/admin_forum_functions.py` (кнопка нужна ОБОИМ экранам — общий рендер строки живёт
здесь один раз). Форма шва — эталон соседей (`admin_reject_reports.py`,
`admin_program.py` сам): своего `Router()` нет, `from handlers.admin import router`; импортирован
ИЗ ХВОСТА `handlers/admin_sections.py`, СРАЗУ ПОСЛЕ `admin_program` (golden snapshot:
`tests/test_refac_snapshot_260816.py`).

Право — `prog_*` (`handlers/admin_caps.py`, `settings`) — тот же префикс, что весь шов
`admin_program.py`, callback уже покрыт им, второй записи не заводим.

Кнопка — ЦИКЛ (table<->photo), не чекбокс, та же идиома, что
`handlers.admin_miniapp.cycle_miniapp_motion`. `back_to` в callback_data («program»/«hub») —
это и есть карта «куда вернуть после нажатия», второй не заводим (D-01/D-15 инвариант)."""
from aiogram import F, types
from aiogram.types import InlineKeyboardButton

from cities import cities_module_on, per_city_key
from handlers.admin import router
from services.program import PROGRAM_VIEW_KEY, resolve_program_view
from settings_audit import set_setting_by_admin
from settings_schema import SETTINGS_SCHEMA

_CYCLE = {"table": "photo", "photo": "table"}


async def program_view_row(code: str, back_to: str) -> tuple[str, InlineKeyboardButton]:
    """(строка статуса, кнопка цикла) для города `code` — общая для экрана «🗓 Программа
    форума» (`back_to="program"`) и хаба «🎪 Форум: функции» (`back_to="hub"`)."""
    current = await resolve_program_view(code)
    label = SETTINGS_SCHEMA[PROGRAM_VIEW_KEY]["option_labels"][current]
    status = f"🗓 Программа в приложении: {label}"
    button = InlineKeyboardButton(
        text=f"🔄 Переключить вид: {label}", callback_data=f"prog_view_toggle:{code}:{back_to}",
    )
    return status, button


@router.callback_query(F.data.startswith("prog_view_toggle:"))
async def prog_view_toggle_go(callback: types.CallbackQuery):
    _prefix, code, back_to = callback.data.split(":", 2)
    current = await resolve_program_view(code)
    new_val = _CYCLE.get(current, "table")
    if code and await cities_module_on():
        composed = per_city_key(PROGRAM_VIEW_KEY, code)
        await set_setting_by_admin(callback.from_user.id, composed, new_val)
    else:
        await set_setting_by_admin(callback.from_user.id, PROGRAM_VIEW_KEY, new_val)

    label = SETTINGS_SCHEMA[PROGRAM_VIEW_KEY]["option_labels"][new_val]
    await callback.answer(f"Программа в приложении: {label}")

    # Ленивый импорт — оба модуля сами импортируют этот шов транзитивно (через хвост
    # admin_sections.py), обратный импорт на уровне модуля замкнул бы цикл.
    if back_to == "hub":
        from handlers.admin_forum_functions import _render_hub
        text, kb = await _render_hub(callback.from_user.id, code)
    else:
        from handlers.admin_program import render_city_program_screen
        text, kb = await render_city_program_screen(callback.from_user.id, code)
    await callback.message.edit_text(text, parse_mode="HTML", reply_markup=kb)
