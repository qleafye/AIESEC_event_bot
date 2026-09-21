"""Ревизия 32-FIX (CR-04/WR-06): визард волны — создание (`WaveCreate`: даты -> вводный
текст -> подтверждение), «📋 Скопировать прошлую»/«со своей карточки» (тот же визард дат,
дальше `services.ambassador_waves.copy_wave`) и правка ОДНОГО поля с карточки (`WaveEdit`:
даты/вводный текст/призовые места). Декорирует ОБЩИЙ `handlers.admin.router` — своего
`Router()` нет, та же техника 13-02, что у соседних швов геймы.

Вынесен ИЗ `handlers/admin_game_waves.py` (импортирован В ХВОСТЕ того файла) — он подошёл
вплотную к потолку размера (`tests/test_module_size_convention_260816.py`), а правки этой
ревизии (CR-04/WR-06 ниже) сюда уже не помещались. Общие помощники карточки/списка волны
(`_wave_card_screen`/`_wave_list_screen`/`_wave_from_prefix`/`_fmt`/`_city_display`) остались
там — здесь они читаются через модульную ссылку `_gw` (та же осторожность с порядком
импорта, что у `admin_game_tasks.py::_ag`: на момент импорта этого файла `admin_game_waves`
уже полностью инициализирован — импорт идёт из его собственного хвоста, — но модульная
ссылка остаётся общим приёмом семьи швов геймы).

CR-04: ни у `WaveCreate`, ни у `WaveEdit` раньше не было обработчика «Отмена»/`/команда» —
кнопка «Отмена» на текстовых шагах сохранялась как вводный текст волны (и уходила в личку
амбассадорам на старте), а на шагах дат/призовых мест любая команда вешала менеджера в
бесконечном «не понял». `wave_wizard_cancel` регистрируется ПЕРЕД шаговыми обработчиками
(admin.router: первое совпадение выигрывает) и одним фильтром ловит и «Отмена», и любую
`/команду` — идиома `grev_step_cancel` (`handlers/admin_gamification.py`).

WR-06: шаги `WaveEdit.*` раньше писали в БД по значению, прочитанному в момент НАЖАТИЯ кнопки
— между кнопкой и присланным сообщением волна могла смениться (стартовая рассылка ушла,
итоги объявили). `_wave_edit_guard` перечитывает волну и заново проверяет право
(`can_edit_wave`) и открытость поля (`wave_editable_fields`) ПЕРЕД каждой записью.
"""
from datetime import datetime

from aiogram import F, types
from aiogram.filters import StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardRemove

import cities
from cities import admin_selected_city
from database.db import (
    create_wave,
    get_wave,
    list_wave_tasks,
    list_waves,
    next_wave_number,
    update_task_deadline,
    update_wave,
)
from game_labels import task_deadline
from keyboards.builders import get_cancel_kb
from services import ambassador_waves as aw
from services.scheduler import (
    schedule_task_deadline_reminder,
    schedule_wave_end,
    schedule_wave_start_for_all,
)
from settings_validation import validate_setting_value
from handlers.states import WaveCreate, WaveEdit
from handlers.admin import router
# Модульная ссылка (не `from ... import name`): та же осторожность с порядком импорта, что у
# handlers/admin_game_tasks.py::_ag — общие помощники карточки/списка волны читаются лениво,
# при вызове, а не при импорте этого модуля.
from handlers import admin_game_waves as _gw

_DATE_HELP = (
    "Пришлите даты волны в формате <code>ДД.ММ.ГГГГ</code>. Можно одной строкой через «;»: "
    "например <code>01.10.2026; 21.10.2026</code> — или сначала только дату начала, я потом "
    "спрошу дату конца."
)

_SECOND_DATE_PROMPT = (
    "Дата начала принята. Теперь пришлите дату конца волны в формате <code>ДД.ММ.ГГГГ</code>, "
    "например <code>21.10.2026</code>."
)


def _wave_city_from_header(header: str | None) -> str | None:
    """Шапка `admin_selected_city` -> `event_city` волны: и «модуль выключен» (None), и
    «выбраны все города» (`cities.ALL_CITIES`) хранятся в БД как NULL — «волна для всех
    городов» (тот же смысл, что и у `game_tasks.event_city`, D-08)."""
    return None if header in (None, cities.ALL_CITIES) else header


