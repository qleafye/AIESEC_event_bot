"""Phase 32 (32-10, D-06/D-09/D-10/D-11/D-13): админка амбассадорских волн — отдельный шов,
своего `Router()` НЕТ, декорирует ОБЩИЙ `handlers.admin.router` (та же техника 13-02, что у
`handlers/admin_game_tasks.py`/`handlers/reg_ambassador.py`).

Почему отдельный файл: `handlers/admin_gamification.py` стоит вплотную к потолку размера
(`tests/test_module_size_convention_260816.py`), новый экран волн туда не помещается —
тот же аргумент, что у `handlers/admin_game_tasks.py`.

Почему импортируется В ХВОСТЕ `handlers/admin_game_tasks.py` (см. последнюю строку того
файла): золотой снимок порядка регистрации (`tests/test_refac_snapshot_260816.py`) только
дополняется, независимо от того, какой модуль импортировали первым в тестах.

Состав экранов: список волн (`show_wave_list`), карточка волны с правкой полей/активацией/
удалением/копией (задача 3), экран итогов (план 32-11, `_wave_finish_screen`/`wavefin*`).

Ревизия 32-FIX (CR-04/WR-06/WR-07/WR-13/WR-16): визард создания/копии/правки волны (FSM
`WaveCreate`/`WaveEdit`, обработчики `wavenew`/`wavecopy*`/`waveedit*`/`wc*`) вынесен в
`handlers/admin_game_wave_wizard.py` — этот файл подошёл вплотную к потолку размера
(`tests/test_module_size_convention_260816.py`), а сами правки визарда (перепроверка права и
состава на каждом шаге, обработчик «Отмена») сюда уже не помещались. Импортирован В ХВОСТЕ
этого файла — та же дисциплина, что у соседних швов: золотой снимок регистрации только
перегруппировывается одним блоком, ни один хендлер не теряется (drift note в
`tests/test_refac_snapshot_260816.py`). WR-13: удаление волны (`wave_delete_go`) больше не
снимает напоминания о дедлайне у её заданий — они остаются жить «вне волн» со своим сроком.

Право на КАЖДОЕ изменяющее действие — `services.ambassador_waves.can_edit_wave`/
`editable_city_codes` (per_city_visible_codes) ПЕРЕД чтением и записью — кнопка могла быть
нарисована до привязки менеджера к городу (T-32-10-01).
"""
import html as html_module
from datetime import datetime

from aiogram import F, types
from aiogram.fsm.context import FSMContext
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

import cities
from cities import ALL_CITIES_LABEL, admin_selected_city
from database.db import (
    delete_wave,
    get_wave,
    list_wave_tasks,
    list_waves,
    set_wave_state,
    task_title,
)
from game_labels import task_deadline, task_deadline_admin, task_has_deadline
from services import ambassador_waves as aw
from services.scheduler import (
    cancel_wave_jobs,
    schedule_task_deadline_reminder,
    schedule_wave_end,
    schedule_wave_results_broadcast,
    schedule_wave_start_for_all,
)
from services.timeutil import msk_now
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


def _fmt(iso: str | None) -> str:
    """ISO «%Y-%m-%d %H:%M:%S» -> «ДД.ММ.ГГГГ»; мусор — как есть (fail-soft)."""
    try:
        return datetime.strptime(str(iso), "%Y-%m-%d %H:%M:%S").strftime("%d.%m.%Y")
    except (TypeError, ValueError):
        return str(iso or "—")


