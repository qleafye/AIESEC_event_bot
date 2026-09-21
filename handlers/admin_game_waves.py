"""Phase 32 (32-10, D-06/D-09/D-10/D-11/D-13): админка амбассадорских волн — отдельный шов,
своего `Router()` НЕТ, декорирует ОБЩИЙ `handlers.admin.router` (та же техника 13-02, что у
`handlers/admin_game_tasks.py`/`handlers/reg_ambassador.py`).

Почему отдельный файл: `handlers/admin_gamification.py` стоит вплотную к потолку размера
(`tests/test_module_size_convention_260816.py`), новый экран волн туда не помещается —
тот же аргумент, что у `handlers/admin_game_tasks.py`.

Почему импортируется В ХВОСТЕ `handlers/admin_game_tasks.py` (см. последнюю строку того
файла): золотой снимок порядка регистрации (`tests/test_refac_snapshot_260816.py`) только
дополняется, независимо от того, какой модуль импортировали первым в тестах.

Состав экранов (задача 2): «🌊 Волны» (список волн города-шапки), визард создания
(`WaveCreate`: даты -> вводный текст -> подтверждение), «📋 Скопировать прошлую» (тот же
визард дат, дальше `copy_wave` вместо `create_wave`). Задача 3 добавляет карточку волны с
правкой полей, активацией и удалением.

Право на КАЖДОЕ изменяющее действие — `services.ambassador_waves.can_edit_wave`/
`editable_city_codes` (per_city_visible_codes) ПЕРЕД чтением и записью — кнопка могла быть
нарисована до привязки менеджера к городу (T-32-10-01).
"""
import html as html_module
from datetime import datetime

from aiogram import F, types
from aiogram.filters import StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardRemove

import cities
from cities import ALL_CITIES_LABEL, admin_selected_city
from database.db import (
    create_wave,
    delete_wave,
    get_wave,
    list_wave_tasks,
    list_waves,
    next_wave_number,
    set_wave_state,
    task_title,
    update_task_deadline,
    update_wave,
)
from game_labels import task_deadline, task_deadline_admin
from keyboards.builders import get_cancel_kb
from services import ambassador_waves as aw
from services.scheduler import (
    cancel_task_deadline_reminder,
    cancel_wave_jobs,
    schedule_task_deadline_reminder,
    schedule_wave_end,
    schedule_wave_results_broadcast,
    schedule_wave_start_for_all,
)
from settings_validation import validate_setting_value
from handlers.states import WaveCreate, WaveEdit
from handlers.admin import router
# Модульная ссылка (не `from ... import name`): та же осторожность с порядком импорта, что у
# handlers/admin_game_tasks.py::_ag — на момент импорта этого файла admin_gamification может
# быть ещё частично инициализирован.
from handlers import admin_gamification as _ag

_STATE_LABELS = {
    "draft": "черновик",
    "active": "идёт",
    "closing": "ждёт итогов",
    "announced": "итоги объявлены",
}

_DATE_HELP = (
    "Пришлите даты волны в формате <code>ДД.ММ.ГГГГ</code>. Можно одной строкой через «;»: "
    "например <code>01.10.2026; 21.10.2026</code> — или сначала только дату начала, я потом "
    "спрошу дату конца."
)

_SECOND_DATE_PROMPT = (
    "Дата начала принята. Теперь пришлите дату конца волны в формате <code>ДД.ММ.ГГГГ</code>, "
    "например <code>21.10.2026</code>."
)


def _fmt(iso: str | None) -> str:
    """ISO «%Y-%m-%d %H:%M:%S» -> «ДД.ММ.ГГГГ»; мусор — как есть (fail-soft)."""
    try:
        return datetime.strptime(str(iso), "%Y-%m-%d %H:%M:%S").strftime("%d.%m.%Y")
    except (TypeError, ValueError):
        return str(iso or "—")


def _wave_city_from_header(header: str | None) -> str | None:
    """Шапка `admin_selected_city` -> `event_city` волны: и «модуль выключен» (None), и
    «выбраны все города» (`cities.ALL_CITIES`) хранятся в БД как NULL — «волна для всех
    городов» (тот же смысл, что и у `game_tasks.event_city`, D-08)."""
    return None if header in (None, cities.ALL_CITIES) else header


async def _city_display(city: str | None) -> str:
    return ALL_CITIES_LABEL if city is None else await cities.city_label(city)


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


# ── карточка волны (задача 3): правка полей, активация, удаление, копия «с карточки» ────────