def _split_date_input(raw: str) -> list[str]:
    return [p.strip() for p in (raw or "").split(";") if p.strip()]


def _parse_one_date(raw: str) -> str | None:
    """«01.10.2026» -> нормализованная «ДД.ММ.ГГГГ» или `None`. Разбор — существующий парсер
    типа `date_only` фазы 31 (`settings_validation.validate_setting_value`, вызван с любым
    зарегистрированным ключом этого типа — `forum_date`, результат парсинга берётся, сам
    ключ значения не хранит и не читает); второго `strptime` этого формата не заводим
    (interfaces плана)."""
    value, err = validate_setting_value("forum_date", raw)
    return None if err else value


def _to_start_of_day(ddmmyyyy: str) -> str:
    return datetime.strptime(ddmmyyyy, "%d.%m.%Y").strftime("%Y-%m-%d 00:00:00")


def _to_end_of_day(ddmmyyyy: str) -> str:
    return datetime.strptime(ddmmyyyy, "%d.%m.%Y").strftime("%Y-%m-%d 23:59:59")


# ── CR-04: отмена визарда — раньше «Отмена»/любая команда посреди шага не отлавливались ────

@router.message(StateFilter(WaveCreate), F.text.in_({"Отмена"}) | F.text.startswith("/"))
@router.message(StateFilter(WaveEdit), F.text.in_({"Отмена"}) | F.text.startswith("/"))
async def wave_wizard_cancel(message: types.Message, state: FSMContext):
    data = await state.get_data()
    await state.clear()
    await message.answer("Отменено, ничего не изменено.", reply_markup=ReplyKeyboardRemove())
    wave_id = data.get("we_wave_id")
    wave = await get_wave(wave_id) if wave_id else None
    if wave is not None:
        text, kb = await _gw._wave_card_screen(message.from_user.id, wave)
    else:
        text, kb = await _gw._wave_list_screen(message.from_user.id)
    await message.answer(text, parse_mode="HTML", reply_markup=kb)


# ── правка одного поля с карточки (WR-06: право/состав перепроверяются на каждом шаге) ──────

async def _wave_edit_guard(message: types.Message, state: FSMContext, wave_id: int, field: str) -> dict | None:
    """Между кнопкой правки и присланным значением проходит произвольное время — волна могла
    смениться (стартовая рассылка ушла, итоги объявили, город менеджера сменился). Перечитывает
    волну и заново проверяет право и `wave_editable_fields` ПЕРЕД записью; при отказе —
    человеческое объяснение и возврат туда, где ещё есть смысл (карточка, если право осталось;
    иначе список волн), правка не сохраняется."""
    wave = await get_wave(wave_id)
    if wave is None:
        await state.clear()
        await message.answer("Волна не найдена — возможно, её уже удалили.", reply_markup=ReplyKeyboardRemove())
        text, kb = await _gw._wave_list_screen(message.from_user.id)
        await message.answer(text, parse_mode="HTML", reply_markup=kb)
        return None
    if not await aw.can_edit_wave(message.from_user.id, wave):
        await state.clear()
        await message.answer(
            "Эта волна стала недоступна вашему городу, пока вы вводили значение — правка не "
            "сохранена.",
            reply_markup=ReplyKeyboardRemove(),
        )
        text, kb = await _gw._wave_list_screen(message.from_user.id)
        await message.answer(text, parse_mode="HTML", reply_markup=kb)
        return None
    if field not in aw.wave_editable_fields(wave):
        await state.clear()
        await message.answer(
            "Это поле больше нельзя менять — волна изменилась, пока вы вводили значение. "
            "Правка не сохранена.",
            reply_markup=ReplyKeyboardRemove(),
        )
        text, kb = await _gw._wave_card_screen(message.from_user.id, wave)
        await message.answer(text, parse_mode="HTML", reply_markup=kb)
        return None
    return wave


