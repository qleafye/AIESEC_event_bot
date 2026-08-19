"""Phase 13 (13-04, REFAC-01): gamification-admin seam.

Task creation/archive/delete wizard ("🎯 Задания" + GameTaskCreate wizard; Phase 16 (16-03):
the NEW point-edit/preset/preview/«✏️ Изменить» handlers live in handlers/admin_game_tasks.py,
imported right after this module in admin.py's seam list -- this file was at its size
ceiling), the "🪙 Монеты
вручную" manual-coins wizard (coinsman_*) + "📜 Журнал монет" (coinsjrn_*), submission review
(grev_*, GameReview wizard), Sheets sync ("🔄 Таблица геймы") and the "📊 Статистика геймы"
screen — everything gated by moderate_game (ADMIN_CAPS, pre-registered 09-01). Decorates the
SAME shared `admin.router` (13-02/13-03 shared-router seam-import technique) — imported LAST
in admin.py's bottom seam-import list, since this region was last-in-order in the original
file.

Drift vs. the 15.08 plan text: this region grew well past the plan's ~800-line estimate
(Phase 14 added the task archive/delete/return screens + the coinsman_*/coinsjrn_* wizards;
09.1 added Sheets parts/autosync; 09.3 added the header-aware task-city wizard step) — see
13-04-SUMMARY.md "Known Gap".

`_resolve_staff_input`/`_STAFF_INPUT_ERROR` (coinsman_person_step's forward/@username lookup,
reusing roles_add_person's own parser) come from handlers.admin_roles (13-04 Task 3 -- imported
straight from that seam module, not re-exported through the aggregator, since admin_roles is
always imported before admin_gamification in handlers/admin.py's seam-import order).
`_parse_positive_int`/`_parse_coins_amount`/`_notify_manual_coins` (coins parsing/delivery) are
still imported from the aggregator itself -- they were not moved to a shared home by this plan.
"""
import csv
import html as html_module
import io
import logging
import re
from datetime import datetime

from aiogram import F, types
from aiogram.filters import Command, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.types import (
    BufferedInputFile,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    ReplyKeyboardRemove,
)

from config import config
from settings_schema import get_setting_typed
from database.db import (
    GAME_CATEGORIES,
    GAME_PROOF_TYPES,
    add_coins,
    archive_task,
    claim_submission,
    count_manual_coin_entries,
    count_rejected_submissions,
    count_task_submissions,
    create_task,
    delete_task,
    export_coins_journal_csv,
    get_balance,
    get_game_stats,
    get_pending_submissions,
    get_pending_submissions_count,
    get_setting,
    get_staff_city,
    get_submission,
    get_submission_parts_or_legacy,
    get_task,
    get_user,
    get_user_by_username,
    list_all_submissions,
    list_all_tasks,
    list_manual_coin_entries,
    parse_proof_types,
    set_setting,
    task_title,
    unarchive_task,
    update_task_photo,
    update_task_title,
)
from keyboards.builders import get_cancel_kb
from services.sheets import sync_named_worksheet
from services.game_sheets import describe_plan, game_tab_plan, rows_for_entry
from services.scheduler import _fmt_dt, _now_moscow_naive, _parse_schedule_dt
from services.game_sync import request_resync as _request_game_resync, set_rebuild as _set_game_rebuild
from handlers.states import CoinsManual, GameReview, GameTaskCreate, GameTaskEdit
from handlers.game_labels import category_label  # Phase 16 (16-01/16-03): RU labels, one source
from handlers.game_task_wizard import (  # Phase 16 (16-03): pure wizard helpers (no router) -- shared
    _DEADLINE_PAST, _PROMPT_CATEGORY, _PROMPT_COINS, _PROMPT_COINS_INVALID, _PROMPT_DEADLINE,  # noqa: F401
    _PROMPT_TEXT, _PROMPT_TEXT_EMPTY, _finish_deadline_step, _game_task_confirm_kb,  # noqa: F401
    _game_task_deadline_preset_kb, _render_game_task_confirm_card, _resolve_deadline_preset,  # noqa: F401
    _show_wizard_preview, _wizard_return_to_preview,  # noqa: F401
)
from cities import (
    admin_selected_city,
    cities_module_on,
    city_codes,
    city_label,
    enabled_cities,
    normalize_city,
)
from handlers.admin_core import (
    _SUBMISSION_OUT_OF_SCOPE_ALERT,
    _admin_city_scope,
    _submission_out_of_scope,
    admin_keyboard_for,
)
from handlers.admin import (
    router,
    _notify_manual_coins,
    _parse_coins_amount,
    _parse_positive_int,
)
from handlers.admin_roles import _STAFF_INPUT_ERROR, _resolve_staff_input

logger = logging.getLogger(__name__)


# ── Phase 9 (GAME-01, D-08, wave 2, 09-02): «📋 Задания» screen + creation wizard ───────────
#
# game_manager-only surface (moderate_game, ADMIN_CAPS pre-registered by 09-01). Task list is
# ~10-20 rows a season (D-08 table), no pagination needed here — unlike the tinder-pattern
# moderation queue wave 4 will add. T-09-05: task text is shown to every delegate later
# (wave 3, parse_mode="HTML") — escaped on EVERY render, not just once at creation.

def _game_task_deadline_display(t: dict) -> str:
    """dd.mm HH:MM (sketch, Экран 6) -- season lives inside one year, the year is noise for a
    manager scanning 10-20 rows; the full date is still on the edit card's prompt."""
    try:
        return datetime.strptime(t["deadline_at"], "%Y-%m-%d %H:%M:%S").strftime("%d.%m %H:%M")
    except (TypeError, ValueError):
        return str(t["deadline_at"] or "—")


async def _game_task_line(t: dict, index: int) -> str:
    """The ONE place a manager-side task row is rendered (shared by the active list and the
    archive). Phase 16 (16-03, GAME-UI-03): numbered two-line shape (mirrors the delegate list
    of 16-01, minus the per-row status emoji -- the manager sees archived/active as screens,
    not per row), RU category via game_labels.category_label -- the raw code never reaches a
    human (CONTEXT.md «кодов человеку не показываем»). Number is the ONLY link between a text
    row and its «№N» action buttons (Telegram can't attach a button to a paragraph)."""
    title = html_module.escape(task_title(t))
    category = html_module.escape(await category_label(str(t["category"])))
    return (
        f"{index}. <b>{title}</b>\n"
        f"{category} · {t['coins']}🪙 · до {_game_task_deadline_display(t)}"
    )


def _game_tasks_toggle_row(active_count: int, archived_count: int) -> list[InlineKeyboardButton]:
    """«Активные (N) | Архив (M)» -- both callback_data values are the pre-existing screen
    entries (admin_game_tasks / admin_game_archive), tapping the side you're already on just
    re-renders the same screen (Telegram has no disabled-button API)."""
    return [
        InlineKeyboardButton(text=f"Активные ({active_count})", callback_data="admin_game_tasks"),
        InlineKeyboardButton(text=f"Архив ({archived_count})", callback_data="admin_game_archive"),
    ]


async def _game_tasks_screen() -> tuple[str, InlineKeyboardMarkup]:
    """«Функция возвращает (text, kb)» idiom -- the four re-render call sites
    (`show_game_tasks`, `cancel_game_task_create`, `game_task_confirm`,
    `game_task_create_cancel`) can never drift on format. Task 1 (14-03, GAME-08).

    Phase 16 (16-03, GAME-UI-03): numbered rows + a toggle row on top + one action row PER
    task «🗄 №N В архив / ✏️ №N / 🗑 №N» (delete only with zero submissions -- T-14-11/T-14-12:
    the SQL-level gate in `delete_task` is the real defense, this is only the UX hint)."""
    all_tasks = await list_all_tasks()
    active = [t for t in all_tasks if not t.get("archived_at")]
    archived_count = sum(1 for t in all_tasks if t.get("archived_at"))

    buttons: list[list[InlineKeyboardButton]] = []
    if all_tasks:
        buttons.append(_game_tasks_toggle_row(len(active), archived_count))
    hidden_delete = False
    lines = []
    for n, t in enumerate(active, start=1):
        lines.append(await _game_task_line(t, n))
        row = [
            InlineKeyboardButton(text=f"🗄 №{n} В архив", callback_data=f"gtarchive:{t['id']}"),
            InlineKeyboardButton(text=f"✏️ №{n}", callback_data=f"gtedit:{t['id']}"),
        ]
        if await count_task_submissions(t["id"]) == 0:
            row.append(InlineKeyboardButton(text=f"🗑 №{n}", callback_data=f"gtdelete:{t['id']}"))
        else:
            hidden_delete = True
        buttons.append(row)

    if not active:
        text = "Заданий пока нет."
        if archived_count:
            text += f"\n\n🗄 В архиве: {archived_count}"
    else:
        parts = ["🎯 <b>Задания</b>", "\n\n".join(lines)]
        if hidden_delete:
            parts.append(
                "🗑 У заданий со сдачами удаления нет — по ним уже есть история. Такое "
                "задание можно только убрать в архив."
            )
        if archived_count:
            parts.append(f"🗄 В архиве: {archived_count}")
        text = "\n\n".join(parts)

    buttons.append([InlineKeyboardButton(text="➕ Новое задание", callback_data="gtnew")])
    return text, InlineKeyboardMarkup(inline_keyboard=buttons)


async def _game_archive_screen() -> tuple[str, InlineKeyboardMarkup]:
    """«Архив» side of the toggle: numbered archived rows + «↩️ №N Вернуть» per row.
    Return-from-archive is a SAFE operation (CONTEXT.md decision A) -- no confirm step,
    unlike archive/delete. Task 1 (14-03, GAME-08); Phase 16 (16-03): toggle + numbering."""
    all_tasks = await list_all_tasks()
    archived = [t for t in all_tasks if t.get("archived_at")]
    active_count = len(all_tasks) - len(archived)

    lines = []
    buttons: list[list[InlineKeyboardButton]] = []
    if all_tasks:
        buttons.append(_game_tasks_toggle_row(active_count, len(archived)))
    for n, t in enumerate(archived, start=1):
        try:
            archived_at = datetime.strptime(t["archived_at"], "%Y-%m-%d %H:%M:%S").strftime("%d.%m")
        except (TypeError, ValueError):
            archived_at = str(t["archived_at"] or "—")
        lines.append(f"{await _game_task_line(t, n)} · в архиве с {archived_at}")
        buttons.append([InlineKeyboardButton(text=f"↩️ №{n} Вернуть", callback_data=f"gtunarchive:{t['id']}")])

    if not archived:
        text = "Архив пуст."
    else:
        text = "🗄 <b>Задания · Архив</b>\n\n" + "\n\n".join(lines)
    if not all_tasks:
        buttons.append([InlineKeyboardButton(text="← К заданиям", callback_data="admin_game_tasks")])
    return text, InlineKeyboardMarkup(inline_keyboard=buttons)


@router.callback_query(F.data == "admin_game_tasks")
async def show_game_tasks(callback: types.CallbackQuery, state: FSMContext):
    """Phase 16 (16-03): edits IN PLACE (symmetric with show_game_archive) -- reachable from
    the toggle row, «← К заданиям», the confirm screens' «← Отмена» AND the admin menu; one
    live message per screen. Fail-soft fallback to a fresh message when the source cannot be
    edited (a photo message, an already-deleted one)."""
    text, kb = await _game_tasks_screen()
    try:
        await callback.message.edit_text(text, parse_mode="HTML", reply_markup=kb)
    except Exception:
        await callback.message.answer(text, parse_mode="HTML", reply_markup=kb)
    await callback.answer()


@router.callback_query(F.data == "admin_game_archive")
async def show_game_archive(callback: types.CallbackQuery):
    text, kb = await _game_archive_screen()
    await callback.message.edit_text(text, parse_mode="HTML", reply_markup=kb)
    await callback.answer()