async def _wave_card_screen(admin_id: int, wave: dict) -> tuple[str, InlineKeyboardMarkup]:
    """Кнопки строятся ровно по `wave_editable_fields` — недоступную в текущем состоянии
    кнопку не рисуем вовсе (правило проекта: менеджеру не показывают заведомо отказывающую
    кнопку), а под запертыми полями активной волны печатаем одну строку объяснения."""
    tasks = await list_wave_tasks(wave["id"], active_only=False)
    editable = aw.wave_editable_fields(wave)
    intro = wave.get("intro_text")
    prize = wave.get("prize_places")

    lines = [
        f"{aw.wave_number_label(wave)} · {_STATE_LABELS.get(wave['state'], wave['state'])}",
        f"{_fmt(wave['starts_at'])}–{_fmt(wave['ends_at'])}",
        f"Вводный текст: {html_module.escape(intro) if intro else 'нет'}",
        f"Призовых мест: {prize if prize else 'как везде'}",
        f"Город: {await _city_display(wave.get('event_city'))}",
    ]
    if tasks:
        task_lines = [
            f"• {html_module.escape(str(task_title(t)))} — {t['coins']}🪙, до {task_deadline_admin(t)}"
            for t in tasks
        ]
        lines.append("Задания волны:\n" + "\n".join(task_lines))
    else:
        lines.append("Заданий в волне пока нет.")
    participants = len(await aw.wave_rating(wave["id"]))
    lines.append(f"Участников: {participants}")
    if wave["state"] == "active" and wave.get("started_notified_at"):
        lines.append(
            "📌 Стартовое сообщение уже отправлено — даты и состав заданий менять больше "
            "нельзя."
        )

    buttons: list[list[InlineKeyboardButton]] = []
    if "dates" in editable:
        buttons.append([InlineKeyboardButton(text="📅 Даты", callback_data=f"waveedit:{wave['id']}:dates")])
    if "intro_text" in editable:
        buttons.append([InlineKeyboardButton(text="📝 Вводный текст", callback_data=f"waveedit:{wave['id']}:intro_text")])
    if "prize_places" in editable:
        buttons.append([InlineKeyboardButton(text="🏅 Призовых мест", callback_data=f"waveedit:{wave['id']}:prize_places")])
    if wave["state"] == "draft":
        buttons.append([InlineKeyboardButton(text="▶️ Запустить волну", callback_data=f"waveactivate:{wave['id']}")])
    if wave["state"] != "announced":
        buttons.append([InlineKeyboardButton(text="📋 Скопировать эту волну", callback_data=f"wavecopy:{wave['id']}")])
        buttons.append([InlineKeyboardButton(text="🗑 Удалить", callback_data=f"wavedel:{wave['id']}")])
    buttons.append([InlineKeyboardButton(text="← К списку волн", callback_data="admin_game_waves")])
    return "\n\n".join(lines), InlineKeyboardMarkup(inline_keyboard=buttons)


async def _wave_from_prefix(callback: types.CallbackQuery, prefix: str) -> tuple[int | None, dict | None]:
    """`<prefix><id>` -> (id, wave) с проверкой прав ПЕРЕД любым чтением/записью (T-32-10-01) —
    единая точка входа для всех изменяющих обработчиков карточки волны."""
    try:
        wave_id = int(callback.data[len(prefix):])
    except ValueError:
        await callback.answer("Некорректная волна", show_alert=True)
        return None, None
    wave = await get_wave(wave_id)
    if wave is None:
        await callback.answer("Волна не найдена — возможно, её уже удалили", show_alert=True)
        return None, None
    if not await aw.can_edit_wave(callback.from_user.id, wave):
        await callback.answer("Эта волна другого города — доступа нет", show_alert=True)
        return None, None
    return wave_id, wave


@router.callback_query(F.data.startswith("wave:"))
async def show_wave_card(callback: types.CallbackQuery, state: FSMContext):
    wave_id, wave = await _wave_from_prefix(callback, "wave:")
    if wave is None:
        return
    await state.clear()
    text, kb = await _wave_card_screen(callback.from_user.id, wave)
    await _ag._edit_or_send_screen(callback.message, text, kb)
    await callback.answer()


# ── правка одного поля с карточки ────────────────────────────────────────────────────────────

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
    text, kb = await _wave_card_screen(message.from_user.id, updated)
    await message.answer(done_text, reply_markup=ReplyKeyboardRemove())
    await message.answer(text, parse_mode="HTML", reply_markup=kb)


