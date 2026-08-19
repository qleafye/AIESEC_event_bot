"""Phase 16 (16-03, GAME-UI-03): manager task-management seam -- the NEW handlers of the
redesigned «🎯 Задания» screens (sketch .planning/sketches/16-game-ui-telegram.html, Экраны
6/7), everything gated by moderate_game (ADMIN_CAPS pre-registered in this plan's Task 1):

- point-edit card actions: description (`gteditdesc:*` / GameTaskEdit.text), coins
  (`gteditcoins:*` / GameTaskEdit.coins), deadline (`gteditdeadline:*` / GameTaskEdit.deadline
  + `gteditdeadline_preset:*` / `gteditdeadline_custom`), «👁 Как видит делегат»
  (`gtpreview:*` / `gtpreview_close`);
- creation wizard: deadline presets (`gtdeadline_preset:*` / `gtdeadline_custom`) and the
  final step's «✏️ Изменить» field menu (`gtwiz_edit_menu` / `gtwiz_edit:*` / `gtwiz_back`).

Decorates the SAME shared `admin.router` (13-02/13-03 seam-import technique) -- imported in
admin.py's bottom seam list right AFTER handlers/admin_gamification.py, so every handler here
lands after the pre-existing gamification handlers in observer order (pure appends to the
golden snapshot, tests/test_refac_snapshot_260816.py). Split out because admin_gamification.py
sits at its size ceiling (tests/test_module_size_convention_260816.py); the rendering helpers
it needs (`_task_edit_screen`, `_game_task_deadline_prompt`, the wizard's category/photo
keyboards) stay in admin_gamification.py and are imported here one-way -- never the reverse
(circular seam import).

Cancel-mid-edit is `cancel_game_task_edit` (admin_gamification.py, StateFilter(GameTaskEdit)),
registered BEFORE the step handlers below -- «Отмена»/`/cancel` never reach a step handler.
"""
import html as html_module
import logging

from aiogram import F, types
from aiogram.fsm.context import FSMContext
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardRemove

from settings_schema import get_setting_typed
from database.db import (
    get_task,
    task_title,
    update_task_coins,
    update_task_deadline,
    update_task_text,
)
from keyboards.builders import get_cancel_kb
from services.scheduler import _fmt_dt, _now_moscow_naive, _parse_schedule_dt
from services.game_sync import request_resync as _request_game_resync
from handlers.states import GameTaskCreate, GameTaskEdit
from handlers.game_labels import render_task_card_text
from handlers.game_task_wizard import (
    _DEADLINE_PAST,
    _PROMPT_CATEGORY,
    _PROMPT_COINS,
    _PROMPT_COINS_INVALID,
    _PROMPT_TEXT,
    _PROMPT_TEXT_EMPTY,
    _finish_deadline_step,
    _game_task_confirm_kb,
    _game_task_deadline_preset_kb,
    _resolve_deadline_preset,
)
from handlers.admin import router, _parse_positive_int
# Module reference, NOT `from ... import name`: when a test imports handlers.admin_gamification
# FIRST, admin.py's seam list reaches this file while admin_gamification is still partially
# initialised -- attribute access is deferred to call time, so both import orders work.
from handlers import admin_gamification as _ag

logger = logging.getLogger(__name__)


# ── shared bits ─────────────────────────────────────────────────────────────────────────────

async def _task_from_callback(callback: types.CallbackQuery) -> tuple[int | None, dict | None]:
    """`<prefix>:<id>` -> (task_id, task). Alerts and returns (None, None) on a malformed id or
    a task that no longer exists (stale button -- T-16-01-02 pattern: re-read, never trust)."""
    try:
        task_id = int(callback.data.split(":", 1)[1])
    except (ValueError, IndexError):
        await callback.answer("Некорректное задание", show_alert=True)
        return None, None
    task = await get_task(task_id)
    if task is None:
        await callback.answer("Задание не найдено", show_alert=True)
        return None, None
    return task_id, task


async def _rerender_edit_card(message: types.Message, task_id: int, done_text: str):
    """After a point-edit write: a one-line confirmation (also drops the reply «Отмена»
    keyboard) + the re-rendered edit card -- NOT the task list, the manager is still editing
    THIS task (same for the older title/photo steps since 16-03)."""
    await message.answer(done_text, parse_mode="HTML", reply_markup=ReplyKeyboardRemove())
    task = await get_task(task_id)
    if task is None:
        return
    text, kb = await _ag._task_edit_screen(task)
    await message.answer(text, parse_mode="HTML", reply_markup=kb)