@router.callback_query(F.data.startswith("gtarchive_go:"))
async def game_task_archive_go(callback: types.CallbackQuery):
    """Real DB write for the archive action — only reachable via the `gtarchive:<id>` confirm
    screen (Task 2, 14-03). Re-renders the tasks screen on BOTH branches (T-14-11-adjacent:
    the button could fire from a stale confirm message after another manager already acted)."""
    try:
        task_id = int(callback.data.split(":", 1)[1])
    except ValueError:
        await callback.answer("Некорректное задание", show_alert=True)
        return
    if await archive_task(task_id):
        _request_game_resync()  # Phase 09.1 (D, GAME-07): archive is a debounced resync trigger
        await callback.answer("Задание убрано в архив")
    else:
        await callback.answer("Задание уже в архиве", show_alert=True)
    text, kb = await _game_tasks_screen()
    await callback.message.edit_text(text, parse_mode="HTML", reply_markup=kb)


@router.callback_query(F.data.startswith("gtunarchive:"))
async def game_task_unarchive(callback: types.CallbackQuery):
    """Simple flip, no confirm step (CONTEXT.md: return-from-archive is safe)."""
    try:
        task_id = int(callback.data.split(":", 1)[1])
    except ValueError:
        await callback.answer("Некорректное задание", show_alert=True)
        return
    if await unarchive_task(task_id):
        _request_game_resync()
        await callback.answer("Задание возвращено")
    else:
        await callback.answer("Задание уже активно", show_alert=True)
    text, kb = await _game_archive_screen()
    await callback.message.edit_text(text, parse_mode="HTML", reply_markup=kb)


# ── Task 2 (14-03, GAME-08): two-step confirm gates for archive/delete ──────────────────────
# Same "confirm-text callback -> *_go execute callback" split as `rebuild_sheet_confirm` ->
# `admin_rebuild_sheet_go` and `dedupe_sheet_confirm` -> `admin_dedupe_sheet_go` (both above,
# same file). Real DB writes live ONLY in the `_go` handlers — T-14-12: the confirm screen
# itself must never touch the database.

@router.callback_query(F.data.startswith("gtarchive:"))
async def game_task_archive_confirm(callback: types.CallbackQuery):
    try:
        task_id = int(callback.data.split(":", 1)[1])
    except ValueError:
        await callback.answer("Некорректное задание", show_alert=True)
        return
    task = await get_task(task_id)
    if task is None:
        await callback.answer("Задание не найдено", show_alert=True)
        return
    name = html_module.escape(task_title(task))
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🗄 Да, в архив", callback_data=f"gtarchive_go:{task_id}")],
        [InlineKeyboardButton(text="← Отмена", callback_data="admin_game_tasks")],
    ])
    await callback.message.edit_text(
        f"🗄 <b>Убрать задание в архив?</b>\n\n«{name}»\n\n"
        "Задание пропадёт у делегатов и его больше нельзя будет сдать; уже сделанные сдачи и "
        "начисленные монеты сохранятся; непроверенные сдачи останутся в «🎮 Проверка». Вернуть "
        "можно в любой момент из «🗄 Архив».",
        parse_mode="HTML",
        reply_markup=kb,
    )
    await callback.answer()


@router.callback_query(F.data.startswith("gtdelete:"))
async def game_task_delete_confirm(callback: types.CallbackQuery):
    try:
        task_id = int(callback.data.split(":", 1)[1])
    except ValueError:
        await callback.answer("Некорректное задание", show_alert=True)
        return
    task = await get_task(task_id)
    if task is None:
        await callback.answer("Задание не найдено", show_alert=True)
        return
    # T-14-12: re-check right here -- the button on the tasks screen could have been rendered
    # before a delegate's submission landed (same race class as the gate inside delete_task's
    # own SQL, defense in depth: this alert is the friendly early exit, the SQL NOT EXISTS
    # clause in delete_task is the one that actually cannot be bypassed).
    if await count_task_submissions(task_id) > 0:
        await callback.answer("У задания появились сдачи — теперь можно только в архив", show_alert=True)
        text, kb = await _game_tasks_screen()
        await callback.message.edit_text(text, parse_mode="HTML", reply_markup=kb)
        return
    name = html_module.escape(task_title(task))
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🗑 Да, удалить", callback_data=f"gtdelete_go:{task_id}")],
        [InlineKeyboardButton(text="← Отмена", callback_data="admin_game_tasks")],
    ])
    await callback.message.edit_text(
        f"🗑 <b>Удалить задание?</b>\n\n«{name}»\n\n"
        "Задание удалится безвозвратно: оно исчезнет из бота и из вкладок «Гейма»/«История "
        "сдач» при следующей пересборке. Это возможно только потому, что по нему нет ни одной "
        "сдачи.",
        parse_mode="HTML",
        reply_markup=kb,
    )
    await callback.answer()


@router.callback_query(F.data.startswith("gtdelete_go:"))
async def game_task_delete_go(callback: types.CallbackQuery):
    try:
        task_id = int(callback.data.split(":", 1)[1])
    except ValueError:
        await callback.answer("Некорректное задание", show_alert=True)
        return
    if await delete_task(task_id):
        _request_game_resync()
        await callback.answer("Задание удалено")
    else:
        # delete_task's own SQL-level NOT EXISTS gate refused -- a submission landed between
        # the confirm screen and this tap (T-14-12). No exception, no crash, same friendly text
        # as the pre-check in game_task_delete_confirm above.
        await callback.answer("У задания появились сдачи — теперь можно только в архив", show_alert=True)
    text, kb = await _game_tasks_screen()
    await callback.message.edit_text(text, parse_mode="HTML", reply_markup=kb)


@router.callback_query(F.data == "gtnew")
async def game_task_new(callback: types.CallbackQuery, state: FSMContext):
    await state.set_data({})  # explicit clear -- set_state alone does not clear get_data()
    # Quick 260819-gtl (CONTEXT.md decision 1): title is now the FIRST wizard step.
    prompt = await get_setting_typed("game_task_title_prompt")
    await callback.message.answer(prompt, reply_markup=get_cancel_kb())
    await state.set_state(GameTaskCreate.title)
    await callback.answer()


def _normalize_task_title(raw: str) -> str:
    """Shared by the creation wizard's title step and the point-edit «✏️ Название» flow
    (CONTEXT.md decision 1): collapses any run of whitespace (including a literal newline --
    "перенос → пробел") into a single space, strips, truncates to 60 chars. Returns "" for an
    all-whitespace input -- the caller re-prompts on that, this never raises."""
    return " ".join(raw.split())[:60]


# Cancel-mid-wizard, registered BEFORE the per-step handlers below (admin.router: first match
# wins) so «Отмена»/«/cancel» never falls through into a step handler and gets treated as task
# text/coins/a deadline string. Mirrors cancel_edit_setting (StateFilter(EditSetting) above).
@router.message(StateFilter(GameTaskCreate), Command("cancel"))
@router.message(StateFilter(GameTaskCreate), F.text == "Отмена")
async def cancel_game_task_create(message: types.Message, state: FSMContext):
    await state.set_state(None)
    await message.answer("Создание задания отменено.", reply_markup=ReplyKeyboardRemove())
    text, kb = await _game_tasks_screen()
    await message.answer(text, parse_mode="HTML", reply_markup=kb)


@router.message(GameTaskCreate.title)
async def game_task_title_step(message: types.Message, state: FSMContext):
    title = _normalize_task_title((message.text or "").strip())
    if not title:
        await message.answer("Название не может быть пустым. Введите название задания:")
        return
    await state.update_data(gt_title=title)
    if await _wizard_return_to_preview(message, state):
        return
    await message.answer(_PROMPT_TEXT, reply_markup=get_cancel_kb())
    await state.set_state(GameTaskCreate.text)


# Human-readable labels for GAME_PROOF_TYPES (D-08/CLAUDE.md «для людей, не для прогеров»):
# the manager taps a labeled button, never types a proof-type code.
_GAME_PROOF_LABELS = {
    "photo": "📷 Скриншот/фото",
    "pdf": "📄 PDF",
    "text": "✍️ Текст",
    "link": "🔗 Ссылка",
}


async def _game_task_category_kb() -> InlineKeyboardMarkup:
    """Phase 16 (16-03): button TEXT is the RU label (game_labels.category_label), the
    callback_data stays the raw code (`gtcat:{cat}`) -- DB storage format unchanged."""
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=await category_label(cat), callback_data=f"gtcat:{cat}")]
        for cat in GAME_CATEGORIES
    ])


def _proof_types_label(raw: str | None) -> str:
    """Phase 09.1 (A): proof_type is now possibly-multiple/possibly-empty (D-01, "можно
    несколько или ни одного") -- shared by the wizard confirm card and the moderation card."""
    codes = parse_proof_types(raw)
    if not codes:
        return "не важно"
    return " + ".join(_GAME_PROOF_LABELS[c] for c in codes)


def _game_task_proof_kb(selected: set[str]) -> InlineKeyboardMarkup:
    """Checkbox toggle keyboard (Phase 09.1 A, same shape as registration.py::_multi_kb) --
    an empty selection is legal (CONTEXT.md: «можно несколько или ни одного»), so unlike
    _multi_kb's own «Готово» there is no not-empty guard on the done callback below.
    `gtproof:{p}` callback_data name is UNCHANGED (already registered under moderate_game
    in the capability map) -- only its handler's behavior (toggle, not advance) changes."""
    rows = [
        [InlineKeyboardButton(
            text=f"{'✅ ' if p in selected else '▫️ '}{_GAME_PROOF_LABELS[p]}",
            callback_data=f"gtproof:{p}",
        )]
        for p in GAME_PROOF_TYPES
    ]
    rows.append([InlineKeyboardButton(text="Готово", callback_data="gtproof_done")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def _bound_task_city(admin_id: int) -> str | None:
    """Город, к которому привязан создатель задания, или None = «не привязан, ограничений
    нет». Суперадмин из config.ADMIN_IDS не ограничен никогда (D-12 фазы 8) — форма
    проверки положительная (structural test test_gate_no_legacy_admin_check_remains
    запрещает в этом файле обратную форму отрицания в кавычках "config.ADMIN_IDS").
    Идиома совпадает с admin_city_switch (:1927): после 09.1 (C) город менеджера —
    ПРАВО, а не фильтр экрана."""
    is_superadmin = admin_id in config.ADMIN_IDS
    if is_superadmin:
        return None
    bound = await get_staff_city(admin_id)
    return normalize_city(bound) if bound else None


async def _game_task_city_kb(admin_id: int) -> InlineKeyboardMarkup:
    """Phase 09.1 (B): "Кому задание?" step, only shown when the cities module is on
    (game_task_proof_done below). Same loop shape as registration.py::_city_fork_kb --
    "🌍 Все города" first, then one button per await enabled_cities().

    Phase 09.3 (CITY-08): the header's city is only HIGHLIGHTED (✅ prefix), never a lock --
    the actual gating for a bound manager stays entirely in _bound_task_city/
    game_task_city_step, untouched by this plan. header_code is read ONCE (WR-05); when the
    header is ALL_CITIES no city row matches it (compared against a real code, never the "*"
    marker) -- no separate branch needed, this falls out of the equality check for free."""
    header_code = await admin_selected_city(admin_id)
    rows = [[InlineKeyboardButton(text="🌍 Все города", callback_data="gttcity:all")]]
    for c in await enabled_cities():
        prefix = "✅ " if c["code"] == header_code else ""
        rows.append([InlineKeyboardButton(text=f"{prefix}{await city_label(c['code'])}", callback_data=f"gttcity:{c['code']}")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _game_task_photo_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="⏭ Пропустить", callback_data="gtphoto_skip")],
    ])


