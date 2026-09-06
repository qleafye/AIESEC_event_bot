import asyncio
import html
import logging
from datetime import datetime
from aiogram import Router, F, types, Bot
from aiogram.filters import Command, StateFilter
from aiogram.types import FSInputFile, InlineKeyboardMarkup, InlineKeyboardButton, WebAppInfo
from aiogram.fsm.context import FSMContext
from database.db import (
    get_user,
    get_referrals,
    get_setting,
    get_balance,
    get_leaderboard,
    get_user_rank,
    create_question,
    list_active_tasks,
    get_task,
    get_active_submission,
    create_submission,
    add_submission_part,
    parse_proof_types,
    count_rejected_submissions,
    task_title,
    list_coin_entries_for_user,
    count_coin_entries_for_user,
    get_reg_draft,
    has_faq_for_city,  # Quick 260906-8uq: экран «❓ Частые вопросы» + гейт формы вопроса
    list_faq_for_city,
)
from handlers.admin_caps import notify_by_capability  # D-13: fan out by capability, not bare ADMIN_IDS
from handlers.game_labels import (  # Phase 16 (16-01): single RU-label source; 16-03: shared card render
    category_label, proof_types_label,
    render_task_card_text as _render_task_card_text, task_deadline_short as _game_task_deadline_short,
)
from handlers.game_submit_counter import (  # Phase 16 (16-02): editable submission counter (Экран 3)
    game_counter_text as _game_counter_text, game_counter_kb as _game_counter_kb, edit_counter as _edit_counter,
)
from cities import (
    cities_module_on, normalize_city, city_scope,  # Phase 09.1 (B): show_game_tasks city filter
    get_setting_for_city,  # Phase 09.2 (B): contacts/info screens resolve by delegate city
)
from keyboards.builders import (
    get_cancel_kb,
    get_main_menu_kb,
    get_info_submenu_kb,
    get_socials_kb
)
from handlers.states import Question, GameSubmit
from settings_schema import get_setting_typed  # Phase 09.1 (A): flow texts live in the registry
from services.background import spawn as _spawn
from services.game_digest import notify_submission as notify_game_submission  # Quick 260822
from services.faq import apply_city_overrides, short as _faq_short  # Quick 260906-8uq
from config import config

router = Router()
logger = logging.getLogger(__name__)

def _gate_decision(status) -> tuple[bool, str | None]:
    """Map a user's status to (allowed, denial_kind). Legacy/missing/unknown -> allowed
    (the ~590 live users have status='approved' via the migration default)."""
    status = status or "approved"
    if status == "pending":
        return False, "pending"
    if status == "rejected":
        return False, "rejected"
    return True, None  # approved + any unknown legacy value


async def ensure_registered(message: types.Message) -> bool:
    user = await get_user(message.from_user.id)
    if not user:
        await message.answer(
            "Чтобы пользоваться ботом, сначала нужно зарегистрироваться. Отправь команду /start.",
        )
        return False

    allowed, kind = _gate_decision(user.get("status"))
    if allowed:
        return True
    if kind == "pending":
        # Phase 17.1 (17.1-01): текст гейта — в реестре (сосед reject_text ниже уже был там).
        await message.answer(await get_setting_typed("pending_gate_text"))
    else:  # rejected
        await message.answer(
            await get_setting("reject_text") or "К сожалению, твоя заявка отклонена.",
        )
    return False


# --- Coins (COIN-03) ---

async def render_leaderboard(rows: list, requester_id: int, requester_rank, requester_balance: int) -> str:
    """Phase 17.1 (17.1-01): заголовок/пустой экран/строка «твоё место» — из реестра
    (`leaderboard_*`), поэтому функция стала async, как соседние `_balance_screen`/
    `_render_task_card_text`. Рендер прежний байт-в-байт: дефолт `leaderboard_rank_line_text`
    использует только {rank} и {balance}; {total} доступен менеджеру дополнительно (то же
    «сколько всего человек в рейтинге», что и в `balance_screen_header`)."""
    lines = [await get_setting_typed("leaderboard_header_text"), ""]
    if not rows:
        lines.append(await get_setting_typed("leaderboard_empty_text"))
    else:
        for i, row in enumerate(rows, start=1):
            name = row.get("full_name") or row.get("username") or str(row.get("user_id"))
            lines.append(f"{i}. {html.escape(str(name))} — {row.get('balance', 0)}")
    lines.append("")
    rank_text = requester_rank if requester_rank is not None else "—"
    # Same scale-acceptable idiom as _balance_screen (CLAUDE.md: 1000-1500 человек за сезон).
    total = len(await get_leaderboard(10_000))
    rank_line_tpl = await get_setting_typed("leaderboard_rank_line_text")
    lines.append(rank_line_tpl.format(
        rank=rank_text, balance=requester_balance, total=total or "—",
    ))
    return "\n".join(lines)


def _format_coin_entry_line(row: dict, manual_label: str, task_label: str) -> str:
    """`"{dd.mm} {sign}{delta}🪙 — {reason or source label}"` — shared by the balance summary
    (last 5) and the paginated «📜 История» screen. `reason` wins when set; otherwise falls
    back to the RU source label (manual/task), or a plain "—" for NULL/legacy rows."""
    try:
        when = datetime.strptime(row["timestamp"], "%Y-%m-%d %H:%M:%S").strftime("%d.%m")
    except (TypeError, ValueError):
        when = str(row.get("timestamp") or "—")
    delta = row.get("delta") or 0
    sign = f"+{delta}" if delta >= 0 else str(delta)
    reason = row.get("reason")
    if reason:
        label = html.escape(str(reason))
    elif row.get("source") == "manual":
        label = manual_label
    elif row.get("source") == "task":
        label = task_label
    else:
        label = "—"
    return f"{when} {sign}{delta}🪙 — {label}"


async def _balance_screen(user_id: int) -> tuple[str, InlineKeyboardMarkup]:
    """Phase 16 (16-01, GAME-UI-01): «🪙 Баланс» summary -- header (balance/rank/total) + up
    to 5 most recent operations, «📜 История»/«🏆 Рейтинг» buttons. This IS the entry screen
    (reached from the reply-keyboard button), so no «◀️» row, unlike its two sub-screens."""
    balance = await get_balance(user_id)
    rank = await get_user_rank(user_id)
    total = len(await get_leaderboard(10_000))  # scale-acceptable per CLAUDE.md (1000-1500/season)
    header_tpl = await get_setting_typed("balance_screen_header")
    header = header_tpl.format(
        balance=balance,
        rank=rank if rank is not None else "—",
        total=total or "—",
    )
    rows = await list_coin_entries_for_user(user_id, limit=5, offset=0)
    lines = [header, ""]
    if not rows:
        lines.append(await get_setting_typed("balance_history_empty"))
    else:
        manual_label = await get_setting_typed("balance_source_manual_label")
        task_label = await get_setting_typed("balance_source_task_label")
        for row in rows:
            lines.append(_format_coin_entry_line(row, manual_label, task_label))
    text = "\n".join(lines)
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📜 История", callback_data="gbal_history:0")],
        [InlineKeyboardButton(text="🏆 Рейтинг", callback_data="gbal_top")],
    ])
    return text, kb


