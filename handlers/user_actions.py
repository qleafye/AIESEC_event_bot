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
    list_waves,  # Phase 32 (32-06, D-31/D-38): доступные амбассадору волны для visible_tasks_for
    get_wave,  # Phase 32 (32-06): экран рейтинга волны
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
    set_ambassador_flag,  # Phase 32 (32-06, D-32/D-38): выход/возврат амбассадора
    set_ambassador_path,  # Phase 32 (32-06, D-24): путь меняет только порядок показа заданий
)
from handlers.admin_caps import notify_by_capability  # D-13: fan out by capability, not bare ADMIN_IDS
# Квик 260915-skg (P7): перевод входа в приложение при lang=en — тот же общий механизм, что
# reg_i18n.say() уже применяет к анкете (ярус A -> tr_map -> русский как есть, T-skg).
from handlers import reg_i18n
from handlers.game_labels import (  # Phase 16 (16-01): single RU-label source; 16-03: shared card render
    category_label, proof_types_label, sort_tasks_for_delegate,
    render_task_card_text as _render_task_card_text, task_deadline_short as _game_task_deadline_short,
    # Phase 32 (32-06, D-27/D-28/D-31/D-36/D-38): один делегатский помощник — амбассадорский
    # блок/порядок по пути на весь проект, срок словами вместо служебной метки.
    task_deadline_text as _game_task_deadline_text,
    visible_tasks_for, sort_tasks_for_ambassador, ambassador_block_index,
)
from services.ambassador_waves import (  # Phase 32 (32-06): участие в волне, рейтинг волны
    eligible_wave_ids, current_wave_for, wave_rating_view, wave_number_label, wave_eligible,
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
    get_socials_kb,
    MENU_TEXTS,
)
from handlers.states import Question, GameSubmit
from settings_schema import get_setting_typed  # Phase 09.1 (A): flow texts live in the registry
from services.background import spawn as _spawn
from services.game_digest import notify_submission as notify_game_submission  # Quick 260822
from services.faq import apply_city_overrides, short as _faq_short  # Quick 260906-8uq
from services.timeutil import msk_now  # Квик 260912-mcj: сравнение с deadline_at (ввод МСК)
from config import config
from reg_engine import build_referral_link  # решение владельца 17.09: один формат amb_<id> везде

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
        await reg_i18n.say(
            message,
            "Чтобы пользоваться ботом, сначала нужно зарегистрироваться. Отправь команду /start.",
        )
        return False

    allowed, kind = _gate_decision(user.get("status"))
    if allowed:
        return True
    if kind == "pending":
        # Phase 17.1 (17.1-01): текст гейта — в реестре (сосед reject_text ниже уже был там).
        await reg_i18n.say(message, await get_setting_typed("pending_gate_text"))
    else:  # rejected
        await reg_i18n.say(
            message,
            await get_setting("reject_text") or "К сожалению, твоя заявка отклонена.",
        )
    return False


# --- Coins (COIN-03) ---

async def render_leaderboard(
    rows: list, requester_id: int, requester_rank, requester_balance: int,
    lang: str = "ru", tr_map: dict | None = None,
) -> str:
    """Phase 17.1 (17.1-01): заголовок/пустой экран/строка «твоё место» — из реестра
    (`leaderboard_*`), поэтому функция стала async, как соседние `_balance_screen`/
    `_render_task_card_text`. Рендер прежний байт-в-байт: дефолт `leaderboard_rank_line_text`
    использует только {rank} и {balance}; {total} доступен менеджеру дополнительно (то же
    «сколько всего человек в рейтинге», что и в `balance_screen_header`).

    Квик 260917-en: `lang`/`tr_map` — тот же контракт, что у `reg_i18n.tr_fmt` (шаблон
    переводится ДО подстановки {rank}/{balance}/{total}, не после — иначе src_hash
    подставленной строки никогда не совпадёт с хешем исходного шаблона в tr_map)."""
    tr_map = tr_map or {}
    lines = [reg_i18n.tr_text(await get_setting_typed("leaderboard_header_text"), lang, tr_map), ""]
    if not rows:
        lines.append(reg_i18n.tr_text(await get_setting_typed("leaderboard_empty_text"), lang, tr_map))
    else:
        for i, row in enumerate(rows, start=1):
            name = row.get("full_name") or row.get("username") or str(row.get("user_id"))
            lines.append(f"{i}. {html.escape(str(name))} — {row.get('balance', 0)}")
    lines.append("")
    rank_text = requester_rank if requester_rank is not None else "—"
    # Same scale-acceptable idiom as _balance_screen (CLAUDE.md: 1000-1500 человек за сезон).
    total = len(await get_leaderboard(10_000))
    lines.append(reg_i18n.tr_fmt(
        await get_setting_typed("leaderboard_rank_line_text"), lang, tr_map,
        rank=rank_text, balance=requester_balance, total=total or "—",
    ))
    return "\n".join(lines)


def _format_coin_entry_line(row: dict, manual_label: str, task_label: str) -> str:
    """`"{dd.mm} {sign}{delta}🪙 — {reason or source label}"` — shared by the balance summary
    (last 5) and the paginated «📜 История» screen. `reason` wins when set; otherwise falls
    back to the RU source label (manual/task), or a plain "—" for NULL/legacy rows.

    Квик 260917-en: `manual_label`/`task_label` приходят УЖЕ переведёнными от вызывающего
    (`_balance_screen`/`_balance_history_screen`) — `reason` НЕ переводится (свободный текст
    менеджера на конкретную операцию, не заранее известный контент)."""
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


async def _balance_screen(
    user_id: int, lang: str = "ru", tr_map: dict | None = None,
) -> tuple[str, InlineKeyboardMarkup]:
    """Phase 16 (16-01, GAME-UI-01): «🪙 Баланс» summary -- header (balance/rank/total) + up
    to 5 most recent operations, «📜 История»/«🏆 Рейтинг» buttons. This IS the entry screen
    (reached from the reply-keyboard button), so no «◀️» row, unlike its two sub-screens."""
    tr_map = tr_map or {}
    balance = await get_balance(user_id)
    rank = await get_user_rank(user_id)
    total = len(await get_leaderboard(10_000))  # scale-acceptable per CLAUDE.md (1000-1500/season)
    header = reg_i18n.tr_fmt(
        await get_setting_typed("balance_screen_header"), lang, tr_map,
        balance=balance, rank=rank if rank is not None else "—", total=total or "—",
    )
    rows = await list_coin_entries_for_user(user_id, limit=5, offset=0)
    lines = [header, ""]
    if not rows:
        lines.append(reg_i18n.tr_text(await get_setting_typed("balance_history_empty"), lang, tr_map))
    else:
        manual_label = reg_i18n.tr_text(await get_setting_typed("balance_source_manual_label"), lang, tr_map)
        task_label = reg_i18n.tr_text(await get_setting_typed("balance_source_task_label"), lang, tr_map)
        for row in rows:
            lines.append(_format_coin_entry_line(row, manual_label, task_label))
    text = "\n".join(lines)
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=reg_i18n.tr_text("📜 История", lang, tr_map), callback_data="gbal_history:0")],
        [InlineKeyboardButton(text=reg_i18n.tr_text("🏆 Рейтинг", lang, tr_map), callback_data="gbal_top")],
    ])
    return text, kb