@router.message(GameTaskCreate.text)
async def game_task_text_step(message: types.Message, state: FSMContext):
    text = (message.text or "").strip()
    if not text:
        await message.answer(_PROMPT_TEXT_EMPTY)
        return
    await state.update_data(gt_text=text)
    if await _wizard_return_to_preview(message, state):
        return
    # Quick 260819-gtl (CONTEXT.md decision 4): photo step right after the description.
    prompt = await get_setting_typed("game_task_photo_prompt")
    await message.answer(prompt, reply_markup=_game_task_photo_kb())
    await state.set_state(GameTaskCreate.photo)


@router.message(GameTaskCreate.photo, F.photo)
async def game_task_photo_step(message: types.Message, state: FSMContext):
    file_id = message.photo[-1].file_id
    await state.update_data(gt_photo_file_id=file_id)
    if await _wizard_return_to_preview(message, state):
        return
    await message.answer(_PROMPT_CATEGORY, reply_markup=await _game_task_category_kb())
    await state.set_state(GameTaskCreate.category)


@router.callback_query(F.data == "gtphoto_skip")
async def game_task_photo_skip(callback: types.CallbackQuery, state: FSMContext):
    await state.update_data(gt_photo_file_id=None)
    if await _wizard_return_to_preview(callback.message, state):
        await callback.answer()
        return
    await callback.message.answer(_PROMPT_CATEGORY, reply_markup=await _game_task_category_kb())
    await state.set_state(GameTaskCreate.category)
    await callback.answer()


@router.message(GameTaskCreate.photo)
async def game_task_photo_step_invalid(message: types.Message, state: FSMContext):
    prompt = await get_setting_typed("game_task_photo_prompt")
    await message.answer(f"Не понял, пришли фото или нажми «⏭ Пропустить».\n\n{prompt}",
                          reply_markup=_game_task_photo_kb())


@router.callback_query(F.data.startswith("gtcat:"))
async def game_task_category_step(callback: types.CallbackQuery, state: FSMContext):
    cat = callback.data.split(":", 1)[1]
    if cat not in GAME_CATEGORIES:
        await callback.answer("Некорректная категория", show_alert=True)
        return
    await state.update_data(gt_category=cat)
    if await _wizard_return_to_preview(callback.message, state):
        await callback.answer()
        return
    await callback.message.answer(_PROMPT_COINS, reply_markup=get_cancel_kb())
    await state.set_state(GameTaskCreate.coins)
    await callback.answer()


@router.message(GameTaskCreate.coins)
async def game_task_coins_step(message: types.Message, state: FSMContext):
    value = _parse_positive_int(message.text)
    if value is None:
        await message.answer(_PROMPT_COINS_INVALID)
        return
    await state.update_data(gt_coins=value)
    if await _wizard_return_to_preview(message, state):
        return
    await state.update_data(gt_proof_types=[])
    await message.answer(
        "Что должен прислать делегат? Отметьте сколько угодно типов (или ни одного):",
        reply_markup=_game_task_proof_kb(set()),
    )
    await state.set_state(GameTaskCreate.proof_type)


@router.callback_query(F.data.startswith("gtproof:"))
async def game_task_proof_step(callback: types.CallbackQuery, state: FSMContext):
    """Phase 09.1 (A): a TOGGLE now, not an advance-to-next-step -- selection accumulates in
    `gt_proof_types` (state data), GameTaskCreate.proof_type itself is unchanged until
    «Готово» (gtproof_done below)."""
    proof = callback.data.split(":", 1)[1]
    if proof not in GAME_PROOF_TYPES:
        await callback.answer("Некорректный тип подтверждения", show_alert=True)
        return
    data = await state.get_data()
    selected = set(data.get("gt_proof_types", []))
    if proof in selected:
        selected.discard(proof)
    else:
        selected.add(proof)
    await state.update_data(gt_proof_types=sorted(selected))
    try:
        await callback.message.edit_reply_markup(reply_markup=_game_task_proof_kb(selected))
    except Exception:
        pass
    await callback.answer()


async def _game_task_deadline_prompt(target, state: FSMContext):
    """Shared by game_task_proof_done (module off), game_task_city_step (module on) and the
    final-step «✏️ Изменить → 📅 Дедлайн» re-entry -- same prompt/state either way. Phase 16
    (16-03): ONE message -- the prompt carries the inline preset keyboard (сегодня 23:59 / +3 /
    +7 / своя дата / отмена); the reply «Отмена» keyboard from the earlier free-text steps is
    still on screen, and typed «Отмена»/`/cancel` keep working via cancel_game_task_create."""
    await target.answer(
        _PROMPT_DEADLINE, reply_markup=_game_task_deadline_preset_kb("gtdeadline", "gtcancel"),
    )
    await state.set_state(GameTaskCreate.deadline)


@router.callback_query(F.data == "gtproof_done")
async def game_task_proof_done(callback: types.CallbackQuery, state: FSMContext):
    """CONTEXT.md A: an empty selection is legal here -- no not-empty guard, unlike
    registration.py's process_multi_done. Phase 09.1 (B): gated "Кому задание?" step --
    module off means zero new buttons/steps, straight to the pre-09.1 deadline prompt."""
    data = await state.get_data()
    codes = [p for p in GAME_PROOF_TYPES if p in set(data.get("gt_proof_types", []))]
    await state.update_data(gt_proof_type=",".join(codes))
    if not await cities_module_on():
        await state.update_data(gt_event_city=None, gt_city_step_shown=False)
        await _game_task_deadline_prompt(callback.message, state)
        await callback.answer()
        return
    bound = await _bound_task_city(callback.from_user.id)
    if bound:
        # CLAUDE.md: не показываем экран, который заведомо откажет. У привязанного менеджера
        # выбор ровно один, поэтому вопрос не задаётся — город проставляется сам, а строка
        # «Кому:» в карточке подтверждения (gt_city_step_shown) остаётся на месте, чтобы
        # человек видел, кому уйдёт задание.
        await state.update_data(
            gt_event_city=bound,
            gt_event_city_label=await city_label(bound),
            gt_city_step_shown=True,
        )
        await _game_task_deadline_prompt(callback.message, state)
        await callback.answer()
        return
    await state.update_data(gt_city_step_shown=True)
    await callback.message.answer("Кому задание?", reply_markup=await _game_task_city_kb(callback.from_user.id))
    await state.set_state(GameTaskCreate.city)
    await callback.answer()


@router.callback_query(F.data.startswith("gttcity:"))
async def game_task_city_step(callback: types.CallbackQuery, state: FSMContext):
    bound = await _bound_task_city(callback.from_user.id)
    code = callback.data.split(":", 1)[1]
    if bound and (code == "all" or normalize_city(code) != bound):
        # Кнопка из истории чата не истекает: экран «Кому задание?» мог быть отрисован до
        # того, как менеджера привязали к городу. «all» проверяется ОТДЕЛЬНОЙ веткой, а не
        # через normalize_city: неизвестный код normalize_city схлопывает в город по
        # умолчанию, поэтому у менеджера, привязанного к дефолтному городу, «all» иначе
        # прошло бы как «свой город» и создало задание всем городам сразу.
        await callback.answer(
            f"Задание можно создать только для вашего города — {await city_label(bound)}",
            show_alert=True,
        )
        return
    if code == "all":
        await state.update_data(gt_event_city=None, gt_event_city_label=None)
    else:
        if code not in city_codes():
            await callback.answer("Неизвестный город", show_alert=True)
            return
        await state.update_data(gt_event_city=code, gt_event_city_label=await city_label(code))
    await _game_task_deadline_prompt(callback.message, state)
    await callback.answer()


@router.message(GameTaskCreate.deadline)
async def game_task_deadline_step(message: types.Message, state: FSMContext):
    when = _parse_schedule_dt(message.text)
    if when is None:
        await message.answer("❌ Не понял дату. Формат: ДД.ММ.ГГГГ ЧЧ:ММ (напр. 01.07.2026 14:30)")
        return
    # TZFIX-260816: admin input is Moscow wall-clock — compare against Moscow, not the
    # container clock (UTC), or a past-MSK time can slip through as "future" and fire instantly.
    if when <= _now_moscow_naive():
        await message.answer(_DEADLINE_PAST)
        return
    await _finish_deadline_step(message, state, when)


@router.callback_query(F.data == "gtconfirm")
async def game_task_confirm(callback: types.CallbackQuery, state: FSMContext):
    data = await state.get_data()
    await create_task(
        text=data["gt_text"],
        category=data["gt_category"],
        coins=data["gt_coins"],
        proof_type=data["gt_proof_type"],
        deadline_at=data["gt_deadline"],
        created_by=callback.from_user.id,
        event_city=data.get("gt_event_city"),
        title=data.get("gt_title"),
        photo_file_id=data.get("gt_photo_file_id"),
    )
    _request_game_resync()  # Phase 09.1 (D, GAME-07): a new task is one of the 3 debounced triggers
    await state.set_state(None)
    await callback.answer("Задание создано")
    text, kb = await _game_tasks_screen()
    await callback.message.answer(text, parse_mode="HTML", reply_markup=kb)


@router.callback_query(F.data == "gtcancel")
async def game_task_create_cancel(callback: types.CallbackQuery, state: FSMContext):
    await state.set_state(None)
    await callback.answer("Отменено")
    text, kb = await _game_tasks_screen()
    await callback.message.answer(text, parse_mode="HTML", reply_markup=kb)


# ── Quick 260819-gtl (CONTEXT.md decisions 1/4): point-edit of an EXISTING task's title/
# photo — «✏️ Правка» button on the active-tasks screen. Not the GameTaskCreate wizard reused:
# this edits ONE field at a time via the small GameTaskEdit StatesGroup, no multi-step flow.
# Photo replace/remove is non-destructive (CONTEXT.md: no confirm step), unlike archive/delete.

async def _task_edit_screen(task: dict) -> tuple[str, InlineKeyboardMarkup]:
    """Point-edit card (Phase 16, 16-03, Экран 6): a compact summary of the task + EVERY
    action -- name / description / coins / deadline / photo (add|replace + remove), archive
    or return, delete (only with zero submissions, T-14-12 hint), «👁 Как видит делегат»
    (the delegate's own card render), back to the list. Archive/delete buttons point at the
    EXISTING confirm-gated callbacks (`gtarchive:`/`gtdelete:` -> *_go), return at the
    existing no-confirm `gtunarchive:`; the point-edit callbacks are non-destructive and
    need no confirm (quick 260819-gtl precedent)."""
    task_id = task["id"]
    title = html_module.escape(task_title(task))
    category = html_module.escape(await category_label(str(task["category"])))
    submissions = await count_task_submissions(task_id)
    lines = [
        f"✏️ <b>{title}</b>",
        f"{category} · {task['coins']}🪙 · до {_game_task_deadline_display(task)}",
        f"Обложка: {'есть' if task.get('photo_file_id') else 'нет'}",
        f"Сдач: {submissions}",
    ]
    if task.get("archived_at"):
        lines.append("В архиве — делегаты его не видят.")
    text = "\n".join(lines)

    rows = [
        [InlineKeyboardButton(text="✏️ Название", callback_data=f"gtedittitle:{task_id}")],
        [InlineKeyboardButton(text="✏️ Описание", callback_data=f"gteditdesc:{task_id}")],
        [
            InlineKeyboardButton(text="💰 Монеты", callback_data=f"gteditcoins:{task_id}"),
            InlineKeyboardButton(text="📅 Дедлайн", callback_data=f"gteditdeadline:{task_id}"),
        ],
    ]
    if task.get("photo_file_id"):
        rows.append([
            InlineKeyboardButton(text="📷 Заменить фото", callback_data=f"gteditphoto:{task_id}"),
            InlineKeyboardButton(text="🗑 Убрать фото", callback_data=f"gtremovephoto:{task_id}"),
        ])
    else:
        rows.append([InlineKeyboardButton(text="📷 Добавить фото", callback_data=f"gteditphoto:{task_id}")])
    if task.get("archived_at"):
        rows.append([InlineKeyboardButton(text="↩️ Вернуть", callback_data=f"gtunarchive:{task_id}")])
    else:
        rows.append([InlineKeyboardButton(text="🗄 В архив", callback_data=f"gtarchive:{task_id}")])
    if submissions == 0:
        rows.append([InlineKeyboardButton(text="🗑 Удалить", callback_data=f"gtdelete:{task_id}")])
    rows.append([InlineKeyboardButton(text="👁 Как видит делегат", callback_data=f"gtpreview:{task_id}")])
    rows.append([InlineKeyboardButton(text="← К заданиям", callback_data="admin_game_tasks")])
    return text, InlineKeyboardMarkup(inline_keyboard=rows)