async def _start_point_edit(callback: types.CallbackQuery, state: FSMContext, target_state, prompt: str, kb=None):
    task_id, task = await _task_from_callback(callback)
    if task is None:
        return
    await state.set_data({"gte_task_id": task_id})
    await callback.message.answer(prompt, reply_markup=kb if kb is not None else get_cancel_kb())
    await state.set_state(target_state)
    await callback.answer()


# ── point-edit: description ─────────────────────────────────────────────────────────────────

@router.callback_query(F.data.startswith("gteditdesc:"))
async def game_task_editdesc_start(callback: types.CallbackQuery, state: FSMContext):
    await _start_point_edit(callback, state, GameTaskEdit.text, _PROMPT_TEXT)


@router.message(GameTaskEdit.text)
async def game_task_editdesc_step(message: types.Message, state: FSMContext):
    data = await state.get_data()
    task_id = data.get("gte_task_id")
    text = (message.text or "").strip()
    if not text:
        await message.answer(_PROMPT_TEXT_EMPTY)
        return
    if not await update_task_text(task_id, text):
        await message.answer("Задание не найдено — возможно, его уже удалили.")
        await state.set_state(None)
        return
    _request_game_resync()
    await state.set_state(None)
    await _rerender_edit_card(message, task_id, "Описание обновлено.")


# ── point-edit: coins ───────────────────────────────────────────────────────────────────────

@router.callback_query(F.data.startswith("gteditcoins:"))
async def game_task_editcoins_start(callback: types.CallbackQuery, state: FSMContext):
    await _start_point_edit(callback, state, GameTaskEdit.coins, _PROMPT_COINS)


@router.message(GameTaskEdit.coins)
async def game_task_editcoins_step(message: types.Message, state: FSMContext):
    data = await state.get_data()
    task_id = data.get("gte_task_id")
    value = _parse_positive_int(message.text)
    if value is None:
        await message.answer(_PROMPT_COINS_INVALID)
        return
    if not await update_task_coins(task_id, value):
        await message.answer("Задание не найдено — возможно, его уже удалили.")
        await state.set_state(None)
        return
    _request_game_resync()
    await state.set_state(None)
    await _rerender_edit_card(message, task_id, f"Монеты обновлены: {value}🪙")


# ── point-edit: deadline (typed or preset, no confirm card) ─────────────────────────────────

@router.callback_query(F.data.startswith("gteditdeadline:"))
async def game_task_editdeadline_start(callback: types.CallbackQuery, state: FSMContext):
    task_id, task = await _task_from_callback(callback)
    if task is None:
        return
    await state.set_data({"gte_task_id": task_id})
    prompt = (
        f"📅 <b>{html_module.escape(task_title(task))}</b>\n\n"
        "Новый дедлайн — выберите готовый вариант кнопкой или введите дату текстом в формате "
        "ДД.ММ.ГГГГ ЧЧ:ММ (например 25.08.2026 23:59):"
    )
    # The edit card itself becomes the prompt (one live message); a preset tap edits it back
    # into the card, «❌ Отмена» (= gtedit:<id>) restores it, a typed date re-sends the card.
    kb = _game_task_deadline_preset_kb("gteditdeadline", f"gtedit:{task_id}")
    try:
        await callback.message.edit_text(prompt, parse_mode="HTML", reply_markup=kb)
    except Exception:
        await callback.message.answer(prompt, parse_mode="HTML", reply_markup=kb)
    await state.set_state(GameTaskEdit.deadline)
    await callback.answer()


async def _apply_point_deadline(task_id: int, when) -> bool:
    if not await update_task_deadline(task_id, _fmt_dt(when)):
        return False
    _request_game_resync()
    return True


@router.message(GameTaskEdit.deadline)
async def game_task_editdeadline_step(message: types.Message, state: FSMContext):
    data = await state.get_data()
    task_id = data.get("gte_task_id")
    when = _parse_schedule_dt(message.text)
    if when is None:
        await message.answer("❌ Не понял дату. Формат: ДД.ММ.ГГГГ ЧЧ:ММ (напр. 01.07.2026 14:30)")
        return
    # TZFIX-260816: admin input is Moscow wall-clock -- compare against Moscow, not container UTC.
    if when <= _now_moscow_naive():
        await message.answer(_DEADLINE_PAST)
        return
    if not await _apply_point_deadline(task_id, when):
        await message.answer("Задание не найдено — возможно, его уже удалили.")
        await state.set_state(None)
        return
    await state.set_state(None)
    await _rerender_edit_card(message, task_id, f"Дедлайн обновлён: {when.strftime('%d.%m.%Y %H:%M')}")