async def _balance_history_screen(
    user_id: int, offset: int = 0, lang: str = "ru", tr_map: dict | None = None,
) -> tuple[str, InlineKeyboardMarkup]:
    """Same LIMIT/OFFSET + «Страница K из N» + «← Раньше»/«Позже →» idiom as
    `admin_gamification._coins_journal_screen`, scoped to one user_id via
    `list_coin_entries_for_user`/`count_coin_entries_for_user`."""
    tr_map = tr_map or {}
    limit = 10
    total = await count_coin_entries_for_user(user_id)
    rows = await list_coin_entries_for_user(user_id, limit=limit, offset=offset)
    lines = [reg_i18n.tr_text(await get_setting_typed("balance_history_header_text"), lang, tr_map)]
    if total == 0:
        lines.append("")
        lines.append(reg_i18n.tr_text(await get_setting_typed("balance_history_empty"), lang, tr_map))
    else:
        total_pages = (total + limit - 1) // limit
        current_page = offset // limit + 1
        page_word = reg_i18n.tr_text("Страница", lang, tr_map)
        of_word = reg_i18n.tr_text("из", lang, tr_map)
        lines.append(f"{page_word} {current_page} {of_word} {total_pages}")
        lines.append("")
        manual_label = reg_i18n.tr_text(await get_setting_typed("balance_source_manual_label"), lang, tr_map)
        task_label = reg_i18n.tr_text(await get_setting_typed("balance_source_task_label"), lang, tr_map)
        for row in rows:
            lines.append(_format_coin_entry_line(row, manual_label, task_label))
    text = "\n".join(lines)

    buttons: list[list[InlineKeyboardButton]] = []
    nav_row: list[InlineKeyboardButton] = []
    if offset > 0:
        nav_row.append(InlineKeyboardButton(
            text=reg_i18n.tr_text("← Раньше", lang, tr_map), callback_data=f"gbal_history:{max(0, offset - limit)}",
        ))
    if offset + limit < total:
        nav_row.append(InlineKeyboardButton(
            text=reg_i18n.tr_text("Позже →", lang, tr_map), callback_data=f"gbal_history:{offset + limit}",
        ))
    if nav_row:
        buttons.append(nav_row)
    buttons.append([InlineKeyboardButton(text=reg_i18n.tr_text("◀️ Баланс", lang, tr_map), callback_data="gbal_back")])
    return text, InlineKeyboardMarkup(inline_keyboard=buttons)


@router.message(F.text.in_(MENU_TEXTS["menu_coins"]))
async def show_my_coins(message: types.Message):
    if not await ensure_registered(message):
        return
    lang, tr_map = await reg_i18n.ctx_for(message)
    text, kb = await _balance_screen(message.from_user.id, lang, tr_map)
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
    lang, tr_map = await reg_i18n.ctx_for(callback)
    text, kb = await _balance_history_screen(callback.from_user.id, offset=offset, lang=lang, tr_map=tr_map)
    await callback.message.edit_text(text, parse_mode="HTML", reply_markup=kb)
    await callback.answer()


@router.callback_query(F.data == "gbal_top")
async def gbal_top(callback: types.CallbackQuery):
    rows = await get_leaderboard(10)
    rank = await get_user_rank(callback.from_user.id)
    balance = await get_balance(callback.from_user.id)
    lang, tr_map = await reg_i18n.ctx_for(callback)
    text = await render_leaderboard(rows, callback.from_user.id, rank, balance, lang, tr_map)
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=reg_i18n.tr_text("◀️ Баланс", lang, tr_map), callback_data="gbal_back")],
    ])
    await callback.message.edit_text(text, parse_mode="HTML", reply_markup=kb)
    await callback.answer()


@router.callback_query(F.data == "gbal_back")
async def gbal_back(callback: types.CallbackQuery):
    lang, tr_map = await reg_i18n.ctx_for(callback)
    text, kb = await _balance_screen(callback.from_user.id, lang, tr_map)
    await callback.message.edit_text(text, parse_mode="HTML", reply_markup=kb)
    await callback.answer()


@router.message(Command("рейтинг", "rating", "leaderboard"))
async def show_leaderboard(message: types.Message):
    if not await ensure_registered(message):
        return
    rows = await get_leaderboard(10)
    rank = await get_user_rank(message.from_user.id)
    balance = await get_balance(message.from_user.id)
    lang, tr_map = await reg_i18n.ctx_for(message)
    await message.answer(
        await render_leaderboard(rows, message.from_user.id, rank, balance, lang, tr_map),
        parse_mode="HTML",
    )


# --- Gamification: task list + submission (GAME-01/02, wave 3, 09-03) ---

PAGE_SIZE = 6  # Phase 16 (16-01): delegate task-list page size (CONTEXT.md "5-6 заданий на страницу")


# `_game_task_deadline_short` -> handlers/game_labels.py::task_deadline_short (16-03), imported
# above under the old name so every existing call site/test keeps working unchanged.


async def _render_game_task_line(
    index: int, task: dict, active: dict | None, user_id: int,
    lang: str = "ru", tr_map: dict | None = None,
) -> tuple[str, bool]:
    """Renders one task's TWO-line status entry for the delegate list ("N. <emoji> <b>title</b>"
    / tail line). Returns (line, needs_submit_button) -- needs_submit_button is True only when
    the task is genuinely open for a fresh submission (not claimed, not over the resubmit
    limit), matching D-08's «одна сдача на пару» invariant surfaced to the delegate.

    Phase 16 (16-01, GAME-UI-01): RU category via `game_labels.category_label`; a terminal
    "❌ отклонено (попытка K из N)" state (no submit button) when `game_resubmit_limit` is set
    and the delegate has exhausted it -- checked BEFORE the `active is None` branch since a
    terminal task also has `active is None` (rejected submissions never come back from
    `get_active_submission`, D-05).

    Квик 260917-en: строка составная (номер/дата/число монет — не язык), переводим только
    статические словесные фрагменты через ярус A (`i18n_ui_en.UI_EN`) — та же граница, что и
    у остального яруса A: короткие служебные слова, не текст менеджера."""
    tr_map = tr_map or {}
    tr = lambda s: reg_i18n.tr_text(s, lang, tr_map)  # noqa: E731 — локальный алиас, короче на 6 использований ниже
    title = html.escape(task_title(task))
    category = await category_label(task["category"])
    category = tr(category)  # game_category_label_* — теперь в делегатском корпусе (group "game")
    deadline, overdue = _game_task_deadline_short(task)
    overdue_mark = " ⏰" if overdue else ""
    # Phase 32 (32-06, D-27): задание без срока (`deadline` пустая строка) показывает словами
    # `_game_task_deadline_text` («без срока» по умолчанию), а не «до » с пустым хвостом.
    deadline_word = f"{tr('до')} {deadline}" if deadline else await _game_task_deadline_text(task)

    limit = await get_setting_typed("game_resubmit_limit")
    if limit:
        rejected = await count_rejected_submissions(task["id"], user_id)
        if active is None and rejected >= limit:
            return (
                f"{index}. ❌ <b>{title}</b>\n"
                f"{tr('отклонено')} ({tr('попытка')} {rejected} {tr('из')} {limit})"
            ), False

    if active is None:
        tail = f"{category} · {task['coins']}🪙 · {deadline_word}"
        if overdue:
            tail += f" — {tr('срок вышел, сдать ещё можно')}"
        return f"{index}. 📤 <b>{title}</b>{overdue_mark}\n{tail}", True
    if active["status"] == "pending":
        when = active.get("submitted_at") or "—"
        try:
            when = datetime.strptime(when, "%Y-%m-%d %H:%M:%S").strftime("%d.%m %H:%M")
        except (TypeError, ValueError):
            pass
        return f"{index}. ⏳ <b>{title}</b>\n{tr('на проверке')} ({tr('сдано')} {when})", False
    if active["status"] == "approved":
        coins_awarded = active.get("coins_awarded")
        return f"{index}. ✅ <b>{title}</b>\n{tr('принято')} (+{coins_awarded}🪙)", False
    # 'rejected' submissions never come back from get_active_submission (D-05) -- unreachable
    # in practice, kept as a fail-soft fallback rather than a silent KeyError.
    tail = f"{category} · {task['coins']}🪙 · {deadline_word}"
    if overdue:
        tail += f" — {tr('срок вышел, сдать ещё можно')}"
    return f"{index}. 📤 <b>{title}</b>{overdue_mark}\n{tail}", True


