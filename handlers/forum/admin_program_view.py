"""D-29 (FORUM-CHECKIN.md, «Решения владельца 24.09»): циклический тумблер «Таблица/Фото»
(`program_miniapp_view`) — что делегат видит по кнопке «📅 Программа» в Mini App.

Отдельный шов, а не тело `handlers/forum/admin_program.py` (тот на потолке размера,
`tests/test_module_size_convention_260816.py`, KNOWN_OVERAGES) и не тело
`handlers/forum/admin_forum_functions.py` (кнопка нужна ОБОИМ экранам — общий рендер строки живёт
здесь один раз). Форма шва — эталон соседей (`admin_reject_reports.py`,
`admin_program.py` сам): своего `Router()` нет, `from handlers.admin import router`; импортирован
ИЗ ХВОСТА `handlers/settings/admin_sections.py`, СРАЗУ ПОСЛЕ `admin_program` (golden snapshot:
`tests/test_refac_snapshot_260816.py`).

Право — `prog_*` (`handlers/access/admin_caps.py`, `settings`) — тот же префикс, что весь шов
`admin_program.py`, callback уже покрыт им, второй записи не заводим.

Кнопка — ЦИКЛ (table<->photo), не чекбокс, та же идиома, что
`handlers.settings.admin_miniapp.cycle_miniapp_motion`. `back_to` в callback_data («program»/«hub») —
это и есть карта «куда вернуть после нажатия», второй не заводим (D-01/D-15 инвариант)."""
import html

from aiogram import F, types
from aiogram.fsm.context import FSMContext
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from domain.cities import cities_module_on, city_label, per_city_key
from handlers.admin import router
from handlers.states import ProgramPhotoUpload
from services.forum.program import (
    PROGRAM_PHOTO_KEY, PROGRAM_VIEW_KEY, own_program_photo, resolve_program_content,
    resolve_program_view,
)
from database.db import get_setting
from services.settings.audit import delete_setting_by_admin, set_setting_by_admin
from domain.settings.schema import SETTINGS_SCHEMA

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
        from handlers.forum.admin_forum_functions import _render_hub
        text, kb = await _render_hub(callback.from_user.id, code)
    else:
        from handlers.forum.admin_program import render_city_program_screen
        text, kb = await render_city_program_screen(callback.from_user.id, code)
    await callback.message.edit_text(text, parse_mode="HTML", reply_markup=kb)


async def _back_screen(admin_id: int, code: str, back_to: str):
    # Ленивый импорт — тот же цикл-разрыв, что у prog_view_toggle_go выше.
    if back_to == "hub":
        from handlers.forum.admin_forum_functions import _render_hub
        return await _render_hub(admin_id, code)
    from handlers.forum.admin_program import render_city_program_screen
    return await render_city_program_screen(admin_id, code)


# ── Фото программы ДЛЯ ГОРОДА ───────────────────────────────────────────────────────────────
#
# Раньше фото программы из бота было одно на все города: загрузила его СПб — увидели Тюмень и
# Москва, и оно перекрывало их программу сессиями. Теперь фото пишется в per_city составной
# ключ города экрана (`services.forum.program.own_program_photo`), а общее показывается только городу
# без своего фото и без сессий (`resolve_program_photo_source`). Модуль городов выключен —
# городов нет, пишется общее фото. Подпись — своя у города (`program_caption` составным ключом).

async def program_rows(code: str, back_to: str) -> tuple[str, list[list[InlineKeyboardButton]]]:
    """Строки статуса и кнопки «вид программы» + «фото программы» для экрана города — общие
    для «🗓 Программа форума» и хаба «🎪 Форум: функции»."""
    view_status, view_button = await program_view_row(code, back_to)
    per_city = await cities_module_on()
    where = f" — {html.escape(await city_label(code))}" if per_city else ""
    own = await own_program_photo(code)
    shown, _src = await resolve_program_content(code)
    photo_status = f"🖼 Фото программы{where}: " + ("✅ загружено" if own else "не загружено")
    if not own and shown == "photo":
        photo_status += " (делегаты видят общее фото)"
    elif not own and per_city and await get_setting(PROGRAM_PHOTO_KEY):
        # Общее фото (загружено до того, как фото стало своим у города) городу с сессиями не
        # показывается — без этой строки менеджер считал бы, что фото на месте.
        photo_status += (" — общее фото делегатам этого города не показывается, у него есть "
                         "сессии. Нужна картинка — загрузите фото для города")
    photo_button = InlineKeyboardButton(
        text=f"📷 {'Заменить' if own else 'Загрузить'} фото программы{where}",
        callback_data=f"prog_photo:{code}:{back_to}",
    )
    rows = [[view_button], [photo_button]]
    if own:
        rows.append([InlineKeyboardButton(
            text=f"🗑 Убрать фото программы{where}", callback_data=f"prog_photo_del:{code}:{back_to}",
        )])
    return f"{view_status}\n{photo_status}", rows


@router.callback_query(F.data.startswith("prog_photo:"))
async def prog_photo_start(callback: types.CallbackQuery, state: FSMContext):
    _prefix, code, back_to = callback.data.split(":", 2)
    await start_program_photo(callback, state, code, back_to)