async def _balance_history_screen(user_id: int, offset: int = 0) -> tuple[str, InlineKeyboardMarkup]:
    """Same LIMIT/OFFSET + «Страница K из N» + «← Раньше»/«Позже →» idiom as
    `admin_gamification._coins_journal_screen`, scoped to one user_id via
    `list_coin_entries_for_user`/`count_coin_entries_for_user`."""
    limit = 10
    total = await count_coin_entries_for_user(user_id)
    rows = await list_coin_entries_for_user(user_id, limit=limit, offset=offset)
    lines = [await get_setting_typed("balance_history_header_text")]
    if total == 0:
        lines.append("")
        lines.append(await get_setting_typed("balance_history_empty"))
    else:
        total_pages = (total + limit - 1) // limit
        current_page = offset // limit + 1
        lines.append(f"Страница {current_page} из {total_pages}")
        lines.append("")
        manual_label = await get_setting_typed("balance_source_manual_label")
        task_label = await get_setting_typed("balance_source_task_label")
        for row in rows:
            lines.append(_format_coin_entry_line(row, manual_label, task_label))
    text = "\n".join(lines)

    buttons: list[list[InlineKeyboardButton]] = []
    nav_row: list[InlineKeyboardButton] = []
    if offset > 0:
        nav_row.append(InlineKeyboardButton(
            text="← Раньше", callback_data=f"gbal_history:{max(0, offset - limit)}",
        ))
    if offset + limit < total:
        nav_row.append(InlineKeyboardButton(
            text="Позже →", callback_data=f"gbal_history:{offset + limit}",
        ))
    if nav_row:
        buttons.append(nav_row)
    buttons.append([InlineKeyboardButton(text="◀️ Баланс", callback_data="gbal_back")])
    return text, InlineKeyboardMarkup(inline_keyboard=buttons)


@router.message(F.text == "🪙 Мои монеты")
async def show_my_coins(message: types.Message):
    if not await ensure_registered(message):
        return
    text, kb = await _balance_screen(message.from_user.id)
    await message.answer(text, parse_mode="HTML", reply_markup=kb)


@router.callback_query(F.data.startswith("gbal_history:"))
async def gbal_history(callback: types.CallbackQuery):
    """T-16-01-01: offset parsed with a try/except, clamped to >= 0 server-side (the deeper
    "beyond total" clamp lives inside `_balance_history_screen`'s own нав-row logic, same
    idiom as `coinsjrn_page`)."""
    try:
        offset = int(callback.data.split(":", 1)[1])
    except (ValueError, IndexError):
        offset = 0
    if offset < 0:
        offset = 0
    text, kb = await _balance_history_screen(callback.from_user.id, offset=offset)
    await callback.message.edit_text(text, parse_mode="HTML", reply_markup=kb)
    await callback.answer()


@router.callback_query(F.data == "gbal_top")
async def gbal_top(callback: types.CallbackQuery):
    rows = await get_leaderboard(10)
    rank = await get_user_rank(callback.from_user.id)
    balance = await get_balance(callback.from_user.id)
    text = await render_leaderboard(rows, callback.from_user.id, rank, balance)
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="◀️ Баланс", callback_data="gbal_back")],
    ])
    await callback.message.edit_text(text, parse_mode="HTML", reply_markup=kb)
    await callback.answer()


@router.callback_query(F.data == "gbal_back")
async def gbal_back(callback: types.CallbackQuery):
    text, kb = await _balance_screen(callback.from_user.id)
    await callback.message.edit_text(text, parse_mode="HTML", reply_markup=kb)
    await callback.answer()


@router.message(Command("рейтинг", "rating", "leaderboard"))
async def show_leaderboard(message: types.Message):
    if not await ensure_registered(message):
        return
    rows = await get_leaderboard(10)
    rank = await get_user_rank(message.from_user.id)
    balance = await get_balance(message.from_user.id)
    await message.answer(
        await render_leaderboard(rows, message.from_user.id, rank, balance),
        parse_mode="HTML",
    )


# --- Gamification: task list + submission (GAME-01/02, wave 3, 09-03) ---

PAGE_SIZE = 6  # Phase 16 (16-01): delegate task-list page size (CONTEXT.md "5-6 заданий на страницу")


# `_game_task_deadline_short` -> handlers/game_labels.py::task_deadline_short (16-03), imported
# above under the old name so every existing call site/test keeps working unchanged.


async def _render_game_task_line(
    index: int, task: dict, active: dict | None, user_id: int,
) -> tuple[str, bool]:
    """Renders one task's TWO-line status entry for the delegate list ("N. <emoji> <b>title</b>"
    / tail line). Returns (line, needs_submit_button) -- needs_submit_button is True only when
    the task is genuinely open for a fresh submission (not claimed, not over the resubmit
    limit), matching D-08's «одна сдача на пару» invariant surfaced to the delegate.

    Phase 16 (16-01, GAME-UI-01): RU category via `game_labels.category_label`; a terminal
    "❌ отклонено (попытка K из N)" state (no submit button) when `game_resubmit_limit` is set
    and the delegate has exhausted it -- checked BEFORE the `active is None` branch since a
    terminal task also has `active is None` (rejected submissions never come back from
    `get_active_submission`, D-05)."""
    title = html.escape(task_title(task))
    category = await category_label(task["category"])
    deadline, overdue = _game_task_deadline_short(task)
    overdue_mark = " ⏰" if overdue else ""

    limit = await get_setting_typed("game_resubmit_limit")
    if limit:
        rejected = await count_rejected_submissions(task["id"], user_id)
        if active is None and rejected >= limit:
            return (
                f"{index}. ❌ <b>{title}</b>\n"
                f"отклонено (попытка {rejected} из {limit})"
            ), False

    if active is None:
        tail = f"{category} · {task['coins']}🪙 · до {deadline}"
        if overdue:
            tail += " — срок вышел, сдать ещё можно"
        return f"{index}. 📤 <b>{title}</b>{overdue_mark}\n{tail}", True
    if active["status"] == "pending":
        when = active.get("submitted_at") or "—"
        try:
            when = datetime.strptime(when, "%Y-%m-%d %H:%M:%S").strftime("%d.%m %H:%M")
        except (TypeError, ValueError):
            pass
        return f"{index}. ⏳ <b>{title}</b>\nна проверке (сдано {when})", False
    if active["status"] == "approved":
        coins_awarded = active.get("coins_awarded")
        return f"{index}. ✅ <b>{title}</b>\nпринято (+{coins_awarded}🪙)", False
    # 'rejected' submissions never come back from get_active_submission (D-05) -- unreachable
    # in practice, kept as a fail-soft fallback rather than a silent KeyError.
    tail = f"{category} · {task['coins']}🪙 · до {deadline}"
    if overdue:
        tail += " — срок вышел, сдать ещё можно"
    return f"{index}. 📤 <b>{title}</b>{overdue_mark}\n{tail}", True