async def _game_task_list_screen(
    user_id: int, page: int = 0, lang: str = "ru", tr_map: dict | None = None,
) -> tuple[str, InlineKeyboardMarkup | None]:
    """Shared by `show_game_tasks` (new message), `mytask_back`/`gtasks_page` (edit_text back
    from a card or a page-nav tap) -- always returns a text (registry `game_task_list_empty`
    when there are no active tasks) + kb (None only when there are no buttons to show).

    Phase 16 (16-01, GAME-UI-01): paginated at `PAGE_SIZE`, numbering is GLOBAL across pages
    (continues from `page*PAGE_SIZE + 1`), a page-nav row ("‹" / "N / M" no-op / "›") is added
    only when there's more than one page.

    Phase 32 (32-06, D-24/D-28/D-31/D-36/D-38): один делегат — один запрос `get_user` (было два
    отдельных условных чтения); тот же `user`/`code` идут и в фильтр видимости заданий, и в
    список доступных волн амбассадора."""
    tr_map = tr_map or {}
    user = await get_user(user_id)
    cities_on = await cities_module_on()
    code = normalize_city(user.get("event_city") if user else None) if cities_on else None
    if cities_on:
        tasks = await list_active_tasks(city_scope=city_scope(code))
    else:
        tasks = await list_active_tasks()

    # Phase 32 (32-06, D-28/D-31/D-38): единственное правило видимости на проект — ДО сортировки,
    # иначе амбассадорское задание (audience="ambassadors") утечёт не-амбассадору, а задание
    # чужой волны — амбассадору, для которого эта волна недоступна.
    is_ambassador = bool(user and user.get("is_ambassador"))
    wave_ids: set[int] = set()
    if is_ambassador:
        waves = await list_waves(city_scope=city_scope(code) if cities_on else None)
        wave_ids = eligible_wave_ids(user, waves)
    tasks = visible_tasks_for(tasks, is_ambassador=is_ambassador, eligible_wave_ids=wave_ids)

    # Квик 260919-m9x: порядок — не тот, в котором отдаёт БД (`ORDER BY deadline_at ASC`,
    # просроченные сверху): открытые задания идут первыми, просроченные — в хвост. Иначе на
    # первой странице (шесть штук) делегат видел августовские задания, а свежее уезжало на
    # вторую — и сдавал ответ в просроченное. Тот же хелпер у списка в Mini App.
    tasks = sort_tasks_for_delegate(tasks)
    # Phase 32 (32-06, D-24/D-28): амбассадорский блок поднимается наверх, внутри него —
    # задания выбранного пути первыми; у не-амбассадора без пути результат байт-в-байт прежний.
    tasks = sort_tasks_for_ambassador(
        tasks, is_ambassador=is_ambassador, path=user.get("ambassador_path") if user else None,
    )
    if not tasks:
        return reg_i18n.tr_text(await get_setting_typed("game_task_list_empty"), lang, tr_map), None

    total_pages = max(1, -(-len(tasks) // PAGE_SIZE))
    page = max(0, min(page, total_pages - 1))
    page_tasks = tasks[page * PAGE_SIZE:(page + 1) * PAGE_SIZE]

    lines = []
    if total_pages > 1:
        page_label = await get_setting_typed("game_task_list_page_label")
        lines.append(reg_i18n.tr_fmt(page_label, lang, tr_map, page=page + 1, total=total_pages))

    # Phase 32 (32-06, D-28): сколько первых элементов УЖЕ отсортированного списка — блок
    # амбассадора; заголовок вставляется ровно перед первым его заданием (глобальный индекс 0),
    # только если блок непуст — пустой блок не меняет экран ни на байт.
    block_count = ambassador_block_index(tasks) if is_ambassador else 0

    buttons = []
    for offset, task in enumerate(page_tasks):
        global_index = page * PAGE_SIZE + offset
        i = global_index + 1
        if block_count and global_index == 0:
            header = reg_i18n.tr_text(await get_setting_typed("ambassador_block_header_text"), lang, tr_map)
            lines.append(f"<b>{header}</b>")
        active = await get_active_submission(task["id"], user_id)
        line, needs_button = await _render_game_task_line(i, task, active, user_id, lang, tr_map)
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

    # Phase 32 (32-06, D-29): кнопка рейтинга волны — только амбассадору, у которого прямо
    # сейчас есть доступная активная волна (та же пара current_wave_for + wave_eligible, что
    # обработчик `ambwave` перепроверит заново по своему тапу — кнопка НЕ несёт wave_id, гейт
    # целиком на стороне обработчика, T-32-06-01).
    if is_ambassador:
        current_wave = await current_wave_for(code)
        if current_wave and wave_eligible(user, current_wave):
            wave_label = reg_i18n.tr_text("🏅 Рейтинг волны", lang, tr_map)
            buttons.append([InlineKeyboardButton(text=wave_label, callback_data="ambwave")])

    kb = InlineKeyboardMarkup(inline_keyboard=buttons) if buttons else None
    return "\n\n".join(lines), kb


@router.message(F.text.in_(MENU_TEXTS["menu_game_tasks"]))
async def show_game_tasks(message: types.Message):
    if not await ensure_registered(message):
        return
    lang, tr_map = await reg_i18n.ctx_for(message)
    text, kb = await _game_task_list_screen(message.from_user.id, page=0, lang=lang, tr_map=tr_map)
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


def _game_task_card_kb(task_id: int, can_submit: bool, lang: str = "ru", tr_map: dict | None = None) -> InlineKeyboardMarkup:
    """Phase 16 (16-01): «◀️ Назад» now targets `gtasks_back:0` (CONTEXT.md rename, page
    threading from list to card not implemented this plan -- `mytask_back` parses generically
    so a future plan can build `gtasks_back:N` for N>0 without another rename). `can_submit`
    False (stale-pending re-tap, T-16-01-02) drops the "📤 Сдать" row entirely."""
    tr_map = tr_map or {}
    submit_text = reg_i18n.tr_text("📤 Сдать", lang, tr_map)
    back_text = reg_i18n.tr_text("◀️ Назад", lang, tr_map)
    if can_submit:
        return InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text=submit_text, callback_data=f"mytask_submit:{task_id}")],
            [InlineKeyboardButton(text=back_text, callback_data="gtasks_back:0")],
        ])
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=back_text, callback_data="gtasks_back:0")],
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
    card to a submit-less "на проверке" variant, closing the stale-tap race).

    Квик 260917-en: `_render_task_card_text` живёт в корневом `game_labels.py`, который делит
    и Mini App (`miniapp/routers/*`) — сигнатуру той функции не трогаем, чтобы не разойтись со
    вторым исполнителем на приложении; переводим только `status_line`, который СОБИРАЕТСЯ
    здесь (бот-only код) ДО передачи внутрь."""
    lang, tr_map = await reg_i18n.ctx_for(callback)
    try:
        task_id = int(callback.data.split(":", 1)[1])
    except ValueError:
        await callback.answer(reg_i18n.tr_text("Некорректное задание", lang, tr_map), show_alert=True)
        return
    task = await get_task(task_id)
    if task is None or task.get("archived_at"):
        # Same wording as mytask_submit_start's own archived-task alert (T-14-01 precedent) --
        # a stale list message can still carry a button for a task removed since it was sent.
        await callback.answer(
            reg_i18n.tr_text(
                "Это задание убрали в архив — сдать его больше нельзя. Загляни в «🎯 Задания», "
                "там актуальный список.",
                lang, tr_map,
            ),
            show_alert=True,
        )
        return

    active = await get_active_submission(task_id, callback.from_user.id)
    if active is not None and active["status"] == "pending":
        status_line = reg_i18n.tr_text("на проверке", lang, tr_map)
        can_submit = False
        attempt = None
    else:
        limit = await get_setting_typed("game_resubmit_limit")
        rejected = await count_rejected_submissions(task_id, callback.from_user.id) if limit else 0
        if limit and rejected:
            new_word = reg_i18n.tr_text("новое", lang, tr_map)
            attempt_word = reg_i18n.tr_text("попытка", lang, tr_map)
            of_word = reg_i18n.tr_text("из", lang, tr_map)
            status_line = f"{new_word} · {attempt_word} {rejected} {of_word} {limit}"
        else:
            status_line = reg_i18n.tr_text("новое", lang, tr_map)
        can_submit = True
        attempt = rejected

    card_text = await _render_task_card_text(task, status_line, attempt)
    card_kb = _game_task_card_kb(task_id, can_submit, lang, tr_map)
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
    lang, tr_map = await reg_i18n.ctx_for(callback)
    text, kb = await _game_task_list_screen(callback.from_user.id, page=page, lang=lang, tr_map=tr_map)
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
    lang, tr_map = await reg_i18n.ctx_for(callback)
    text, kb = await _game_task_list_screen(callback.from_user.id, page=page, lang=lang, tr_map=tr_map)
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
    await _edit_counter(bot, data, list(data.get("gs_parts", [])), telegram_id=chat_id)


@router.callback_query(F.data.startswith("mytask_submit:"))
async def mytask_submit_start(callback: types.CallbackQuery, state: FSMContext):
    lang, tr_map = await reg_i18n.ctx_for(callback)
    try:
        task_id = int(callback.data.split(":", 1)[1])
    except ValueError:
        await callback.answer(reg_i18n.tr_text("Некорректное задание", lang, tr_map), show_alert=True)
        return

    task = await get_task(task_id)
    if task is None:
        await callback.answer(reg_i18n.tr_text("Задание не найдено", lang, tr_map), show_alert=True)
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
            await callback.answer(reg_i18n.tr_text("Это задание для другого города", lang, tr_map), show_alert=True)
            return

    # A-05 (созвон 13.08): дедлайн мягкий -- НЕ блокирует сдачу. Единственный оставшийся
    # серверный гвард на этом пути -- дубль-сдача (T-09-09), проверяется ниже.
    active = await get_active_submission(task_id, callback.from_user.id)
    if active is not None:
        await callback.answer(reg_i18n.tr_text("Уже отправлено, ожидай проверки", lang, tr_map), show_alert=True)
        return

    # T-14-01 (GAME-08, Pitfall 3): a delegate may already have the OLD task-list message
    # open with the old "Сдать" button when the manager archives the task -- Telegram never
    # retroactively disables an already-sent inline keyboard. The gate belongs HERE, not only
    # in the list-render filter (list_active_tasks already excludes archived tasks from the
    # CURRENT render, but that does nothing for a stale message already on the delegate's
    # screen).
    if task.get("archived_at"):
        await callback.answer(
            reg_i18n.tr_text(
                "Это задание убрали в архив — сдать его больше нельзя. Загляни в «🎯 Мои "
                "задания», там актуальный список.",
                lang, tr_map,
            ),
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
                reg_i18n.tr_fmt(
                    "Лимит попыток по этому заданию исчерпан ({limit}). Если считаешь, что "
                    "это ошибка — напиши менеджеру через «❓ Задать вопрос».",
                    lang, tr_map, limit=limit,
                ),
                show_alert=True,
            )
            return

    await state.update_data(gs_task_id=task_id, gs_parts=[])

    prompt = await _build_proof_prompt(task)
    prompt = reg_i18n.tr_text(prompt, lang, tr_map)
    # Phase 32 (32-06, D-27): единственный разбор `deadline_at` — общий помощник
    # `game_labels.task_deadline_short` (тот же, что красит строку в списке/карточке), а не
    # собственная копия `strptime` здесь — задание без срока (`NO_DEADLINE_AT`) никогда не даёт
    # «просрочено» (helper возвращает `("", False)`), и правило одно на весь проект.
    _, deadline_passed = _game_task_deadline_short(task)
    if deadline_passed:
        # Делегат не должен узнавать об этом только из отсутствия коинов -- предупреждаем
        # прямо в промпте, отправка при этом РАЗРЕШЕНА (A-05, созвон 13.08).
        prompt = (
            reg_i18n.tr_text(
                "⏰ Срок сдачи вышел. Отправить можно, но начислять коины будет решать менеджер.",
                lang, tr_map,
            ) + "\n\n" + prompt
        )

    await callback.message.answer(prompt, reply_markup=get_cancel_kb())
    # T-091 (CONTEXT.md A): «✅ Готово» must be available from the very first message, or the
    # empty-submission hint can never be reached before anything is sent. Phase 16 (16-02):
    # that message IS the counter -- sent once here, edited in place on every part after.
    sent = await callback.message.answer(
        await _game_counter_text([], lang, tr_map), reply_markup=await _game_counter_kb([], lang, tr_map),
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
    await reg_i18n.say(message, "Действие отменено.", reply_markup=await get_main_menu_kb(message.from_user.id))


@router.message(GameSubmit.proof)
async def receive_proof(message: types.Message, bot: Bot, state: FSMContext):
    kind, content, caption = _classify_part(message)
    if kind is None:
        await reg_i18n.say(message, "Не понял, пришли фото, документ, текст или ссылку.")
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
        lang, tr_map = await reg_i18n.ctx_for(message)
        await message.answer(
            reg_i18n.tr_fmt(
                "Больше {max_parts} частей в одну сдачу не влезет — нажми «✅ Готово», "
                "менеджер уже увидит присланное.",
                lang, tr_map, max_parts=MAX_PARTS,
            ),
            reply_markup=await _game_counter_kb([], lang, tr_map),  # «Готово»/«Отмена» под рукой, без «Убрать»
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

    await _edit_counter(bot, data, parts, telegram_id=message.from_user.id)  # одно служебное сообщение, не новое на каждую часть


@router.callback_query(F.data == "gs_done", GameSubmit.proof)
async def finalize_game_submission(callback: types.CallbackQuery, bot: Bot, state: FSMContext):
    """T-091-01: task_id comes ONLY from state (gs_task_id), never from callback_data --
    can't be tampered with to finalize under a different task."""
    lang, tr_map = await reg_i18n.ctx_for(callback)
    data = await state.get_data()
    task_id = data.get("gs_task_id")
    parts = data.get("gs_parts", [])

    if not parts:
        # CONTEXT.md A: the ONE server-side validation -- state is NOT reset, delegate can
        # keep sending parts.
        await callback.answer(
            reg_i18n.tr_text(await get_setting_typed("game_proof_empty_hint"), lang, tr_map), show_alert=True,
        )
        return

    task = await get_task(task_id)
    if task is None:
        # Задание исчезло, пока делегат собирал сдачу -- выходим из состояния, не молчим.
        await state.set_state(None)
        await callback.answer()
        await callback.message.answer(
            reg_i18n.tr_text("Это задание больше не доступно.", lang, tr_map),
            reply_markup=await get_main_menu_kb(callback.from_user.id),
        )
        return

    first = parts[0]
    submission_id = await create_submission(
        task_id, callback.from_user.id,
        content_type=_LEGACY_CONTENT_TYPE.get(first["kind"], "text"),
        content=first.get("content") or "",
        submitted_at=msk_now().strftime("%Y-%m-%d %H:%M:%S"),
    )
    if submission_id is None:
        # T-09-01/D-05: гонка -- параллельная сдача той же пары успела раньше. Партиционный
        # индекс отклонил вставку. Без уведомления менеджеров, без технической ошибки делегату.
        await state.set_state(None)
        await callback.answer()
        await callback.message.answer(
            reg_i18n.tr_text("Уже отправлено — кто-то опередил на долю секунды. Обнови список заданий.", lang, tr_map),
            reply_markup=await get_main_menu_kb(callback.from_user.id),
        )
        return

    for i, part in enumerate(parts):
        await add_submission_part(submission_id, i, part["kind"], part.get("content"), part.get("caption"))

    await state.set_state(None)
    await callback.answer()
    await callback.message.answer(
        reg_i18n.tr_text(await get_setting_typed("game_submit_accepted_text"), lang, tr_map),
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
        await callback.answer(await reg_i18n.tr_for(callback, "Уже пусто"), show_alert=True)
        return
    parts.pop()
    await state.update_data(gs_parts=parts)
    lang, tr_map = await reg_i18n.ctx_for(callback)
    try:
        # Кнопка живёт на самом счётчике -- callback.message и есть редактируемое сообщение.
        await callback.message.edit_text(
            await _game_counter_text(parts, lang, tr_map), reply_markup=await _game_counter_kb(parts, lang, tr_map),
        )
    except Exception:
        pass
    await callback.answer()


@router.callback_query(F.data == "gs_cancel", GameSubmit.proof)
async def gs_cancel(callback: types.CallbackQuery, state: FSMContext):
    await _do_cancel_submit(state)
    try:
        await callback.message.edit_text(await reg_i18n.tr_for(callback, "Сдача отменена, части не сохранены."))
    except Exception:
        pass
    await callback.answer()
    # Reply-клавиатуру «Отмена» edit'ом не убрать -- главное меню новым сообщением (UAT-fix c79cd6f).
    await reg_i18n.say(
        callback.message, "Действие отменено.", reply_markup=await get_main_menu_kb(callback.from_user.id),
    )


@router.message(F.text.in_(MENU_TEXTS["menu_payment"]))
async def upload_receipt_entry(message: types.Message, bot: Bot):
    """Re-entry into the payment step for a user who deferred (or lost FSM state on a
    bot restart). The button only appears while a receipt is owed, but re-check here in
    case status changed since the keyboard was rendered."""
    if not await ensure_registered(message):
        return
    from handlers.payment import should_offer_receipt_upload, start_payment_step
    if not await should_offer_receipt_upload(message.from_user.id):
        await reg_i18n.say(message, "Оплатили или оплата не требуется.")
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
@router.message(F.text.in_(MENU_TEXTS["menu_info"]))
async def show_info_menu(message: types.Message):
    if not await ensure_registered(message):
        return

    logger.info(f"User {message.from_user.id} requested Info menu")

    lang, tr_map = await reg_i18n.ctx_for(message)
    tr = lambda s: reg_i18n.tr_text(s, lang, tr_map)  # noqa: E731

    code = await _delegate_city(message.from_user.id)
    event_date = await get_setting_for_city("event_date", code)
    event_time = await get_setting_for_city("event_time", code)
    place_name = await get_setting_for_city("event_place_name", code)

    if event_date and place_name:
        text = f"<b>{tr('Информация о мероприятии')}</b>\n\n"
        text += f"🗓 <b>{tr('Дата')}:</b> {html.escape(tr(event_date))}\n"
        if event_time:
            text += f"⌚ <b>{tr('Время')}:</b> {html.escape(tr(event_time))}\n"
        text += f"📍 <b>{tr('Место')}:</b> {html.escape(tr(place_name))}"
    else:
        text = (
            f"{tr('Информация о мероприятии пока заполняется.')}\n\n"
            f"{tr('Выбери, что тебя интересует:')}"
        )
    await message.answer(text, reply_markup=get_info_submenu_kb(), parse_mode="HTML")

@router.callback_query(F.data == "info_date")
async def info_date(callback: types.CallbackQuery):
    lang, tr_map = await reg_i18n.ctx_for(callback)
    tr = lambda s: reg_i18n.tr_text(s, lang, tr_map)  # noqa: E731
    code = await _delegate_city(callback.from_user.id)
    event_date = await get_setting_for_city("event_date", code)
    event_time = await get_setting_for_city("event_time", code)
    if event_date:
        text = f"🗓 {tr('Форум пройдет')} <b>{html.escape(tr(event_date))}</b>!"
        if event_time:
            text += f"\n⌚ {tr('Время')}: {html.escape(tr(event_time))}"
    else:
        text = tr("🗓 Дата пока уточняется. Скоро сообщим! 🙂")
    await callback.message.answer(text, parse_mode="HTML")
    await callback.answer()

@router.callback_query(F.data == "info_place")
async def info_place(callback: types.CallbackQuery):
    lang, tr_map = await reg_i18n.ctx_for(callback)
    tr = lambda s: reg_i18n.tr_text(s, lang, tr_map)  # noqa: E731
    code = await _delegate_city(callback.from_user.id)
    place_name = await get_setting_for_city("event_place_name", code)
    place_address = await get_setting_for_city("event_place_address", code)
    if place_name:
        text = f"<b>{tr('Наша площадка')} — {html.escape(tr(place_name))}!</b> 🚀"
        if place_address:
            text += f"\n\n📍 <b>{tr('Адрес')}:</b> {html.escape(tr(place_address))}"

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
            tr("📍 Место проведения в процессе подтверждения. Как только всё будет готово, мы напишем!")
        )

    await callback.answer()