@router.callback_query(F.data.startswith("waveedit:"))
async def wave_edit_field_start(callback: types.CallbackQuery, state: FSMContext):
    try:
        _, wave_id_s, field = callback.data.split(":", 2)
        wave_id = int(wave_id_s)
    except ValueError:
        await callback.answer("Некорректное поле", show_alert=True)
        return
    wave = await get_wave(wave_id)
    if wave is None:
        await callback.answer("Волна не найдена — возможно, её уже удалили", show_alert=True)
        return
    if not await aw.can_edit_wave(callback.from_user.id, wave):
        await callback.answer("Эта волна другого города — доступа нет", show_alert=True)
        return
    if field not in aw.wave_editable_fields(wave):
        await callback.answer("Это поле сейчас нельзя менять", show_alert=True)
        return

    await state.set_data({"we_wave_id": wave_id})
    if field == "dates":
        await state.set_state(WaveEdit.dates)
        await callback.message.answer(_DATE_HELP, parse_mode="HTML", reply_markup=get_cancel_kb())
    elif field == "intro_text":
        await state.set_state(WaveEdit.intro_text)
        await callback.message.answer(
            "Пришлите новый вводный текст волны, или «-», чтобы убрать его.",
            reply_markup=get_cancel_kb(),
        )
    else:  # prize_places
        await state.set_state(WaveEdit.prize_places)
        await callback.message.answer(
            "Пришлите число призовых мест для этой волны, например 3, или «-», чтобы "
            "использовать общую настройку.",
            reply_markup=get_cancel_kb(),
        )
    await callback.answer()


async def _wave_edit_done(message: types.Message, state: FSMContext, wave_id: int, done_text: str):
    await state.clear()
    updated = await get_wave(wave_id)
    if updated is None:
        return
    text, kb = await _gw._wave_card_screen(message.from_user.id, updated)
    await message.answer(done_text, reply_markup=ReplyKeyboardRemove())
    await message.answer(text, parse_mode="HTML", reply_markup=kb)


@router.message(WaveEdit.dates)
async def wave_edit_dates_step(message: types.Message, state: FSMContext):
    data = await state.get_data()
    wave_id = data.get("we_wave_id")
    wave = await _wave_edit_guard(message, state, wave_id, "dates")
    if wave is None:
        return

    parts = _split_date_input(message.text or "")
    pending_start = data.get("we_start")
    if pending_start and len(parts) == 1:
        end = _parse_one_date(parts[0])
        if end is None:
            await message.answer(_DATE_HELP, parse_mode="HTML")
            return
        start_ddmmyyyy = pending_start
    elif len(parts) in (1, 2):
        parsed = [_parse_one_date(p) for p in parts]
        if any(p is None for p in parsed):
            await message.answer(_DATE_HELP, parse_mode="HTML")
            return
        if len(parsed) == 1:
            await state.update_data(we_start=parsed[0])
            await message.answer(_SECOND_DATE_PROMPT, parse_mode="HTML")
            return
        start_ddmmyyyy, end = parsed
    else:
        await message.answer(_DATE_HELP, parse_mode="HTML")
        return

    new_starts = _to_start_of_day(start_ddmmyyyy)
    new_ends = _to_end_of_day(end)
    error = await aw.validate_wave_dates(new_starts, new_ends, wave.get("event_city"), exclude_id=wave_id)
    if error:
        await state.update_data(we_start=None)
        await message.answer(error)
        return

    old_ends = wave["ends_at"]
    await update_wave(wave_id, starts_at=new_starts, ends_at=new_ends)
    # Дедлайны заданий волны, равные ПРЕЖНЕМУ концу волны (то есть заведённые «по умолчанию»,
    # без собственного срока), сдвигаются вместе с волной; заданию с собственным более ранним
    # сроком дата не трогается.
    for t in await list_wave_tasks(wave_id, active_only=True):
        if t.get("deadline_at") == old_ends:
            await update_task_deadline(t["id"], new_ends)

    if wave.get("state") == "active":
        new_ends_dt = datetime.strptime(new_ends, "%Y-%m-%d %H:%M:%S")
        schedule_wave_end(wave_id, new_ends_dt)
        if not wave.get("started_notified_at"):
            await schedule_wave_start_for_all(wave_id)
        for t in await list_wave_tasks(wave_id, active_only=True):
            dl = task_deadline(t)
            if dl is not None:
                schedule_task_deadline_reminder(t["id"], dl)

    await _wave_edit_done(message, state, wave_id, "✅ Даты обновлены.")