@router.message(WaveEdit.dates)
async def wave_edit_dates_step(message: types.Message, state: FSMContext):
    data = await state.get_data()
    wave_id = data.get("we_wave_id")
    wave = await get_wave(wave_id)
    if wave is None:
        await message.answer("Волна не найдена — возможно, её уже удалили.")
        await state.clear()
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
    wave = await get_wave(wave_id)
    if wave is None:
        await message.answer("Волна не найдена — возможно, её уже удалили.")
        await state.clear()
        return
    raw = (message.html_text or message.text or "").strip()
    new_intro = None if raw == "-" else raw
    await update_wave(wave_id, intro_text=new_intro)
    await _wave_edit_done(message, state, wave_id, "✅ Вводный текст обновлён.")


@router.message(WaveEdit.prize_places)
async def wave_edit_prize_step(message: types.Message, state: FSMContext):
    data = await state.get_data()
    wave_id = data.get("we_wave_id")
    wave = await get_wave(wave_id)
    if wave is None:
        await message.answer("Волна не найдена — возможно, её уже удалили.")
        await state.clear()
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


# ── активация ─────────────────────────────────────────────────────────────────────────────

@router.callback_query(F.data.startswith("waveactivate:"))
async def wave_activate_confirm(callback: types.CallbackQuery, state: FSMContext):
    wave_id, wave = await _wave_from_prefix(callback, "waveactivate:")
    if wave is None:
        return
    if wave["state"] != "draft":
        await callback.answer("Волну уже нельзя запустить из этого состояния", show_alert=True)
        return
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="▶️ Да, запустить", callback_data=f"waveactivate_go:{wave_id}")],
        [InlineKeyboardButton(text="← Отмена", callback_data=f"wave:{wave_id}")],
    ])
    await callback.message.edit_text(
        f"▶️ <b>Запустить {aw.wave_number_label(wave)}?</b>\n\n"
        "Всем участникам сразу уйдёт стартовое сообщение со списком заданий волны и "
        "дедлайнами. После этого даты и состав заданий менять будет нельзя — рассылка уже "
        "ушла.",
        parse_mode="HTML", reply_markup=kb,
    )
    await callback.answer()


@router.callback_query(F.data.startswith("waveactivate_go:"))
async def wave_activate_go(callback: types.CallbackQuery, state: FSMContext):
    wave_id, wave = await _wave_from_prefix(callback, "waveactivate_go:")
    if wave is None:
        return
    ok = await set_wave_state(wave_id, "active", expected_state="draft")
    if not ok:
        await callback.answer("Волна уже запущена", show_alert=True)
        updated = await get_wave(wave_id)
        text, kb = await _wave_card_screen(callback.from_user.id, updated)
        await callback.message.edit_text(text, parse_mode="HTML", reply_markup=kb)
        return

    await schedule_wave_start_for_all(wave_id)
    ends_dt = datetime.strptime(wave["ends_at"], "%Y-%m-%d %H:%M:%S")
    schedule_wave_end(wave_id, ends_dt)
    for t in await list_wave_tasks(wave_id, active_only=True):
        dl = task_deadline(t)
        if dl is not None:
            schedule_task_deadline_reminder(t["id"], dl)

    await callback.answer("Волна запущена")
    updated = await get_wave(wave_id)
    text, kb = await _wave_card_screen(callback.from_user.id, updated)
    await callback.message.edit_text(text, parse_mode="HTML", reply_markup=kb)


# ── удаление ──────────────────────────────────────────────────────────────────────────────

@router.callback_query(F.data.startswith("wavedel:"))
async def wave_delete_confirm(callback: types.CallbackQuery, state: FSMContext):
    wave_id, wave = await _wave_from_prefix(callback, "wavedel:")
    if wave is None:
        return
    if wave["state"] == "announced":
        await callback.answer("Волна с объявленными итогами не удаляется", show_alert=True)
        return
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🗑 Да, удалить", callback_data=f"wavedel_go:{wave_id}")],
        [InlineKeyboardButton(text="← Отмена", callback_data=f"wave:{wave_id}")],
    ])
    await callback.message.edit_text(
        f"🗑 <b>Удалить {aw.wave_number_label(wave)}?</b>\n\n"
        "Пропадёт сама волна и её рейтинг. Задания волны останутся — они станут заданиями "
        "вне волн, уже начисленные баллы никуда не денутся.",
        parse_mode="HTML", reply_markup=kb,
    )
    await callback.answer()