# Phase 09.2: подписи program_caption/speakers_caption не ключи SETTINGS_SCHEMA (пишутся
# как побочный эффект загрузки фото) — их пер-городной вариант отложен, см.
# 09.2-RESEARCH Pitfall 1.
# 📅 Программа форума
@router.message(F.text.in_(MENU_TEXTS["menu_program"]))
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
        await reg_i18n.say(message, await get_setting_typed("program_empty_text"))

# 🗣 Спикеры
@router.message(F.text.in_(MENU_TEXTS["menu_speakers"]))
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
    await reg_i18n.say(message, await get_setting_typed("speakers_empty_text"))

# 📞 Контакты
@router.message(F.text.in_(MENU_TEXTS["menu_contacts"]))
async def show_contacts(message: types.Message):
    if not await ensure_registered(message):
        return

    logger.info(f"User {message.from_user.id} requested Contacts")

    lang, tr_map = await reg_i18n.ctx_for(message)
    code = await _delegate_city(message.from_user.id)
    contact_person = await get_setting_for_city("contact_person", code)
    contact_vk = await get_setting_for_city("contact_vk", code)
    contact_tg = await get_setting_for_city("contact_tg", code)

    if not contact_person and not contact_vk and not contact_tg:
        # Phase 17.1 (17.1-03): empty-state из реестра.
        await reg_i18n.say(message, await get_setting_typed("contacts_empty_text"))
        return

    parts = []
    if contact_person:
        # Квик 260917-en: contact_person/contact_vk/contact_tg сами НЕ переводятся
        # (юзернейм/URL, см. services/i18n_sources.py::_NON_LANGUAGE_EVENT_KEYS) — переводим
        # только обёртку вокруг них.
        parts.append(f"{reg_i18n.tr_text('По всем вопросам пиши сюда', lang, tr_map)}: {contact_person}")
    links = []
    if contact_vk:
        links.append(f"VK: {contact_vk}")
    if contact_tg:
        links.append(f"TG: {contact_tg}")
    if links:
        parts.append(f"{reg_i18n.tr_text('Наши группы', lang, tr_map)}:\n" + "\n".join(links))

    text = "\n\n".join(parts)
    # WR-04: an invalid admin URL (BUTTON_URL_INVALID) or stray &/< in a contact field under
    # the bot's default HTML parse mode would otherwise fail this send with no fallback.
    try:
        await message.answer(text, reply_markup=get_socials_kb(contact_tg, contact_vk))
    except Exception as e:
        logger.error(f"show_contacts send failed for {message.from_user.id}: {e}")
        await message.answer(text, parse_mode=None)