@router.message(WaveEdit.intro_text)
async def wave_edit_intro_step(message: types.Message, state: FSMContext):
    data = await state.get_data()
    wave_id = data.get("we_wave_id")
    wave = await _wave_edit_guard(message, state, wave_id, "intro_text")
    if wave is None:
        return
    raw = (message.html_text or message.text or "").strip()
    new_intro = None if raw == "-" else raw
    await update_wave(wave_id, intro_text=new_intro)
    await _wave_edit_done(message, state, wave_id, "✅ Вводный текст обновлён.")


@router.message(WaveEdit.prize_places)
async def wave_edit_prize_step(message: types.Message, state: FSMContext):
    data = await state.get_data()
    wave_id = data.get("we_wave_id")
    wave = await _wave_edit_guard(message, state, wave_id, "prize_places")
    if wave is None:
        return
    raw = (message.text or "").strip()
    if raw == "-":
        new_value = None
    else:
        try:
            new_value = int(raw)
        except ValueError:
            new_value = 0
        if new_value <= 0:
            await message.answer(
                "Нужно целое число больше нуля, например 3, или «-» для общей настройки."
            )
            return
    await update_wave(wave_id, prize_places=new_value)
    await _wave_edit_done(message, state, wave_id, "✅ Число призовых мест обновлено.")


@router.callback_query(F.data.startswith("wavecopy:"))
async def wave_copy_from_card(callback: types.CallbackQuery, state: FSMContext):
    wave_id, wave = await _gw._wave_from_prefix(callback, "wavecopy:")
    if wave is None:
        return
    await _start_wave_copy(callback.message, state, wave)
    await callback.answer()


# ── список волн + визард создания/копии ─────────────────────────────────────────────────────

@router.callback_query(F.data == "wavenew")
async def wave_create_start(callback: types.CallbackQuery, state: FSMContext):
    admin_id = callback.from_user.id
    header = await admin_selected_city(admin_id)
    city = _wave_city_from_header(header)
    allowed = await aw.editable_city_codes(admin_id)
    # T-32-10-01: право проверяется ДО начала визарда — кнопка могла отрисоваться до привязки
    # менеджера к городу.
    if header == cities.ALL_CITIES and set(allowed) != set(cities.city_codes()):
        await callback.answer(
            "Выберите свой город в шапке — волна «для всех городов» вам недоступна",
            show_alert=True,
        )
        return
    if city is not None and city not in allowed:
        await callback.answer("Нет прав на этот город", show_alert=True)
        return
    await state.set_data({"wc_mode": "create", "wc_city": city})
    await state.set_state(WaveCreate.dates)
    await callback.message.answer(_DATE_HELP, parse_mode="HTML", reply_markup=get_cancel_kb())
    await callback.answer()


async def _start_wave_copy(message: types.Message, state: FSMContext, src_wave: dict):
    await state.set_data({
        "wc_mode": "copy", "wc_copy_src": src_wave["id"], "wc_city": src_wave.get("event_city"),
    })
    await state.set_state(WaveCreate.dates)
    await message.answer(
        f"📋 Копируем {aw.wave_number_label(src_wave)}. {_DATE_HELP}",
        parse_mode="HTML", reply_markup=get_cancel_kb(),
    )


@router.callback_query(F.data == "wavecopy")
async def wave_copy_last_start(callback: types.CallbackQuery, state: FSMContext):
    admin_id = callback.from_user.id
    header = await admin_selected_city(admin_id)
    waves = await list_waves(city_scope=cities.city_scope(header))
    if not waves:
        await callback.answer("Волн пока нет — сначала создайте одну", show_alert=True)
        return
    src = waves[0]  # list_waves: ORDER BY starts_at DESC, id DESC — самая свежая первая
    if not await aw.can_edit_wave(admin_id, src):
        await callback.answer("Нет прав на эту волну", show_alert=True)
        return
    await _start_wave_copy(callback.message, state, src)
    await callback.answer()