@router.callback_query(F.data.startswith("wavedel_go:"))
async def wave_delete_go(callback: types.CallbackQuery, state: FSMContext):
    wave_id, wave = await _wave_from_prefix(callback, "wavedel_go:")
    if wave is None:
        return
    if wave["state"] == "announced":
        await callback.answer("Волна с объявленными итогами не удаляется", show_alert=True)
        return

    cancel_wave_jobs(wave_id)
    for t in await list_wave_tasks(wave_id, active_only=True):
        cancel_task_deadline_reminder(t["id"])

    await delete_wave(wave_id)
    await callback.answer("Волна удалена")
    text, kb = await _wave_list_screen(callback.from_user.id)
    await callback.message.edit_text(text, parse_mode="HTML", reply_markup=kb)


@router.callback_query(F.data.startswith("wavecopy:"))
async def wave_copy_from_card(callback: types.CallbackQuery, state: FSMContext):
    wave_id, wave = await _wave_from_prefix(callback, "wavecopy:")
    if wave is None:
        return
    await _start_wave_copy(callback.message, state, wave)
    await callback.answer()


# ── список волн + визард создания/копии ─────────────────────────────────────────────────────

async def _wave_list_screen(admin_id: int) -> tuple[str, InlineKeyboardMarkup]:
    header = await admin_selected_city(admin_id)
    waves = await list_waves(city_scope=cities.city_scope(header))
    lines = []
    buttons: list[list[InlineKeyboardButton]] = []
    for w in waves:
        task_count = len(await list_wave_tasks(w["id"], active_only=False))
        lines.append(
            f"{aw.wave_number_label(w)} · {_fmt(w['starts_at'])}–{_fmt(w['ends_at'])} · "
            f"{_STATE_LABELS.get(w['state'], w['state'])} · заданий {task_count}"
        )
        buttons.append([InlineKeyboardButton(
            text=aw.wave_number_label(w), callback_data=f"wave:{w['id']}",
        )])
    text = "🌊 <b>Волны</b>\n\n" + ("\n".join(lines) if lines else "Волн пока нет.")
    buttons.append([InlineKeyboardButton(text="➕ Новая волна", callback_data="wavenew")])
    if waves:
        buttons.append([InlineKeyboardButton(text="📋 Скопировать прошлую", callback_data="wavecopy")])
    buttons.append([InlineKeyboardButton(text="← Назад", callback_data="admin_sec:game")])
    return text, InlineKeyboardMarkup(inline_keyboard=buttons)


@router.callback_query(F.data == "admin_game_waves")
async def show_wave_list(callback: types.CallbackQuery, state: FSMContext):
    await state.clear()
    text, kb = await _wave_list_screen(callback.from_user.id)
    await _ag._edit_or_send_screen(callback.message, text, kb)
    await callback.answer()


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
        f"{_fmt(starts_at)}–{_fmt(ends_at)} ({days} дн.)",
        f"Вводный текст: {html_module.escape(intro) if intro else 'нет'}",
        f"Город: {await _city_display(city)}",
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
        f"{_fmt(starts_at)}–{_fmt(ends_at)}?",
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
    text, kb = await _wave_card_screen(admin_id, wave)
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
    text, kb = await _wave_card_screen(admin_id, wave)
    await callback.message.answer(
        "✅ Волна скопирована — поправьте тексты при необходимости.",
        reply_markup=ReplyKeyboardRemove(),
    )
    await callback.message.answer(text, parse_mode="HTML", reply_markup=kb)
    await callback.answer()


@router.callback_query(F.data == "wccancel")
async def wave_create_cancel(callback: types.CallbackQuery, state: FSMContext):
    await state.clear()
    text, kb = await _wave_list_screen(callback.from_user.id)
    await callback.message.answer("Отменено.", reply_markup=ReplyKeyboardRemove())
    await callback.message.answer(text, parse_mode="HTML", reply_markup=kb)
    await callback.answer()


# ── план 32-11 (D-16/D-17): итоги волны — подготовленный топ, подтверждение менеджера ──────
# Кнопка `wavefin:{wave_id}` приходит из `services.scheduler.send_wave_end_ping` личным
# сообщением менеджеру (не через карточку волны в админке) — `_wave_from_prefix` даёт ту же
# проверку прав на город (T-32-11-03), что и у остальных изменяющих действий этого файла, и
# заодно человеческий ответ на устаревшую/чужую кнопку.