@router.message(F.text.in_(MENU_TEXTS["menu_referral"]))
async def my_referral_link(message: types.Message, bot: Bot):
    if not await ensure_registered(message):
        return

    bot_user = await bot.get_me()
    referral_link = build_referral_link(bot_user.username, message.from_user.id)
    # Phase 17.1 (17.1-01): текст из реестра, ссылка подставляется в {link}.
    lang, tr_map = await reg_i18n.ctx_for(message)
    tpl = await get_setting_typed("referral_link_prompt_text")
    await message.answer(reg_i18n.tr_fmt(tpl, lang, tr_map, link=referral_link))


@router.message(F.text.in_(MENU_TEXTS["menu_invites"]))
async def my_referrals(message: types.Message, bot: Bot):
    if not await ensure_registered(message):
        return

    lang, tr_map = await reg_i18n.ctx_for(message)
    referrals = await get_referrals(message.from_user.id)

    if not referrals:
        bot_user = await bot.get_me()
        referral_link = build_referral_link(bot_user.username, message.from_user.id)
        empty_tpl = await get_setting_typed("referral_list_empty_text")
        await message.answer(reg_i18n.tr_fmt(empty_tpl, lang, tr_map, link=referral_link))
        return

    names = "\n".join(f"• {html.escape(str(name))}" for name in referrals)
    # Phase 17.1 (17.1-01): заголовок из реестра; сам список имён строится ботом (не текст,
    # который менеджер может испортить) и приклеивается через тот же «\n\n», что и раньше.
    header_tpl = await get_setting_typed("referral_list_header_text")
    await message.answer(
        f"{reg_i18n.tr_fmt(header_tpl, lang, tr_map, count=len(referrals))}\n\n{names}",
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


async def faq_screen(
    city_code: str | None, offset: int = 0, lang: str = "ru", tr_map: dict | None = None,
) -> tuple[str, InlineKeyboardMarkup]:
    """(text, kb) для экрана «❓ Частые вопросы» — пустой FAQ рисует `faq_empty_text` +
    кнопку «спросить менеджера» вместо пустого сообщения, при любом offset (в т.ч. когда
    пункт исчез между открытием списка и тапом по стейл-клавиатуре).

    Квик 260917-en: сами вопросы/ответы FAQ (`item["question"]`/`item["answer"]`) — свободный
    текст менеджера per-пункт, вне делегатского корпуса (см. докстринг `services/i18n_sources.py`
    — та же граница, что у текста рассылок/опросов); переводятся только структурные подписи
    экрана (вступление/пусто/кнопки/навигация)."""
    tr_map = tr_map or {}
    items = await _faq_visible_items(city_code)

    if not items:
        text = reg_i18n.tr_text(await get_setting_typed("faq_empty_text"), lang, tr_map)
        kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(
            text=reg_i18n.tr_text(await get_setting_typed("faq_ask_button_text"), lang, tr_map),
            callback_data="faq_ask",
        )]])
        return text, kb

    text = reg_i18n.tr_text(await get_setting_typed("faq_intro_text"), lang, tr_map)
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
        text=reg_i18n.tr_text(await get_setting_typed("faq_ask_button_text"), lang, tr_map), callback_data="faq_ask",
    )])
    return text, InlineKeyboardMarkup(inline_keyboard=buttons)