@router.callback_query(F.data.startswith("gteditdeadline_preset:"))
async def game_task_editdeadline_preset(callback: types.CallbackQuery, state: FSMContext):
    data = await state.get_data()
    task_id = data.get("gte_task_id")
    if task_id is None or await state.get_state() != GameTaskEdit.deadline.state:
        await callback.answer("Правка уже закрыта — откройте задание заново", show_alert=True)
        return
    when = _resolve_deadline_preset(callback.data.split(":", 1)[1])
    if when is None:
        await callback.answer("Неизвестный вариант", show_alert=True)
        return
    if when <= _now_moscow_naive():
        await callback.answer("Это время уже прошло — выберите другой вариант", show_alert=True)
        return
    if not await _apply_point_deadline(task_id, when):
        await callback.answer("Задание не найдено", show_alert=True)
        await state.set_state(None)
        return
    await state.set_state(None)
    await callback.answer(f"Дедлайн: {when.strftime('%d.%m.%Y %H:%M')}")
    task = await get_task(task_id)
    text, kb = await _ag._task_edit_screen(task)
    await callback.message.edit_text(text, parse_mode="HTML", reply_markup=kb)


@router.callback_query(F.data == "gteditdeadline_custom")
async def game_task_editdeadline_custom(callback: types.CallbackQuery):
    await callback.answer("Введите дату текстом ниже", show_alert=False)


# ── «👁 Как видит делегат» ──────────────────────────────────────────────────────────────────

@router.callback_query(F.data.startswith("gtpreview:"))
async def game_task_preview(callback: types.CallbackQuery):
    """The delegate's own card render (game_labels.render_task_card_text, status «новое» --
    a preview of what a fresh delegate sees). No photo -> the edit card itself is edited into
    the preview («← Назад» = `gtedit:<id>` restores it in place, one live message). Photo ->
    a text message can't become a photo message, so the preview is a separate photo message
    with «✖ Закрыть» (`gtpreview_close` deletes it, the edit card underneath stays)."""
    task_id, task = await _task_from_callback(callback)
    if task is None:
        return
    intro = await get_setting_typed("game_task_preview_intro")
    card = await render_task_card_text(task, "новое", None)
    text = f"{intro}\n\n{card}"
    photo_id = task.get("photo_file_id")
    if photo_id:
        caption = text if len(text) <= 1024 else text[:1021] + "…"
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="✖ Закрыть", callback_data="gtpreview_close")],
        ])
        await callback.message.answer_photo(photo_id, caption=caption, parse_mode="HTML", reply_markup=kb)
    else:
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="← Назад", callback_data=f"gtedit:{task_id}")],
        ])
        await callback.message.edit_text(text, parse_mode="HTML", reply_markup=kb)
    await callback.answer()


@router.callback_query(F.data == "gtpreview_close")
async def game_task_preview_close(callback: types.CallbackQuery):
    try:
        await callback.message.delete()
    except Exception:
        pass
    await callback.answer()


# ── creation wizard: deadline presets ───────────────────────────────────────────────────────

@router.callback_query(F.data.startswith("gtdeadline_preset:"))
async def game_task_deadline_preset(callback: types.CallbackQuery, state: FSMContext):
    """Preset tap on the wizard's deadline prompt -- resolves to a datetime and proceeds
    exactly as if it had been typed (`_finish_deadline_step`: same preview, same photo/no-photo
    branch). Past-time edge (today 23:59 already gone) -> same rejection as manual entry."""
    if await state.get_state() != GameTaskCreate.deadline.state:
        await callback.answer("Этот шаг уже пройден", show_alert=True)
        return
    when = _resolve_deadline_preset(callback.data.split(":", 1)[1])
    if when is None:
        await callback.answer("Неизвестный вариант", show_alert=True)
        return
    if when <= _now_moscow_naive():
        await callback.answer("Это время уже прошло — выберите другой вариант", show_alert=True)
        return
    try:
        await callback.message.edit_reply_markup(reply_markup=None)
    except Exception:
        pass
    await _finish_deadline_step(callback.message, state, when)
    await callback.answer()


@router.callback_query(F.data == "gtdeadline_custom")
async def game_task_deadline_custom(callback: types.CallbackQuery):
    await callback.answer("Введите дату текстом ниже", show_alert=False)


# ── creation wizard: final step «✏️ Изменить» -> field -> step -> back to preview ───────────