async def _game_task_list_screen(
    user_id: int, page: int = 0,
) -> tuple[str, InlineKeyboardMarkup | None]:
    """Shared by `show_game_tasks` (new message), `mytask_back`/`gtasks_page` (edit_text back
    from a card or a page-nav tap) -- always returns a text (registry `game_task_list_empty`
    when there are no active tasks) + kb (None only when there are no buttons to show).

    Phase 16 (16-01, GAME-UI-01): paginated at `PAGE_SIZE`, numbering is GLOBAL across pages
    (continues from `page*PAGE_SIZE + 1`), a page-nav row ("‹" / "N / M" no-op / "›") is added
    only when there's more than one page."""
    if await cities_module_on():
        user = await get_user(user_id)
        code = normalize_city(user.get("event_city") if user else None)
        tasks = await list_active_tasks(city_scope=city_scope(code))
    else:
        tasks = await list_active_tasks()
    if not tasks:
        return await get_setting_typed("game_task_list_empty"), None

    total_pages = max(1, -(-len(tasks) // PAGE_SIZE))
    page = max(0, min(page, total_pages - 1))
    page_tasks = tasks[page * PAGE_SIZE:(page + 1) * PAGE_SIZE]

    lines = []
    if total_pages > 1:
        page_label = await get_setting_typed("game_task_list_page_label")
        lines.append(page_label.format(page=page + 1, total=total_pages))

    buttons = []
    for offset, task in enumerate(page_tasks):
        i = page * PAGE_SIZE + offset + 1
        active = await get_active_submission(task["id"], user_id)
        line, needs_button = await _render_game_task_line(i, task, active, user_id)
        lines.append(line)
        if needs_button:
            buttons.append([InlineKeyboardButton(
                text=_submit_button_label(task), callback_data=f"gtask_open:{task['id']}",
            )])

    if total_pages > 1:
        nav_row = []
        if page > 0:
            nav_row.append(InlineKeyboardButton(text="‹", callback_data=f"gtasks_page:{page - 1}"))
        nav_row.append(InlineKeyboardButton(text=f"{page + 1} / {total_pages}", callback_data="gtasks_noop"))
        if page < total_pages - 1:
            nav_row.append(InlineKeyboardButton(text="›", callback_data=f"gtasks_page:{page + 1}"))
        buttons.append(nav_row)

    kb = InlineKeyboardMarkup(inline_keyboard=buttons) if buttons else None
    return "\n\n".join(lines), kb


@router.message(F.text == "🎯 Задания")
async def show_game_tasks(message: types.Message):
    if not await ensure_registered(message):
        return
    text, kb = await _game_task_list_screen(message.from_user.id, page=0)
    await message.answer(text, parse_mode="HTML", reply_markup=kb)


# Phase 14 (GAME-08): delegate-facing submit-button label — several tasks used to share the
# identical literal "Сдать", making the keyboard ambiguous when more than one was open at
# once. Pure/sync (no I/O) -- text-only, never HTML-escaped because this is a button label,
# not parse_mode="HTML" body text.
# Quick 260819-gtl (CONTEXT.md decision 3): "📤 <title>" (обрезка 30), title not raw text.
def _submit_button_label(task: dict) -> str:
    name = task_title(task)
    if len(name) > 30:
        name = name[:30] + "…"
    return f"📤 {name}"


# `_render_task_card_text` -> handlers/game_labels.py::render_task_card_text (16-03, GAME-UI-03):
# the manager's «👁 Как видит делегат» / wizard preview render the SAME card via the same
# function -- imported above under the old name (tests call `ua_mod._render_task_card_text`).


def _game_task_card_kb(task_id: int, can_submit: bool) -> InlineKeyboardMarkup:
    """Phase 16 (16-01): «◀️ Назад» now targets `gtasks_back:0` (CONTEXT.md rename, page
    threading from list to card not implemented this plan -- `mytask_back` parses generically
    so a future plan can build `gtasks_back:N` for N>0 without another rename). `can_submit`
    False (stale-pending re-tap, T-16-01-02) drops the "📤 Сдать" row entirely."""
    if can_submit:
        return InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="📤 Сдать", callback_data=f"mytask_submit:{task_id}")],
            [InlineKeyboardButton(text="◀️ Назад", callback_data="gtasks_back:0")],
        ])
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="◀️ Назад", callback_data="gtasks_back:0")],
    ])


@router.callback_query(F.data.startswith("gtask_open:"))
async def mytask_open(callback: types.CallbackQuery):
    """Quick 260819-gtl (CONTEXT.md decision 5) + Phase 16 (16-01, CONTEXT.md `<specifics>`
    rename `mytask:` -> `gtask_open:`): opens the task card. Photo present -> send_photo as a
    SEPARATE message (edit_text can never turn a text message into a photo message) -- the list
    message keeps its own keyboard untouched. No photo -> edit_text turns THIS message (the
    list) into the card in place, zero new messages in the chat.

    T-16-01-02: re-reads `get_task`/`get_active_submission` on every tap -- never trusts what
    a stale button implies about current state (a "pending" active submission collapses the
    card to a submit-less "на проверке" variant, closing the stale-tap race)."""
    try:
        task_id = int(callback.data.split(":", 1)[1])
    except ValueError:
        await callback.answer("Некорректное задание", show_alert=True)
        return
    task = await get_task(task_id)
    if task is None or task.get("archived_at"):
        # Same wording as mytask_submit_start's own archived-task alert (T-14-01 precedent) --
        # a stale list message can still carry a button for a task removed since it was sent.
        await callback.answer(
            "Это задание убрали в архив — сдать его больше нельзя. Загляни в «🎯 Задания», "
            "там актуальный список.",
            show_alert=True,
        )
        return

    active = await get_active_submission(task_id, callback.from_user.id)
    if active is not None and active["status"] == "pending":
        status_line = "на проверке"
        can_submit = False
        attempt = None
    else:
        limit = await get_setting_typed("game_resubmit_limit")
        rejected = await count_rejected_submissions(task_id, callback.from_user.id) if limit else 0
        status_line = f"новое · попытка {rejected} из {limit}" if limit and rejected else "новое"
        can_submit = True
        attempt = rejected

    card_text = await _render_task_card_text(task, status_line, attempt)
    card_kb = _game_task_card_kb(task_id, can_submit)
    photo_id = task.get("photo_file_id")
    if photo_id:
        caption = card_text if len(card_text) <= 1024 else card_text[:1021] + "…"
        await callback.message.answer_photo(
            photo_id, caption=caption, parse_mode="HTML", reply_markup=card_kb,
        )
    else:
        await callback.message.edit_text(card_text, parse_mode="HTML", reply_markup=card_kb)
    await callback.answer()


@router.callback_query(F.data.startswith("gtasks_back:"))
async def mytask_back(callback: types.CallbackQuery):
    """Quick 260819-gtl (CONTEXT.md decision 5) + Phase 16 (16-01, CONTEXT.md `<specifics>`
    rename `mytask_back` -> `gtasks_back:N`): a photo card is its own message -- "back" just
    removes it (the list message underneath, untouched, still carries its own keyboard, "это
    ок" per CONTEXT.md). A no-photo card IS the (edited) list message -- "back" re-renders the
    list into the SAME message via edit_text, at the page parsed from callback_data (default 0
    on parse failure, T-16-01-01)."""
    if callback.message.photo:
        try:
            await callback.message.delete()
        except Exception:
            pass
        await callback.answer()
        return
    try:
        page = int(callback.data.split(":", 1)[1])
    except (ValueError, IndexError):
        page = 0
    text, kb = await _game_task_list_screen(callback.from_user.id, page=page)
    await callback.message.edit_text(text, parse_mode="HTML", reply_markup=kb)
    await callback.answer()