@router.message(WaveCreate.dates)
async def wave_create_dates_step(message: types.Message, state: FSMContext):
    data = await state.get_data()
    parts = _split_date_input(message.text or "")
    pending_start = data.get("wc_start")

    if pending_start and len(parts) == 1:
        end = _parse_one_date(parts[0])
        if end is None:
            await message.answer(_DATE_HELP, parse_mode="HTML")
            return
        await _wave_dates_collected(message, state, data, pending_start, end)
        return

    if len(parts) not in (1, 2):
        await message.answer(_DATE_HELP, parse_mode="HTML")
        return
    parsed = [_parse_one_date(p) for p in parts]
    if any(p is None for p in parsed):
        await message.answer(_DATE_HELP, parse_mode="HTML")
        return
    if len(parsed) == 1:
        await state.update_data(wc_start=parsed[0])
        await message.answer(_SECOND_DATE_PROMPT, parse_mode="HTML")
        return
    await _wave_dates_collected(message, state, data, parsed[0], parsed[1])


def _intro_skip_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="⏭ Без текста", callback_data="wcintro_skip")],
    ])


async def _wave_dates_collected(message: types.Message, state: FSMContext, data: dict,
                                 start_ddmmyyyy: str, end_ddmmyyyy: str):
    starts_at = _to_start_of_day(start_ddmmyyyy)
    ends_at = _to_end_of_day(end_ddmmyyyy)
    city = data.get("wc_city")
    error = await aw.validate_wave_dates(starts_at, ends_at, city)
    if error:
        await state.update_data(wc_start=None)
        await message.answer(error)
        return
    await state.update_data(wc_starts=starts_at, wc_ends=ends_at, wc_start=None)
    if data.get("wc_mode") == "copy":
        await _show_copy_confirm(message, state)
        return
    await message.answer(
        "Вводный текст волны — необязательная «история», уходит амбассадорам в личку на "
        "старте волны. Можно пропустить.",
        reply_markup=_intro_skip_kb(),
    )
    await state.set_state(WaveCreate.intro)


@router.message(WaveCreate.intro)
async def wave_create_intro_step(message: types.Message, state: FSMContext):
    text = (message.html_text or message.text or "").strip()
    await state.update_data(wc_intro=text or None)
    await _show_create_confirm(message, state)


@router.callback_query(F.data == "wcintro_skip", StateFilter(WaveCreate.intro))
async def wave_create_intro_skip(callback: types.CallbackQuery, state: FSMContext):
    await state.update_data(wc_intro=None)
    await _show_create_confirm(callback.message, state)
    await callback.answer()


async def _show_create_confirm(message: types.Message, state: FSMContext):
    data = await state.get_data()
    starts_at, ends_at = data["wc_starts"], data["wc_ends"]
    city = data.get("wc_city")
    number = await next_wave_number(city)
    days = (
        datetime.strptime(ends_at, "%Y-%m-%d %H:%M:%S")
        - datetime.strptime(starts_at, "%Y-%m-%d %H:%M:%S")
    ).days + 1
    intro = data.get("wc_intro")
    lines = [
        f"Волна {number}",
        f"{_gw._fmt(starts_at)}–{_gw._fmt(ends_at)} ({days} дн.)",
        f"Вводный текст: {intro if intro else 'нет'}",
        f"Город: {await _gw._city_display(city)}",
    ]
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Создать", callback_data="wccreate_go")],
        [InlineKeyboardButton(text="✏️ Изменить даты", callback_data="wcredates")],
        [InlineKeyboardButton(text="❌ Отмена", callback_data="wccancel")],
    ])
    await state.set_state(WaveCreate.confirm)
    await message.answer("\n".join(lines), parse_mode="HTML", reply_markup=kb)


async def _show_copy_confirm(message: types.Message, state: FSMContext):
    data = await state.get_data()
    src = await get_wave(data["wc_copy_src"])
    starts_at, ends_at = data["wc_starts"], data["wc_ends"]
    task_count = len(await list_wave_tasks(data["wc_copy_src"], active_only=True))
    lines = [
        f"📋 Скопировать {aw.wave_number_label(src) if src else '?'} в новую волну "
        f"{_gw._fmt(starts_at)}–{_gw._fmt(ends_at)}?",
        f"Заданий будет перенесено: {task_count}.",
        "Дедлайны заданий сдвинутся на ту же разницу, что и даты волны; если дедлайн выйдет "
        "за конец новой волны — он подрежется до её конца.",
        "Тексты и даты волны можно будет поправить после копирования.",
    ]
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Скопировать", callback_data=f"wavecopy_go:{data['wc_copy_src']}")],
        [InlineKeyboardButton(text="✏️ Изменить даты", callback_data="wcredates")],
        [InlineKeyboardButton(text="❌ Отмена", callback_data="wccancel")],
    ])
    await state.set_state(WaveCreate.confirm)
    await message.answer("\n".join(lines), reply_markup=kb)


