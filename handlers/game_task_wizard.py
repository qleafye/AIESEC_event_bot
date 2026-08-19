"""Phase 16 (16-03, GAME-UI-03): pure helpers of the task-creation wizard's FINAL step
(«👁 Так увидит делегат» preview + «✅ Опубликовать / ✏️ Изменить / ❌ Отмена») and of the
deadline presets («сегодня 23:59 / +3 дня / +7 дней / своя дата»). No router, no handlers --
a seam module in the game_labels.py / game_submit_counter.py mould, imported by BOTH
handlers/admin_gamification.py (creation steps) and handlers/admin_game_tasks.py (preset
callbacks, «✏️ Изменить» re-entry, point-edit deadline) -- admin_gamification.py sits at its
size ceiling (tests/test_module_size_convention_260816.py) and admin_game_tasks.py cannot be
imported from it (circular seam import), so the shared pieces live here.

Names keep their leading underscore: admin_gamification.py re-exports them under the same
names (existing tests reach `admin_gamification._render_game_task_confirm_card`).
"""
import html as html_module
from datetime import datetime, timedelta

from aiogram.fsm.context import FSMContext
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from settings_schema import get_setting_typed
from services.scheduler import _fmt_dt, _now_moscow_naive
from handlers.states import GameTaskCreate
from handlers.game_labels import render_task_card_text


# Phase 16 (16-03, GAME-UI-03): the wizard's free-text prompts, shared by the creation steps
# below, the final-step «✏️ Изменить» re-entry (handlers/admin_game_tasks.py) and the
# point-edit card -- one literal per prompt, never two copies that can drift.
_PROMPT_TEXT = "Введите текст задания:"
_PROMPT_TEXT_EMPTY = "Текст не может быть пустым. Введите текст задания:"
_PROMPT_CATEGORY = "Выберите категорию:"
_PROMPT_COINS = "Сколько монет за это задание?"
_PROMPT_COINS_INVALID = "Введите положительное целое число монет:"
_PROMPT_DEADLINE = (
    "Дедлайн сдачи — выберите готовый вариант кнопкой или введите дату текстом в формате "
    "ДД.ММ.ГГГГ ЧЧ:ММ (например 25.08.2026 23:59):"
)
_DEADLINE_PAST = "❌ Это время уже прошло. Введите будущую дату."

# Deadline presets (sketch, Экран 7): code -> button label. Resolution lives in
# `_resolve_deadline_preset` (fixed known-set, T-16-03-02: an unknown code is None, never an
# exception); the same keyboard serves the creation wizard (`gtdeadline_*`) and the point-edit
# flow (`gteditdeadline_*`) via the callback prefix.
_DEADLINE_PRESETS = (("today", "Сегодня 23:59"), ("plus3", "+3 дня"), ("plus7", "+7 дней"))


def _resolve_deadline_preset(code: str) -> datetime | None:
    """today -> today 23:59:00 MSK; plus3/plus7 -> that + 3/7 days; anything else -> None."""
    base = _now_moscow_naive().replace(hour=23, minute=59, second=0, microsecond=0)
    if code == "today":
        return base
    if code == "plus3":
        return base + timedelta(days=3)
    if code == "plus7":
        return base + timedelta(days=7)
    return None


def _game_task_deadline_preset_kb(prefix: str, cancel_cb: str) -> InlineKeyboardMarkup:
    """3 presets + an informational «✏️ Своя дата» + «❌ Отмена» (`cancel_cb` -- `gtcancel`
    for the wizard, `gtedit:<id>` for the point-edit flow, both pre-registered)."""
    rows = [
        [InlineKeyboardButton(text=label, callback_data=f"{prefix}_preset:{code}")]
        for code, label in _DEADLINE_PRESETS
    ]
    rows.append([InlineKeyboardButton(text="✏️ Своя дата", callback_data=f"{prefix}_custom")])
    rows.append([InlineKeyboardButton(text="❌ Отмена", callback_data=cancel_cb)])
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def _wizard_return_to_preview(target, state: FSMContext) -> bool:
    """Phase 16 (16-03, Task 4): a creation step re-entered from the final preview's
    «✏️ Изменить» menu (`gt_wiz_edit` flag set by handlers/admin_game_tasks.py) does NOT
    advance to the next step -- it goes straight back to the preview with the rest of the FSM
    data intact. Returns True when it took over (caller returns), False on a normal first pass."""
    data = await state.get_data()
    if not data.get("gt_wiz_edit"):
        return False
    await state.update_data(gt_wiz_edit=False)
    await _show_wizard_preview(target, state)
    return True