async def start_program_photo(callback: types.CallbackQuery, state: FSMContext, code: str, back_to: str):
    """Вход в загрузку — и с экранов программы, и с «📷 📅 Программа» раздела «🎪 Событие»
    (`handlers/settings/admin_settings.py::settings_photo_start`, когда в шапке выбран город)."""
    from handlers.forum.admin_program import _city_allowed

    per_city = await cities_module_on()
    if per_city and (per_city_key(PROGRAM_PHOTO_KEY, code) is None
                     or not await _city_allowed(callback.from_user.id, code)):
        await callback.answer("Этот город вам недоступен — откройте программу своего города.", show_alert=True)
        return
    who = f"делегаты города «{html.escape(await city_label(code))}»" if per_city else "делегаты"
    text = (
        f"📷 <b>Фото программы</b>\n\nОтправьте фото (можно с подписью) — его увидят только "
        f"{who}. Другие города продолжат видеть свою программу."
        if per_city else
        "📷 <b>Фото программы</b>\n\nОтправьте фото (можно с подписью) — его увидят делегаты по "
        "кнопке «📅 Программа форума»."
    )
    await state.set_state(ProgramPhotoUpload.waiting)
    await state.set_data({"code": code, "back_to": back_to})
    kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="❌ Отмена", callback_data=f"prog_photo_cancel:{code}:{back_to}"),
    ]])
    await callback.message.edit_text(text, parse_mode="HTML", reply_markup=kb)
    await callback.answer()


@router.callback_query(F.data.startswith("prog_photo_cancel:"))
async def prog_photo_cancel(callback: types.CallbackQuery, state: FSMContext):
    _prefix, code, back_to = callback.data.split(":", 2)
    await state.clear()
    text, kb = await _back_screen(callback.from_user.id, code, back_to)
    await callback.message.edit_text(text, parse_mode="HTML", reply_markup=kb)
    await callback.answer("Отменено")


def _photo_keys(code: str | None, per_city: bool) -> tuple[str | None, str | None]:
    if per_city and code:
        return per_city_key(PROGRAM_PHOTO_KEY, code), per_city_key("program_caption", code)
    return PROGRAM_PHOTO_KEY, "program_caption"


@router.callback_query(F.data.startswith("prog_photo_del:"))
async def prog_photo_del_ask(callback: types.CallbackQuery):
    """«🗑 Убрать фото программы» — подтверждение с тем, что увидят делегаты после."""
    _prefix, code, back_to = callback.data.split(":", 2)
    per_city = await cities_module_on()
    where = f" города «{html.escape(await city_label(code))}»" if per_city else ""
    text = (
        f"🗑 Убрать фото программы{where}?\n\nКартинка и её подпись удалятся. Делегаты увидят "
        "таблицу сессий, если она заведена"
        + (", иначе — общее фото программы, если оно есть." if per_city else ".")
    )
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🗑 Да, убрать фото", callback_data=f"prog_photo_delgo:{code}:{back_to}")],
        [InlineKeyboardButton(text="← Отмена", callback_data=f"prog_photo_cancel:{code}:{back_to}")],
    ])
    await callback.message.edit_text(text, parse_mode="HTML", reply_markup=kb)
    await callback.answer()


@router.callback_query(F.data.startswith("prog_photo_delgo:"))
async def prog_photo_del_go(callback: types.CallbackQuery):
    from handlers.forum.admin_program import _city_allowed

    _prefix, code, back_to = callback.data.split(":", 2)
    per_city = await cities_module_on()
    photo_key, caption_key = _photo_keys(code, per_city)
    if photo_key is None or caption_key is None or (
            per_city and not await _city_allowed(callback.from_user.id, code)):
        await callback.answer("Этот город вам недоступен.", show_alert=True)
        return
    await delete_setting_by_admin(callback.from_user.id, photo_key)
    await delete_setting_by_admin(callback.from_user.id, caption_key)
    await callback.answer("Фото программы убрано.")
    text, kb = await _back_screen(callback.from_user.id, code, back_to)
    await callback.message.edit_text(text, parse_mode="HTML", reply_markup=kb)


@router.message(ProgramPhotoUpload.waiting, F.photo)
async def prog_photo_receive(message: types.Message, state: FSMContext):
    from handlers.forum.admin_program import _city_allowed

    data = await state.get_data()
    code, back_to = data.get("code"), data.get("back_to", "program")
    per_city = await cities_module_on()
    photo_key = per_city_key(PROGRAM_PHOTO_KEY, code) if per_city and code else PROGRAM_PHOTO_KEY
    caption_key = per_city_key("program_caption", code) if per_city and code else "program_caption"
    if photo_key is None or caption_key is None or not await _city_allowed(message.from_user.id, code):
        await state.clear()
        await message.answer("Этот город вам недоступен — откройте программу своего города заново.")
        return
    await set_setting_by_admin(message.from_user.id, photo_key, message.photo[-1].file_id)
    if message.caption:
        await set_setting_by_admin(message.from_user.id, caption_key, message.html_text)
    else:
        await delete_setting_by_admin(message.from_user.id, caption_key)
    await state.clear()

    where = f" для города «{html.escape(await city_label(code))}»" if per_city else ""
    reply = f"✅ Фото программы{where} сохранено."
    if (await resolve_program_content(code))[0] == "table":
        reply += (
            "\n\nСейчас у делегатов вид «🗓 Таблица сессий» — фото они увидят, когда вы "
            "переключите вид кнопкой «🔄 Переключить вид» ниже."
        )
    await message.answer(reply, parse_mode="HTML")
    text, kb = await _back_screen(message.from_user.id, code, back_to)
    await message.answer(text, parse_mode="HTML", reply_markup=kb)


@router.message(ProgramPhotoUpload.waiting)
async def prog_photo_not_photo(message: types.Message, state: FSMContext):
    if (message.text or "").strip() in ("Отмена", "/cancel"):
        data = await state.get_data()
        await state.clear()
        text, kb = await _back_screen(message.from_user.id, data.get("code"), data.get("back_to", "program"))
        await message.answer(text, parse_mode="HTML", reply_markup=kb)
        return
    await message.answer("Пришлите фото программы картинкой (не файлом) — или нажмите «❌ Отмена».")
