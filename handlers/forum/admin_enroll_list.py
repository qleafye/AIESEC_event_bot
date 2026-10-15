"""«📋 Записи на сессии» города: список с цифрами и закрытием в один тап, выгрузка записанных
файлом CSV, экран настроек модуля «📅 Запись на сессии» (тумблер, дата закрытия, тексты).

Форма шва — как у `admin_enroll.py` (общий `admin.router`, импорт хвостом `handlers/admin.py`,
право `settings` по префиксу `prog_*`). В Google-таблицу записи уходят двумя общими вкладками
(`services/forum/enroll_sheets.py`: сами обновляются, кнопка «🔄 Таблица записей» пересобирает
вручную), по сессии — ещё и выгрузка файлом. Модуль не входит в общие тумблеры и порядок событийных полей: его правит этот экран."""
import csv
import html as html_module
import io

from aiogram import F, types
from aiogram.fsm.context import FSMContext
from aiogram.types import BufferedInputFile, InlineKeyboardButton, InlineKeyboardMarkup

from domain.cities import cities_module_on, city_label, get_setting_typed_for_city, header_snapshot, per_city_key
from database import quiz_db
from database import session_enroll_db as edb
from database.db import _csv_safe, get_program_session, update_program_session
from handlers.admin import router
from handlers.forum.admin_program import _CITY_FORBIDDEN_ALERT, _city_allowed, _short
from handlers.states import EditSetting
from services.forum.enroll_sheets import enable_autosync, enroll_tab_names, rebuild_enroll_sheets
from services.forum.session_enroll import ENROLL_TEXT_KEYS, common_enabled, deadline_label, module_enabled, session_open_state
from services.settings.audit import set_setting_by_admin
from domain.settings.placeholders import hint
from domain.settings.schema import SETTINGS_SCHEMA

_PAGE = 8
_TEXT_PAGE = 8
_CANCEL_ROW = [InlineKeyboardButton(text="❌ Отмена", callback_data="settings_cancel")]


async def _deny(callback: types.CallbackQuery, code: str | None) -> bool:
    if await _city_allowed(callback.from_user.id, code):
        return False
    await callback.answer(_CITY_FORBIDDEN_ALERT, show_alert=True)
    return True


async def _write_key(base_key: str, code: str) -> str:
    """Ключ для записи: при выключенном модуле городов — голый (составной не подействовал бы)."""
    return per_city_key(base_key, code) if await cities_module_on() else base_key


# ── Список «📋 Записи на сессии» ──────────────────────────────────────────────────────────────