async def _game_task_confirm_kb() -> InlineKeyboardMarkup:
    """Final wizard step (Phase 16, 16-03, Экран 7): «✅ Опубликовать» (callback `gtconfirm`
    unchanged -- creation logic and ADMIN_CAPS untouched), «✏️ Изменить» (field menu, swapped
    in via edit_reply_markup by handlers/admin_game_tasks.py), «❌ Отмена» (existing)."""
    publish = await get_setting_typed("game_wizard_publish_btn")
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=publish, callback_data="gtconfirm")],
        [
            InlineKeyboardButton(text="✏️ Изменить", callback_data="gtwiz_edit_menu"),
            InlineKeyboardButton(text="❌ Отмена", callback_data="gtcancel"),
        ],
    ])


def _wizard_task_like(data: dict) -> dict:
    """FSM draft -> the task-shaped dict game_labels.render_task_card_text expects (the same
    keys a `game_tasks` row has), so the preview is rendered by the DELEGATE's own function."""
    return {
        "title": data.get("gt_title"),
        "text": data.get("gt_text") or "",
        "category": data.get("gt_category") or "",
        "coins": data.get("gt_coins"),
        "deadline_at": data.get("gt_deadline"),
        "proof_type": data.get("gt_proof_type"),
        "photo_file_id": data.get("gt_photo_file_id"),
    }


async def _render_game_task_confirm_card(data: dict) -> str:
    """Phase 16 (16-03, Экран 7): «👁 Так увидит делегат» + the delegate card rendered by the
    ONE shared function (title, RU category, coins, deadline, status «новое», proof hint,
    description in <blockquote expandable> -- HTML-escaped inside, T-09-05/T-16-01-03), then
    the manager-only «Кому:» line. Phase 09.1 (B): «Кому:» appears only when the city step
    was actually shown (gt_city_step_shown, resolved once by game_task_proof_done)."""
    header = await get_setting_typed("game_wizard_preview_title")
    card = await render_task_card_text(_wizard_task_like(data), "новое", None)
    parts = [header, "", card]
    if data.get("gt_city_step_shown"):
        city_label_text = data.get("gt_event_city_label") or "🌍 Все города"
        parts += ["", f"Кому: {html_module.escape(str(city_label_text))}"]
    return "\n".join(parts)


async def _show_wizard_preview(target, state: FSMContext):
    """Sends the final-step preview (photo with caption when a cover is set -- 1024-char
    caption ceiling -- else a plain message) and parks the wizard in GameTaskCreate.confirm.
    Shared by the deadline step (typed or preset) and every «✏️ Изменить» re-entry."""
    data = await state.get_data()
    card_text = await _render_game_task_confirm_card(data)
    kb = await _game_task_confirm_kb()
    photo_id = data.get("gt_photo_file_id")
    if photo_id:
        caption = card_text if len(card_text) <= 1024 else card_text[:1021] + "…"
        await target.answer_photo(photo_id, caption=caption, parse_mode="HTML", reply_markup=kb)
    else:
        await target.answer(card_text, parse_mode="HTML", reply_markup=kb)
    await state.set_state(GameTaskCreate.confirm)


async def _finish_deadline_step(target, state: FSMContext, when: datetime):
    """The tail shared by the typed-date step above and the preset callback
    (handlers/admin_game_tasks.py::game_task_deadline_preset): store the resolved deadline,
    clear a pending «✏️ Изменить» flag (the preview IS the return point) and show the preview."""
    await state.update_data(gt_deadline=_fmt_dt(when), gt_wiz_edit=False)
    await _show_wizard_preview(target, state)