@router.callback_query(F.data.startswith("gtedit:"))
async def game_task_edit_screen(callback: types.CallbackQuery, state: FSMContext):
    try:
        task_id = int(callback.data.split(":", 1)[1])
    except ValueError:
        await callback.answer("Некорректное задание", show_alert=True)
        return
    task = await get_task(task_id)
    if task is None:
        await callback.answer("Задание не найдено", show_alert=True)
        return
    await state.set_state(None)
    text, kb = await _task_edit_screen(task)
    await callback.message.edit_text(text, parse_mode="HTML", reply_markup=kb)
    await callback.answer()


@router.callback_query(F.data.startswith("gtedittitle:"))
async def game_task_edittitle_start(callback: types.CallbackQuery, state: FSMContext):
    try:
        task_id = int(callback.data.split(":", 1)[1])
    except ValueError:
        await callback.answer("Некорректное задание", show_alert=True)
        return
    task = await get_task(task_id)
    if task is None:
        await callback.answer("Задание не найдено", show_alert=True)
        return
    await state.set_data({"gte_task_id": task_id})
    prompt = await get_setting_typed("game_task_title_prompt")
    await callback.message.answer(prompt, reply_markup=get_cancel_kb())
    await state.set_state(GameTaskEdit.title)
    await callback.answer()


@router.callback_query(F.data.startswith("gteditphoto:"))
async def game_task_editphoto_start(callback: types.CallbackQuery, state: FSMContext):
    try:
        task_id = int(callback.data.split(":", 1)[1])
    except ValueError:
        await callback.answer("Некорректное задание", show_alert=True)
        return
    task = await get_task(task_id)
    if task is None:
        await callback.answer("Задание не найдено", show_alert=True)
        return
    await state.set_data({"gte_task_id": task_id})
    prompt = await get_setting_typed("game_task_photo_prompt")
    await callback.message.answer(prompt, reply_markup=get_cancel_kb())
    await state.set_state(GameTaskEdit.photo)
    await callback.answer()


@router.callback_query(F.data.startswith("gtremovephoto:"))
async def game_task_removephoto(callback: types.CallbackQuery):
    try:
        task_id = int(callback.data.split(":", 1)[1])
    except ValueError:
        await callback.answer("Некорректное задание", show_alert=True)
        return
    task = await get_task(task_id)
    if task is None:
        await callback.answer("Задание не найдено", show_alert=True)
        return
    await update_task_photo(task_id, None)
    _request_game_resync()
    await callback.answer("Фото убрано")
    task = await get_task(task_id)
    text, kb = await _task_edit_screen(task)
    await callback.message.edit_text(text, parse_mode="HTML", reply_markup=kb)


# Cancel-mid-edit, registered BEFORE the per-step handlers below (same "first match wins"
# precedent as cancel_game_task_create above).
@router.message(StateFilter(GameTaskEdit), Command("cancel"))
@router.message(StateFilter(GameTaskEdit), F.text == "Отмена")
async def cancel_game_task_edit(message: types.Message, state: FSMContext):
    await state.set_state(None)
    await message.answer("Правка отменена.", reply_markup=ReplyKeyboardRemove())
    text, kb = await _game_tasks_screen()
    await message.answer(text, parse_mode="HTML", reply_markup=kb)


@router.message(GameTaskEdit.title)
async def game_task_edittitle_step(message: types.Message, state: FSMContext):
    data = await state.get_data()
    task_id = data.get("gte_task_id")
    title = _normalize_task_title((message.text or "").strip())
    if not title:
        await message.answer("Название не может быть пустым. Введите название задания:")
        return
    if not await update_task_title(task_id, title):
        await message.answer("Задание не найдено — возможно, его уже удалили.")
        await state.set_state(None)
        return
    _request_game_resync()
    await state.set_state(None)
    await message.answer(
        f"Название обновлено: «{html_module.escape(title)}»", parse_mode="HTML",
        reply_markup=ReplyKeyboardRemove(),
    )
    text, kb = await _task_edit_screen(await get_task(task_id))
    await message.answer(text, parse_mode="HTML", reply_markup=kb)


@router.message(GameTaskEdit.photo, F.photo)
async def game_task_editphoto_step(message: types.Message, state: FSMContext):
    data = await state.get_data()
    task_id = data.get("gte_task_id")
    file_id = message.photo[-1].file_id
    if not await update_task_photo(task_id, file_id):
        await message.answer("Задание не найдено — возможно, его уже удалили.")
        await state.set_state(None)
        return
    _request_game_resync()
    await state.set_state(None)
    await message.answer("Фото обновлено.", reply_markup=ReplyKeyboardRemove())
    text, kb = await _task_edit_screen(await get_task(task_id))
    await message.answer(text, parse_mode="HTML", reply_markup=kb)


@router.message(GameTaskEdit.photo)
async def game_task_editphoto_invalid(message: types.Message, state: FSMContext):
    prompt = await get_setting_typed("game_task_photo_prompt")
    await message.answer(f"Не понял, пришли фото.\n\n{prompt}")


# ── Phase 14 (14-04, GAME-09): «🪙 Монеты вручную» — button wizard, «для людей» (CLAUDE.md):
# forward/@username person lookup (reuses _resolve_staff_input/roles_add_person's pattern
# verbatim, not a second parser), a card with the current balance, sign, amount (Task 2), then
# reason + confirm + ledger write + notification (Task 3). Lives entirely under moderate_game
# (handlers/admin_caps.py) -- T-14-16 (GAME-09's own threat register): monetary right, not
# registration-queue right.

def _coinsman_person_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="➕ Начислить", callback_data="coinsman_sign:plus")],
        [InlineKeyboardButton(text="➖ Списать", callback_data="coinsman_sign:minus")],
        [InlineKeyboardButton(text="← Отмена", callback_data="coinsman_cancel")],
    ])


async def _coinsman_card_text(user: dict, balance: int) -> str:
    """Card shown right after the person resolves -- ФИО, @username, город (only when the
    cities module is on -- same `cities_module_on()` gate roles screen's own city line uses),
    current balance. City resolution is best-effort: a manager may not be registered as a
    delegate themselves (same fallback shape as roles_add_person's display_name)."""
    tid = user.get("telegram_id")
    name = html_module.escape(str(user.get("full_name") or user.get("username") or tid))
    lines = [f"🪙 <b>{name}</b>"]
    username = user.get("username")
    if username:
        lines.append(html_module.escape(str(username)))
    if await cities_module_on():
        code = normalize_city(user.get("event_city"))
        lines.append(f"🏙 {html_module.escape(await city_label(code))}")
    lines.append(f"Текущий баланс: {balance}🪙")
    lines.append("")
    lines.append("Начисляем или списываем?")
    return "\n".join(lines)


@router.callback_query(F.data == "admin_coins_manual")
async def admin_coins_manual(callback: types.CallbackQuery, state: FSMContext):
    await state.set_data({})  # explicit clear -- set_state alone does not clear get_data()
    await state.set_state(CoinsManual.person)
    await callback.message.answer(
        "Кому меняем баланс? Перешлите сюда любое сообщение этого человека или пришлите его "
        "@username.",
        reply_markup=get_cancel_kb(),
    )
    await callback.answer()


# Cancel-mid-wizard, registered BEFORE the per-step handlers below (admin.router: first match
# wins) -- same WR-03-class guard as cancel_game_task_create/grev_step_cancel: «Отмена»/
# «/cancel» must never fall through into a step handler and get treated as a username/amount/
# reason. No ledger write happens on cancel at any step (T-14-18's sibling: cancel is always
# safe, only coinsman_confirm below ever calls add_coins).
@router.message(StateFilter(CoinsManual), Command("cancel"))
@router.message(StateFilter(CoinsManual), F.text == "Отмена")
async def coinsman_cancel_text(message: types.Message, state: FSMContext):
    await state.set_state(None)
    await message.answer("Отменено.", reply_markup=ReplyKeyboardRemove())
    text, kb = await _game_tasks_screen()
    await message.answer(text, parse_mode="HTML", reply_markup=kb)


@router.callback_query(F.data == "coinsman_cancel")
async def coinsman_cancel_cb(callback: types.CallbackQuery, state: FSMContext):
    await state.set_state(None)
    await callback.answer("Отменено")
    text, kb = await _game_tasks_screen()
    await callback.message.answer(text, parse_mode="HTML", reply_markup=kb)


@router.message(CoinsManual.person)
async def coinsman_person_step(message: types.Message, state: FSMContext):
    telegram_id, marker = _resolve_staff_input(message)
    if telegram_id is None and marker is not None and marker.startswith("@"):
        user = await get_user_by_username(marker)
        if user is None:
            await message.answer(
                f"Не нашёл {html_module.escape(marker)} среди зарегистрированных. Пришлите "
                "@username или перешлите его сообщение."
            )
            return  # T-14-17-adjacent: stay on the step, nothing recorded yet
        telegram_id = user["telegram_id"]
        marker = None

    if telegram_id is None:
        await message.answer(marker or _STAFF_INPUT_ERROR)
        return

    user = await get_user(telegram_id)
    if user is None:
        await message.answer(
            "Не нашёл такого человека среди зарегистрированных. Пришлите @username или "
            "перешлите его сообщение."
        )
        return

    await state.update_data(cm_user_id=telegram_id)
    balance = await get_balance(telegram_id)
    await message.answer(
        await _coinsman_card_text(user, balance), parse_mode="HTML",
        reply_markup=_coinsman_person_kb(),
    )


@router.callback_query(F.data.startswith("coinsman_sign:"))
async def coinsman_sign_step(callback: types.CallbackQuery, state: FSMContext):
    sign = callback.data.split(":", 1)[1]
    if sign not in ("plus", "minus"):
        await callback.answer("Неизвестная кнопка", show_alert=True)
        return
    data = await state.get_data()
    if data.get("cm_user_id") is None:
        # Stale card from an earlier/abandoned wizard run -- nothing to charge.
        await callback.answer("Сначала укажите получателя", show_alert=True)
        return
    await state.update_data(cm_sign=sign)
    await state.set_state(CoinsManual.amount)
    await callback.message.answer("Сколько монет? Пришлите число, например 5.", reply_markup=get_cancel_kb())
    await callback.answer()


@router.message(CoinsManual.amount)
async def coinsman_amount_step(message: types.Message, state: FSMContext):
    # Reuses _parse_coins_amount (not a second parser) -- only the sign is taken from it is
    # discarded, the sign was already picked via coinsman_sign:*; garbage/zero -> re-ask.
    parsed = _parse_coins_amount(message.text)
    if parsed is None or parsed == 0:
        await message.answer("Не понял число. Пришлите количество монет, например 5:")
        return
    value = abs(parsed)
    data = await state.get_data()
    sign = data.get("cm_sign")
    delta = value if sign == "plus" else -value
    await state.update_data(cm_delta=delta)
    await state.set_state(CoinsManual.reason)
    await message.answer("За что? Напишите причину коротко, например: за помощь на стенде.")