@router.callback_query(F.data.startswith("gtasks_page:"))
async def gtasks_page(callback: types.CallbackQuery):
    """Phase 16 (16-01): list pagination -- edits the SAME message (T-16-01-01: page parsed
    with a try/except, clamped server-side inside `_game_task_list_screen`, never trusts the
    client-supplied page number as-is)."""
    try:
        page = int(callback.data.split(":", 1)[1])
    except (ValueError, IndexError):
        page = 0
    text, kb = await _game_task_list_screen(callback.from_user.id, page=page)
    await callback.message.edit_text(text, parse_mode="HTML", reply_markup=kb)
    await callback.answer()


@router.callback_query(F.data == "gtasks_noop")
async def gtasks_noop(callback: types.CallbackQuery):
    """Phase 16 (16-01): the non-functional "N / M" page-indicator button in the nav row."""
    await callback.answer()


# Phase 09.1 (A): the flow's texts live in settings_schema (group "game"), not literals here
# -- the old per-type prompt/mismatch literal dicts are gone. proof_type is no longer a
# validator, only a hint baked into the prompt (_build_proof_prompt below).

_LEGACY_CONTENT_TYPE = {"photo": "photo", "document": "pdf", "text": "text", "link": "link"}

# module dict, same "first message wins, spawn one debounced ack" shape as
# handlers/admin_broadcasts.py::pending_albums / _wait_and_send_album (Phase 13, 13-05: moved
# from handlers/admin.py) -- keyed by media_group_id only
# (Telegram media_group_id is unique enough in practice, same assumption the broadcast idiom
# already makes). ONLY collects an ack here -- never finalizes the submission (no timeout).
_gs_pending_albums: dict[str, bool] = {}

# CR-01 (09.1-REVIEW.md): 20 parts x 500 chars in the card is well under Telegram's 4096
# sendMessage limit, and caps MemoryStorage growth -- a delegate (malicious or not) can no
# longer jam the moderation queue by attaching unlimited parts of unlimited length.
MAX_PARTS = 20
MAX_TEXT_PART = 1000


async def _build_proof_prompt(task: dict) -> str:
    codes = parse_proof_types(task.get("proof_type"))
    if len(codes) == 1:
        body = await get_setting_typed(f"game_proof_prompt_{codes[0]}")
    else:
        body = await get_setting_typed("game_proof_prompt_any")
        if len(codes) > 1:
            for code in codes:
                body += f"\n• {await get_setting_typed(f'game_proof_prompt_{code}')}"
    hint = await get_setting_typed("game_proof_done_hint")
    return body + "\n\n" + hint


def _classify_part(message: types.Message) -> tuple[str | None, str | None, str | None]:
    """(kind, content, caption) for one incoming message, or (None, None, None) for anything
    the free-form flow doesn't recognize (voice/video/sticker/etc -- a soft refusal, state
    stays put)."""
    if message.photo:
        return "photo", message.photo[-1].file_id, getattr(message, "caption", None)
    if message.document:
        return "document", message.document.file_id, getattr(message, "caption", None)
    if message.text:
        text = message.text
        if text.strip().lower().startswith(("http://", "https://")):
            return "link", text, None
        return "text", text, None
    return None, None, None


async def _ack_album(media_group_id: str, bot: Bot, chat_id: int, state: FSMContext):
    await asyncio.sleep(0.8)  # collection window ONLY -- the finalize gate is still «✅ Готово»
    _gs_pending_albums.pop(media_group_id, None)
    data = await state.get_data()
    await _edit_counter(bot, data, list(data.get("gs_parts", [])))


@router.callback_query(F.data.startswith("mytask_submit:"))
async def mytask_submit_start(callback: types.CallbackQuery, state: FSMContext):
    try:
        task_id = int(callback.data.split(":", 1)[1])
    except ValueError:
        await callback.answer("Некорректное задание", show_alert=True)
        return

    task = await get_task(task_id)
    if task is None:
        await callback.answer("Задание не найдено", show_alert=True)
        return

    # WR-06 (09.1-REVIEW.md): show_game_tasks filters the LIST by city, but this handler used
    # to resolve the task purely from callback_data with no city check -- same class as CR-03,
    # one layer down. cities_module_on() first so an off module stays byte-identical to
    # pre-09.1; task.get("event_city") second so an "all cities" task never triggers a check
    # at all. normalize_city on BOTH sides -- a delegate without a city reads as the default
    # city, same rule show_game_tasks already uses.
    if await cities_module_on() and task.get("event_city"):
        user = await get_user(callback.from_user.id)
        if normalize_city(user.get("event_city") if user else None) != normalize_city(task["event_city"]):
            await callback.answer("Это задание для другого города", show_alert=True)
            return

    # A-05 (созвон 13.08): дедлайн мягкий -- НЕ блокирует сдачу. Единственный оставшийся
    # серверный гвард на этом пути -- дубль-сдача (T-09-09), проверяется ниже.
    active = await get_active_submission(task_id, callback.from_user.id)
    if active is not None:
        await callback.answer("Уже отправлено, ожидай проверки", show_alert=True)
        return

    # T-14-01 (GAME-08, Pitfall 3): a delegate may already have the OLD task-list message
    # open with the old "Сдать" button when the manager archives the task -- Telegram never
    # retroactively disables an already-sent inline keyboard. The gate belongs HERE, not only
    # in the list-render filter (list_active_tasks already excludes archived tasks from the
    # CURRENT render, but that does nothing for a stale message already on the delegate's
    # screen).
    if task.get("archived_at"):
        await callback.answer(
            "Это задание убрали в архив — сдать его больше нельзя. Загляни в «🎯 Мои "
            "задания», там актуальный список.",
            show_alert=True,
        )
        return

    # T-14-03 (GAME-10): resubmit limit, counted server-side on every entry into this
    # handler (not cached in FSM/keyboard state) -- a limit=0/None both mean "no limit",
    # byte-identical to pre-phase behavior (regression guard).
    limit = await get_setting_typed("game_resubmit_limit")
    if limit:
        rejected_count = await count_rejected_submissions(task_id, callback.from_user.id)
        if rejected_count >= limit:
            await callback.answer(
                f"Лимит попыток по этому заданию исчерпан ({limit}). Если считаешь, что "
                "это ошибка — напиши менеджеру через «❓ Задать вопрос».",
                show_alert=True,
            )
            return

    await state.update_data(gs_task_id=task_id, gs_parts=[])

    prompt = await _build_proof_prompt(task)
    try:
        deadline_passed = (
            datetime.strptime(task["deadline_at"], "%Y-%m-%d %H:%M:%S") <= datetime.now()
        )
    except (TypeError, ValueError):
        deadline_passed = False
    if deadline_passed:
        # Делегат не должен узнавать об этом только из отсутствия коинов -- предупреждаем
        # прямо в промпте, отправка при этом РАЗРЕШЕНА (A-05, созвон 13.08).
        prompt = (
            "⏰ Срок сдачи вышел. Отправить можно, но начислять коины будет решать менеджер.\n\n"
            + prompt
        )

    await callback.message.answer(prompt, reply_markup=get_cancel_kb())
    # T-091 (CONTEXT.md A): «✅ Готово» must be available from the very first message, or the
    # empty-submission hint can never be reached before anything is sent. Phase 16 (16-02):
    # that message IS the counter -- sent once here, edited in place on every part after.
    sent = await callback.message.answer(
        await _game_counter_text([]), reply_markup=await _game_counter_kb([]),
    )
    await state.update_data(
        gs_counter_msg_id=getattr(sent, "message_id", None),
        gs_counter_chat_id=callback.from_user.id,
    )
    await state.set_state(GameSubmit.proof)
    await callback.answer()