async def render_enroll_list(code: str, page: int) -> tuple[str, InlineKeyboardMarkup]:
    common_on = await common_enabled(code)
    sessions = await edb.list_trackable_sessions(code, include_common=common_on)
    counts = await edb.enrollment_counts_for_city(code)
    pages = max(1, -(-len(sessions) // _PAGE))
    page = min(max(page, 0), pages - 1)
    lines = [
        f"📋 <b>Записи на сессии</b> — {html_module.escape(await city_label(code))}", "",
        f"Записались: {await edb.count_enrolled_users(code, include_common=common_on)} чел., "
        f"подтвердили расписание: {await edb.count_schedule_confirms(code)}",
    ]
    buttons: list[list[InlineKeyboardButton]] = []
    if not sessions:
        lines += ["", "Сессий с треком пока нет. Назначьте трек в карточке сессии («🧭 Запись и треки»)."]
    elif common_on:
        lines += ["", "Общие сессии идут в списке с пометкой «🏛 общая»: записываться на них не обязательно."]
    else:
        lines += ["", "Нажмите на сессию, чтобы выгрузить список записанных файлом. "
                      "Замок рядом закрывает или открывает запись."]
    many_days = len({s["day"] for s in sessions}) > 1
    for s in sessions[page * _PAGE:(page + 1) * _PAGE]:
        n = counts.get(s["id"], 0)
        taken = f"{n}/{s['enroll_limit']}" if s.get("enroll_limit") is not None else str(n)
        day = f"{s['day'][8:10]}.{s['day'][5:7]} " if many_days else ""
        label = f"{day}{s['start_time']} {s['track_name'] or '🏛 общая'} · {_short(s['title'], 24)} — {taken}"
        closed = bool(s.get("enroll_closed"))
        buttons.append([
            InlineKeyboardButton(text=("🔒 " if closed else "") + label, callback_data=f"prog_enrx:{s['id']}"),
            InlineKeyboardButton(text="🔓" if closed else "🔒", callback_data=f"prog_enrlt:{s['id']}:{page}"),
        ])
    if pages > 1:
        nav = []
        if page > 0:
            nav.append(InlineKeyboardButton(text="◀️", callback_data=f"prog_enrl:{code}:{page - 1}"))
        nav.append(InlineKeyboardButton(text=f"{page + 1}/{pages}", callback_data=f"prog_enrl:{code}:{page}"))
        if page < pages - 1:
            nav.append(InlineKeyboardButton(text="▶️", callback_data=f"prog_enrl:{code}:{page + 1}"))
        buttons.append(nav)
    buttons.append([InlineKeyboardButton(text="⚙️ Настройки записи", callback_data=f"prog_enrset:{code}")])
    buttons.append([InlineKeyboardButton(text="← К программе", callback_data=f"prog_city:{code}")])
    return "\n".join(lines), InlineKeyboardMarkup(inline_keyboard=buttons)


@router.callback_query(F.data.startswith("prog_enrl:"))
async def prog_enrl(callback: types.CallbackQuery):
    _, code, page_s = callback.data.split(":", 2)
    if await _deny(callback, code):
        return
    text, kb = await render_enroll_list(code, int(page_s))
    await callback.message.edit_text(text, parse_mode="HTML", reply_markup=kb)
    await callback.answer()


@router.callback_query(F.data.startswith("prog_enrlt:"))
async def prog_enrlt(callback: types.CallbackQuery):
    _, sid_s, page_s = callback.data.split(":", 2)
    session = await get_program_session(int(sid_s))
    if session is None:
        await callback.answer("Сессия больше недоступна.", show_alert=True)
        return
    if await _deny(callback, session["city"]):
        return
    await update_program_session(session["id"], enroll_closed=0 if session.get("enroll_closed") else 1)
    text, kb = await render_enroll_list(session["city"], int(page_s))
    await callback.message.edit_text(text, parse_mode="HTML", reply_markup=kb)
    await callback.answer()


@router.callback_query(F.data.startswith("prog_enrx:"))
async def prog_enrx(callback: types.CallbackQuery):
    session = await get_program_session(int(callback.data.split(":", 1)[1]))
    if session is None:
        await callback.answer("Сессия больше недоступна.", show_alert=True)
        return
    if await _deny(callback, session["city"]):
        return
    users = await edb.list_enrolled_users(session["id"])
    if not users:
        await callback.answer("На эту сессию пока никто не записан.", show_alert=True)
        return
    out = io.StringIO()
    writer = csv.writer(out, delimiter=";", quotechar='"', quoting=csv.QUOTE_MINIMAL)
    writer.writerow(["Telegram ID", "ФИО", "Username", "Телефон", "Записан", "Как записан"])
    for u in users:
        row = (
            u["telegram_id"], u.get("full_name"), u.get("username"), u.get("phone"),
            u.get("created_at"), "сам" if u.get("source") == "self" else "волонтёр",
        )
        writer.writerow([_csv_safe(cell) for cell in row])
    document = BufferedInputFile(out.getvalue().encode("utf-8-sig"), filename=f"enroll_{session['id']}.csv")
    await callback.message.answer_document(
        document, caption=f"Записанные на «{html_module.escape(session['title'] or '')}»: {len(users)} чел.",
    )
    await callback.answer()


# ── Настройки модуля ─────────────────────────────────────────────────────────────────────────

async def _quiz_offer_on(code: str) -> bool:
    try:
        return await get_setting_typed_for_city("session_enroll_quiz_offer", code) == "on"
    except Exception:
        return False


async def render_enroll_settings(code: str) -> tuple[str, InlineKeyboardMarkup]:
    on = await module_enabled(code)
    deadline = await deadline_label(code)
    lines = [
        f"📅 <b>Запись на сессии</b> — {html_module.escape(await city_label(code))}", "",
        "Делегаты сами выбирают сессии своих треков. Запись открыта, пока модуль включён и "
        "не наступила дата закрытия.", "",
        f"Запись на сессии: {'✅ Вкл' if on else '❌ Выкл'}",
        f"Запись закрывается: {deadline or 'дата не задана'}",
    ]
    offer = await _quiz_offer_on(code)
    offer_line = f"Предлагать тест перед записью: {'✅ Вкл' if offer else '❌ Выкл'}"
    if offer and await quiz_db.active_quiz_for_city(code) is None:
        offer_line += " (теста нет — развилка не покажется)"
    lines.append(offer_line)
    common_on = await common_enabled(code)
    lines.append(
        f"Общие сессии (открытие, закрытие): запись по желанию {'✅ Вкл' if common_on else '❌ Выкл'}"
        + (" — делегаты отмечают, на какие придут; прийти можно и без записи." if common_on else "")
    )
    sessions_tab, people_tab = await enroll_tab_names()
    lines.append(
        f"Google-таблица: вкладки «{html_module.escape(sessions_tab)}» и «{html_module.escape(people_tab)}» "
        "обновляются сами через полминуты после записи или правки программы."
    )
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(
            text="✅ Вкл → ❌ Выкл" if on else "❌ Выкл → ✅ Вкл", callback_data=f"prog_enrsw:{code}",
        )],
        [InlineKeyboardButton(
            text="🧭 Тест перед записью: ✅ → ❌" if offer else "🧭 Тест перед записью: ❌ → ✅",
            callback_data=f"prog_enrqo:{code}",
        )],
        [InlineKeyboardButton(
            text=f"🏛 Общие сессии: запись по желанию — {'✅ вкл' if common_on else '❌ выкл'}",
            callback_data=f"prog_enrco:{code}",
        )],
        [InlineKeyboardButton(text=f"⏳ Закрыть запись: {deadline or 'не задано'}",
                              callback_data=f"prog_enrdl:{code}")],
        [InlineKeyboardButton(text="✏️ Тексты", callback_data=f"prog_enrtx:{code}:0")],
        [InlineKeyboardButton(text="🧭 Тест компетенций", callback_data=f"prog_qz:{code}")],
        [InlineKeyboardButton(text="📋 Записи на сессии", callback_data=f"prog_enrl:{code}:0")],
        [InlineKeyboardButton(text="⏰ Напоминания о сессиях", callback_data=f"prog_srm:{code}")],
        [InlineKeyboardButton(text="🧭 Треки", callback_data=f"prog_trkl:{code}"),
         InlineKeyboardButton(text="🎯 Компетенции", callback_data=f"prog_cmpl:{code}")],
        [InlineKeyboardButton(text="🔄 Таблица записей", callback_data=f"prog_enrsh:{code}")],
        [InlineKeyboardButton(text="← К программе", callback_data=f"prog_city:{code}")],
    ])
    return "\n".join(lines), kb