async def _start_question_form(message: types.Message, state: FSMContext) -> None:
    """Общий шаг «открыть форму вопроса» — вызывается и из `faq_ask` (кнопка «Не нашёл
    ответ»), и из `ask_organizer_start` (пустой FAQ). Вторая копия текста/состояния
    недопустима (одна и та же форма, один и тот же приглашающий текст)."""
    await reg_i18n.say(
        message,
        await get_setting_typed("ask_question_prompt_text"),
        reply_markup=get_cancel_kb(),
    )
    await state.set_state(Question.waiting_for_question)


@router.message(F.text.in_(MENU_TEXTS["menu_faq"]))
async def show_faq(message: types.Message):
    if not await ensure_registered(message):
        return
    lang, tr_map = await reg_i18n.ctx_for(message)
    city = await _delegate_city_for_faq(message.from_user.id)
    text, kb = await faq_screen(city, lang=lang, tr_map=tr_map)
    await message.answer(text, parse_mode="HTML", reply_markup=kb)


@router.callback_query(F.data.startswith("faq_list:"))
async def faq_page(callback: types.CallbackQuery):
    try:
        offset = int(callback.data.split(":", 1)[1])
    except (IndexError, ValueError):
        offset = 0
    if offset < 0:
        offset = 0
    lang, tr_map = await reg_i18n.ctx_for(callback)
    city = await _delegate_city_for_faq(callback.from_user.id)
    text, kb = await faq_screen(city, offset, lang, tr_map)
    await callback.message.edit_text(text, parse_mode="HTML", reply_markup=kb)
    await callback.answer()