def _coinsman_confirm_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Подтвердить", callback_data="coinsman_confirm")],
        [InlineKeyboardButton(text="← Отмена", callback_data="coinsman_cancel")],
    ])


async def _coinsman_display_name(user_id: int) -> str:
    """Shared by the confirm card and the final report -- same fallback shape as
    roles_add_person's display_name (full_name -> username -> bare id), HTML-escaped."""
    user = await get_user(user_id)
    name = (user.get("full_name") or user.get("username")) if user else None
    return html_module.escape(str(name or user_id))


def _render_coinsman_confirm_card(recipient_name: str, delta: int, reason: str) -> str:
    """T-14-19: `reason` is a human-typed free text that will also be shown to the delegate
    with parse_mode="HTML" (_notify_manual_coins) -- escaped here too, not just once at the
    eventual delegate render (same double-escape-site precedent as
    _render_game_task_confirm_card's task text)."""
    return (
        "🪙 <b>Подтвердите операцию</b>\n\n"
        f"Кому: {recipient_name}\n"
        f"Сумма: {delta:+d} монет(ы)\n"
        f"Причина: {html_module.escape(reason)}\n\n"
        "Делегат получит сообщение с суммой, причиной и новым балансом."
    )


@router.message(CoinsManual.reason)
async def coinsman_reason_step(message: types.Message, state: FSMContext):
    raw_reason = message.text or ""
    if not raw_reason.strip():
        await message.answer(
            "Причина обязательна: журнал монет должен отвечать на вопрос «кто, кому, за что». "
            "Напишите коротко, например: за помощь на стенде."
        )
        return
    await state.update_data(cm_reason=raw_reason)  # raw text, not trimmed (Task 3 action note)
    data = await state.get_data()
    name = await _coinsman_display_name(data.get("cm_user_id"))
    await message.answer(
        _render_coinsman_confirm_card(name, data.get("cm_delta"), raw_reason),
        parse_mode="HTML",
        reply_markup=_coinsman_confirm_kb(),
    )


@router.callback_query(F.data == "coinsman_confirm")
async def coinsman_confirm(callback: types.CallbackQuery, state: FSMContext):
    data = await state.get_data()
    user_id = data.get("cm_user_id")
    delta = data.get("cm_delta")
    reason = data.get("cm_reason")
    # T-14-18: a stale confirm screen (state already cleared by an earlier tap, or an
    # abandoned/cancelled wizard) must not write a second ledger row -- same class of guard as
    # the archive/delete race gate (14-01/14-03).
    if user_id is None or delta is None or not reason:
        await callback.answer("Операция уже завершена или отменена", show_alert=True)
        return

    # Порядок важен (Task 3 action note): запись в журнал СНАЧАЛА, уведомление -- потом. Сбой
    # уведомления (делегат заблокировал бота, T-14-20) не должен быть причиной, по которой
    # операция выглядит несостоявшейся.
    await add_coins(user_id, delta, reason=reason, changed_by=callback.from_user.id, source="manual")
    await state.set_state(None)
    await state.set_data({})
    _request_game_resync()  # Phase 09.1 (D, GAME-07): a coin edit is one of the 3 debounced triggers
    balance = await get_balance(user_id)
    notified = await _notify_manual_coins(callback.bot, user_id, delta, reason, balance)
    notify_suffix = "" if notified else " (делегат не получил уведомление)"

    name = await _coinsman_display_name(user_id)
    sign_word = "начислено" if delta >= 0 else "списано"
    await callback.answer("Готово")
    await callback.message.answer(
        f"🪙 {sign_word} {abs(delta)} монет(ы) для {name}.\n"
        f"Новый баланс: <b>{balance}</b>.{notify_suffix}",
        parse_mode="HTML",
    )


# ── Phase 14 (14-05, GAME-09): «📜 Журнал монет» — paginated screen + full CSV export ────────
#
# The screen shows ONLY `source = 'manual'` rows (never task-award credits or pre-Phase-14
# legacy rows -- Pitfall 6, T-14-25); the CSV button (coinsjrn_csv) exports the FULL journal
# unfiltered, with the type spelled out in RU (never the raw `source` code). 10 rows per page
# -- CLAUDE.md: a 1000+-row list must never render in one message (T-14-24).

async def _coins_journal_screen(offset: int = 0) -> tuple[str, InlineKeyboardMarkup]:
    """«Функция возвращает (text, kb)» idiom (same shape as `_game_tasks_screen`) -- every
    re-render call site (open + page nav) stays byte-identical in format."""
    limit = 10
    total = await count_manual_coin_entries()
    rows = await list_manual_coin_entries(limit=10, offset=offset)

    lines = ["📜 <b>Журнал монет</b>"]
    if total == 0:
        lines.append("")
        lines.append("Ручных операций пока не было.")
    else:
        total_pages = (total + limit - 1) // limit
        current_page = offset // limit + 1
        lines.append(f"Страница {current_page} из {total_pages}")
        lines.append("")
        for row in rows:
            try:
                when = datetime.strptime(row["timestamp"], "%Y-%m-%d %H:%M:%S").strftime("%d.%m.%Y %H:%M")
            except (TypeError, ValueError):
                when = str(row.get("timestamp") or "—")
            recipient = html_module.escape(str(
                row.get("user_full_name") or row.get("user_username") or row.get("user_id")
            ))
            delta = row.get("delta") or 0
            sign = f"+{delta}" if delta >= 0 else str(delta)
            reason = html_module.escape(str(row.get("reason") or "—"))
            changed_by = row.get("changed_by")
            changer = await _coinsman_display_name(changed_by) if changed_by is not None else "—"
            lines.append(f"{when} · {recipient} · {sign}🪙 · {reason}")
            lines.append(f"изменил: {changer}")
    text = "\n".join(lines)

    buttons: list[list[InlineKeyboardButton]] = []
    nav_row: list[InlineKeyboardButton] = []
    if offset > 0:
        nav_row.append(InlineKeyboardButton(
            text="← Раньше", callback_data=f"coinsjrn_page:{max(0, offset - limit)}",
        ))
    if offset + limit < total:
        nav_row.append(InlineKeyboardButton(
            text="Позже →", callback_data=f"coinsjrn_page:{offset + limit}",
        ))
    if nav_row:
        buttons.append(nav_row)
    buttons.append([InlineKeyboardButton(text="📄 Выгрузить журнал (CSV)", callback_data="coinsjrn_csv")])
    buttons.append([InlineKeyboardButton(text="← Назад", callback_data="admin_menu")])
    return text, InlineKeyboardMarkup(inline_keyboard=buttons)


@router.callback_query(F.data == "admin_coins_journal")
async def admin_coins_journal(callback: types.CallbackQuery):
    text, kb = await _coins_journal_screen(offset=0)
    await callback.message.answer(text, parse_mode="HTML", reply_markup=kb)
    await callback.answer()


@router.callback_query(F.data.startswith("coinsjrn_page:"))
async def coinsjrn_page(callback: types.CallbackQuery):
    raw = callback.data.split(":", 1)[1]
    try:
        offset = int(raw)
    except ValueError:
        offset = -1
    if offset < 0:
        await callback.answer("Некорректная страница", show_alert=True)
        return
    text, kb = await _coins_journal_screen(offset=offset)
    await callback.message.edit_text(text, parse_mode="HTML", reply_markup=kb)
    await callback.answer()


@router.callback_query(F.data == "coinsjrn_csv")
async def coinsjrn_csv(callback: types.CallbackQuery):
    headers, rows = await export_coins_journal_csv()
    output = io.StringIO()
    writer = csv.writer(output, delimiter=';', quotechar='"', quoting=csv.QUOTE_MINIMAL)
    writer.writerow(headers)
    writer.writerows(rows)
    file_bytes = output.getvalue().encode('utf-8-sig')
    document = BufferedInputFile(file_bytes, filename="coins_journal.csv")
    await callback.message.answer_document(document, caption="Журнал монет — все операции")
    await callback.answer()


# ── Phase 9 (GAME-02/03, wave 4, 09-04): «🎮 Проверка заданий» — moderation queue ───────────
#
# Same tinder-pattern/pagination as «Заявки»/«Чеки» above (D-01, CLAUDE.md: 1000+ submissions
# must never be one message per row). `claim_submission` is the atomic single-row UPDATE (same
# idiom as `approve_user_atomic`/`claim_question`) — `add_coins` is called ONLY from the branch
# where it returned True, so two managers racing to approve the same card can never both credit
# coins (T-09-11).

async def _get_submission_and_task(submission_id: int) -> tuple[dict | None, dict | None]:
    """`get_submission()` (09-01) is a plain, un-joined SELECT on game_submissions — unlike
    `get_pending_submissions()`, it carries no task_text/task_coins. grev_approve/grev_reject/
    grev_approve_custom_start act on a submission id straight from callback_data with no task
    context on hand, so both rows are needed to notify the delegate and credit the right
    amount. Rule 3 (blocking-issue autofix, not a new db.py accessor): combines the two
    already-locked 09-01 accessors (`get_submission` + `get_task`) rather than changing
    `get_submission`'s contract or adding a new one."""
    submission = await get_submission(submission_id)
    if submission is None:
        return None, None
    task = await get_task(submission["task_id"])
    return submission, task


# CR-01 (09.1-REVIEW.md): hard ceilings so a submission card can never blow past Telegram's
# sendMessage limit (4096 chars). _CARD_PART_MAX truncates one rendered part; _CARD_MAX
# truncates the whole assembled card as a last-resort backstop.
_CARD_PART_MAX = 500
_CARD_MAX = 3800

# CR-02 (09.1-REVIEW.md): sendMediaGroup accepts 2-10 items -- 11+ raises and used to drop the
# whole group silently. MEDIA_GROUP_MAX chunks the resend; _MEDIA_CAPTION_MAX (Telegram's own
# caption limit is 1024) truncates a caption with a margin, since captions come straight from
# unvalidated delegate input.
MEDIA_GROUP_MAX = 10
_MEDIA_CAPTION_MAX = 1000