async def _show_settings(callback: types.CallbackQuery, code: str, note: str | None = None) -> None:
    text, kb = await render_enroll_settings(code)
    from handlers.forum.admin_forum_hub_nav import keep_hub_back  # открыт из хаба форума — «Назад» в хаб
    kb = keep_hub_back(callback.message, kb)
    await callback.message.edit_text(text, parse_mode="HTML", reply_markup=kb)
    await callback.answer(note)


@router.callback_query(F.data.startswith("prog_enrset:"))
async def prog_enrset(callback: types.CallbackQuery):
    code = callback.data.split(":", 1)[1]
    if await _deny(callback, code):
        return
    await _show_settings(callback, code)


@router.callback_query(F.data.startswith("prog_enrsw:"))
async def prog_enrsw(callback: types.CallbackQuery):
    code = callback.data.split(":", 1)[1]
    if await _deny(callback, code):
        return
    new_val = "off" if await module_enabled(code) else "on"
    await set_setting_by_admin(callback.from_user.id, await _write_key("session_enroll_enabled", code), new_val)
    await _show_settings(callback, code, "Сохранено.")


@router.callback_query(F.data.startswith("prog_enrqo:"))
async def prog_enrqo(callback: types.CallbackQuery):
    code = callback.data.split(":", 1)[1]
    if await _deny(callback, code):
        return
    new_val = "off" if await _quiz_offer_on(code) else "on"
    await set_setting_by_admin(callback.from_user.id, await _write_key("session_enroll_quiz_offer", code), new_val)
    await _show_settings(callback, code, "Сохранено.")