async def _wave_finish_screen(wave: dict) -> tuple[str, InlineKeyboardMarkup]:
    """Экран D-16: подготовленный ботом топ + число сдач на проверке. Кнопка перехода к очереди
    проверки печатается ТОЛЬКО когда очередь непустая — CLAUDE.md запрещает рисовать кнопку,
    которой сейчас нечего делать."""
    summary = await aw.wave_end_summary(wave["id"])
    top_lines = [
        f"{row['place']}. {html_module.escape(str(row.get('name') or row['user_id']))} — {row['points']}"
        for row in summary["top"]
    ]
    lines = [
        f"🏁 {aw.wave_number_label(wave)} закончилась",
        "Топ:\n" + ("\n".join(top_lines) if top_lines else "пока пусто"),
    ]
    pending = summary["pending"]
    buttons: list[list[InlineKeyboardButton]] = []
    if pending:
        lines.append(
            f"На проверке ещё {pending} сдач — их результат в призёры уже не попадёт, если "
            "объявить итоги сейчас. Сначала проверьте, потом объявляйте."
        )
        buttons.append([InlineKeyboardButton(
            text="📋 Открыть сдачи на проверке", callback_data="admin_game_review",
        )])
    buttons.append([InlineKeyboardButton(text="🏁 Объявить итоги", callback_data=f"wavefin_go:{wave['id']}")])
    buttons.append([InlineKeyboardButton(text="← К карточке волны", callback_data=f"wave:{wave['id']}")])
    return "\n\n".join(lines), InlineKeyboardMarkup(inline_keyboard=buttons)


@router.callback_query(F.data.startswith("wavefin:"))
async def wave_finish_screen(callback: types.CallbackQuery, state: FSMContext):
    wave_id, wave = await _wave_from_prefix(callback, "wavefin:")
    if wave is None:
        return
    if wave["state"] == "announced":
        await callback.answer("Итоги этой волны уже объявлены", show_alert=True)
        return
    if wave["state"] != "closing":
        await callback.answer("Волна ещё не закончилась", show_alert=True)
        return
    await state.clear()
    text, kb = await _wave_finish_screen(wave)
    await _ag._edit_or_send_screen(callback.message, text, kb)
    await callback.answer()


@router.callback_query(F.data.startswith("wavefin_go:"))
async def wave_finish_confirm(callback: types.CallbackQuery, state: FSMContext):
    wave_id, wave = await _wave_from_prefix(callback, "wavefin_go:")
    if wave is None:
        return
    if wave["state"] != "closing":
        await callback.answer(
            "Итоги этой волны уже объявлены" if wave["state"] == "announced" else "Волна ещё не закончилась",
            show_alert=True,
        )
        return
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Да, объявить", callback_data=f"wavefin_do:{wave_id}")],
        [InlineKeyboardButton(text="← Отмена", callback_data=f"wave:{wave_id}")],
    ])
    await callback.message.edit_text(
        f"🏁 <b>Объявить итоги {aw.wave_number_label(wave)}?</b>\n\n"
        "Список призёров после этого не изменится: сдача, которую проверят позже, в призы уже "
        "не попадёт. Всем участникам волны уйдёт сообщение с их местом, призёрам — отдельное "
        "поздравление.",
        parse_mode="HTML", reply_markup=kb,
    )
    await callback.answer()


@router.callback_query(F.data.startswith("wavefin_do:"))
async def wave_finish_go(callback: types.CallbackQuery, state: FSMContext):
    wave_id, wave = await _wave_from_prefix(callback, "wavefin_do:")
    if wave is None:
        return
    result = await aw.announce_results(wave_id)
    if result is None:
        await callback.answer("Итоги этой волны уже объявлены", show_alert=True)
    else:
        schedule_wave_results_broadcast(wave_id, result["standings"])
        await callback.answer("Итоги объявлены")
    updated = await get_wave(wave_id)
    text, kb = await _wave_card_screen(callback.from_user.id, updated)
    await callback.message.edit_text(text, parse_mode="HTML", reply_markup=kb)


__all__ = [
    "show_wave_card", "show_wave_list", "wave_create_start", "wave_copy_last_start",
    "wave_create_dates_step", "wave_create_intro_step", "wave_create_intro_skip",
    "wave_create_redates", "wave_create_go", "wave_copy_go", "wave_create_cancel",
    "wave_edit_field_start", "wave_edit_dates_step", "wave_edit_intro_step",
    "wave_edit_prize_step", "wave_activate_confirm", "wave_activate_go",
    "wave_delete_confirm", "wave_delete_go", "wave_copy_from_card",
    "wave_finish_screen", "wave_finish_confirm", "wave_finish_go",
]