async def _do_cancel_submit(state: FSMContext) -> None:
    await state.update_data(gs_parts=[])
    await state.set_state(None)


@router.message(GameSubmit.proof, F.text.in_({"Отмена"}))
async def cancel_game_submit(message: types.Message, state: FSMContext):
    await _do_cancel_submit(state)
    await message.answer("Действие отменено.", reply_markup=await get_main_menu_kb(message.from_user.id))


@router.message(GameSubmit.proof)
async def receive_proof(message: types.Message, bot: Bot, state: FSMContext):
    kind, content, caption = _classify_part(message)
    if kind is None:
        await message.answer("Не понял, пришли фото, документ, текст или ссылку.")
        return  # остаёмся в GameSubmit.proof, часть НЕ добавлена

    data = await state.get_data()
    parts = list(data.get("gs_parts", []))

    if len(parts) >= MAX_PARTS:
        # CR-01: part NOT added, state NOT reset -- delegate stays in GameSubmit.proof and can
        # still press «✅ Готово». Dedup within one album: an album can carry up to 10 parts
        # that would each hit this branch and produce 10 identical hints.
        mgid = message.media_group_id
        if mgid and mgid == data.get("gs_overflow_mgid"):
            return
        await message.answer(
            f"Больше {MAX_PARTS} частей в одну сдачу не влезет — нажми «✅ Готово», "
            "менеджер уже увидит присланное.",
            reply_markup=await _game_counter_kb([]),  # «Готово»/«Отмена» под рукой, без «Убрать»
        )
        if mgid:
            await state.update_data(gs_overflow_mgid=mgid)
        return

    if kind in ("text", "link") and content and len(content) > MAX_TEXT_PART:
        content = content[:MAX_TEXT_PART]

    parts.append({"kind": kind, "content": content, "caption": caption})
    await state.update_data(gs_parts=parts)

    mgid = message.media_group_id
    if mgid:
        if mgid not in _gs_pending_albums:
            _gs_pending_albums[mgid] = True
            _spawn(_ack_album(mgid, bot, message.from_user.id, state))
        return  # ack приходит одним сообщением после сборки альбома, не на каждое фото

    await _edit_counter(bot, data, parts)  # одно служебное сообщение, не новое на каждую часть


@router.callback_query(F.data == "gs_done", GameSubmit.proof)
async def finalize_game_submission(callback: types.CallbackQuery, bot: Bot, state: FSMContext):
    """T-091-01: task_id comes ONLY from state (gs_task_id), never from callback_data --
    can't be tampered with to finalize under a different task."""
    data = await state.get_data()
    task_id = data.get("gs_task_id")
    parts = data.get("gs_parts", [])

    if not parts:
        # CONTEXT.md A: the ONE server-side validation -- state is NOT reset, delegate can
        # keep sending parts.
        await callback.answer(await get_setting_typed("game_proof_empty_hint"), show_alert=True)
        return

    task = await get_task(task_id)
    if task is None:
        # Задание исчезло, пока делегат собирал сдачу -- выходим из состояния, не молчим.
        await state.set_state(None)
        await callback.answer()
        await callback.message.answer(
            "Это задание больше не доступно.",
            reply_markup=await get_main_menu_kb(callback.from_user.id),
        )
        return

    first = parts[0]
    submission_id = await create_submission(
        task_id, callback.from_user.id,
        content_type=_LEGACY_CONTENT_TYPE.get(first["kind"], "text"),
        content=first.get("content") or "",
        submitted_at=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    )
    if submission_id is None:
        # T-09-01/D-05: гонка -- параллельная сдача той же пары успела раньше. Партиционный
        # индекс отклонил вставку. Без уведомления менеджеров, без технической ошибки делегату.
        await state.set_state(None)
        await callback.answer()
        await callback.message.answer(
            "Уже отправлено — кто-то опередил на долю секунды. Обнови список заданий.",
            reply_markup=await get_main_menu_kb(callback.from_user.id),
        )
        return

    for i, part in enumerate(parts):
        await add_submission_part(submission_id, i, part["kind"], part.get("content"), part.get("caption"))

    await state.set_state(None)
    await callback.answer()
    await callback.message.answer(
        await get_setting_typed("game_submit_accepted_text"),
        reply_markup=await get_main_menu_kb(callback.from_user.id),
    )

    submitter_name = callback.from_user.full_name or str(callback.from_user.id)
    # D-13: fan out to every current moderate_game holder, not a bare loop over ADMIN_IDS.
    # Quick 260822: режим (каждую / дайджест) и город делегата — в services/game_digest.py.
    await notify_game_submission(
        bot, submission_id=submission_id, user_id=callback.from_user.id, task_id=task_id,
        task_text=task["text"], submitter_name=submitter_name,
    )


@router.callback_query(F.data == "gs_remove_last", GameSubmit.proof)
async def gs_remove_last(callback: types.CallbackQuery, state: FSMContext):
    """Убирает последнюю часть ЧЕРНОВИКА (FSM gs_parts) -- в БД на этом этапе ещё ничего нет
    (game_submission_parts пишет только finalize_game_submission). T-16-02-01: пусто -> alert."""
    data = await state.get_data()
    parts = list(data.get("gs_parts", []))
    if not parts:
        await callback.answer("Уже пусто", show_alert=True)
        return
    parts.pop()
    await state.update_data(gs_parts=parts)
    try:
        # Кнопка живёт на самом счётчике -- callback.message и есть редактируемое сообщение.
        await callback.message.edit_text(
            await _game_counter_text(parts), reply_markup=await _game_counter_kb(parts),
        )
    except Exception:
        pass
    await callback.answer()


@router.callback_query(F.data == "gs_cancel", GameSubmit.proof)
async def gs_cancel(callback: types.CallbackQuery, state: FSMContext):
    await _do_cancel_submit(state)
    try:
        await callback.message.edit_text("Сдача отменена, части не сохранены.")
    except Exception:
        pass
    await callback.answer()
    # Reply-клавиатуру «Отмена» edit'ом не убрать -- главное меню новым сообщением (UAT-fix c79cd6f).
    await callback.message.answer(
        "Действие отменено.", reply_markup=await get_main_menu_kb(callback.from_user.id),
    )