@router.callback_query(F.data.startswith("prog_enrco:"))
async def prog_enrco(callback: types.CallbackQuery):
    code = callback.data.split(":", 1)[1]
    if await _deny(callback, code):
        return
    new_val = "off" if await common_enabled(code) else "on"
    await set_setting_by_admin(callback.from_user.id, await _write_key("session_enroll_common_optional", code), new_val)
    await _show_settings(callback, code, "Сохранено.")


@router.callback_query(F.data.startswith("prog_enrdl:"))
async def prog_enrdl(callback: types.CallbackQuery, state: FSMContext):
    code = callback.data.split(":", 1)[1]
    if await _deny(callback, code):
        return
    key = await _write_key("session_enroll_deadline", code)
    await state.set_state(EditSetting.waiting_for_value)
    await state.set_data({"setting_key": key, "return_cb": f"prog_enrset:{code}", "return_label": "← К записи на сессии",
                          **await header_snapshot(callback.from_user.id)})  # город из кнопки, не из шапки
    prompt = SETTINGS_SCHEMA["session_enroll_deadline"]["prompt"]
    await callback.message.answer(
        html_module.escape(prompt), parse_mode="HTML", reply_markup=InlineKeyboardMarkup(inline_keyboard=[_CANCEL_ROW]),
    )
    await callback.answer()


# ── Тексты модуля ────────────────────────────────────────────────────────────────────────────

@router.callback_query(F.data.startswith("prog_enrtx:"))
async def prog_enrtx(callback: types.CallbackQuery):
    _, code, page_s = callback.data.split(":", 2)
    if await _deny(callback, code):
        return
    pages = -(-len(ENROLL_TEXT_KEYS) // _TEXT_PAGE)
    page = min(max(int(page_s), 0), pages - 1)
    buttons = []
    for idx in range(page * _TEXT_PAGE, min(len(ENROLL_TEXT_KEYS), (page + 1) * _TEXT_PAGE)):
        label = SETTINGS_SCHEMA[ENROLL_TEXT_KEYS[idx]]["label"].replace("📅 Запись на сессии: ", "")
        buttons.append([InlineKeyboardButton(text=_short(label, 50), callback_data=f"prog_enrte:{code}:{idx}")])
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton(text="◀️", callback_data=f"prog_enrtx:{code}:{page - 1}"))
    nav.append(InlineKeyboardButton(text=f"{page + 1}/{pages}", callback_data=f"prog_enrtx:{code}:{page}"))
    if page < pages - 1:
        nav.append(InlineKeyboardButton(text="▶️", callback_data=f"prog_enrtx:{code}:{page + 1}"))
    buttons.append(nav)
    buttons.append([InlineKeyboardButton(text="← Настройки записи", callback_data=f"prog_enrset:{code}")])
    await callback.message.edit_text(
        "✏️ <b>Тексты записи на сессии</b>\n\nВыберите, что изменить.",
        parse_mode="HTML", reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons),
    )
    await callback.answer()