def _render_submission_card(row: dict, position: int, total: int, parts: list[dict] | None = None,
                             city_labels: tuple[str, str] | None = None,
                             attempt: tuple[int, int] | None = None) -> str:
    """HTML card for one pending submission; all free-text (task text, submitter name) escaped
    — T-09-12: this is the FIRST render of delegate-supplied content to a manager.

    Phase 09.1 (A): `parts` defaults to None so every pre-existing call site/test keeps the
    single content_type/content rendering byte-for-byte. Pass `parts` (from
    `get_submission_parts_or_legacy`) to render every part of a free-form submission instead.

    Phase 09.1 (B): `city_labels` (delegate_label, task_label) defaults to None so this stays
    byte-identical when the cities module is off. This function is synchronous and cannot
    itself call cities_module_on()/city_label() -- the caller (_show_current_submission)
    resolves both labels ONCE and hands them down, same "resolve once" shape the confirm
    card uses for gt_city_step_shown.

    Phase 14 (14-03, GAME-10): `attempt` (K, N) defaults to None so this stays byte-identical
    when `game_resubmit_limit` is unset/0 -- this function is SYNCHRONOUS and cannot itself
    read the registry or count rejected submissions, same "resolve once, caller hands down"
    shape as `city_labels` -- the caller (`_show_current_submission`) does the async resolve."""
    def esc(v):
        return html_module.escape(str(v)) if v not in (None, "", "-") else None

    header = f"🎮 <b>Сдача {position}/{total}</b>"
    # Quick 260819-gtl (CONTEXT.md decision 6): "Задание: <title>" line, not a raw text
    # preview -- task photo is deliberately NOT duplicated here (the submission's own parts
    # are what the manager needs to see).
    title = task_title({"title": row.get("task_title"), "text": row.get("task_text")})
    lines = [header, "", f"Задание: {esc(title) or '—'}"]
    lines.append(f"Категория: {esc(row.get('task_category')) or '—'}")
    lines.append(f"Предложено: {row.get('task_coins')}🪙")
    name = esc(row.get("user_full_name")) or "—"
    uname = esc(row.get("user_username"))
    lines.append(f"👤 {name}" + (f" ({uname})" if uname else ""))
    if city_labels is not None:
        delegate_label, task_label = city_labels
        lines.append(f"🏙 Город делегата: {esc(delegate_label) or '—'}")
        lines.append(f"🎯 Кому задание: {esc(task_label) or '—'}")
    proof_label = _proof_types_label(row.get("task_proof_type"))
    lines.append(f"Тип подтверждения: {proof_label}")
    if parts is None:
        content_type = row.get("content_type")
        if content_type in ("text", "link"):
            lines.append(f"Содержимое: {esc(row.get('content')) or '—'}")
        elif content_type in ("photo", "pdf"):
            lines.append("Содержимое: см. файл ниже")
    elif not parts:
        lines.append("Содержимое: —")
    else:
        lines.append("Содержимое:")
        for part in parts:
            caption = esc(part.get("caption"))
            if part.get("kind") in ("text", "link"):
                # CR-01 «Важно 1»: truncate the RAW content, escape after -- slicing an
                # already-escaped string can split an HTML entity in half (&amp; -> &am) and
                # reproduce the exact parse_mode="HTML" failure this truncation defends against.
                raw = str(part.get("content") or "")
                if len(raw) > _CARD_PART_MAX:
                    raw = raw[:_CARD_PART_MAX] + "…"
                lines.append(f"• {esc(raw) or '—'}")
            else:
                # T-09-12/backward-compat: same "см. файл ниже" wording the pre-09.1 single-
                # content_type render used (tests/test_gamification_review_phase9.py asserts
                # this literal string and is NOT modified by this plan).
                tail = f" ({caption})" if caption else ""
                lines.append(f"• см. файл ниже{tail}")
    # Phase 14 (14-03, GAME-08): task_archived_at is exposed on the queue rows by plan 14-01.
    # The submission itself is untouched by the archive — the manager still has to decide it.
    if row.get("task_archived_at"):
        lines.append("🗄 Задание в архиве — сдачу всё равно нужно решить")
    # Phase 14 (14-03, GAME-10): resolved once by the caller (limit==0/None -> attempt stays
    # None -> this line never appears, byte-identical to pre-phase behavior).
    if attempt is not None:
        k, n = attempt
        lines.append(f"🔁 Попытка {k} из {n}")
    # A-05 (созвон 13.08): дедлайн мягкий, бот сдачу принял — единственный ограничитель здесь
    # человек. Просрочка нигде не хранится, только вычисляется здесь при каждом рендере.
    submitted_at = row.get("submitted_at")
    deadline_at = row.get("task_deadline_at")
    if submitted_at and deadline_at and str(submitted_at) > str(deadline_at):
        lines.append(f"⏰ Сдано после дедлайна ({deadline_at}) — решение за вами")
    return "\n".join(lines)


def _submission_card_kb(submission_id: int, coins: int) -> InlineKeyboardMarkup:
    # NOTE: the plan's own <action> block names this function's second parameter `total`
    # (copy-pasted from `_appr_card_kb(tid, has_resume, total)`), but the button label needs
    # the task's coin default, and the plan explicitly drops the "Одобрить все" row that
    # `total` would have fed — `total` is unused dead weight in that literal spec. Named
    # `coins` here to match what the button text actually needs (documented in SUMMARY as a
    # plan-authoring inconsistency, same class as 09-02's grep-count note / 09-03's stale test
    # name, not a functional deviation).
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text=f"✅ Одобрить {coins}🪙", callback_data=f"grev_approve:{submission_id}"),
            InlineKeyboardButton(text="✏️ Другая сумма", callback_data=f"grev_approve_custom:{submission_id}"),
        ],
        [
            InlineKeyboardButton(text="❌ Отклонить", callback_data=f"grev_reject:{submission_id}"),
            InlineKeyboardButton(text="⏭ Пропустить", callback_data=f"grev_skip:{submission_id}"),
        ],
    ])


async def _show_current_submission(target: types.Message, state: FSMContext):
    """Render the oldest non-skipped pending submission (DB-driven, restart-safe) — byte-for-
    byte the same batched-pagination loop as `_show_current_card` (limit=50 per batch, CLAUDE.md:
    never one query for "all at once")."""
    admin_id = state.key.user_id
    # Phase 09.1 (B, T-091-06): scope resolved from the CALLING admin's id via the same
    # resolver the applications/receipts queues use -- never from callback_data. Module off
    # -> scope is None -> get_pending_submissions_count/get_pending_submissions behave exactly
    # as before 09.1.
    scope = await _admin_city_scope(admin_id)
    skipped = set((await state.get_data()).get("grev_skipped", []))
    total = await get_pending_submissions_count(city_scope=scope)
    offset = 0
    visible: list[dict] = []
    while not visible and offset < total:
        batch = await get_pending_submissions(limit=50, offset=offset, city_scope=scope)
        if not batch:
            break
        visible = [s for s in batch if s["id"] not in skipped]
        offset += len(batch)
    if not visible:
        await target.answer("Сдач на проверке нет.", reply_markup=await admin_keyboard_for(admin_id))
        return
    current = visible[0]
    position = min(len(skipped) + 1, total)
    # Phase 09.1 (A): parts=[] for a pre-migration row is impossible here -- get_submission_
    # parts_or_legacy synthesizes exactly one part from content/content_type unless content is
    # itself empty (submission-призрак), so this stays a strict superset of the old behavior.
    parts = await get_submission_parts_or_legacy(current)
    # Phase 09.1 (B): city_labels stays None (no new lines) unless the module is on.
    city_labels = None
    if await cities_module_on():
        delegate_label = await city_label(normalize_city(current.get("user_event_city")))
        task_code = current.get("task_event_city")
        task_label = await city_label(task_code) if task_code else "🌍 Все города"
        city_labels = (delegate_label, task_label)
    # Phase 14 (14-03, GAME-10): «попытка K из N» -- resolved ONCE here (the card renderer is
    # synchronous), same shape as city_labels above. limit==0/None (falsy) -> attempt stays
    # None -> _render_submission_card renders byte-identical to pre-phase.
    attempt = None
    limit = await get_setting_typed("game_resubmit_limit")
    if limit:
        rejected = await count_rejected_submissions(current["task_id"], current["user_id"])
        attempt = (rejected + 1, limit)
    card = _render_submission_card(current, position, total, parts, city_labels, attempt)
    if len(card) > _CARD_MAX:
        # CR-01 «Важно 2»: a hard slice can leave a truncated HTML entity tail (the only tag
        # in the card is <b> at position 0, well before any slice point at _CARD_MAX chars).
        card = re.sub(r"&[a-zA-Z#0-9]*$", "", card[:_CARD_MAX]) + "\n…(обрезано)"
    try:
        await target.answer(
            card,
            parse_mode="HTML",
            reply_markup=_submission_card_kb(current["id"], current["task_coins"]),
        )
    except Exception as e:
        logger.error(f"submission card render failed, submission={current['id']}: {e}")
        try:
            await target.answer(
                f"🎮 Сдача {position}/{total} — содержимое слишком длинное, показать не смог.",
                reply_markup=_submission_card_kb(current["id"], current["task_coins"]),
            )
        except Exception as e2:
            logger.error(f"submission card fallback send failed, submission={current['id']}: {e2}")
    # Модератор видит все части пачкой: consecutive photo parts group into ONE
    # send_media_group, each document part resends individually, text/link parts are already
    # in the card body above. A single photo still goes through answer_photo (Telegram
    # rejects a one-element media group) -- byte-identical to the pre-09.1 legacy path, which
    # is exactly why the old review-phase9 tests stay green without being touched.
    bot = getattr(target, "bot", None)
    resend_failures = 0
    i = 0
    while i < len(parts):
        part = parts[i]
        kind = part.get("kind")
        if kind == "photo":
            group = [part]
            j = i + 1
            while j < len(parts) and parts[j].get("kind") == "photo":
                group.append(parts[j])
                j += 1
            # CR-02: chunk to MEDIA_GROUP_MAX -- sendMediaGroup accepts 2-10 items, 11+ raises
            # and previously silently dropped the whole group. Each chunk's failure is
            # independent: chunk 3 failing must not swallow chunks already shown.
            for chunk_start in range(0, len(group), MEDIA_GROUP_MAX):
                chunk = group[chunk_start:chunk_start + MEDIA_GROUP_MAX]
                try:
                    if len(chunk) == 1 or bot is None:
                        for p in chunk:
                            await target.answer_photo(p["content"])
                    else:
                        media = [
                            types.InputMediaPhoto(
                                media=p["content"],
                                caption=(p.get("caption") or None) and p["caption"][:_MEDIA_CAPTION_MAX],
                            )
                            for p in chunk
                        ]
                        await bot.send_media_group(admin_id, media=media)
                except Exception as e:
                    logger.error(f"Failed to resend submission content, submission={current['id']}: {e}")
                    resend_failures += 1
            i = j
        elif kind == "document":
            try:
                await target.answer_document(part["content"])
            except Exception as e:
                logger.error(f"Failed to resend submission content, submission={current['id']}: {e}")
                resend_failures += 1
            i += 1
        else:
            i += 1
    if resend_failures:
        # CR-02: one visible warning per render, not one per failed chunk -- the moderator
        # must know evidence was hidden, without getting spammed.
        try:
            await target.answer("⚠️ Часть вложений показать не удалось — см. лог сервера.")
        except Exception as e:
            logger.error(f"submission attachment warning send failed, submission={current['id']}: {e}")


@router.callback_query(F.data == "admin_game_review")
async def show_game_review(callback: types.CallbackQuery, state: FSMContext):
    await state.update_data(grev_skipped=[])  # session-only skip set, same as appr_skipped (D-07)
    await callback.answer()
    await _show_current_submission(callback.message, state)


# CR-03 (09.1-REVIEW.md): deliberately NOT gated by `_submission_out_of_scope`. This handler
# writes only to the caller's own FSM `grev_skipped` list and re-renders the caller's own
# queue -- no DB row, no coins, no delegate notification. The queue itself is already scoped
# by `city_scope` (get_pending_submissions), so another city's id cannot even appear in it;
# "hiding" a foreign id in one's own skip set costs nothing.
@router.callback_query(F.data.startswith("grev_skip:"))
async def grev_skip(callback: types.CallbackQuery, state: FSMContext):
    try:
        sid = int(callback.data.split(":", 1)[1])
    except (ValueError, IndexError):
        sid = None
    data = await state.get_data()
    skipped = list(data.get("grev_skipped", []))
    if sid is not None and sid not in skipped:
        skipped.append(sid)
    await state.update_data(grev_skipped=skipped)
    try:
        await callback.message.delete()
    except Exception:
        pass
    await callback.answer("Пропущено")
    await _show_current_submission(callback.message, state)