@router.message(F.text == "💳 Оплата")
async def upload_receipt_entry(message: types.Message, bot: Bot):
    """Re-entry into the payment step for a user who deferred (or lost FSM state on a
    bot restart). The button only appears while a receipt is owed, but re-check here in
    case status changed since the keyboard was rendered."""
    if not await ensure_registered(message):
        return
    from handlers.payment import should_offer_receipt_upload, start_payment_step
    if not await should_offer_receipt_upload(message.from_user.id):
        await message.answer("Оплатили или оплата не требуется.")
        return
    try:
        user_row = await get_user(message.from_user.id)
        participant_type = (user_row or {}).get("participant_type") or "full"
    except Exception as e:
        logger.error(f"Failed to resolve participant_type for {message.from_user.id}, defaulting to 'full': {e}")
        participant_type = "full"
    await start_payment_step(bot, message.from_user.id, participant_type)


# Phase 09.2 (B): shared делегат-city resolve for the four info/contacts screens below --
# same idiom as show_game_tasks (09.1 B), pulled into one helper because now FOUR screens
# read it instead of one. Module off, or any resolve failure (bad row, exception), fails
# soft to None -- a screen must never break because a city couldn't be resolved.
async def _delegate_city(telegram_id: int) -> str | None:
    try:
        if not await cities_module_on():
            return None
        user = await get_user(telegram_id)
        return normalize_city(user.get("event_city") if user else None)
    except Exception as e:
        logger.error(f"_delegate_city failed for {telegram_id}: {e}")
        return None


#ℹ️ Информация о форуме
@router.message(F.text == "ℹ️ Информация о форуме")
async def show_info_menu(message: types.Message):
    if not await ensure_registered(message):
        return

    logger.info(f"User {message.from_user.id} requested Info menu")

    code = await _delegate_city(message.from_user.id)
    event_date = await get_setting_for_city("event_date", code)
    event_time = await get_setting_for_city("event_time", code)
    place_name = await get_setting_for_city("event_place_name", code)

    if event_date and place_name:
        text = "<b>Информация о мероприятии</b>\n\n"
        text += f"🗓 <b>Дата:</b> {html.escape(event_date)}\n"
        if event_time:
            text += f"⌚ <b>Время:</b> {html.escape(event_time)}\n"
        text += f"📍 <b>Место:</b> {html.escape(place_name)}"
    else:
        text = (
            "Информация о мероприятии пока заполняется.\n\n"
            "Выбери, что тебя интересует:"
        )
    await message.answer(text, reply_markup=get_info_submenu_kb(), parse_mode="HTML")

@router.callback_query(F.data == "info_date")
async def info_date(callback: types.CallbackQuery):
    code = await _delegate_city(callback.from_user.id)
    event_date = await get_setting_for_city("event_date", code)
    event_time = await get_setting_for_city("event_time", code)
    if event_date:
        text = f"🗓 Форум пройдет <b>{html.escape(event_date)}</b>!"
        if event_time:
            text += f"\n⌚ Время: {html.escape(event_time)}"
    else:
        text = "🗓 Дата пока уточняется. Скоро сообщим! 🙂"
    await callback.message.answer(text, parse_mode="HTML")
    await callback.answer()

@router.callback_query(F.data == "info_place")
async def info_place(callback: types.CallbackQuery):
    code = await _delegate_city(callback.from_user.id)
    place_name = await get_setting_for_city("event_place_name", code)
    place_address = await get_setting_for_city("event_place_address", code)
    if place_name:
        text = f"<b>Наша площадка — {html.escape(place_name)}!</b> 🚀"
        if place_address:
            text += f"\n\n📍 <b>Адрес:</b> {html.escape(place_address)}"

        # Phase 09.2: venue_photo_file_id is out of the per-city mechanism (RESEARCH
        # Pitfall 1 — photo/file fields are never independently editable registry text,
        # same reasoning that keeps program_photo_file_id/speakers_photo_file_id global).
        venue_photo = await get_setting("venue_photo_file_id")
        if venue_photo:
            try:
                await callback.message.answer_photo(venue_photo, caption=text, parse_mode="HTML")
                await callback.answer()
                return
            except Exception:
                pass

        try:
            photo = FSInputFile("resources/venue.jpg")
            await callback.message.answer_photo(photo, caption=text, parse_mode="HTML")
        except Exception:
            await callback.message.answer(text, parse_mode="HTML")
    else:
        await callback.message.answer(
            "📍 Место проведения в процессе подтверждения. Как только всё будет готово, мы напишем!"
        )

    await callback.answer()


# Phase 09.2: подписи program_caption/speakers_caption не ключи SETTINGS_SCHEMA (пишутся
# как побочный эффект загрузки фото) — их пер-городной вариант отложен, см.
# 09.2-RESEARCH Pitfall 1.
# 📅 Программа форума
@router.message(F.text == "📅 Программа форума")
async def show_program(message: types.Message):
    if not await ensure_registered(message):
        return

    logger.info(f"User {message.from_user.id} requested Program")

    program_file_id = await get_setting("program_photo_file_id")
    program_caption = await get_setting("program_caption")
    program_caption = html.escape(program_caption) if program_caption else program_caption

    if program_file_id:
        try:
            await message.answer_photo(program_file_id, caption=program_caption, parse_mode="HTML")
            return
        except Exception:
            pass

    try:
        photo = FSInputFile("resources/program.jpg")
        await message.answer_photo(photo, caption=program_caption, parse_mode="HTML")
    except Exception:
        # Phase 17.1 (17.1-03): empty-state из реестра.
        await message.answer(await get_setting_typed("program_empty_text"))

# 🗣 Спикеры
@router.message(F.text == "🗣 Спикеры")
async def show_speakers(message: types.Message):
    if not await ensure_registered(message):
        return

    logger.info(f"User {message.from_user.id} requested Speakers")

    speakers_file_id = await get_setting("speakers_photo_file_id")
    speakers_caption = await get_setting("speakers_caption")
    speakers_caption = html.escape(speakers_caption) if speakers_caption else speakers_caption

    if speakers_file_id:
        try:
            await message.answer_photo(speakers_file_id, caption=speakers_caption, parse_mode="HTML")
            return
        except Exception:
            pass

    # Phase 17.1 (17.1-03): empty-state из реестра.
    await message.answer(await get_setting_typed("speakers_empty_text"))

# 📞 Контакты
@router.message(F.text == "📞 Контакты")
async def show_contacts(message: types.Message):
    if not await ensure_registered(message):
        return

    logger.info(f"User {message.from_user.id} requested Contacts")

    code = await _delegate_city(message.from_user.id)
    contact_person = await get_setting_for_city("contact_person", code)
    contact_vk = await get_setting_for_city("contact_vk", code)
    contact_tg = await get_setting_for_city("contact_tg", code)

    if not contact_person and not contact_vk and not contact_tg:
        # Phase 17.1 (17.1-03): empty-state из реестра.
        await message.answer(await get_setting_typed("contacts_empty_text"))
        return

    parts = []
    if contact_person:
        parts.append(f"По всем вопросам пиши сюда: {contact_person}")
    links = []
    if contact_vk:
        links.append(f"VK: {contact_vk}")
    if contact_tg:
        links.append(f"TG: {contact_tg}")
    if links:
        parts.append("Наши группы:\n" + "\n".join(links))

    text = "\n\n".join(parts)
    # WR-04: an invalid admin URL (BUTTON_URL_INVALID) or stray &/< in a contact field under
    # the bot's default HTML parse mode would otherwise fail this send with no fallback.
    try:
        await message.answer(text, reply_markup=get_socials_kb(contact_tg, contact_vk))
    except Exception as e:
        logger.error(f"show_contacts send failed for {message.from_user.id}: {e}")
        await message.answer(text, parse_mode=None)