@router.callback_query(F.data.startswith("prog_enrte:"))
async def prog_enrte(callback: types.CallbackQuery, state: FSMContext):
    _, code, idx_s = callback.data.split(":", 2)
    if await _deny(callback, code):
        return
    idx = int(idx_s)
    if not 0 <= idx < len(ENROLL_TEXT_KEYS):
        await callback.answer("Этого текста нет — обновите экран.", show_alert=True)
        return
    base = ENROLL_TEXT_KEYS[idx]
    entry = SETTINGS_SCHEMA[base]
    current = await get_setting_typed_for_city(base, code)
    prompt = entry["prompt"]
    extra = hint(base)
    lines = [f"✏️ <b>{html_module.escape(entry['label'])}</b>", "",
             f"Сейчас: <b>{html_module.escape(str(current))}</b>" if current else "Сейчас: стандартный текст", "",
             html_module.escape(prompt)]
    if extra and "Подстановки" not in prompt:
        lines += ["", html_module.escape(extra)]
    lines += ["", "<i>«-» — вернуть стандартный текст.</i>"]
    await state.set_state(EditSetting.waiting_for_value)
    await state.set_data({"setting_key": await _write_key(base, code),
                          "return_cb": f"prog_enrtx:{code}:{idx // _TEXT_PAGE}", "return_label": "← К текстам записи",
                          **await header_snapshot(callback.from_user.id)})  # город из кнопки, не из шапки
    await callback.message.edit_text(
        "\n".join(lines), parse_mode="HTML", reply_markup=InlineKeyboardMarkup(inline_keyboard=[_CANCEL_ROW]),
    )
    await callback.answer()


# ── Таблица записей ──────────────────────────────────────────────────────────────────────────

@router.callback_query(F.data.startswith("prog_enrsh:"))
async def prog_enrsh(callback: types.CallbackQuery):
    code = callback.data.split(":", 1)[1]
    if await _deny(callback, code):
        return
    sessions_tab, people_tab = await enroll_tab_names()
    await callback.message.edit_text(
        "🔄 <b>Пересобрать вкладки записи?</b>\n\n"
        f"Вкладки «{html_module.escape(sessions_tab)}» и «{html_module.escape(people_tab)}» "
        "перезапишутся целиком из базы бота — <b>всё, что вы писали в них руками, пропадёт</b>.\n\n"
        "Вкладки общие для всех городов, город указан в колонке «Город». "
        "Сами записи делегатов не меняются.",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="✅ Да, пересобрать", callback_data=f"prog_enrshgo:{code}")],
            [InlineKeyboardButton(text="← Назад", callback_data=f"prog_enrset:{code}")],
        ]),
    )
    await callback.answer()


@router.callback_query(F.data.startswith("prog_enrshgo:"))
async def prog_enrshgo(callback: types.CallbackQuery):
    code = callback.data.split(":", 1)[1]
    if await _deny(callback, code):
        return
    await callback.answer("🔄 Пересобираю…")
    sessions_tab, people_tab = await enroll_tab_names()
    written = await rebuild_enroll_sheets()
    lines = []
    for tab, n in zip((sessions_tab, people_tab), written):
        report = (
            f"{n} строк" if n >= 0
            else "⚠️ не удалось записать — проверьте, что Google-таблица подключена (раздел «📊 Данные»)"
        )
        lines.append(f"«{html_module.escape(tab)}»: {report}.")
    head = "✅ " if all(n >= 0 for n in written) else "⚠️ "
    await callback.message.edit_text(
        head + "\n".join(lines), parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="← Настройки записи", callback_data=f"prog_enrset:{code}")],
        ]),
    )


# Пересборку по событиям записи регистрирует только процесс бота (в веб-процессе Mini App этот
# модуль не импортируется — таймер там тихо ничего не делает).
enable_autosync()