_WIZARD_EDIT_FIELDS = (
    ("title", "📝 Название"),
    ("text", "📄 Описание"),
    ("category", "🏷 Категория"),
    ("coins", "💰 Монеты"),
    ("deadline", "📅 Дедлайн"),
    ("photo", "📷 Фото"),
)


def _wizard_edit_menu_kb() -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(text=label, callback_data=f"gtwiz_edit:{field}")]
        for field, label in _WIZARD_EDIT_FIELDS
    ]
    rows.append([InlineKeyboardButton(text="◀️ Назад", callback_data="gtwiz_back")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def _wizard_preview_alive(callback: types.CallbackQuery, state: FSMContext) -> bool:
    """The preview's buttons only make sense while the draft is parked at `confirm`. Mid-step
    (a field re-entry already in progress) and no-draft (restart, cancelled) get different,
    honest alerts -- the manager is told what to do, not just «нельзя»."""
    current = await state.get_state()
    if current == GameTaskCreate.confirm.state:
        return True
    if current and current.startswith("GameTaskCreate:"):
        await callback.answer(
            "Сначала завершите текущий шаг — ответьте на вопрос выше или нажмите «Отмена»",
            show_alert=True,
        )
    else:
        await callback.answer("Черновик не найден — начните создание заново", show_alert=True)
    return False


@router.callback_query(F.data == "gtwiz_edit_menu")
async def game_task_wizard_edit_menu(callback: types.CallbackQuery, state: FSMContext):
    """Swaps ONLY the keyboard of the preview message (edit_reply_markup works for both the
    text and the photo variant of the preview) -- the card text stays on screen."""
    if not await _wizard_preview_alive(callback, state):
        return
    try:
        await callback.message.edit_reply_markup(reply_markup=_wizard_edit_menu_kb())
    except Exception:
        pass
    await callback.answer()


@router.callback_query(F.data == "gtwiz_back")
async def game_task_wizard_back(callback: types.CallbackQuery, state: FSMContext):
    if not await _wizard_preview_alive(callback, state):
        return
    try:
        await callback.message.edit_reply_markup(reply_markup=await _game_task_confirm_kb())
    except Exception:
        pass
    await callback.answer()


@router.callback_query(F.data.startswith("gtwiz_edit:"))
async def game_task_wizard_edit_field(callback: types.CallbackQuery, state: FSMContext):
    """Re-enters ONE creation step with `gt_wiz_edit=True`; the step handler (admin_gamification)
    stores the value and, seeing the flag, returns straight to the preview instead of
    advancing (`_wizard_return_to_preview`). Deadline always ends in the preview anyway."""
    if not await _wizard_preview_alive(callback, state):
        return
    field = callback.data.split(":", 1)[1]
    if field not in {f for f, _ in _WIZARD_EDIT_FIELDS}:
        await callback.answer("Неизвестное поле", show_alert=True)
        return
    await state.update_data(gt_wiz_edit=True)
    message = callback.message
    if field == "title":
        await message.answer(await get_setting_typed("game_task_title_prompt"), reply_markup=get_cancel_kb())
        await state.set_state(GameTaskCreate.title)
    elif field == "text":
        await message.answer(_PROMPT_TEXT, reply_markup=get_cancel_kb())
        await state.set_state(GameTaskCreate.text)
    elif field == "category":
        await message.answer(_PROMPT_CATEGORY, reply_markup=await _ag._game_task_category_kb())
        await state.set_state(GameTaskCreate.category)
    elif field == "coins":
        await message.answer(_PROMPT_COINS, reply_markup=get_cancel_kb())
        await state.set_state(GameTaskCreate.coins)
    elif field == "deadline":
        await _ag._game_task_deadline_prompt(message, state)
    else:  # photo
        prompt = await get_setting_typed("game_task_photo_prompt")
        await message.answer(f"{prompt}\n\n⏭ Пропустить = задание без обложки.", reply_markup=_ag._game_task_photo_kb())
        await state.set_state(GameTaskCreate.photo)
    await callback.answer()


__all__ = [
    "game_task_editdesc_start", "game_task_editdesc_step",
    "game_task_editcoins_start", "game_task_editcoins_step",
    "game_task_editdeadline_start", "game_task_editdeadline_step",
    "game_task_editdeadline_preset", "game_task_editdeadline_custom",
    "game_task_preview", "game_task_preview_close",
    "game_task_deadline_preset", "game_task_deadline_custom",
    "game_task_wizard_edit_menu", "game_task_wizard_back", "game_task_wizard_edit_field",
]