@router.message(F.text == "🔗 Моя реферальная ссылка")
async def my_referral_link(message: types.Message, bot: Bot):
    if not await ensure_registered(message):
        return

    bot_user = await bot.get_me()
    referral_link = f"https://t.me/{bot_user.username}?start={message.from_user.id}"
    # Phase 17.1 (17.1-01): текст из реестра, ссылка подставляется в {link}.
    tpl = await get_setting_typed("referral_link_prompt_text")
    await message.answer(tpl.format(link=referral_link))


@router.message(F.text == "👥 Мои приглашённые")
async def my_referrals(message: types.Message, bot: Bot):
    if not await ensure_registered(message):
        return

    referrals = await get_referrals(message.from_user.id)

    if not referrals:
        bot_user = await bot.get_me()
        referral_link = f"https://t.me/{bot_user.username}?start={message.from_user.id}"
        empty_tpl = await get_setting_typed("referral_list_empty_text")
        await message.answer(empty_tpl.format(link=referral_link))
        return

    names = "\n".join(f"• {html.escape(str(name))}" for name in referrals)
    # Phase 17.1 (17.1-01): заголовок из реестра; сам список имён строится ботом (не текст,
    # который менеджер может испортить) и приклеивается через тот же «\n\n», что и раньше.
    header_tpl = await get_setting_typed("referral_list_header_text")
    await message.answer(
        f"{header_tpl.format(count=len(referrals))}\n\n{names}",
        parse_mode="HTML",
    )


# ── Quick 260906-8uq (FAQ-01..06): «❓ Частые вопросы» ────────────────────────────────────────
#
# Правило видимости (какой пункт виден делегату, городской перекрывает общий) живёт ровно
# один раз в services/faq.py; здесь — только резолв города делегата (тот же fail-soft приём,
# что process_question ниже использует для фан-аута вопроса по городу) и рендер (text, kb).
FAQ_PAGE_SIZE = 8


async def _delegate_city_for_faq(user_id: int) -> str | None:
    """Fail-soft резолв города делегата — ошибка чтения не должна ронять экран FAQ, только
    сузить его до общих пунктов (city=None), тот же приём, что show_game_tasks/process_question."""
    if not await cities_module_on():
        return None
    try:
        user = await get_user(user_id)
        return normalize_city(user.get("event_city") if user else None)
    except Exception as e:
        logger.error(f"FAQ: city resolve failed for {user_id}: {e}")
        return None


async def _faq_visible_items(city_code: str | None) -> list[dict]:
    try:
        rows = await list_faq_for_city(city_code)
    except Exception as e:
        logger.error(f"FAQ: list_faq_for_city failed for city={city_code!r}: {e}")
        rows = []
    return apply_city_overrides(rows, city_code)


async def faq_screen(city_code: str | None, offset: int = 0) -> tuple[str, InlineKeyboardMarkup]:
    """(text, kb) для экрана «❓ Частые вопросы» — пустой FAQ рисует `faq_empty_text` +
    кнопку «спросить менеджера» вместо пустого сообщения, при любом offset (в т.ч. когда
    пункт исчез между открытием списка и тапом по стейл-клавиатуре)."""
    items = await _faq_visible_items(city_code)

    if not items:
        text = await get_setting_typed("faq_empty_text")
        kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(
            text=await get_setting_typed("faq_ask_button_text"), callback_data="faq_ask",
        )]])
        return text, kb

    text = await get_setting_typed("faq_intro_text")
    page = items[offset: offset + FAQ_PAGE_SIZE]
    buttons: list[list[InlineKeyboardButton]] = [
        [InlineKeyboardButton(
            text=_faq_short(str(item.get("question") or ""), 60),
            callback_data=f"faq_q:{item['id']}",
        )]
        for item in page
    ]
    nav_row: list[InlineKeyboardButton] = []
    if offset > 0:
        nav_row.append(InlineKeyboardButton(
            text="⬅️", callback_data=f"faq_list:{max(0, offset - FAQ_PAGE_SIZE)}",
        ))
    if offset + FAQ_PAGE_SIZE < len(items):
        nav_row.append(InlineKeyboardButton(
            text="➡️", callback_data=f"faq_list:{offset + FAQ_PAGE_SIZE}",
        ))
    if nav_row:
        buttons.append(nav_row)
    buttons.append([InlineKeyboardButton(
        text=await get_setting_typed("faq_ask_button_text"), callback_data="faq_ask",
    )])
    return text, InlineKeyboardMarkup(inline_keyboard=buttons)


async def _start_question_form(message: types.Message, state: FSMContext) -> None:
    """Общий шаг «открыть форму вопроса» — вызывается и из `faq_ask` (кнопка «Не нашёл
    ответ»), и из `ask_organizer_start` (пустой FAQ). Вторая копия текста/состояния
    недопустима (одна и та же форма, один и тот же приглашающий текст)."""
    await message.answer(
        await get_setting_typed("ask_question_prompt_text"),
        reply_markup=get_cancel_kb(),
    )
    await state.set_state(Question.waiting_for_question)


@router.message(F.text == "❓ Частые вопросы")
async def show_faq(message: types.Message):
    if not await ensure_registered(message):
        return
    city = await _delegate_city_for_faq(message.from_user.id)
    text, kb = await faq_screen(city)
    await message.answer(text, parse_mode="HTML", reply_markup=kb)


@router.callback_query(F.data.startswith("faq_list:"))
async def faq_page(callback: types.CallbackQuery):
    try:
        offset = int(callback.data.split(":", 1)[1])
    except (IndexError, ValueError):
        offset = 0
    if offset < 0:
        offset = 0
    city = await _delegate_city_for_faq(callback.from_user.id)
    text, kb = await faq_screen(city, offset)
    await callback.message.edit_text(text, parse_mode="HTML", reply_markup=kb)
    await callback.answer()


@router.callback_query(F.data.startswith("faq_q:"))
async def faq_open_answer(callback: types.CallbackQuery):
    try:
        item_id = int(callback.data.split(":", 1)[1])
    except (IndexError, ValueError):
        await callback.answer("Вопрос не найден.", show_alert=True)
        return
    city = await _delegate_city_for_faq(callback.from_user.id)
    items = await _faq_visible_items(city)
    item = next((r for r in items if r.get("id") == item_id), None)
    if item is None:
        # Стейл-клавиатура: пункт удалили/скрыли/сменили город — экран не пустой, а список.
        text, kb = await faq_screen(city)
        await callback.message.edit_text(text, parse_mode="HTML", reply_markup=kb)
        await callback.answer("Этот вопрос уже недоступен.", show_alert=True)
        return
    text = (
        f"❓ <b>{html.escape(str(item.get('question') or ''))}</b>\n\n"
        f"{html.escape(str(item.get('answer') or ''))}"
    )
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="← К вопросам", callback_data="faq_list:0")],
        [InlineKeyboardButton(
            text=await get_setting_typed("faq_ask_button_text"), callback_data="faq_ask",
        )],
    ])
    await callback.message.edit_text(text, parse_mode="HTML", reply_markup=kb)
    await callback.answer()