@router.callback_query(F.data.startswith("grev_approve:"))
async def grev_approve(callback: types.CallbackQuery, state: FSMContext):
    try:
        sid = int(callback.data.split(":", 1)[1])
    except (ValueError, IndexError):
        await callback.answer("Некорректная сдача", show_alert=True)
        return
    submission, task = await _get_submission_and_task(sid)
    if submission is None or task is None:
        await callback.answer("Не найдено", show_alert=True)
        return
    if await _submission_out_of_scope(callback.from_user.id, submission):
        await callback.answer(_SUBMISSION_OUT_OF_SCOPE_ALERT, show_alert=True)
        return
    coins = task["coins"]
    # T-09-11: add_coins is called ONLY from this branch, after claim_submission's atomic
    # UPDATE ... WHERE status = 'pending' actually flipped the row — a concurrent second tap
    # on the same card gets won=False and never reaches add_coins (exactly one credit, ever).
    won = await claim_submission(sid, callback.from_user.id, "approved", coins_awarded=coins)
    if won:
        await add_coins(
            submission["user_id"], coins,
            reason=f"Задание: {str(task['text'])[:60]}",
            changed_by=callback.from_user.id,
            source="task",
        )
        # Phase 09.1 (D, GAME-07): a moderator decision is one of the 3 debounced triggers --
        # only in the branch where claim_submission actually won the race (T-091-15/20-in-a-
        # row-collapse-to-one still holds: this fires once per real decision, not per tap).
        _request_game_resync()
        try:
            await callback.bot.send_message(
                submission["user_id"],
                f"✅ Задание «{html_module.escape(str(task['text']))}» одобрено! +{coins}🪙",
                parse_mode="HTML",
            )
        except Exception as e:
            logger.error(f"Failed to notify user {submission['user_id']} of task approval: {e}")
        await callback.answer("Одобрено")
    else:
        await callback.answer("Уже обработано")
    try:
        await callback.message.delete()
    except Exception:
        pass
    await _show_current_submission(callback.message, state)


@router.callback_query(F.data.startswith("grev_approve_custom:"))
async def grev_approve_custom_start(callback: types.CallbackQuery, state: FSMContext):
    try:
        sid = int(callback.data.split(":", 1)[1])
    except (ValueError, IndexError):
        await callback.answer("Некорректная сдача", show_alert=True)
        return
    submission, task = await _get_submission_and_task(sid)
    if submission is None or task is None:
        await callback.answer("Не найдено", show_alert=True)
        return
    if await _submission_out_of_scope(callback.from_user.id, submission):
        await callback.answer(_SUBMISSION_OUT_OF_SCOPE_ALERT, show_alert=True)
        return
    await state.update_data(grev_submission_id=sid)
    await callback.message.answer(
        f"Сколько монет начислить? (по умолчанию {task['coins']}):",
        reply_markup=get_cancel_kb(),
    )
    await state.set_state(GameReview.approve_amount)
    await callback.answer()


@router.callback_query(F.data.startswith("grev_reject:"))
async def grev_reject_start(callback: types.CallbackQuery, state: FSMContext):
    try:
        sid = int(callback.data.split(":", 1)[1])
    except (ValueError, IndexError):
        await callback.answer("Некорректная сдача", show_alert=True)
        return
    submission, task = await _get_submission_and_task(sid)
    if submission is None or task is None:
        await callback.answer("Не найдено", show_alert=True)
        return
    if await _submission_out_of_scope(callback.from_user.id, submission):
        await callback.answer(_SUBMISSION_OUT_OF_SCOPE_ALERT, show_alert=True)
        return
    await state.update_data(grev_submission_id=sid)
    await callback.message.answer("Причина отклонения (или «-», если без причины):", reply_markup=get_cancel_kb())
    await state.set_state(GameReview.reject_reason)
    await callback.answer()


# WR-03-class guard (mirrors appr_reject_cancel): registered BEFORE the two per-step catch-all
# handlers below (admin.router: first match wins) so «Отмена»/any stray «/command» typed mid-
# amount-entry or mid-reason-entry is intercepted here, not swallowed as the amount/reason
# itself (T-09-14).
@router.message(GameReview.approve_amount, F.text.in_({"Отмена"}) | F.text.startswith("/"))
@router.message(GameReview.reject_reason, F.text.in_({"Отмена"}) | F.text.startswith("/"))
async def grev_step_cancel(message: types.Message, state: FSMContext):
    await state.set_state(None)
    text = (message.text or "").strip()
    if text not in ("Отмена", "/cancel"):
        note = "Действие отменено (введена команда). При необходимости повторите её."
    else:
        note = "Действие отменено."
    await message.answer(note, reply_markup=ReplyKeyboardRemove())
    await _show_current_submission(message, state)


# CR-03: `grev_approve_amount_step` / `grev_reject_reason` (below) do NOT get a second
# `_submission_out_of_scope` gate. `grev_submission_id` in state is set ONLY inside the two
# already-gated starting handlers (`grev_approve_custom_start`/`grev_reject_start`) -- there is
# no way to reach this state with an out-of-scope id already loaded.
@router.message(GameReview.approve_amount)
async def grev_approve_amount_step(message: types.Message, state: FSMContext):
    amount = _parse_positive_int(message.text)
    if amount is None:
        await message.answer("Введите положительное целое число:")
        return
    data = await state.get_data()
    sid = data.get("grev_submission_id")
    submission, task = await _get_submission_and_task(sid) if sid is not None else (None, None)
    if submission is None or task is None:
        await state.set_state(None)
        await message.answer("Сдача не найдена.", reply_markup=ReplyKeyboardRemove())
        await _show_current_submission(message, state)
        return
    won = await claim_submission(sid, message.from_user.id, "approved", coins_awarded=amount)
    if won:
        await add_coins(
            submission["user_id"], amount,
            reason=f"Задание: {str(task['text'])[:60]}",
            changed_by=message.from_user.id,
            source="task",
        )
        _request_game_resync()  # Phase 09.1 (D, GAME-07): same trigger as grev_approve
        try:
            await message.bot.send_message(
                submission["user_id"],
                f"✅ Задание «{html_module.escape(str(task['text']))}» одобрено! +{amount}🪙",
                parse_mode="HTML",
            )
        except Exception as e:
            logger.error(f"Failed to notify user {submission['user_id']} of task approval: {e}")
        await message.answer("Одобрено.", reply_markup=ReplyKeyboardRemove())
    else:
        await message.answer("Уже обработано.", reply_markup=ReplyKeyboardRemove())
    await state.set_state(None)
    await _show_current_submission(message, state)


@router.message(GameReview.reject_reason)
async def grev_reject_reason(message: types.Message, state: FSMContext):
    reason = (message.text or "-").strip()
    data = await state.get_data()
    sid = data.get("grev_submission_id")
    submission, task = await _get_submission_and_task(sid) if sid is not None else (None, None)
    won = False
    if sid is not None:
        won = await claim_submission(
            sid, message.from_user.id, "rejected",
            reject_reason=None if reason == "-" else reason,
        )
    if won:
        # Phase 09.1 (D, GAME-07): a moderator decision is one of the 3 debounced triggers --
        # only when claim_submission actually won the race, same rule as grev_approve.
        _request_game_resync()
    if won and submission is not None and task is not None:
        user_msg = f"❌ Задание «{html_module.escape(str(task['text']))}» отклонено."
        if reason != "-":  # A-02: причина доходит до делегата, только если менеджер её написал
            user_msg += f"\n\nПричина: {html_module.escape(reason)}"
        try:
            await message.bot.send_message(submission["user_id"], user_msg, parse_mode="HTML")
        except Exception as e:
            logger.error(f"Failed to notify user {submission['user_id']} of task rejection: {e}")
        await message.answer("Сдача отклонена.", reply_markup=ReplyKeyboardRemove())
    else:
        await message.answer("Уже обработано.", reply_markup=ReplyKeyboardRemove())
    await state.set_state(None)
    await _show_current_submission(message, state)


# ── 09-05 (GAME-01..03, D-05): «🔄 Таблица геймы» — full-rebuild sync of two named tabs ──────
# Deliberately the SAME `sync_named_worksheet` full clear+rewrite already used by
# `admin_export_incomplete`/the party/incomplete named tabs (services/sheets.py, unmodified by
# this plan) -- a manual button a manager taps a couple times a day, not a background job
# (CLAUDE.md: keep the existing stack, no APScheduler for this). TWO independent calls per
# T-09-16: a failure on one tab must not swallow the other, and must be reported by text, not
# silently dropped.

_GAME_HISTORY_STATUS_LABELS = {"pending": "Ожидает", "approved": "Одобрено", "rejected": "Отклонено"}


def _build_game_matrix(tasks: list[dict], submissions: list[dict]) -> tuple[list[str], list[list]]:
    """Pure builder for the «Гейма» matrix tab (участники x задания). `tasks` must already be
    sorted oldest-first (created_at ASC) by the caller -- the earliest-created task lands in the
    leftmost column. Only participants with at least one submission become a row (CLAUDE.md:
    1000+ registered users with zero submissions must never turn into 1000+ empty rows).

    Phase 09.1 (B, CONTEXT.md "Уточнение (ночь 17→18.08…)"): this tab is NOT cut by the
    viewing admin's city_scope -- it is rebuilt by a background debounce (plan 09.1-04) with no
    admin identity, and `sync_named_worksheet` clears the whole sheet on every rebuild, so a
    partial rebuild would erase other cities' rows. "Уважает city_scope" is implemented here as
    a "Город" COLUMN (filterable in Sheets itself), not a row filter -- only the LIVE «🎮
    Проверка заданий» queue above is actually scoped."""
    # Phase 14 (14-03, GAME-08): archived tasks stay in the matrix with a «🗄 » column-header
    # prefix — added BEFORE the 40-char truncation so the marker can never be sliced off.
    # Quick 260819-gtl (CONTEXT.md decision 8): column uses task_title(), same fallback.
    def _matrix_col_header(t: dict) -> str:
        prefix = "🗄 " if t.get("archived_at") else ""
        return (prefix + task_title(t))[:40]

    headers = ["telegram_id", "ФИО", "Город", "Юзернейм"] + [_matrix_col_header(t) for t in tasks]

    subs_by_user: dict[int, list[dict]] = {}
    for s in submissions:
        subs_by_user.setdefault(s["user_id"], []).append(s)

    rows: list[list] = []
    for user_id, user_subs in subs_by_user.items():
        sample = user_subs[-1]
        row = [user_id, sample.get("user_full_name") or "-", sample.get("user_event_city") or "-",
               sample.get("user_username") or "-"]
        for t in tasks:
            task_subs = [s for s in user_subs if s["task_id"] == t["id"]]
            if not task_subs:
                row.append("-")
                continue
            # D-05: pick the LATEST attempt for this (task, user) pair by id, not list order --
            # only that attempt can be the currently-active one (idx_game_submissions_active
            # guarantees at most one non-rejected row per pair at any time).
            latest = max(task_subs, key=lambda s: s["id"])
            if latest["status"] == "approved":
                row.append(f"✅ {latest.get('coins_awarded')}")
            elif latest["status"] == "pending":
                row.append("⏳")
            else:  # rejected, and (since this IS the latest attempt) no later active submission exists
                row.append("❌")
        rows.append(row)
    return headers, rows


def _build_game_history(submissions: list[dict]) -> tuple[list[str], list[list]]:
    """Pure builder for the «История сдач» audit-log tab -- one row per `list_all_submissions()`
    row, NO status filter. This is the whole mechanism D-05 asked for: resolving "я сдавал, мне
    не засчитали" needs every attempt visible, including rejected ones and later resubmissions
    of the same (task, user) pair."""
    headers = ["ID сдачи", "Задание", "Категория", "Участник", "Город", "Юзернейм", "Тип", "Отправлено",
               "Статус", "Проверил", "Когда", "Начислено", "Причина отказа"]

    # Phase 14 (14-03, GAME-08): same «🗄 » prefix idiom as _build_game_matrix's column header
    # — added BEFORE the 60-char truncation.
    # Quick 260819-gtl (CONTEXT.md decision 8): column uses task_title(), same fallback.
    def _history_task_label(s: dict) -> str:
        prefix = "🗄 " if s.get("task_archived_at") else ""
        title = task_title({"title": s.get("task_title"), "text": s.get("task_text")})
        return (prefix + title)[:60]

    rows = [
        [
            s["id"],
            _history_task_label(s),
            s.get("task_category") or "-",
            s.get("user_full_name") or "-",
            s.get("user_event_city") or "-",
            s.get("user_username") or "-",
            _GAME_PROOF_LABELS.get(s.get("content_type"), s.get("content_type") or "-"),
            s.get("submitted_at") or "-",
            _GAME_HISTORY_STATUS_LABELS.get(s.get("status"), s.get("status") or "-"),
            s.get("reviewed_by") if s.get("reviewed_by") is not None else "-",
            s.get("reviewed_at") or "-",
            s.get("coins_awarded") if s.get("coins_awarded") is not None else "-",
            s.get("reject_reason") or "-",
        ]
        for s in submissions
    ]
    return headers, rows