@router.callback_query(F.data.startswith("faq_q:"))
async def faq_open_answer(callback: types.CallbackQuery):
    lang, tr_map = await reg_i18n.ctx_for(callback)
    try:
        item_id = int(callback.data.split(":", 1)[1])
    except (IndexError, ValueError):
        await callback.answer(reg_i18n.tr_text("Вопрос не найден.", lang, tr_map), show_alert=True)
        return
    city = await _delegate_city_for_faq(callback.from_user.id)
    items = await _faq_visible_items(city)
    item = next((r for r in items if r.get("id") == item_id), None)
    if item is None:
        # Стейл-клавиатура: пункт удалили/скрыли/сменили город — экран не пустой, а список.
        text, kb = await faq_screen(city, lang=lang, tr_map=tr_map)
        await callback.message.edit_text(text, parse_mode="HTML", reply_markup=kb)
        await callback.answer(reg_i18n.tr_text("Этот вопрос уже недоступен.", lang, tr_map), show_alert=True)
        return
    # Квик 260917-en: сам вопрос/ответ — свободный текст менеджера per-пункт FAQ, не переводится
    # (см. faq_screen docstring выше).
    text = (
        f"❓ <b>{html.escape(str(item.get('question') or ''))}</b>\n\n"
        f"{html.escape(str(item.get('answer') or ''))}"
    )
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=reg_i18n.tr_text("← К вопросам", lang, tr_map), callback_data="faq_list:0")],
        [InlineKeyboardButton(
            text=reg_i18n.tr_text(await get_setting_typed("faq_ask_button_text"), lang, tr_map), callback_data="faq_ask",
        )],
    ])
    await callback.message.edit_text(text, parse_mode="HTML", reply_markup=kb)
    await callback.answer()


@router.callback_query(F.data == "faq_ask")
async def faq_ask(callback: types.CallbackQuery, state: FSMContext):
    await _start_question_form(callback.message, state)
    await callback.answer()


# ❓ Задать вопрос
@router.message(F.text.in_(MENU_TEXTS["menu_question"]))
async def ask_organizer_start(message: types.Message, state: FSMContext):
    if not await ensure_registered(message):
        return

    logger.info(f"User {message.from_user.id} wants to ask a question")
    # Quick 260906-8uq (FAQ-01..06): непустой FAQ показывается СНАЧАЛА — форма открывается по
    # «Не нашёл ответ» (faq_ask), не сразу. Пустой FAQ — байт-в-байт сегодняшнее поведение
    # (17.1-03), ни одного изменения текста/состояния на этой ветке.
    lang, tr_map = await reg_i18n.ctx_for(message)
    city = await _delegate_city_for_faq(message.from_user.id)
    try:
        show_faq_first = await has_faq_for_city(city)
    except Exception as e:
        logger.error(f"ask_organizer_start: has_faq_for_city failed for {message.from_user.id}: {e}")
        show_faq_first = False
    if show_faq_first:
        text, kb = await faq_screen(city, lang=lang, tr_map=tr_map)
        await message.answer(text, parse_mode="HTML", reply_markup=kb)
        return
    await _start_question_form(message, state)

@router.message(Question.waiting_for_question, F.text.in_({"Отмена", "/cancel"}))
async def cancel_question(message: types.Message, state: FSMContext):
    logger.info(f"User {message.from_user.id} canceled question")
    await state.clear()
    await reg_i18n.say(message, "Действие отменено.", reply_markup=await get_main_menu_kb(message.from_user.id))


@router.message(Question.waiting_for_question)
async def process_question(message: types.Message, state: FSMContext, bot: Bot):
    if not message.text:
        await reg_i18n.say(message, "Пожалуйста, отправь вопрос текстом.")
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
        await reg_i18n.say(
            message,
            await get_setting_typed("ask_question_sent_text"),
            reply_markup=await get_main_menu_kb(message.from_user.id),
        )
    elif config.ADMIN_IDS:
        logger.error(f"Failed to send question from {message.from_user.id} to any admin")
        await reg_i18n.say(message, "Не удалось отправить вопрос, попробуйте позже.", reply_markup=await get_main_menu_kb(message.from_user.id))
    else:
        logger.warning("No admins configured to receive questions")
        await reg_i18n.say(message, "Администраторы не настроены.", reply_markup=await get_main_menu_kb(message.from_user.id))

    await state.clear()


# ── Phase 19 (08, D-10): точка входа «📱 Приложение» — reply-кнопка ТЕКСТОВАЯ (Pitfall 1:
# KeyboardButton(web_app=...) в reply-клавиатуре даёт simple web view БЕЗ initData, делегат не
# аутентифицируется). Хендлер шлёт сообщение с inline web_app-кнопкой — только там initData
# полный. Полностью вне CapabilityMiddleware (кнопка делегатская, права не нужны).
@router.message(F.text.in_(MENU_TEXTS["menu_miniapp"]))
async def open_miniapp_button(message: types.Message):
    # Квик 260915-skg (P7): реестровые тексты/подпись кнопки этого хендлера уходили сырым
    # message.answer мимо reg_i18n — reply-кнопка меню уже переводится (builders.py::MENU_EN),
    # а ОТВЕТ хендлера при lang=en оставался русским. Контекст берём один раз — при lang="ru"
    # tr_text/tr_kb возвращают ТЕ ЖЕ объекты (reg_i18n docstring), русская ветка ничем не платит.
    lang, tr_map = await reg_i18n.ctx_for(message)
    try:
        enabled = await get_setting_typed("miniapp_enabled") == "on"
        url = config.DASHBOARD_PUBLIC_URL
        # T-19-54/Pitfall 10: выключенный тумблер ИЛИ пустой адрес — короткое человеческое
        # объяснение, что приложение сейчас недоступно, без падения хендлера.
        if not (enabled and url):
            text = reg_i18n.tr_text(await get_setting_typed("miniapp_disabled_text"), lang, tr_map)
            await message.answer(text)
            return
        kb = InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(
                text=await get_setting_typed("miniapp_open_button"),
                # web_app.url не трогаем переводом — _tr_button переводит только .text
                # (reg_i18n.py::_tr_button докстринг).
                web_app=WebAppInfo(url=url.rstrip("/") + "/app"),
            ),
        ]])
        kb = reg_i18n.tr_kb(kb, lang, tr_map)
        text = reg_i18n.tr_text(await get_setting_typed("miniapp_open_text"), lang, tr_map)
        await message.answer(text, reply_markup=kb)
    except Exception as e:
        # Fail-soft: любая ошибка построения кнопки не должна ронять обработчик.
        logger.error(f"open_miniapp_button: failed for {message.from_user.id}: {e}")
        text = reg_i18n.tr_text(await get_setting_typed("miniapp_disabled_text"), lang, tr_map)
        await message.answer(text)