async def _city_display(city: str | None) -> str:
    return ALL_CITIES_LABEL if city is None else await cities.city_label(city)


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
        f"Вводный текст: {intro if intro else 'нет'}",
        f"Призовых мест: {prize if prize else 'как везде'}",
        f"Город: {await _city_display(wave.get('event_city'))}",
    ]
    if tasks:
        # WR-12: `task_deadline_admin` для задания без срока уже возвращает готовое «без
        # срока» — приклеенное безусловно «до » давало менеджеру «до без срока».
        task_lines = [
            (
                f"• {html_module.escape(str(task_title(t)))} — {t['coins']}🪙, "
                f"{'до ' + task_deadline_admin(t) if task_has_deadline(t) else task_deadline_admin(t)}"
            )
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
    if wave["state"] == "closing":
        # Личное сообщение о конце волны менеджер мог потерять — итоги объявляются и отсюда.
        buttons.append([InlineKeyboardButton(text="🏁 Итоги волны", callback_data=f"wavefin:{wave['id']}")])
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


# ── активация ─────────────────────────────────────────────────────────────────────────────

@router.callback_query(F.data.startswith("waveactivate:"))
async def wave_activate_confirm(callback: types.CallbackQuery, state: FSMContext):
    wave_id, wave = await _wave_from_prefix(callback, "waveactivate:")
    if wave is None:
        return
    if wave["state"] != "draft":
        await callback.answer("Волну уже нельзя запустить из этого состояния", show_alert=True)
        return
    # WR-16: пустую волну запускать нечего — амбассадоры получат «Задания волны:» с пустым
    # списком, отменить рассылку будет нельзя, состав уже заперт.
    if not await list_wave_tasks(wave_id, active_only=True):
        await callback.answer(
            f"Сначала добавьте задания: «🎯 Задания» → «➕ Новое» → {aw.wave_number_label(wave)}",
            show_alert=True,
        )
        return
    now = msk_now()
    ends_dt = datetime.strptime(wave["ends_at"], "%Y-%m-%d %H:%M:%S")
    if ends_dt <= now:
        await callback.answer("Дата конца волны уже прошла — поправьте даты и запустите снова", show_alert=True)
        return
    starts_dt = datetime.strptime(wave["starts_at"], "%Y-%m-%d %H:%M:%S")
    # WR-16: раньше текст безусловно обещал рассылку «сразу» — на самом деле она уходит в
    # starts_at, а «сразу» верно только для уже наступившей даты начала.
    when_text = (
        f"в дату начала волны, {_fmt(wave['starts_at'])} в 00:00"
        if starts_dt > now else "в течение минуты — дата начала уже наступила"
    )
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="▶️ Да, запустить", callback_data=f"waveactivate_go:{wave_id}")],
        [InlineKeyboardButton(text="← Отмена", callback_data=f"wave:{wave_id}")],
    ])
    await callback.message.edit_text(
        f"▶️ <b>Запустить {aw.wave_number_label(wave)}?</b>\n\n"
        f"Стартовое сообщение со списком заданий волны и дедлайнами уйдёт {when_text} всем "
        "участникам волны. До этого момента даты и состав заданий ещё можно поправить — "
        "после отправки будет нельзя.",
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

    # WR-13: снимаем ТОЛЬКО волновые джобы (старт/конец/итоги) — они привязаны к самой волне и
    # без неё бессмысленны. Задания волны переживают удаление (`delete_wave`: их `wave_id`
    # обнуляется, срок остаётся их собственным) и становятся заданиями «вне волн» — их
    # напоминание о дедлайне (D-26) снимать нельзя, иначе оно тихо потеряется навсегда, хотя
    # карточка удаления честно предупреждает, что задания останутся жить дальше.
    cancel_wave_jobs(wave_id)
    await delete_wave(wave_id)
    await callback.answer("Волна удалена")
    text, kb = await _wave_list_screen(callback.from_user.id)
    await callback.message.edit_text(text, parse_mode="HTML", reply_markup=kb)


# ── список волн ───────────────────────────────────────────────────────────────────────────

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
    lines = [
        f"🏁 <b>Объявить итоги {aw.wave_number_label(wave)}?</b>",
        "",
        "Список призёров после этого не изменится: сдача, которую проверят позже, в призы уже "
        "не попадёт. Всем участникам волны уйдёт сообщение с их местом, призёрам — отдельное "
        "поздравление.",
    ]
    # CR-07: при ничьей на границе призовых мест призёров окажется больше, чем самих мест —
    # менеджер должен узнать об этом ДО подтверждения, а не после (список призёров сразу
    # заперт, D-17).
    tie_note = await aw.prize_tie_note(wave_id)
    if tie_note:
        lines.append(tie_note)
    await callback.message.edit_text(
        "\n".join(lines), parse_mode="HTML", reply_markup=kb,
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
        # CR-06: джоба читает снимок из БД сама — standings больше не передаётся аргументом.
        schedule_wave_results_broadcast(wave_id)
        await callback.answer("Итоги объявлены")
    updated = await get_wave(wave_id)
    text, kb = await _wave_card_screen(callback.from_user.id, updated)
    await callback.message.edit_text(text, parse_mode="HTML", reply_markup=kb)


__all__ = [
    "show_wave_card", "show_wave_list",
    "wave_activate_confirm", "wave_activate_go",
    "wave_delete_confirm", "wave_delete_go",
    "wave_finish_screen", "wave_finish_confirm", "wave_finish_go",
]

# Ревизия 32-FIX: визард создания/копии/правки волны — новый шов, импортирован В ХВОСТЕ этого
# файла (та же дисциплина, что у admin_game_tasks.py::admin_game_waves выше по цепочке) —
# золотой снимок порядка (tests/test_refac_snapshot_260816.py) видит чистое перемещение блока
# хендлеров, независимо от того, какой модуль импортировали первым в тестах.
from handlers import admin_game_wave_wizard  # noqa: E402,F401