def _last_sync_phrase(raw: str | None, now: "datetime") -> str:
    """Phase 09.1 (D, GAME-07): pure formatter for the "обновлено N мин назад" line on the
    sync-confirm screen. `raw` is whatever `bot_settings.game_sheet_last_synced_at` currently
    holds (written by rebuild_game_sheets on a fully-successful rebuild) -- None/empty/
    unparsable (never actually written by this bot, but a human could delete/corrupt the
    bot_settings row directly) all degrade to "ещё не обновлялось" rather than raising."""
    if not raw:
        return "ещё не обновлялось"
    try:
        synced = datetime.strptime(raw, "%Y-%m-%d %H:%M:%S")
    except (ValueError, TypeError):
        return "ещё не обновлялось"
    delta = now - synced
    minutes = delta.total_seconds() / 60
    if minutes < 0:
        minutes = 0
    if minutes < 1:
        return "обновлено только что"
    if minutes < 60:
        return f"обновлено {int(minutes)} мин назад"
    hours = minutes / 60
    if hours < 24:
        return f"обновлено {int(hours)} ч назад"
    return f"обновлено {synced.strftime('%d.%m в %H:%M')}"


@router.callback_query(F.data == "admin_game_sync_sheet")
async def sync_game_sheets_confirm(callback: types.CallbackQuery):
    """Quick 260814-gsg (находка верификации фазы 9): синхронизация вкладок геймы делает
    `sync_named_worksheet` = ПОЛНАЯ очистка листа и перезапись, но запускалась одним тапом.
    Обе соседние разрушительные кнопки («♻️ Пересобрать таблицу», «🧹 Убрать дубли») получили
    подтверждение после инцидента 13.08 — эта осталась без него.

    Обе вкладки целиком выводятся из БД, поэтому потерять можно только то, что менеджер дописал
    руками ПРЯМО в листе (заметки в «Истории сдач» для разбора споров — ровно тот сценарий, ради
    которого лист и заводился, см. 09-CONTEXT.md). Экран называет это прямым текстом.

    Quick 260815-3hw: имена вкладок теперь читаются из реестра (game_matrix_tab/
    game_history_tab, экран «⚙️ Настройки → 📄 Вкладки таблицы») — менеджер, переименовавший
    их кнопкой, должен видеть в подтверждении СВОИ имена, а не старый хардкод «Гейма»/«История
    сдач» (иначе гейт называет не ту вкладку, что реально перезапишется).

    Phase 09.1 (D, GAME-07): вкладки теперь пересобираются и сами (фоновый дебаунс), поэтому
    экран честно говорит, когда это было в последний раз -- game_sheet_last_synced_at это
    служебный ключ bot_settings, не заведённый в SETTINGS_SCHEMA (менеджер его не редактирует,
    реестр — только для человеко-редактируемых текстов)."""
    # Quick GAME-CITY-TABS: with the cities module on, the rebuild also writes a matrix +
    # history pair per enabled city («СПб Гейма» / «СПб История сдач») — the screen lists
    # every tab that will be wiped. A manager bound to one city (Phase 09.3) sees the shared
    # tabs + THEIR city's pair (staff city binding, Phase 8/09.1 -- not admin_selected_city,
    # whose "no choice yet" default would hide the other cities from an unbound superadmin);
    # the button itself always rebuilds everything (cheap, and one code path for the button
    # and the background autosync).
    plan = await game_tab_plan()
    only_city = None
    if callback.from_user.id not in config.ADMIN_IDS:  # D-12: bootstrap superadmins see everything
        bound = await get_staff_city(callback.from_user.id)
        if bound:
            only_city = normalize_city(bound)
    tab_lines = describe_plan(plan, only_city=only_city)
    has_city_tabs = any(e["city"] for e in plan)
    raw_synced_at = await get_setting("game_sheet_last_synced_at")
    sync_phrase = _last_sync_phrase(raw_synced_at, _now_moscow_naive())
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔄 Да, пересобрать вкладки", callback_data="admin_game_sync_sheet_go")],
        [InlineKeyboardButton(text="← Отмена", callback_data="admin_menu")],
    ])
    intro = "Заново соберу из базы бота вкладки:" if has_city_tabs else "Заново соберу из базы бота две вкладки:"
    others_note = ""
    if only_city is not None and any(e["city"] and e["city"] != only_city for e in plan):
        others_note = "Вкладки других городов пересоберутся тоже — из той же базы.\n\n"
    await callback.message.edit_text(
        "🔄 <b>Пересобрать вкладки геймификации?</b>\n\n"
        f"{intro}\n" + "\n".join(f"• {line}" for line in tab_lines) + "\n\n"
        f"{others_note}"
        f"Вкладки уже обновляются сами после каждого задания/решения/правки монет — {sync_phrase}.\n\n"
        "⚠️ Эти вкладки очищаются целиком и заполняются заново. <b>Заметки, которые вы писали "
        "руками прямо в этих листах, пропадут</b> — в базе бота их нет. Остальные вкладки "
        "таблицы не затрагиваются.",
        parse_mode="HTML",
        reply_markup=kb,
    )
    await callback.answer()


async def rebuild_game_sheets_detailed() -> list[dict]:
    """Phase 09.1 (D, GAME-07): the ONE place that actually rebuilds the gamification sheet
    tabs — shared by the "🔄 Да, пересобрать вкладки" button (sync_game_sheets below, which
    calls this directly, bypassing the debounce -- CONTEXT.md D's "пересобрать сейчас" escape
    hatch) and services.game_sync's debounced background resync (registered via set_rebuild()
    at the bottom of this module, through the tuple-returning wrapper rebuild_game_sheets).

    Quick GAME-CITY-TABS: the tab list comes from services.game_sheets.game_tab_plan() —
    the two whole-event tabs first, then (cities module ON) a matrix + history pair per
    enabled city, built from the same two DB reads filtered per city (delegate's city for
    submissions, task's city / NULL="all" for the matrix columns). Returns one dict per plan
    entry with `written` (rows written, -1 on failure) added. Every tab is written
    independently — one tab's failure never skips the rest (T-09-16)."""
    # list_all_tasks() is created_at DESC (moderation-friendly, newest-first); the matrix wants
    # columns oldest-first (left to right) -- reversed by an explicit sort here, not a new
    # db.py accessor, per the plan's own instruction.
    # Phase 14 (14-03, GAME-08): list_all_tasks()/list_all_submissions() are DELIBERATELY
    # unfiltered by archive status -- archived tasks stay in both tabs (marked «🗄 » by the
    # builders above), deleted tasks vanish on their own because they no longer exist in
    # either table. Do NOT add an archived_at filter here.
    tasks = sorted(await list_all_tasks(), key=lambda t: t["created_at"])
    submissions = await list_all_submissions()

    results: list[dict] = []
    for entry in await game_tab_plan():
        entry_tasks, entry_subs = rows_for_entry(entry, tasks, submissions)
        if entry["kind"] == "matrix":
            headers, rows = _build_game_matrix(entry_tasks, entry_subs)
        else:
            headers, rows = _build_game_history(entry_subs)
        try:
            written = await sync_named_worksheet(entry["tab"], headers, rows)
        except Exception as e:  # sync_named_worksheet is already fail-soft; belt and braces
            logger.error(f"rebuild_game_sheets: tab {entry['tab']!r} failed: {e}")
            written = -1
        results.append({**entry, "written": written})

    # T-091-17: the "обновлено N мин назад" timestamp must never lie about a partially-failed
    # sync -- only written when EVERY tab actually wrote successfully. game_sheet_last_synced_at
    # is a service key, not a SETTINGS_SCHEMA entry (see sync_game_sheets_confirm's comment).
    if all(r["written"] >= 0 for r in results):
        await set_setting("game_sheet_last_synced_at", _now_moscow_naive().strftime("%Y-%m-%d %H:%M:%S"))

    return results


async def rebuild_game_sheets() -> tuple[int, ...]:
    """Row counts per tab in game_tab_plan() order (whole-event matrix, whole-event history,
    then each city's pair) — -1 for a failed tab. Cities module OFF -> exactly a 2-tuple, the
    shape services.game_sync and the pre-cities callers already understand."""
    return tuple(r["written"] for r in await rebuild_game_sheets_detailed())


@router.callback_query(F.data == "admin_game_sync_sheet_go")
async def sync_game_sheets(callback: types.CallbackQuery):
    await callback.answer("🔄 Синхронизация...")
    logger.info(f"admin={callback.from_user.id} action=game_sync_sheet start")

    results = await rebuild_game_sheets_detailed()

    lines = []
    for r in results:
        report = f"{r['written']} строк" if r["written"] >= 0 else "⚠️ ошибка синхронизации (см. лог)"
        lines.append(f"{html_module.escape(r['tab'])}: {report}.")
    failed = sum(1 for r in results if r["written"] < 0)
    head = "✅ " if not failed else "⚠️ "
    await callback.message.answer(
        head + "\n".join(lines),
        parse_mode="HTML",
        reply_markup=await admin_keyboard_for(callback.from_user.id),
    )


# Phase 09.1 (D, GAME-07): register the shared rebuild with the debounced background resync
# helper. Inversion-of-control instead of moving the sheet-builder code into services/ --
# services.game_sync must not import handlers.* (import-cycle guard).
_set_game_rebuild(rebuild_game_sheets)


@router.callback_query(F.data == "admin_game_stats")
async def show_game_stats(callback: types.CallbackQuery):
    """«📊 Статистика геймы» — the single agregate screen for the gamification manager: who is
    participating and where each submission stands, plus an approved-only breakdown by
    category. `get_game_stats()` (09-01) is the ONLY aggregating query behind this screen — the
    Sheets matrix (09-05) computes its own per-row cells independently and is not reused here,
    per the plan's own <key_links> contract.

    CLAUDE.md (13.08, «бот для людей, не для прогеров»): an event manager reads this screen, not
    a developer -- a genuinely empty database (nobody has submitted anything at all yet) must
    read as a plain sentence, not a table of zeroes that looks broken.
    """
    stats = await get_game_stats()

    if stats["participants"] == 0:
        text = "📊 <b>Статистика геймификации</b>\n\nПока никто ничего не сдавал."
    else:
        lines = [
            "📊 <b>Статистика геймификации</b>",
            "",
            f"Участников: {stats['participants']}",
            f"Сдано на проверке: {stats['pending']}",
            f"Одобрено: {stats['approved']}",
            f"Отклонено: {stats['rejected']}",
            "",
            "<b>По категориям (одобрено):</b>",
        ]
        by_category = stats["by_category"]
        if not by_category:
            lines.append("пока нет одобренных сдач")
        else:
            # Fixed GAME_CATEGORIES order (Light/Medium/Hard/Referral/Special), not dict
            # insertion order from SQL -- stable row order on every render, zero-count
            # categories skipped rather than padding the screen with "0" lines.
            for cat in GAME_CATEGORIES:
                count = by_category.get(cat, 0)
                if count:
                    lines.append(f"• {cat}: {count}")
        text = "\n".join(lines)

    await callback.message.answer(
        text,
        parse_mode="HTML",
        reply_markup=await admin_keyboard_for(callback.from_user.id),
    )
    await callback.answer()