# Quick 260904-3vm (эстафета): делегат БЕЗ активного FSM-состояния (Registration уже сброшена —
# takeover уже прошёл, а не в узком гонка-окне, которое ловит RegHandoffGuard в
# handlers/reg_handoff.py) пишет произвольный текст, пока анкета открыта в приложении. Placed
# LAST, ПОСЛЕ open_miniapp_button — так все кнопки меню (F.text == "...") сохраняют приоритет:
# аiogram останавливается на первом совпавшем хендлере в router, а этот фолбэк стоит в самом
# хвосте. Вешать его на registration.router нельзя — registration.router подключён РАНЬШЕ
# user_actions.router (main.py), он перехватил бы меню первым.
@router.message(StateFilter(None), F.text)
async def reg_handoff_idle_fallback(message: types.Message) -> None:
    """Квик 260919-u7e (находка #3): расширено вторым, самостоятельным поводом молчать боту
    без ответа. Раньше единственной причиной было «черновик держит приложение» (эстафета,
    260904-3vm) — теперь ЭТА ЖЕ, последняя реально достижимая точка приватного text-пайплайна
    (см. докстринг `handlers/reg_silence_fallback.py` — тот модуль своей текстовой веткой сюда
    физически не дотягивается, аiogram останавливает апдейт уже здесь) обязана поймать и
    второй случай: черновик держит БОТ (или ничей), а живого FSM-состояния нет, потому что
    MemoryStorage не пережила рестарт контейнера — 14 из 38 делегатов, оказавшихся в анкете
    за 20 минут до рестарта 05-16.09, не вернулись ни разу."""
    from services.reg_handoff import draft_holder, SURFACE_APP
    from handlers.reg_handoff import handoff_plate

    try:
        draft = await get_reg_draft(message.from_user.id)
    except Exception as e:
        logger.error(f"reg_handoff_idle_fallback: draft lookup failed for {message.from_user.id}: {e}")
        return
    if draft_holder(draft) == SURFACE_APP:
        await handoff_plate(message)
        return
    from handlers.reg_silence_fallback import offer_if_resumable
    await offer_if_resumable(message)


# ── Phase 32 (32-06, D-29): экран рейтинга волны ─────────────────────────────────────────────
# В самом хвосте файла (golden-снапшот `tests/test_refac_snapshot_260816.py` фиксирует порядок
# роутера — новый callback_query-хендлер обязан быть чистым аппендом, а не вставкой посреди
# уже существующего callback_query-блока).

async def _wave_rating_screen(
    wave_id: int, viewer_id: int, lang: str = "ru", tr_map: dict | None = None,
) -> tuple[str, InlineKeyboardMarkup]:
    """Phase 32 (32-06, D-29): экран рейтинга ТЕКУЩЕЙ волны — заголовок с номером волны, при
    включённом тумблере имён топ строками «место. имя — баллы» (имена экранированы,
    T-32-06-05), затем строка собственного места (видна ВСЕГДА, даже когда имена скрыты —
    `wave_rating_view.rows` в этом случае просто пуст, второй код ветвления здесь не нужен:
    граница раскрытия данных — на уровне сервиса, план 32-03). «← Назад» возвращает список
    заданий тем же `_game_task_list_screen`."""
    tr_map = tr_map or {}
    wave = await get_wave(wave_id)
    header = reg_i18n.tr_text(await get_setting_typed("wave_rating_header_text"), lang, tr_map)
    lines = [f"{header} · {wave_number_label(wave)}"]

    view = await wave_rating_view(wave_id, viewer_id)
    if view["rows"]:
        lines.append("")
        for row in view["rows"]:
            lines.append(f"{row['place']}. {html.escape(str(row['name']))} — {row['points']}🪙")

    own = view["own"]
    if own is not None:
        gap = own["gap_to_prize"] if own["gap_to_prize"] is not None else 0
        own_line = reg_i18n.tr_fmt(
            await get_setting_typed("wave_rating_own_line_text"), lang, tr_map,
            rank=own["place"], total=own["total"], place=view["prize_places"], gap=gap,
        )
        lines.append("")
        lines.append(own_line)

    back_text = reg_i18n.tr_text("◀️ Назад", lang, tr_map)
    kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text=back_text, callback_data="gtasks_back:0"),
    ]])
    return "\n".join(lines), kb


@router.callback_query(F.data == "ambwave")
async def show_wave_rating(callback: types.CallbackQuery):
    """Phase 32 (32-06, D-29, T-32-06-01): кнопка в списке заданий не несёт `wave_id` — гейт
    целиком здесь, ПЕРЕД любым чтением рейтинга (кнопка в Telegram не истекает: экран мог быть
    отрисован ещё до того, как нажавший вышел из амбассадоров или волна закрылась).
    Не амбассадор/не участник ТЕКУЩЕЙ волны -> короткий alert, сообщение не перерисовывается.
    Активной волны нет вовсе -> это нормальное пустое состояние, не отказ — редактируем
    сообщение на `wave_rating_closed_text`, а не молчим alert'ом."""
    lang, tr_map = await reg_i18n.ctx_for(callback)
    user = await get_user(callback.from_user.id)
    cities_on = await cities_module_on()
    code = normalize_city(user.get("event_city") if user else None) if cities_on else None
    current_wave = await current_wave_for(code)

    if current_wave is None:
        text = reg_i18n.tr_text(await get_setting_typed("wave_rating_closed_text"), lang, tr_map)
        back_text = reg_i18n.tr_text("◀️ Назад", lang, tr_map)
        kb = InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text=back_text, callback_data="gtasks_back:0"),
        ]])
        await callback.message.edit_text(text, reply_markup=kb)
        await callback.answer()
        return

    is_ambassador = bool(user and user.get("is_ambassador"))
    if not is_ambassador or not wave_eligible(user, current_wave):
        await callback.answer(
            reg_i18n.tr_text(
                "Рейтинг волны виден только участникам текущей волны амбассадоров.", lang, tr_map,
            ),
            show_alert=True,
        )
        return

    text, kb = await _wave_rating_screen(current_wave["id"], callback.from_user.id, lang, tr_map)
    await callback.message.edit_text(text, parse_mode="HTML", reply_markup=kb)
    await callback.answer()