@router.callback_query(F.data == "wcredates", StateFilter(WaveCreate.confirm))
async def wave_create_redates(callback: types.CallbackQuery, state: FSMContext):
    await state.update_data(wc_start=None, wc_starts=None, wc_ends=None)
    await state.set_state(WaveCreate.dates)
    await callback.message.answer(_DATE_HELP, parse_mode="HTML", reply_markup=get_cancel_kb())
    await callback.answer()


@router.callback_query(F.data == "wccreate_go", StateFilter(WaveCreate.confirm))
async def wave_create_go(callback: types.CallbackQuery, state: FSMContext):
    admin_id = callback.from_user.id
    data = await state.get_data()
    city = data.get("wc_city")
    allowed = await aw.editable_city_codes(admin_id)
    if city is not None and city not in allowed:
        await callback.answer("Нет прав на этот город", show_alert=True)
        await state.clear()
        return
    error = await aw.validate_wave_dates(data["wc_starts"], data["wc_ends"], city)
    if error:
        await callback.message.answer(error)
        await state.set_state(WaveCreate.dates)
        await callback.answer()
        return
    wave_id = await create_wave(
        data["wc_starts"], data["wc_ends"], intro_text=data.get("wc_intro"), event_city=city,
        created_by=admin_id,
    )
    await state.clear()
    wave = await get_wave(wave_id)
    text, kb = await _gw._wave_card_screen(admin_id, wave)
    await callback.message.answer("✅ Волна создана.", reply_markup=ReplyKeyboardRemove())
    await callback.message.answer(text, parse_mode="HTML", reply_markup=kb)
    await callback.answer()


@router.callback_query(F.data.startswith("wavecopy_go:"), StateFilter(WaveCreate.confirm))
async def wave_copy_go(callback: types.CallbackQuery, state: FSMContext):
    admin_id = callback.from_user.id
    data = await state.get_data()
    try:
        src_id = int(callback.data.split(":", 1)[1])
    except ValueError:
        await callback.answer("Некорректная волна", show_alert=True)
        return
    if src_id != data.get("wc_copy_src"):
        await callback.answer("Устаревшая кнопка — начните заново", show_alert=True)
        await state.clear()
        return
    src = await get_wave(src_id)
    if src is None or not await aw.can_edit_wave(admin_id, src):
        await callback.answer("Нет прав на эту волну", show_alert=True)
        await state.clear()
        return
    new_id = await aw.copy_wave(src_id, data["wc_starts"], data["wc_ends"], created_by=admin_id)
    await state.clear()
    wave = await get_wave(new_id)
    text, kb = await _gw._wave_card_screen(admin_id, wave)
    await callback.message.answer(
        "✅ Волна скопирована — поправьте тексты при необходимости.",
        reply_markup=ReplyKeyboardRemove(),
    )
    await callback.message.answer(text, parse_mode="HTML", reply_markup=kb)
    await callback.answer()


@router.callback_query(F.data == "wccancel")
async def wave_create_cancel(callback: types.CallbackQuery, state: FSMContext):
    await state.clear()
    text, kb = await _gw._wave_list_screen(callback.from_user.id)
    await callback.message.answer("Отменено.", reply_markup=ReplyKeyboardRemove())
    await callback.message.answer(text, parse_mode="HTML", reply_markup=kb)
    await callback.answer()


__all__ = [
    "wave_wizard_cancel",
    "wave_edit_field_start", "wave_edit_dates_step", "wave_edit_intro_step",
    "wave_edit_prize_step", "wave_copy_from_card",
    "wave_create_start", "wave_copy_last_start", "wave_create_dates_step",
    "wave_create_intro_step", "wave_create_intro_skip", "wave_create_redates",
    "wave_create_go", "wave_copy_go", "wave_create_cancel",
]