@router.callback_query(F.data == "faq_ask")
async def faq_ask(callback: types.CallbackQuery, state: FSMContext):
    await _start_question_form(callback.message, state)
    await callback.answer()


# ❓ Задать вопрос
@router.message(F.text == "❓ Задать вопрос")
async def ask_organizer_start(message: types.Message, state: FSMContext):
    if not await ensure_registered(message):
        return

    logger.info(f"User {message.from_user.id} wants to ask a question")
    # Quick 260906-8uq (FAQ-01..06): непустой FAQ показывается СНАЧАЛА — форма открывается по
    # «Не нашёл ответ» (faq_ask), не сразу. Пустой FAQ — байт-в-байт сегодняшнее поведение
    # (17.1-03), ни одного изменения текста/состояния на этой ветке.
    city = await _delegate_city_for_faq(message.from_user.id)
    try:
        show_faq_first = await has_faq_for_city(city)
    except Exception as e:
        logger.error(f"ask_organizer_start: has_faq_for_city failed for {message.from_user.id}: {e}")
        show_faq_first = False
    if show_faq_first:
        text, kb = await faq_screen(city)
        await message.answer(text, parse_mode="HTML", reply_markup=kb)
        return
    await _start_question_form(message, state)

@router.message(Question.waiting_for_question, F.text.in_({"Отмена", "/cancel"}))
async def cancel_question(message: types.Message, state: FSMContext):
    logger.info(f"User {message.from_user.id} canceled question")
    await state.clear()
    await message.answer("Действие отменено.", reply_markup=await get_main_menu_kb(message.from_user.id))


@router.message(Question.waiting_for_question)
async def process_question(message: types.Message, state: FSMContext, bot: Bot):
    if not message.text:
        await message.answer("Пожалуйста, отправь вопрос текстом.")
        return
    question_text = message.text
    logger.info(f"User {message.from_user.id} sent question: {question_text}")
    user_info = f"@{message.from_user.username}" if message.from_user.username else f"ID: {message.from_user.id}"

    # D-14: the row is created ONCE, before the D-13 fan-out below -- every recipient's copy
    # of admin_text embeds the SAME question_id, so a reply from any one of them resolves to
    # the same claim target (08-RESEARCH Pitfall 6). Do not move this call after the fan-out.
    question_id = await create_question(message.from_user.id, question_text)

    # Phase 09.2 (D, CITY-06): resolve the delegate's city ONCE, same idiom as
    # show_game_tasks (cities_module_on -> get_user -> normalize_city). Fail-soft: a resolve
    # error must never eat the question, so any exception here falls back to city=None
    # (today's global fan-out), same shape as the fail-soft gates in cmd_start.
    city = None
    if await cities_module_on():
        try:
            _q_user = await get_user(message.from_user.id)
            city = normalize_city(_q_user.get("event_city") if _q_user else None)
        except Exception as e:
            logger.error(f"Failed to resolve city for question from {message.from_user.id}: {e}")
            city = None

    admin_text = (
        f"❓ <b>Новый вопрос от {user_info}:</b>\n"
        f"🆔 <code>{message.from_user.id}</code>\n"
        f"🧾 Вопрос #<code>{question_id}</code>\n\n"
        f"{html.escape(question_text)}\n\n"
        f"<i>↩️ Ответьте reply'ем на это сообщение, чтобы отправить ответ.</i>"
    )

    # D-13: fan out to every current moderate_reg holder (falls back to config.ADMIN_IDS if
    # nobody holds it -- T-08-31, never silently dropped). Phase 09.2 (D): city narrows the
    # fan-out to the delegate's city (None = today's global fan-out); the "filter emptied the
    # list" fallback lives inside notify_by_capability/capability_holders, not here.
    sent_count = await notify_by_capability(bot, "moderate_reg", admin_text, parse_mode="HTML", city=city)

    if sent_count > 0:
        # Phase 17.1 (17.1-03): подтверждение из реестра.
        await message.answer(
            await get_setting_typed("ask_question_sent_text"),
            reply_markup=await get_main_menu_kb(message.from_user.id),
        )
    elif config.ADMIN_IDS:
        logger.error(f"Failed to send question from {message.from_user.id} to any admin")
        await message.answer("Не удалось отправить вопрос, попробуйте позже.", reply_markup=await get_main_menu_kb(message.from_user.id))
    else:
        logger.warning("No admins configured to receive questions")
        await message.answer("Администраторы не настроены.", reply_markup=await get_main_menu_kb(message.from_user.id))

    await state.clear()


# ── Phase 19 (08, D-10): точка входа «📱 Приложение» — reply-кнопка ТЕКСТОВАЯ (Pitfall 1:
# KeyboardButton(web_app=...) в reply-клавиатуре даёт simple web view БЕЗ initData, делегат не
# аутентифицируется). Хендлер шлёт сообщение с inline web_app-кнопкой — только там initData
# полный. Полностью вне CapabilityMiddleware (кнопка делегатская, права не нужны).
@router.message(F.text == "📱 Приложение")
async def open_miniapp_button(message: types.Message):
    try:
        enabled = await get_setting_typed("miniapp_enabled") == "on"
        url = config.DASHBOARD_PUBLIC_URL
        # T-19-54/Pitfall 10: выключенный тумблер ИЛИ пустой адрес — короткое человеческое
        # объяснение, что приложение сейчас недоступно, без падения хендлера.
        if not (enabled and url):
            await message.answer(await get_setting_typed("miniapp_disabled_text"))
            return
        kb = InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(
                text=await get_setting_typed("miniapp_open_button"),
                web_app=WebAppInfo(url=url.rstrip("/") + "/app"),
            ),
        ]])
        await message.answer(await get_setting_typed("miniapp_open_text"), reply_markup=kb)
    except Exception as e:
        # Fail-soft: любая ошибка построения кнопки не должна ронять обработчик.
        logger.error(f"open_miniapp_button: failed for {message.from_user.id}: {e}")
        await message.answer(await get_setting_typed("miniapp_disabled_text"))


# Quick 260904-3vm (эстафета): делегат БЕЗ активного FSM-состояния (Registration уже сброшена —
# takeover уже прошёл, а не в узком гонка-окне, которое ловит RegHandoffGuard в
# handlers/reg_handoff.py) пишет произвольный текст, пока анкета открыта в приложении. Placed
# LAST, ПОСЛЕ open_miniapp_button — так все кнопки меню (F.text == "...") сохраняют приоритет:
# аiogram останавливается на первом совпавшем хендлере в router, а этот фолбэк стоит в самом
# хвосте. Вешать его на registration.router нельзя — registration.router подключён РАНЬШЕ
# user_actions.router (main.py), он перехватил бы меню первым.
@router.message(StateFilter(None), F.text)
async def reg_handoff_idle_fallback(message: types.Message) -> None:
    from services.reg_handoff import draft_holder, SURFACE_APP
    from handlers.reg_handoff import handoff_plate

    try:
        draft = await get_reg_draft(message.from_user.id)
    except Exception as e:
        logger.error(f"reg_handoff_idle_fallback: draft lookup failed for {message.from_user.id}: {e}")
        return
    if draft_holder(draft) != SURFACE_APP:
        return
    await handoff_plate(message)
