import csv
import html as html_module
import io
import asyncio
import json
import logging
import os
import re
import sqlite3
import tempfile
from datetime import datetime
from aiogram import Router, F, types, Bot
from aiogram.filters import Command, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.types import BufferedInputFile, InlineKeyboardMarkup, InlineKeyboardButton, ReplyKeyboardRemove
from config import config
from settings_schema import SETTINGS_SCHEMA, get_setting_typed  # REG-02/REG-03: registry + typed accessor
from database.db import (
    get_stats,
    get_all_users_ids,
    get_all_users_dicts,
    export_users_csv,
    get_user,
    get_user_by_username,
    get_monthly_registration_stats,
    get_source_stats,
    get_setting,
    set_setting,
    delete_setting,
    add_coins,
    get_balance,
    get_non_subscriber_ids,
    get_incomplete_user_ids,
    get_dropout_step_stats,
    get_pending_users,
    get_pending_count,
    approve_user_atomic,
    reject_user,
    approve_all_pending,
    create_scheduled_broadcast,
    list_pending_broadcasts,
    cancel_scheduled_broadcast,
    count_and_list_filtered,
    get_distinct_filter_values,
    get_receipt_pending_users,
    get_receipt_pending_count,
    update_payment_status,
    get_city_counts,
    list_staff,
    add_staff,
    remove_staff,
    get_staff_city,
    set_staff_city,
    get_question,
    claim_question,
    set_question_answer,
    get_stuck_questions,
    list_all_tasks,
    create_task,
    get_task,
    get_submission,
    get_pending_submissions,
    get_pending_submissions_count,
    claim_submission,
    list_all_submissions,
    get_game_stats,
    GAME_CATEGORIES,
    GAME_PROOF_TYPES,
    parse_proof_types,
    get_submission_parts_or_legacy,
    archive_task,
    unarchive_task,
    delete_task,
    count_task_submissions,
    count_rejected_submissions,
    list_manual_coin_entries,
    count_manual_coin_entries,
    export_coins_journal_csv,
    # Phase 14 (14-07, CITY-07): cities table writes + delete-safety counters
    update_city,
    insert_city,
    delete_city_row,
    count_users_by_city,
    count_tasks_by_city,
    # Phase 07.3 (02, RET-01): «🔄 Новый сезон» wizard accessors (plan 01)
    count_current_season_users,
    mark_season_ended,
    # Phase 07.3 (05, RET-03): менеджерские поверхности повторного делегата
    get_returning_count,
    # Phase 07.3 (06, RET-04): импорт делегатов прошлого события
    bulk_insert_users_if_absent,
    count_existing_telegram_ids,
)
from aiogram.exceptions import TelegramRetryAfter, TelegramForbiddenError, TelegramBadRequest
from services.sheets import get_existing_sheet_ids, append_rows_to_sheet, ensure_sheet_header, sync_named_worksheet, dedupe_sheet_by_id, update_status_in_sheet, bulk_update_status_in_sheet, rebuild_main_sheet, REFUSED_UNPINNED_TAB, _reset_sheet_cache, tab_row_count
from services.scheduler import (
    _parse_schedule_dt,
    _fmt_dt,
    _now_moscow_naive,
    schedule_broadcast_job,
    cancel_broadcast_job,
)
from services.allowlist import refresh_allowlist, allowlist_size
from services.background import spawn as _spawn
from services.game_sync import request_resync as _request_game_resync, set_rebuild as _set_game_rebuild
from handlers.states import Broadcast, EditSetting, Approval, ReceiptReview, StaffAdd, GameTaskCreate, GameReview, CoinsManual, CityForm, SeasonReset, SeasonImport
from handlers.admin_caps import ALL_CAPABILITIES, CAP_LABELS, ROLES, role_caps_key, role_enabled_key, CapabilityMiddleware, required_capability, has_capability, resolve_capabilities, ANY_CAPABILITY, capability_holders
from keyboards.builders import get_cancel_kb, MENU_BUTTONS, get_main_menu_kb
from handlers.registration import REG_FLOW, REG_DEFAULTS, REG_LABELS, REG_PRESETS, REG_CATEGORIES, SHEET_HEADERS, STATUS_LABELS, _build_sheet_row, active_sheet_headers, set_sheet_schema, _sheet_value_map, approve_user, dropout_step_label, _apply_party_preset, _apply_short_preset, city_row_tab, incomplete_city_batches
from cities import (  # Phase 07.1 (CITY-04): admin city screen; Phase 07.2 (CITY-02): admin city switcher + scoping
    CITIES,
    is_city_enabled,
    city_label,
    cities_module_on,
    admin_selected_city,
    set_admin_city,
    city_scope,
    city_codes,
    normalize_city,
    ALL_CITIES,  # Phase 09.3 (09.3-02, CITY-08): third _admin_city_view state, «все города»
    ALL_CITIES_LABEL,  # single source of truth for the label — never redefined in this file
    enabled_cities,  # Phase 09.1 (B): "Кому задание?" wizard step
    is_per_city,  # Phase 09.2 (C, CITY-05): «🏙 Для города…» per-setting override sub-flow
    per_city_key,
    city_override_codes,
    get_setting_for_city,
    get_setting_typed_for_city,
    PER_CITY_SEP,
    # Phase 14 (14-07, CITY-07): full CRUD screen — cache read/reload + registry default
    all_cities,
    reload_cities,
    default_city_code,
    make_city_code,
)

router = Router()
logger = logging.getLogger(__name__)

# ROLE-01 (D-01): the one enforcement point. INNER middleware (`.middleware()` -- deliberately
# NOT the router's outer-hook variant) -- it only wraps a handler whose OWN filter already
# matched, so it never touches events belonging to sibling routers (payment/registration/
# user_actions), regardless of `admin.router` being registered first in main.py. See
# handlers/admin_caps.py for the map + resolver + the class itself.
router.callback_query.middleware(CapabilityMiddleware())
router.message.middleware(CapabilityMiddleware())

# INVARIANT for future phases (Phase 9/12 add handlers to this file): every `@router.*`
# decorator below MUST fit on ОДНОЙ строкой (a single line). The capability-map completeness
# test (tests/test_roles_phase8.py) extracts each handler's callback_data/command/state literal
# straight from the decorator's source TEXT, line by line -- a decorator split across multiple
# lines leaves its handler with no derivable key, and ADMIN_CAPS's deny-by-default (D-02)
# silently locks it for everyone until someone notices and fixes the line wrap.

pending_albums = {}

MONTH_NAMES = {
    "01": "Январь",
    "02": "Февраль",
    "03": "Март",
    "04": "Апрель",
    "05": "Май",
    "06": "Июнь",
    "07": "Июль",
    "08": "Август",
    "09": "Сентябрь",
    "10": "Октябрь",
    "11": "Ноябрь",
    "12": "Декабрь",
}

def _parse_coins_amount(token: str) -> int | None:
    """Parse a signed coin amount like '+10', '-3', '10'. None on failure."""
    token = (token or "").strip()
    if not token:  # IN-03: check emptiness AFTER strip so a whitespace-only token can't IndexError
        return None
    body = token[1:] if token[0] in "+-" else token
    if not (body.isascii() and body.isdigit()):
        return None
    value = int(body)
    return -value if token[0] == "-" else value


def _parse_positive_int(text: str) -> int | None:
    """No-sign positive-int parser for the game-task coins step (D-08). Unlike
    `_parse_coins_amount` above (`/coins`'s signed delta, `+N`/`-N`), a task's coin value is
    never negative and never zero -- `"0"`/`"-5"`/non-digit input all resolve to None."""
    token = (text or "").strip()
    if not token or not (token.isascii() and token.isdigit()):
        return None
    value = int(token)
    return value if value > 0 else None


# ROLE-01 (D-15): the ONE list of (text, callback_data) menu rows — same order the plain
# 14-button panel has always rendered in (13 original rows + Phase 07.2's «Города
# мероприятия» row, appended last). This is the single source `_visible_menu_rows` filters
# and `build_admin_keyboard` renders from; there is no second "button -> required right" map
# anywhere in this file (D-01/D-15 invariant) — the required capability for each row is
# looked up straight from `ADMIN_CAPS` (via `required_capability`), the exact map
# `CapabilityMiddleware` enforces on the backend.
_ADMIN_MENU_ROWS: list[tuple[str, str]] = [
    ("📊 Статистика регистраций", "admin_stats"),
    ("🗓 Регистрации по месяцам", "admin_monthly_stats"),
    ("📈 Источники", "admin_source_stats"),
    ("📄 Экспорт CSV", "admin_export_csv"),
    ("📝 Незавершённые → таблица", "admin_export_incomplete"),
    ("📋 Заявки", "admin_applications"),
    ("🧾 Чеки", "admin_receipts"),
    ("🔒 Залипшие вопросы", "admin_stuck_questions"),
    ("📢 Рассылка", "admin_broadcast"),
    ("🔄 Синхронизация таблицы", "admin_sync_sheet"),
    ("♻️ Пересобрать таблицу", "admin_rebuild_sheet"),
    ("🧹 Убрать дубли из таблицы", "admin_dedupe_sheet"),
    ("⚙️ Настройки форума", "admin_settings"),
    ("📖 Справка по настройкам", "admin_settings_guide"),
    ("🏙 Города мероприятия", "admin_cities"),
    ("📋 Задания", "admin_game_tasks"),
    ("🎮 Проверка заданий", "admin_game_review"),
    ("🪙 Монеты вручную", "admin_coins_manual"),
    ("📜 Журнал монет", "admin_coins_journal"),
    ("🔄 Таблица геймы", "admin_game_sync_sheet"),
    ("📊 Статистика геймы", "admin_game_stats"),
]


def _visible_menu_rows(caps: set) -> list[tuple[str, str]]:
    """Pure, synchronous, no I/O (CONVENTIONS.md `_private`-helper idiom) — the unit-testable
    half of D-15's "menu built from this person's rights". `caps` must already be resolved
    (the SQLite read happens once, in `build_admin_keyboard`, not per-row here). Hiding a row
    the caller can't reach is convenience only — `CapabilityMiddleware` (handlers/admin_caps.py)
    is what actually enforces access on every callback, independently of what this function
    returns (D-15 requires "AND", never "OR")."""
    rows = []
    for text, callback_data in _ADMIN_MENU_ROWS:
        cap = required_capability(callback_data=callback_data)
        if cap is None:
            continue  # deny-by-default (D-02): an unmapped row is never shown either
        if cap == ANY_CAPABILITY:
            if caps:
                rows.append((text, callback_data))
        elif cap in caps:
            rows.append((text, callback_data))
    return rows


async def build_admin_keyboard(user_id: int) -> InlineKeyboardMarkup:
    """ROLE-01 (D-15): the panel built for THIS person — a fresh capability read
    (`resolve_capabilities`, D-05: no cache) followed by the pure row filter above. Every
    caller MUST await this now; there is deliberately no synchronous fallback left, so a
    forgotten `await` fails loudly (a coroutine object is not a valid `reply_markup`) instead
    of silently showing an unfiltered panel to everyone."""
    caps = await resolve_capabilities(user_id)
    rows = _visible_menu_rows(caps)
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=text, callback_data=callback_data)]
        for text, callback_data in rows
    ])


# Phase 07.2 (CITY-02): per-admin city switcher, rendered as a header row over the panel.
# `build_admin_keyboard(user_id)` already does the D-15 capability filtering (ROLE-01/08-05);
# this wrapper only adds the city-switcher header row on top, unchanged from Phase 07.2.
async def admin_keyboard_for(admin_id: int) -> InlineKeyboardMarkup:
    code = await admin_selected_city(admin_id)
    base = await build_admin_keyboard(admin_id)
    if code is None:  # module off — byte-identical to today, no switcher row
        return base
    # Phase 09.3 (CITY-08): «Все города» mode gets its own label (no "🏙 Город: " prefix) —
    # the button text ITSELF is ALL_CITIES_LABEL, not the label glued after the usual prefix.
    label_text = ALL_CITIES_LABEL if code == ALL_CITIES else f"🏙 Город: {await city_label(code)}"
    header = [InlineKeyboardButton(text=label_text, callback_data="admin_city_switch")]
    return InlineKeyboardMarkup(inline_keyboard=[header] + base.inline_keyboard)


async def render_monthly_stats() -> str:
    rows = await get_monthly_registration_stats()
    if not rows:
        return "📅 <b>Регистрации по месяцам</b>\n\nПока нет ни одной регистрации."

    lines = ["📅 <b>Регистрации по месяцам</b>", ""]
    for month, count in rows:
        if not month or len(month) != 7:
            label = month or "Неизвестно"
        else:
            year, month_num = month.split("-")
            month_name = MONTH_NAMES.get(month_num, month_num)
            label = f"{month_name} {year}"
        lines.append(f"• {label}: {count}")

    return "\n".join(lines)


# Phase 07.2 (CITY-02): shared render for /stats and the «📊 Статистика регистраций» screen —
# previously cmd_stats and show_admin_stats each built this text independently (duplicated
# f-strings). The base text (through the top-3-universities loop) is UNCHANGED, character for
# character, from what both call sites built before this plan.
#
# The «По городам» block below is the DELIBERATE EXCEPTION to city scoping (07.2-CONTEXT.md):
# every other admin surface in this phase filters by the admin's SELECTED city
# (_admin_city_scope) — this screen does not. The lead needs to compare cities side by side on
# one screen, not click through each one and add totals by hand. Do not "fix" this into a
# filtered view.
async def render_stats_text() -> str:
    total, top_unis = await get_stats()

    text = (
        f"📊 <b>Статистика:</b>\n"
        f"Всего регистраций: {total}\n"
        f"🏆 <b>Топ-3 ВУЗа:</b>\n"
    )

    for i, (uni, count) in enumerate(top_unis, 1):
        text += f"{i}. {html_module.escape(str(uni))} — {count}\n"

    # Phase 07.3 (05, RET-03): счётчик повторных делегатов — глобальный, без городского
    # разреза (CONTEXT.md блок C: «одна строка»), поэтому строка идёт ДО опционального блока
    # «🏙 По городам», а не внутри if await cities_module_on().
    text += f"🔁 Повторных: {await get_returning_count()}\n"

    # WR-06: `and CITIES` — с пустым реестром (битый EVENT_CITIES в .env) `normalize_city`
    # отдаёт литерал "msk", которого в CITIES нет: цикл рендера не выводил НИ ОДНОЙ строки
    # города, а счётчики всё равно попадали в «Итого». На экране оставались заголовок
    # «🏙 По городам:» и одинокая строка «Итого» — обещанный в ADMIN_GUIDE инвариант «сумма по
    # городам сходится со Всего регистраций» визуально нарушался без единого предупреждения.
    # Пустой реестр = показывать в разрезе городов нечего, блок не рисуется вовсе.
    if await cities_module_on() and CITIES:
        rows = await get_city_counts()
        # Same collapse the Sheets tabs and _city_clause's default-city branch already use:
        # NULL / unknown-code rows fold into the default city here, not in the SQL (db.py
        # cannot import cities.normalize_city — see get_city_counts()'s docstring).
        per_city = {code: [0, 0, 0] for code in (c["code"] for c in CITIES)}
        grand_total = grand_pending = grand_approved = 0
        for raw_city, cnt, pending, approved in rows:
            cnt = cnt or 0
            pending = pending or 0
            approved = approved or 0
            code = normalize_city(raw_city)
            # WR-06: с НЕПУСТЫМ реестром normalize_city возвращает либо код из CITIES, либо
            # default_city_code(), который сам берётся из CITIES — то есть ключ здесь есть
            # всегда, и каждая строка попадает в корзину, которая ниже будет ОТРИСОВАНА.
            # Прежний `per_city.setdefault(code, [0, 0, 0])` был недостижимой веткой и
            # маскировал этот инвариант, создавая «висячие» корзины вне цикла рендера.
            bucket = per_city[code]
            bucket[0] += cnt
            bucket[1] += pending
            bucket[2] += approved
            grand_total += cnt
            grand_pending += pending
            grand_approved += approved

        text += "\n🏙 <b>По городам:</b>\n"
        for c in CITIES:
            t, p, a = per_city[c["code"]]
            label = html_module.escape(await city_label(c["code"]))
            text += f"• {label} — всего {t}, на модерации {p}, одобрено {a}\n"
        text += f"• <b>Итого</b> — всего {grand_total}, на модерации {grand_pending}, одобрено {grand_approved}\n"

    return text


@router.message(Command("admin"))
async def cmd_admin_help(message: types.Message, state: FSMContext):
    caps = await resolve_capabilities(message.from_user.id)
    rows = _visible_menu_rows(caps)

    if not rows:
        # D-16 (empty set): a real, currently-enabled role with zero mapped menu rows (e.g.
        # `game_manager` today — gamification ships in Phase 9) must never see a blank
        # keyboard; T-08-26 also means this text must not enumerate the sections that DO
        # exist for other roles.
        await message.answer("Для твоей роли пока нет доступных разделов. Обратись к администратору.")
        return

    # D-16: exactly one section available -> open it directly, skipping the one-button menu.
    # `_pick_auto_open` is the pure decision (unit-tested standalone); it returns None for
    # everything except a single row whose callback_data is in the closed `_AUTO_OPEN_SECTIONS`
    # whitelist (screens only, never a destructive action -- T-08-25).
    auto_open = _pick_auto_open(rows)
    if auto_open is not None:
        handler, needs_state = auto_open
        _, callback_data = rows[0]
        # T-08-24: this bypasses CapabilityMiddleware by calling the handler directly, which is
        # safe ONLY because `callback_data` came from `rows[0]` -- a row that already survived
        # `_visible_menu_rows(caps)` against a capability set freshly resolved two lines above,
        # never from raw user input. `_MessageAsCallback` doesn't accept an externally supplied
        # `data` from anywhere else in this function.
        fake_callback = _MessageAsCallback(message, callback_data)
        if needs_state:
            await handler(fake_callback, state)
        else:
            await handler(fake_callback)
        return

    text = (
        "👮‍♂️ <b>Панель администратора</b>\n\n"
        "/stats - Статистика регистраций\n"
        "/stats_monthly - Регистрации по месяцам\n"
        "/create_link &lt;название&gt; - Создать ссылку с меткой\n"
        "/export - Скачать базу пользователей (CSV)\n"
        "/broadcast - Рассылка сообщения всем\n"
        "/find @username - Найти пользователя по юзернейму\n"
        "/coins @username +N причина - Начислить/списать монеты\n"
        "/scheduled - Запланированные рассылки\n"
        "/refresh_allowlist - Обновить список отобранных\n"
        "/settings_guide - 📖 Справка по всем настройкам бота"
    )
    await message.answer(text, parse_mode="HTML", reply_markup=await admin_keyboard_for(message.from_user.id))


# Phase 14 (GAME-09): shared by the button wizard (coinsman_confirm) and /coins -- one place
# builds the delegate-facing notification text, so the two paths can never drift on wording.
# `.replace` per placeholder (not `.format`): a manager-edited template may carry a stray `{`/`}`
# and `.format` would raise on that, breaking the notification entirely. `{delta}` always carries
# an explicit sign (f"{delta:+d}") since the same template covers both credit and debit (CONTEXT.md
# B). `{reason}` (free text from a human) is HTML-escaped -- the bot sends with parse_mode="HTML".
async def _notify_manual_coins(bot: Bot, user_id: int, delta: int, reason: str, balance: int) -> bool:
    """Returns True on successful delivery, False on any failure (delegate blocked the bot,
    etc.) -- logged, never raised. The ledger write already happened before this is called
    (T-14-20): a failed notification must never be the reason an operation looks undone."""
    template = await get_setting_typed("coins_manual_notify_text")
    if not template:
        template = SETTINGS_SCHEMA["coins_manual_notify_text"]["default"]
    text = (
        str(template)
        .replace("{delta}", f"{delta:+d}")
        .replace("{reason}", html_module.escape(str(reason)))
        .replace("{balance}", str(balance))
    )
    try:
        await bot.send_message(user_id, text, parse_mode="HTML")
        return True
    except Exception as e:
        logger.warning(f"Failed to notify user {user_id} of manual coins change: {e}", exc_info=True)
        return False


@router.message(Command("coins"))
async def cmd_coins(message: types.Message, bot: Bot):
    args = (message.text or "").split(maxsplit=3)
    # GAME-09: причина обязательна на обоих путях -- «журнал монет должен отвечать на вопрос
    # «кто, кому, за что»» (owner, CONTEXT.md B). Quick path stays for people used to it, but
    # follows the same rule as the button wizard.
    hint = (
        "⚠️ Формат: /coins @username +N причина — причину нужно указать: журнал монет "
        "должен отвечать на вопрос «кто, кому, за что»."
    )
    if len(args) < 4 or not args[3].strip():
        await message.answer(hint)
        return

    user = await get_user_by_username(args[1])
    if not user:
        await message.answer(f"❌ Пользователь {html_module.escape(args[1])} не найден.")
        return

    amount = _parse_coins_amount(args[2])
    if amount is None:
        await message.answer(hint)
        return

    reason = args[3]
    await add_coins(user["telegram_id"], amount, reason=reason, changed_by=message.from_user.id, source="manual")
    _request_game_resync()  # Phase 09.1 (D, GAME-07): a coin edit is one of the 3 debounced triggers
    balance = await get_balance(user["telegram_id"])

    safe_username = html_module.escape(str(user.get("username") or args[1]))
    sign = "начислено" if amount >= 0 else "списано"
    notified = await _notify_manual_coins(bot, user["telegram_id"], amount, reason, balance)
    notify_suffix = "" if notified else " (делегат не получил уведомление)"
    await message.answer(
        f"🪙 {sign} {abs(amount)} монет(ы) для {safe_username}.\n"
        f"Новый баланс: <b>{balance}</b>.{notify_suffix}",
        parse_mode="HTML",
    )

@router.message(Command("find"))
async def cmd_find_user(message: types.Message):
    args = message.text.split()
    if len(args) < 2:
        await message.answer("⚠️ Используйте формат: /find @username")
        return

    username = args[1]
    user = await get_user_by_username(username)

    if user:
        text = (
            f"👤 <b>Пользователь найден:</b>\n"
            f"ID: <code>{user['telegram_id']}</code>\n"
            f"Имя: {html_module.escape(str(user['full_name'] or ''))}\n"
            f"Username: {html_module.escape(str(user['username'] or ''))}\n"
            f"Email: {html_module.escape(str(user['email'] or ''))}\n"
            f"Регистрация: {user['registration_date']}"
        )
        await message.answer(text, parse_mode="HTML")
    else:
        await message.answer(f"❌ Пользователь {username} не найден в базе данных.")


# Метка едет в deep-link как `?start=src_<метка>`, а Telegram разрешает в этом параметре
# только латиницу, цифры, «_» и «-» (всего 64 символа, из них 4 занимает префикс `src_`).
# Всё остальное молча ломает ссылку, поэтому проверяем до отправки, а не после.
_SOURCE_TAG_RE = re.compile(r"^[A-Za-z0-9_-]{1,60}$")


@router.message(Command("create_link"))
async def cmd_create_link(message: types.Message, bot: Bot):
    args = message.text.split(maxsplit=1)
    if len(args) < 2 or not args[1].strip():
        await message.answer("⚠️ Используйте формат: /create_link &lt;название&gt;\nПример: /create_link vk_poster", parse_mode="HTML")
        return

    raw = args[1].strip()
    # Реальный случай 14.08: менеджер скопировал формат из справки ВМЕСТЕ с угловыми скобками
    # («/create_link <инфо ВК>»). Скобки попадали в HTML-ответ неэкранированными, Telegram видел
    # «<инфо» как открывающий тег и отклонял ВСЁ сообщение — человек не получал ни ссылки, ни
    # ошибки. Снимаем скобки молча (намерение очевидно), а остальное объясняем словами.
    tag = raw[1:-1].strip() if raw.startswith("<") and raw.endswith(">") else raw

    if not _SOURCE_TAG_RE.match(tag):
        await message.answer(
            "⚠️ Метка подставляется прямо в ссылку, поэтому в ней можно использовать только "
            "латинские буквы, цифры, «_» и «-» — без пробелов, русских букв и знаков.\n\n"
            f"Вы прислали: <code>{html_module.escape(raw)}</code>\n"
            "Например, для афиши во ВКонтакте: <code>/create_link vk_poster</code>",
            parse_mode="HTML",
        )
        return

    bot_user = await bot.get_me()
    link = f"https://t.me/{bot_user.username}?start=src_{tag}"
    await message.answer(
        f"🔗 Ссылка с меткой <b>{html_module.escape(tag)}</b>:\n\n"
        f"<code>{html_module.escape(link)}</code>\n\n"
        f"Регистрации по этой ссылке появятся в разделе «📈 Источники».",
        parse_mode="HTML",
    )



async def is_question_reply(message: types.Message) -> bool:
    # ROLE-01 (D-01): same source of truth the middleware uses (ADMIN_CAPS via
    # required_capability), applied here for ROUTING, not a second authorization mechanism.
    # This predicate still matches by message SHAPE (reply to a forwarded question card, with
    # the 🆔/❓ markers) -- without the identity check below, a delegate's reply to a similarly-
    # shaped message would also match and get routed into the admin router.
    cap = required_capability(special="question_reply")
    if not cap or not await has_capability(message.from_user.id, cap):
        return False
    replied = message.reply_to_message
    if not replied or not replied.text:
        return False
    return "🆔" in replied.text and "❓" in replied.text


async def _notify_other_moderate_reg_holders(bot: Bot, admin_name: str, user_id: int, exclude_id: int):
    """D-13: everyone else who currently holds moderate_reg -- not just config.ADMIN_IDS --
    learns who answered. Fail-soft/silent per recipient, same shape used before this plan."""
    safe_admin_name = html_module.escape(admin_name)
    for other_id in await capability_holders("moderate_reg"):
        if other_id == exclude_id:
            continue
        try:
            await bot.send_message(
                other_id,
                f"✅ {safe_admin_name} ответил(а) на вопрос от пользователя <code>{user_id}</code>.",
                parse_mode="HTML",
            )
        except Exception:
            pass


async def _deliver_question_reply(message: types.Message, bot: Bot, user_id: int, admin_name: str):
    """Shared delivery: send the reply (text or a copy of the admin's message) to the
    delegate, ack the replying admin, and fan out «who answered» to other moderate_reg
    holders. Raises on delivery failure -- callers decide what happens to a claim, if any."""
    if message.text:
        reply_text = f"💬 <b>Ответ от организаторов:</b>\n\n{message.html_text}"
        await bot.send_message(user_id, reply_text, parse_mode="HTML")
    else:
        await bot.send_message(user_id, "💬 <b>Ответ от организаторов:</b>", parse_mode="HTML")
        await message.send_copy(user_id)
    await message.reply("✅ Ответ отправлен пользователю.")
    await _notify_other_moderate_reg_holders(bot, admin_name, user_id, message.from_user.id)


# T-08-33 (quick task), part B: a manager whose delivery attempt fails deserves to know
# whether trying again could ever work. TelegramForbiddenError (the delegate blocked the
# bot) and TelegramBadRequest (chat gone/invalid, e.g. a deleted account) are PERMANENT --
# no retry, by anyone, at any time, can succeed. Everything else (TelegramRetryAfter,
# TelegramNetworkError, generic timeouts) is TRANSIENT -- a retry is a reasonable next step.
# Neither branch releases the claim (T-08-33's own accepted-risk text, unchanged): variant A
# was explicitly rejected by the project owner because releasing reopens the double-send race
# D-14 exists to close.
_PERMANENT_DELIVERY_ERRORS = (TelegramForbiddenError, TelegramBadRequest)


async def _reply_with_delivery_error(message: types.Message, error: Exception):
    if isinstance(error, _PERMANENT_DELIVERY_ERRORS):
        await message.reply(
            "❌ Доставить ответ невозможно: делегат заблокировал бота или чат недоступен. "
            "Повтор не поможет — свяжитесь с делегатом другим способом."
        )
    else:
        await message.reply(
            "❌ Не удалось отправить ответ пользователю (временная ошибка). Можно попробовать ещё раз."
        )


async def _attempt_question_delivery(message: types.Message, bot: Bot, user_id: int, admin_name: str, qid: int):
    """Deliver + record, shared by the first-claim path and the C-variant same-person retry
    path below -- both need identical delivery/error-handling behaviour."""
    try:
        await _deliver_question_reply(message, bot, user_id, admin_name)
        # D: delivered_at is stamped here, together with answer_text, ONLY on success --
        # see set_question_answer's own docstring for why it can't be derived from
        # answer_text alone.
        await set_question_answer(qid, message.html_text or message.text or "")
    except Exception as e:
        # T-08-33 (accepted risk): the claim is NOT released here -- releasing it would let a
        # retry double-send to the delegate. The manager sees an explicit failure and can
        # follow up out-of-band; documented as a known limitation in 08-06-SUMMARY.md.
        logger.error(f"Failed to send reply to user {user_id}: {e}")
        await _reply_with_delivery_error(message, e)


@router.message(is_question_reply)
async def admin_reply_to_question(message: types.Message, bot: Bot):
    replied = message.reply_to_message
    match = re.search(r"🆔\s*(\d+)", replied.text)
    if not match:
        return

    user_id = int(match.group(1))
    admin_name = message.from_user.full_name or message.from_user.username or "Админ"

    # D-14: `replied.text` is the PLAIN rendered text Telegram hands back (HTML markup like
    # <code> is display-only, carried via separate `entities`, never present in `.text`
    # itself -- same reason the pre-existing 🆔 regex above has no tag in its pattern) --
    # match the bare digits after the marker, not the `<code>` wrapper it was SENT with.
    # Only ASCII digits match (same protection as _parse_coins_amount, via the `[0-9]`
    # character class). A message without the marker -- sent before this migration, or
    # referencing a since-purged question row -- falls back to the legacy (no-claim) path so
    # those older chats keep working (T-08-28).
    qid_match = re.search(r"Вопрос #([0-9]+)", replied.text)
    question = await get_question(int(qid_match.group(1))) if qid_match else None

    if question is None:
        logger.info(
            "admin_reply_to_question: legacy (no-claim) path, question_id=%s",
            qid_match.group(1) if qid_match else None,
        )
        try:
            await _deliver_question_reply(message, bot, user_id, admin_name)
        except Exception as e:
            logger.error(f"Failed to send reply to user {user_id}: {e}")
            await _reply_with_delivery_error(message, e)
        return

    qid = question["id"]
    claimed = await claim_question(qid, message.from_user.id, admin_name)

    if not claimed:
        # T-08-33, part C: this specific person may already hold the claim from an earlier
        # attempt that failed to deliver -- `claim_question` only flips a row from
        # answered_by IS NULL, so a second call from the SAME claimant correctly returns
        # False here too. Distinguish that from a genuinely different responder: only the
        # winner, retrying their own failed delivery, gets to try again; anyone else still
        # sees "already answered by X" and nothing is sent to the delegate. Always re-read
        # (never reuse the pre-claim `question` snapshot): `claim_question` returning False
        # means someone's state changed since that read, and this is the one place that
        # decision hinges on the CURRENT answered_by/delivered_at, not a stale copy.
        row = await get_question(qid)
        if (
            row
            and row.get("answered_by") == message.from_user.id
            and not row.get("delivered_at")
        ):
            await _attempt_question_delivery(message, bot, user_id, admin_name, qid)
            return
        winner_name = (row or {}).get("answered_by_name") or "коллега"
        await message.reply(f"⚠️ На этот вопрос уже ответил(а) {winner_name}.")
        return

    await _attempt_question_delivery(message, bot, user_id, admin_name, qid)


@router.message(Command("stats"))
async def cmd_stats(message: types.Message):
    await message.answer(await render_stats_text(), parse_mode="HTML")


@router.message(Command("stats_monthly"))
async def cmd_stats_monthly(message: types.Message):
    await message.answer(await render_monthly_stats(), parse_mode="HTML")


@router.callback_query(F.data == "admin_stats")
async def show_admin_stats(callback: types.CallbackQuery):
    text = await render_stats_text()
    await callback.message.edit_text(text, parse_mode="HTML", reply_markup=await admin_keyboard_for(callback.from_user.id))
    await callback.answer()


@router.callback_query(F.data == "admin_monthly_stats")
async def show_admin_monthly_stats(callback: types.CallbackQuery):
    await callback.message.edit_text(await render_monthly_stats(), parse_mode="HTML", reply_markup=await admin_keyboard_for(callback.from_user.id))
    await callback.answer()


@router.callback_query(F.data == "admin_source_stats")
async def show_admin_source_stats(callback: types.CallbackQuery):
    rows = await get_source_stats()
    if not rows:
        text = "📈 <b>Источники регистраций</b>\n\nПока нет данных."
    else:
        lines = ["📈 <b>Источники регистраций</b>", ""]
        for source, count in rows:
            lines.append(f"• {html_module.escape(str(source))} — {count}")
        text = "\n".join(lines)

    await callback.message.edit_text(text, parse_mode="HTML", reply_markup=await admin_keyboard_for(callback.from_user.id))
    await callback.answer()


# T-08-33 (quick task), part D: claimed-but-never-delivered delegate questions. Gated by
# moderate_reg (same capability as the rest of the question-reply flow), not a mass
# moderation queue like «Заявки»/«Чеки» (CLAUDE.md's pagination constraint targets queues on
# the order of 1000+ live rows -- a stuck question only ever exists when delivery genuinely
# failed, expected to be rare) -- a single text screen, same shape as render_stats_text/
# render_monthly_stats above, not a per-row card UI.
async def render_stuck_questions_text() -> str:
    rows = await get_stuck_questions()
    if not rows:
        return "🔒 <b>Залипшие вопросы</b>\n\nНет вопросов, захваченных без доставки ответа."

    lines = ["🔒 <b>Залипшие вопросы</b>", "", "Захвачены, но ответ не дошёл до делегата:"]
    for row in rows:
        name = html_module.escape(str(row.get("answered_by_name") or "неизвестно"))
        question_text = html_module.escape(str(row.get("question_text") or ""))
        lines.append(
            f"• 🆔 <code>{row['user_id']}</code> — захватил(а) {name}\n"
            f"  «{question_text}»"
        )
    return "\n".join(lines)


@router.callback_query(F.data == "admin_stuck_questions")
async def show_stuck_questions(callback: types.CallbackQuery):
    text = await render_stuck_questions_text()
    await callback.message.edit_text(text, parse_mode="HTML", reply_markup=await admin_keyboard_for(callback.from_user.id))
    await callback.answer()


# --- Settings ---

# REG-03: the event-group text/enum entries are GENERATED from settings_schema.SETTINGS_SCHEMA
# (single source of truth, D-13) instead of hand-written literals. Order is pinned explicitly
# (not a dict-order assumption) so the settings screen stays byte-identical to the
# pre-registry literal table. Remaining (unmigrated) groups below stay literal tuples — no
# change — until their own migration wave (coexistence invariant, SC#3).
_EVENT_FIELD_ORDER = [
    "event_date", "event_time", "event_place_name", "event_place_address",
    "contact_person", "contact_vk", "contact_tg", "start_text", "start_text_registered",
    "start_text_returning",
    "event_name", "event_season", "event_type",
]
_EVENT_FIELDS = [
    (k, SETTINGS_SCHEMA[k]["label"], SETTINGS_SCHEMA[k]["prompt"])
    for k in _EVENT_FIELD_ORDER
]

# REG-01/REG-03 (06-02): reg/pay/party/consent groups GENERATED from settings_schema.
# SETTINGS_SCHEMA (same computed-view splice as the event pilot, D-13) — every text/list/
# int/date key that used to be a hand-written SETTINGS_FIELDS tuple now lives ONLY in the
# registry; order is pinned per group (not registry dict-insertion order) so the on-screen
# order stays byte-identical to the pre-migration literal tables.
_REG_FIELD_ORDER = [
    "source_options", "reg_complete_text", "approve_text", "reject_text",
    "pending_reminder_interval", "city_options", "study_field_options",
    "goal_options", "formats_options", "university_options",
]
_PAY_FIELD_ORDER = [
    "payment_options", "payment_requisites", "payment_requisites_by_lc",
    "payment_deadline", "payment_reminder_text", "payment_overdue_text", "penalty_schedule",
]
_PARTY_FIELD_ORDER = [
    "party_closed_text", "approve_text__party",
]
_CONSENT_FIELD_ORDER = [
    "consent_button_text", "consent_list",
]

# Phase 09.1 (A): every editable text in the free-form submission flow, one group
# «🎮 Геймификация» — promo prompts by type, the general/multi-type fallback, the
# "жми Готово" hint, the button's own label, the empty-submission hint, and the accepted text.
_GAME_FIELD_ORDER = [
    "game_proof_prompt_photo", "game_proof_prompt_pdf", "game_proof_prompt_text",
    "game_proof_prompt_link", "game_proof_prompt_any", "game_proof_done_hint",
    "game_proof_done_button", "game_proof_empty_hint", "game_submit_accepted_text",
    "game_resubmit_limit", "coins_manual_notify_text",
]

# Phase 14 (CFG-01): group «🔧 Система» — proxy timings that used to live only in .env.
_SYSTEM_FIELD_ORDER = ["proxy_recheck_seconds", "proxy_connect_timeout"]

# Quick 260815-3hw (TABS-01/02/03): every Google Sheets tab NAME in one group — «📄 Вкладки
# таблицы». short_sheet_tab/party_sheet_tab moved here from reg/party (physically relocated in
# settings_schema.py, not duplicated). Order is the on-screen order, not registry insertion order.
_SHEETS_FIELD_ORDER = [
    "main_sheet_tab", "short_sheet_tab", "party_sheet_tab", "incomplete_sheet_tab",
    "game_matrix_tab", "game_history_tab", "preselect_tab",
    "city_tab_suffix__short", "city_tab_suffix__party", "city_tab_suffix__incomplete",
]

_REG_FIELDS = [(k, SETTINGS_SCHEMA[k]["label"], SETTINGS_SCHEMA[k]["prompt"]) for k in _REG_FIELD_ORDER]
_PAY_FIELDS = [(k, SETTINGS_SCHEMA[k]["label"], SETTINGS_SCHEMA[k]["prompt"]) for k in _PAY_FIELD_ORDER]
_PARTY_FIELDS = [(k, SETTINGS_SCHEMA[k]["label"], SETTINGS_SCHEMA[k]["prompt"]) for k in _PARTY_FIELD_ORDER]
_CONSENT_FIELDS = [(k, SETTINGS_SCHEMA[k]["label"], SETTINGS_SCHEMA[k]["prompt"]) for k in _CONSENT_FIELD_ORDER]
_SHEETS_FIELDS = [(k, SETTINGS_SCHEMA[k]["label"], SETTINGS_SCHEMA[k]["prompt"]) for k in _SHEETS_FIELD_ORDER]
_GAME_FIELDS = [(k, SETTINGS_SCHEMA[k]["label"], SETTINGS_SCHEMA[k]["prompt"]) for k in _GAME_FIELD_ORDER]
_SYSTEM_FIELDS = [(k, SETTINGS_SCHEMA[k]["label"], SETTINGS_SCHEMA[k]["prompt"]) for k in _SYSTEM_FIELD_ORDER]

# NOTE: reg_university_mode и edu_conditional вынесены в кнопки-переключатели (build_settings_keyboard).
# PDF согласий грузятся в разделе «🧾 PDF согласий».
# Phase 5 (D-11a/D-13): party-track text settings (party_enabled/party_fork_question/
# party_approval are toggle buttons in build_settings_keyboard, not here).
SETTINGS_FIELDS = (
    _EVENT_FIELDS + _REG_FIELDS + _PAY_FIELDS + _PARTY_FIELDS + _CONSENT_FIELDS
    + _SHEETS_FIELDS + _GAME_FIELDS + _SYSTEM_FIELDS
)

# Phase 5 (D-11a): default text shown in render_settings_text when a text setting is unset,
# so the manager sees what users actually receive today, not a bare "не указано". REG-01
# (06-02): derived from the registry `default` field instead of a hand-written literal dict
# (T-06-06) — restricted to `type == "text"` entries with a genuinely non-empty default so a
# functional parse-fallback default (e.g. pending_reminder_interval's int default 1800) is
# never mistaken for a display default.
_SETTINGS_DISPLAY_DEFAULTS = {
    k: v["default"] for k, v in SETTINGS_SCHEMA.items()
    if v["type"] == "text" and v.get("default") not in (None, "")
}

# Quick 260724-c0x: group→keys grouping (NOT a per-key metadata registry) so the settings
# landing screen can route into per-group sub-screens instead of dumping every field's value
# inline. Shape mirrors REG_CATEGORIES (handlers/registration.py) — (label, token, [keys]).
# REG-03: the "event" row's key list is generated from SETTINGS_SCHEMA (registry is the
# source, D-13) — same pinned _EVENT_FIELD_ORDER used to build _EVENT_FIELDS above, filtered
# to the text/enum keys (photo/file keys are handled separately by the event branch in
# render_settings_group_text/build_settings_group_keyboard via PHOTO_FIELDS/FILE_FIELDS,
# unchanged per D-10).
_EVENT_GROUP_KEYS = [
    k for k in _EVENT_FIELD_ORDER
    if SETTINGS_SCHEMA[k]["type"] in ("text", "enum")
]

SETTINGS_GROUPS = [
    ("🎪 Событие/Медиа", "event", _EVENT_GROUP_KEYS),
    ("📝 Регистрация", "reg", _REG_FIELD_ORDER),
    # Quick 260815-3hw: placed right after «📝 Регистрация» — a manager looks for tab names
    # near the registration settings, not buried at the tail of the settings list.
    ("📄 Вкладки таблицы", "sheets", _SHEETS_FIELD_ORDER),
    ("💳 Оплата", "pay", _PAY_FIELD_ORDER),
    ("🎉 Party", "party", _PARTY_FIELD_ORDER),
    ("📋 Согласия", "consent", _CONSENT_FIELD_ORDER),
    ("🎮 Геймификация", "game", _GAME_FIELD_ORDER),
    ("🔧 Система", "system", _SYSTEM_FIELD_ORDER),
]


def _settings_group_keys(token: str) -> list[str]:
    """Keys for a given SETTINGS_GROUPS token, including leftover-safety: any SETTINGS_FIELDS
    key not placed in a declared group lands in the trailing «Прочие»/"misc" group so nothing
    is ever silently hidden (mirrors _categorized_question_keys leftover handling)."""
    for _, tok, keys in SETTINGS_GROUPS:
        if tok == token:
            return list(keys)
    if token == "misc":
        seen = {k for _, __, keys in SETTINGS_GROUPS for k in keys}
        return [k for k, _, _ in SETTINGS_FIELDS if k not in seen]
    return []


def _settings_group_label(token: str) -> str:
    for label, tok, _ in SETTINGS_GROUPS:
        if tok == token:
            return label
    if token == "misc":
        return "📦 Прочие"
    return token


def _settings_nav_groups() -> list[tuple[str, str]]:
    """(label, token) rows for the landing keyboard nav buttons — declared groups plus a
    trailing «Прочие» group ONLY if leftover keys exist."""
    rows = [(label, tok) for label, tok, _ in SETTINGS_GROUPS]
    if _settings_group_keys("misc"):
        rows.append(("📦 Прочие", "misc"))
    return rows

PHOTO_FIELDS = [
    ("program", "📅 Программа", "Отправьте фото программы (можно с подписью)."),
    ("speakers", "🗣 Спикеры", "Отправьте одно фото со всеми спикерами (можно с подписью)."),
    ("start", "💬 Фото приветствия", "Отправьте фото для приветственного сообщения (/start)."),
    ("venue", "🏢 Площадка", "Отправьте фото площадки (можно с подписью)."),
]

FILE_FIELDS = [
    ("reg_bonus", "🎁 Бонус за регистрацию", "Отправьте файл или фото бонуса (можно с подписью)."),
]


# Phase 09.3 (04, CITY-09): admin_id=None means "no header context" — reserved for tests
# and call sites where the admin is unknown; every production call site MUST pass the real
# admin id (structural test: tests/test_regmode_header_093.py asserts no empty-parens call
# of render_settings_text()/build_settings_keyboard() remains in this file).
async def render_settings_text(admin_id: int | None = None) -> str:
    # Phase 09.3 (04, CITY-09): WR-05 — resolve the header ONCE for this whole render call.
    # `admin_id is None` (tests, unknown-caller sites) and `header_code in (None, ALL_CITIES)`
    # (module off / no choice yet / explicit «все города») all collapse to the SAME branch
    # below — byte-identical to pre-phase output (CONTEXT D module-off parity).
    header_code = await admin_selected_city(admin_id) if admin_id is not None else None
    per_city_ctx = bool(header_code and header_code != ALL_CITIES)

    lines = []
    if per_city_ctx:
        lines.append(f"🏙 {html_module.escape(await city_label(header_code))}")
    lines += ["⚙️ <b>Настройки форума</b>", ""]

    if per_city_ctx:
        # T-093-13: composed key comes ONLY from cities.per_city_key (closed-set guard).
        reg_mode = await get_setting_typed_for_city("registration_mode", header_code)
        mode_label = "📋 Полная" if reg_mode == "full" else "⚡ Краткая"
        own_key = per_city_key("registration_mode", header_code)
        own_mark = " — своё" if (own_key and await get_setting(own_key)) else " — как везде"
        lines.append(f"📝 Форма регистрации: <b>{mode_label}</b>{own_mark}")
    else:
        # REG-02 (06-07): final-coverage sweep — closes the 06-05-flagged boundary; byte-
        # identical to the prior `get_setting(k) or "<literal>"` idiom (enum falsy->default).
        reg_mode = await get_setting_typed("registration_mode")
        mode_label = "📋 Полная" if reg_mode == "full" else "⚡ Краткая"
        lines.append(f"📝 Форма регистрации: <b>{mode_label}</b>")

    # REG-02 (06-05): feature-switch reads resolved via the registry's enum default,
    # byte-identical to the prior `get_setting(k) or "<literal>"` idiom (get_setting_typed's
    # enum branch is `raw if raw else default`, matching falsy-to-default on empty-string).
    bonus_enabled = await get_setting_typed("reg_bonus_enabled")
    bonus_label = "✅ Вкл" if bonus_enabled == "on" else "❌ Выкл"
    lines.append(f"🎁 Бонус за регистрацию: <b>{bonus_label}</b>")

    full_appr = await get_setting_typed("full_approval")
    short_appr = await get_setting_typed("short_approval")
    notify_mode = await get_setting_typed("pending_notify_mode")
    appr_lbl = lambda v: "👮 Ручная" if v == "manual" else "⚡ Авто"
    lines.append(f"✅ Модерация полной формы: <b>{appr_lbl(full_appr)}</b>")
    lines.append(f"✅ Модерация краткой формы: <b>{appr_lbl(short_appr)}</b>")
    notify_lbl = "📨 Сразу" if notify_mode == "instant" else "🕒 Пачкой (напоминалка)"
    lines.append(f"🔔 Уведомление о заявке: <b>{notify_lbl}</b>")

    payment_enabled = await get_setting_typed("payment_enabled")
    consent_enabled = await get_setting_typed("consent_enabled")
    lines.append(f"💳 Модуль оплаты: <b>{'✅ Вкл' if payment_enabled == 'on' else '❌ Выкл'}</b>")
    lines.append(f"📋 Модуль согласий: <b>{'✅ Вкл' if consent_enabled == 'on' else '❌ Выкл'}</b>")
    pay_rem_enabled = await get_setting_typed("payment_reminders_enabled")
    lines.append(f"⏰ Автонапоминания об оплате: <b>{'✅ Вкл' if pay_rem_enabled == 'on' else '❌ Выкл'}</b>")

    # Phase 5 (D-13): party settings always read as off/manual when unset — new-capability
    # default-OFF posture (Phase-4 D-15 lineage), independent of full_approval/short_approval.
    party_enabled = await get_setting_typed("party_enabled")
    party_fork_question = await get_setting_typed("party_fork_question")
    party_approval = await get_setting_typed("party_approval")
    lines.append(f"🎉 Трек вечеринки: <b>{'✅ Вкл' if party_enabled == 'on' else '❌ Выкл'}</b>")
    lines.append(f"🔀 Вопрос-развилка формата: <b>{'✅ Вкл' if party_fork_question == 'on' else '❌ Выкл'}</b>")
    lines.append(f"✅ Модерация вечеринки: <b>{appr_lbl(party_approval)}</b>")

    enabled_q = 0
    for _, sk, *_rest in REG_FLOW:
        # REG-02 (06-04): resolves via the registry's toggle default, byte-identical to the
        # prior REG_DEFAULTS.get(sk, "on") == "on" fallback (get_setting_typed's toggle
        # branch is (raw == "on") if raw is not None else (default == "on")).
        is_on = await get_setting_typed(sk)
        if is_on:
            enabled_q += 1
    lines.append(f"📋 Вопросы: <b>{enabled_q} из {len(REG_FLOW)}</b> включено")

    enabled_m = 0
    for key, _ in MENU_BUTTONS:
        if per_city_ctx:
            # Effective per-city resolver (fallback to global) — CONTEXT B: the counter at
            # the header's city must reflect what that city's delegates actually see.
            is_on = await get_setting_typed_for_city(key, header_code) == "on"
        else:
            v = await get_setting(key)
            is_on = (v == "on") if v is not None else True
        if is_on:
            enabled_m += 1
    lines.append(f"🔘 Меню: <b>{enabled_m} из {len(MENU_BUTTONS)}</b> кнопок")
    lines.append("")

    lines.append("✏️ Тексты и медиа — по кнопкам групп ниже.")

    lines.append("")
    lines.append("<i>Отправьте «-» при редактировании текстовых полей, чтобы скрыть.</i>")
    return "\n".join(lines)


# Phase 09.3 (04, CITY-09): admin_id=None means "no header context" — see the comment
# above render_settings_text (same contract, same structural test).
async def build_settings_keyboard(admin_id: int | None = None):
    # Phase 09.3 (04, CITY-09): WR-05 — single header read for this render call, same
    # contract as render_settings_text above.
    header_code = await admin_selected_city(admin_id) if admin_id is not None else None
    per_city_ctx = bool(header_code and header_code != ALL_CITIES)

    # REG-02 (06-05): feature-switch reads resolved via the registry's enum default,
    # byte-identical to the prior `get_setting(k) or "<literal>"` idiom — button TEXT
    # ternaries and callback_data strings are intentionally untouched (D-12).
    if per_city_ctx:
        reg_mode = await get_setting_typed_for_city("registration_mode", header_code)
    else:
        reg_mode = await get_setting_typed("registration_mode")
    toggle_text = "📝 Регистрация: ⚡ Краткая → 📋 Полная" if reg_mode == "short" else "📝 Регистрация: 📋 Полная → ⚡ Краткая"

    bonus_enabled = await get_setting_typed("reg_bonus_enabled")
    bonus_toggle_text = "🎁 Бонус: ❌ Выкл → ✅ Вкл" if bonus_enabled == "off" else "🎁 Бонус: ✅ Вкл → ❌ Выкл"

    full_appr = await get_setting_typed("full_approval")
    short_appr = await get_setting_typed("short_approval")
    notify_mode = await get_setting_typed("pending_notify_mode")
    full_txt = "✅ Полная форма: 👮 Ручная → ⚡ Авто" if full_appr == "manual" else "✅ Полная форма: ⚡ Авто → 👮 Ручная"
    short_txt = "✅ Краткая форма: 👮 Ручная → ⚡ Авто" if short_appr == "manual" else "✅ Краткая форма: ⚡ Авто → 👮 Ручная"
    notify_txt = "🔔 Уведомление: 📨 Сразу → 🕒 Пачкой" if notify_mode == "instant" else "🔔 Уведомление: 🕒 Пачкой → 📨 Сразу"

    payment_enabled = await get_setting_typed("payment_enabled")
    consent_enabled = await get_setting_typed("consent_enabled")
    payment_toggle_text = "💳 Оплата: ❌ Выкл → ✅ Вкл" if payment_enabled != "on" else "💳 Оплата: ✅ Вкл → ❌ Выкл"
    consent_toggle_text = "📋 Согласия: ❌ Выкл → ✅ Вкл" if consent_enabled != "on" else "📋 Согласия: ✅ Вкл → ❌ Выкл"
    pay_rem_enabled = await get_setting_typed("payment_reminders_enabled")
    pay_rem_toggle_text = ("⏰ Автонапоминания оплаты: ✅ Вкл → ❌ Выкл" if pay_rem_enabled == "on"
                           else "⏰ Автонапоминания оплаты: ❌ Выкл → ✅ Вкл")

    uni_mode = await get_setting_typed("reg_university_mode")
    uni_mode_text = ("🏫 ВУЗ: выбор из списка → свободный ввод" if uni_mode == "list"
                     else "🏫 ВУЗ: свободный ввод → выбор из списка")
    edu_cond = await get_setting_typed("edu_conditional")
    edu_cond_text = ("🎓 ВУЗ/курс только у студентов: ✅ Вкл → ❌ Выкл" if edu_cond == "on"
                     else "🎓 ВУЗ/курс только у студентов: ❌ Выкл → ✅ Вкл")
    show_progress = await get_setting_typed("reg_show_progress")
    show_progress_text = ("🔢 Нумерация вопросов: ✅ Вкл → ❌ Выкл" if show_progress == "on"
                          else "🔢 Нумерация вопросов: ❌ Выкл → ✅ Вкл")

    # Phase 5 (D-13): party_enabled / party_fork_question default OFF; party_approval
    # default "manual" — resolved via the registry's enum default (REG-02, 06-05).
    party_enabled = await get_setting_typed("party_enabled")
    party_toggle_text = ("🎉 Трек вечеринки: ❌ Выкл → ✅ Вкл" if party_enabled != "on"
                         else "🎉 Трек вечеринки: ✅ Вкл → ❌ Выкл")
    party_fork_question = await get_setting_typed("party_fork_question")
    party_fork_toggle_text = ("🔀 Вопрос-развилка формата: ❌ Выкл → ✅ Вкл" if party_fork_question != "on"
                              else "🔀 Вопрос-развилка формата: ✅ Вкл → ❌ Выкл")
    party_approval = await get_setting_typed("party_approval")
    party_appr_txt = ("✅ Модерация вечеринки: 👮 Ручная → ⚡ Авто" if party_approval == "manual"
                      else "✅ Модерация вечеринки: ⚡ Авто → 👮 Ручная")

    buttons = [
        [InlineKeyboardButton(text=toggle_text, callback_data="settings_toggle_reg")],
    ]
    # Phase 09.3 (04, CITY-09): registration_mode has no settings_edit:{key} screen of its
    # own (it's a landing toggle, not a SETTINGS_FIELDS text entry) — the header toggle above
    # IS its per-city editor now; the old picker shortcut («🏙 Форма по городам», entering
    # the per-key city picker screen) is gone. Reset row only when the header's city has an
    # own override to reset (same "↩️ Как везде only when something to undo" idiom the
    # header-scoped per-key editor uses below — never shown with nothing to undo).
    if per_city_ctx:
        own_key = per_city_key("registration_mode", header_code)
        if own_key and await get_setting(own_key):
            buttons.append([InlineKeyboardButton(text="↩️ Как везде", callback_data="settings_regmode_reset")])
    buttons += [
        [InlineKeyboardButton(text=bonus_toggle_text, callback_data="settings_toggle_bonus")],
        [InlineKeyboardButton(text=full_txt, callback_data="settings_toggle_full_approval")],
        [InlineKeyboardButton(text=short_txt, callback_data="settings_toggle_short_approval")],
        [InlineKeyboardButton(text=notify_txt, callback_data="settings_toggle_notify")],
        [InlineKeyboardButton(text=payment_toggle_text, callback_data="toggle_payment_enabled")],
        [InlineKeyboardButton(text=pay_rem_toggle_text, callback_data="toggle_payment_reminders")],
        [InlineKeyboardButton(text=consent_toggle_text, callback_data="toggle_consent_enabled")],
        [InlineKeyboardButton(text="🧾 PDF согласий", callback_data="admin_consent_pdfs")],
        [InlineKeyboardButton(text=uni_mode_text, callback_data="toggle_uni_mode")],
        [InlineKeyboardButton(text=edu_cond_text, callback_data="toggle_edu_conditional")],
        [InlineKeyboardButton(text=show_progress_text, callback_data="toggle_show_progress")],
        [InlineKeyboardButton(text=party_toggle_text, callback_data="toggle_party_enabled")],
        [InlineKeyboardButton(text=party_fork_toggle_text, callback_data="toggle_party_fork_question")],
        [InlineKeyboardButton(text=party_appr_txt, callback_data="settings_toggle_party_approval")],
        [InlineKeyboardButton(text="🎛 Тип события (пресет)", callback_data="admin_event_preset")],
        [InlineKeyboardButton(text="📋 Вопросы регистрации", callback_data="admin_reg_questions")],
        [InlineKeyboardButton(text="✏️ Тексты вопросов", callback_data="admin_reg_prompts")],
        [InlineKeyboardButton(text="🔘 Кнопки меню", callback_data="admin_menu_buttons")],
        [InlineKeyboardButton(text="👥 Роли и доступы", callback_data="admin_roles")],
    ]
    for label, token in _settings_nav_groups():
        buttons.append([InlineKeyboardButton(text=label, callback_data=f"settings_group:{token}")])
    buttons.append([InlineKeyboardButton(text="← Назад", callback_data="settings_back")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


async def render_settings_group_text(token: str, admin_id: int | None = None) -> str:
    """Quick 260724-c0x: per-group sub-screen — status FLAGS only («задано»/«не задано»/
    «по умолчанию»), never the raw value inline (that stays behind the existing
    settings_edit tap-through, unchanged).

    Phase 09.3 (05, CITY-09): WR-05 — resolve the header ONCE for this whole render call.
    `admin_id is None` (tests, unknown-caller sites) and `header_code in (None, ALL_CITIES)`
    (module off / explicit «все города») all collapse to the SAME branch below —
    byte-identical to pre-phase output (CONTEXT D module-off parity)."""
    header_code = await admin_selected_city(admin_id) if admin_id is not None else None
    per_city_ctx = bool(header_code and header_code != ALL_CITIES)

    group_label = _settings_group_label(token)
    lines = []
    if per_city_ctx:
        lines.append(f"🏙 {html_module.escape(await city_label(header_code))}")
    lines += [f"⚙️ <b>Настройки → {group_label}</b>", ""]

    field_labels = {k: lbl for k, lbl, _ in SETTINGS_FIELDS}
    # Phase 09.2 (C, CITY-05): compact «🏙 N» override-count marker, only when the cities
    # module is on — module off = byte-identical to today's text (CONTEXT C). The full list
    # of city names lives on the per-key editor screen (settings_edit callback family), not
    # here — this screen deliberately never shows raw values inline (quick 260724-c0x contract).
    city_module_on = await cities_module_on()
    for key in _settings_group_keys(token):
        label = field_labels.get(key, key)
        if per_city_ctx and is_per_city(key):
            # Phase 09.3 (05, CITY-09, CONTEXT B): flag relative to the header's city —
            # «✏️ своё» if the city has its own value, else «как везде · {общее}» using the
            # SAME ladder as the global branch below (no duplicated wording rules).
            own_key = per_city_key(key, header_code)
            if own_key and await get_setting(own_key):
                flag = "✏️ своё"
            else:
                value = await get_setting(key)
                if value:
                    common = "задано"
                elif key in _SETTINGS_DISPLAY_DEFAULTS:
                    common = "по умолчанию"
                else:
                    common = "не задано"
                flag = f"как везде · {common}"
            lines.append(f"{label}: {flag}")
            continue
        value = await get_setting(key)
        if value:
            flag = "✏️ задано"
        elif key in _SETTINGS_DISPLAY_DEFAULTS:
            flag = "<i>по умолчанию</i>"
        else:
            flag = "<i>— не задано</i>"
        city_suffix = ""
        if city_module_on and is_per_city(key):
            codes = await city_override_codes(key)
            if codes:
                city_suffix = f" · 🏙 {len(codes)}"
        lines.append(f"{label}: {flag}{city_suffix}")

    if token == "event":
        for prefix, label, _ in PHOTO_FIELDS:
            photo = await get_setting(f"{prefix}_photo_file_id")
            lines.append(f"{label}: {'✅ загружена' if photo else '<i>— не задано</i>'}")
        for prefix, label, _ in FILE_FIELDS:
            photo = await get_setting(f"{prefix}_photo_file_id")
            doc = await get_setting(f"{prefix}_doc_file_id")
            lines.append(f"{label}: {'✅ загружен' if (photo or doc) else '<i>— не задано</i>'}")

    return "\n".join(lines)


async def build_settings_group_keyboard(token: str, admin_id: int | None = None):
    """Reuses the existing settings_edit/settings_photo/settings_file callbacks unchanged —
    only the button placement changes. Configured fields first, then a noop section-header
    button (req #2: collapse unconfigured fields), then unconfigured fields.

    Phase 09.3 (05, CITY-09): WR-05 — resolve the header ONCE, same contract as
    render_settings_group_text above (kept as a second read here, not shared across the two
    functions, since they're independent render calls per call site — matches the
    render_settings_text/build_settings_keyboard precedent from plan 04)."""
    header_code = await admin_selected_city(admin_id) if admin_id is not None else None
    per_city_ctx = bool(header_code and header_code != ALL_CITIES)

    field_labels = {k: lbl for k, lbl, _ in SETTINGS_FIELDS}
    configured: list[InlineKeyboardButton] = []
    unconfigured: list[InlineKeyboardButton] = []

    for key in _settings_group_keys(token):
        label = field_labels.get(key, key)
        btn = InlineKeyboardButton(text=f"✏️ {label}", callback_data=f"settings_edit:{key}")
        if per_city_ctx and is_per_city(key):
            # CONTEXT B: «настроено» = effective value at the header's city (своё, иначе
            # общее) — a key with only a city override must land in «настроено», not the
            # collapsed «не настроено» section.
            own_key = per_city_key(key, header_code)
            own_value = await get_setting(own_key) if own_key else None
            value = own_value or await get_setting(key)
        else:
            value = await get_setting(key)
        (configured if value else unconfigured).append(btn)

    if token == "event":
        for prefix, label, _ in PHOTO_FIELDS:
            btn = InlineKeyboardButton(text=f"📷 {label}", callback_data=f"settings_photo:{prefix}")
            photo = await get_setting(f"{prefix}_photo_file_id")
            (configured if photo else unconfigured).append(btn)
        for prefix, label, _ in FILE_FIELDS:
            btn = InlineKeyboardButton(text=f"📎 {label}", callback_data=f"settings_file:{prefix}")
            photo = await get_setting(f"{prefix}_photo_file_id")
            doc = await get_setting(f"{prefix}_doc_file_id")
            (configured if (photo or doc) else unconfigured).append(btn)

    buttons = [[b] for b in configured]
    if unconfigured:
        buttons.append([InlineKeyboardButton(text="── не настроено ──", callback_data="settings_group_noop")])
        buttons.extend([[b] for b in unconfigured])
    if token == "event" and admin_id is not None and admin_id in config.ADMIN_IDS:
        # Phase 07.3 (02, RET-01, T-073-02-01): capability `settings` already gates this whole
        # screen, but «Новый сезон» is stricter — superadmin only. Hiding the button from a
        # non-superadmin `settings` holder is "bot for people" UX (CLAUDE.md), NOT the real
        # gate — the real gate is the ADMIN_IDS re-check inside every wizard handler below,
        # because a stale inline keyboard rendered before rights changed lives in the chat
        # forever (same reasoning as roles_city_start's own inline re-check).
        buttons.append([InlineKeyboardButton(text="🔄 Новый сезон", callback_data="admin_season_reset")])
    if token == "event":
        # Phase 07.3 (06, RET-04): visible to EVERYONE who reaches this screen — unlike «Новый
        # сезон», import is not superadmin-only (CONTEXT D); the gate is the `settings`
        # capability that already got them here.
        buttons.append([InlineKeyboardButton(text="📥 Импорт прошлого события", callback_data="admin_season_import")])
    buttons.append([InlineKeyboardButton(text="← Назад к настройкам", callback_data="admin_settings")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


@router.callback_query(F.data == "admin_settings")
async def show_admin_settings(callback: types.CallbackQuery):
    text = await render_settings_text(callback.from_user.id)
    await callback.message.edit_text(text, parse_mode="HTML", reply_markup=await build_settings_keyboard(callback.from_user.id))
    await callback.answer()


@router.callback_query(F.data.startswith("settings_group:"))
async def show_settings_group(callback: types.CallbackQuery):
    token = callback.data.split(":", 1)[1]
    text = await render_settings_group_text(token, callback.from_user.id)
    await callback.message.edit_text(text, parse_mode="HTML", reply_markup=await build_settings_group_keyboard(token, callback.from_user.id))
    await callback.answer()


@router.callback_query(F.data == "settings_group_noop")
async def settings_group_noop(callback: types.CallbackQuery):
    # Section-header button in the collapsed «не настроено» view — not actionable.
    await callback.answer()


@router.callback_query(F.data == "settings_toggle_reg")
async def toggle_registration_mode(callback: types.CallbackQuery):
    # Phase 09.3 (04, CITY-09): WR-05 — single header read for this handler.
    admin_id = callback.from_user.id
    header_code = await admin_selected_city(admin_id)
    if header_code and header_code != ALL_CITIES:
        # T-093-12: re-check the RIGHT in the handler, not just via a hidden button.
        visible = await _per_city_visible_codes(admin_id)
        if header_code not in visible:
            await callback.answer("Этот город правит суперадмин", show_alert=True)
            return
        # T-093-13: composed key comes ONLY from cities.per_city_key.
        composed = per_city_key("registration_mode", header_code)
        if composed is None:
            await callback.answer("Неизвестный город", show_alert=True)
            return
        current = await get_setting_typed_for_city("registration_mode", header_code)
        new_mode = "full" if current == "short" else "short"
        await set_setting(composed, new_mode)
        city_txt = await city_label(header_code)
        human = _enum_human_label("registration_mode", new_mode)
        await callback.answer(f"Форма регистрации для {city_txt}: {human}", show_alert=True)
    else:
        # REG-02 (06-05): current-value read migrated to the registry; write path unchanged.
        current = await get_setting_typed("registration_mode")
        new_mode = "full" if current == "short" else "short"
        await set_setting("registration_mode", new_mode)
        label = "📋 Полная" if new_mode == "full" else "⚡ Краткая"
        await callback.answer(f"Форма регистрации: {label}", show_alert=True)

    text = await render_settings_text(admin_id)
    await callback.message.edit_text(text, parse_mode="HTML", reply_markup=await build_settings_keyboard(admin_id))
    # Phase 7 (SHORT-03, gate #5): materialize the short tab the moment the manager flips
    # into "Краткая" — no need to wait for the first registration. Switching back to "Полная"
    # is a no-op (the gate inside returns early); the tab and its data are never touched.
    # Materializing the tab is a property of the EVENT ("Акция"), not the city — always run,
    # even when this toggle just wrote a per-city override.
    await _refresh_short_sheet_header()


@router.callback_query(F.data == "settings_regmode_reset")
async def settings_regmode_reset(callback: types.CallbackQuery):
    """Confirm screen for «↩️ Как везде» on the header-scoped registration-mode toggle —
    same two-step confirm gate idiom as the header-scoped per-key editor's own reset pair
    below (settings_reset_city/settings_reset_city_go)."""
    admin_id = callback.from_user.id
    header_code = await admin_selected_city(admin_id)
    if not header_code or header_code == ALL_CITIES:
        await callback.answer("Нет своего значения для сброса", show_alert=True)
        return
    visible = await _per_city_visible_codes(admin_id)
    if header_code not in visible:
        await callback.answer("Этот город правит суперадмин", show_alert=True)
        return
    composed = per_city_key("registration_mode", header_code)
    if composed is None or not await get_setting(composed):
        await callback.answer("Нет своего значения для сброса", show_alert=True)
        return

    city_txt = await city_label(header_code)
    global_value = await get_setting_typed("registration_mode")
    global_human = _enum_human_label("registration_mode", global_value)
    text = (
        f"Город {html_module.escape(city_txt)} снова будет использовать общую форму "
        f"регистрации:\n<b>{global_human}</b>\n\nСвоя форма регистрации города будет удалена."
    )
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Да, как везде", callback_data=f"settings_regmode_reset_go:{header_code}")],
        [InlineKeyboardButton(text="← Отмена", callback_data="admin_settings")],
    ])
    await callback.message.edit_text(text, parse_mode="HTML", reply_markup=kb)
    await callback.answer()


@router.callback_query(F.data.startswith("settings_regmode_reset_go:"))
async def settings_regmode_reset_go(callback: types.CallbackQuery):
    admin_id = callback.from_user.id
    code = callback.data.split(":", 1)[1]

    # T-093-13: composed key comes ONLY from cities.per_city_key — refuse on an unknown code
    # before touching rights or freshness (mirrors settings_reset_city_go's own guard order).
    composed = per_city_key("registration_mode", code)
    if composed is None:
        await callback.answer("Неизвестный город", show_alert=True)
        return
    # T-093-12: RIGHT check against the code carried in callback_data (not just the current
    # header) — this is what catches a bound manager's forged confirmation for another city,
    # since the freshness check below alone could never distinguish "forged" from "stale"
    # (a bound manager's OWN header can never actually become another city).
    visible = await _per_city_visible_codes(admin_id)
    if code not in visible:
        await callback.answer("Этот город правит суперадмин", show_alert=True)
        return
    # T-093-14: freshness — the confirm screen named the header's city; if the header moved
    # on since, refuse and make the admin re-open the confirm screen for the NEW city.
    current = await admin_selected_city(admin_id)
    if code != current:
        await callback.answer("Город админки изменился — подтвердите заново.", show_alert=True)
        text = await render_settings_text(admin_id)
        await callback.message.edit_text(text, parse_mode="HTML", reply_markup=await build_settings_keyboard(admin_id))
        return

    await delete_setting(composed)  # idempotent — safe if already absent
    city_txt = await city_label(code)
    await callback.answer(f"Готово: {city_txt} — как везде", show_alert=True)
    text = await render_settings_text(admin_id)
    await callback.message.edit_text(text, parse_mode="HTML", reply_markup=await build_settings_keyboard(admin_id))


async def _toggle_approval_setting(callback: types.CallbackQuery, key: str, default: str, title: str):
    # REG-02 (06-07): final-coverage sweep — key is always in SETTINGS_SCHEMA (full_approval/
    # short_approval/party_approval), registry default byte-identical to the `default` param.
    current = await get_setting_typed(key)
    new_val = "auto" if current == "manual" else "manual"
    await set_setting(key, new_val)
    await callback.answer(f"{title}: {'👮 Ручная' if new_val == 'manual' else '⚡ Авто'}", show_alert=True)
    text = await render_settings_text(callback.from_user.id)
    await callback.message.edit_text(text, parse_mode="HTML", reply_markup=await build_settings_keyboard(callback.from_user.id))


@router.callback_query(F.data == "settings_toggle_full_approval")
async def toggle_full_approval(callback: types.CallbackQuery):
    await _toggle_approval_setting(callback, "full_approval", "manual", "Модерация полной формы")


@router.callback_query(F.data == "settings_toggle_short_approval")
async def toggle_short_approval(callback: types.CallbackQuery):
    await _toggle_approval_setting(callback, "short_approval", "auto", "Модерация краткой формы")


@router.callback_query(F.data == "settings_toggle_party_approval")
async def toggle_party_approval(callback: types.CallbackQuery):
    # D-13: independent setting — never reads/writes/derives from full_approval or
    # short_approval, no fallback chain between them.
    await _toggle_approval_setting(callback, "party_approval", "manual", "Модерация вечеринки")


# ── Phase 4: module on/off toggles (payment, consent) + event-type preset ────

async def _toggle_module_setting(callback: types.CallbackQuery, key: str, title: str):
    """On/off toggle for a Phase 4 module flag (fail-safe default OFF, D-15)."""
    # REG-02 (06-07): final-coverage sweep — key is always in SETTINGS_SCHEMA
    # (payment_enabled/consent_enabled/party_enabled/party_fork_question), all default "off".
    current = await get_setting_typed(key)
    new_val = "off" if current == "on" else "on"
    await set_setting(key, new_val)
    label = "✅ Вкл" if new_val == "on" else "❌ Выкл"
    await callback.answer(f"{title}: {label}", show_alert=True)
    text = await render_settings_text(callback.from_user.id)
    await callback.message.edit_text(text, parse_mode="HTML", reply_markup=await build_settings_keyboard(callback.from_user.id))


@router.callback_query(F.data == "toggle_payment_enabled")
async def toggle_payment_enabled(callback: types.CallbackQuery):
    await _toggle_module_setting(callback, "payment_enabled", "💳 Оплата")


@router.callback_query(F.data == "toggle_consent_enabled")
async def toggle_consent_enabled(callback: types.CallbackQuery):
    await _toggle_module_setting(callback, "consent_enabled", "📋 Согласия")


@router.callback_query(F.data == "toggle_party_enabled")
async def toggle_party_enabled(callback: types.CallbackQuery):
    # D-11a master gate: default OFF (fail-safe, Phase-4 D-15 lineage).
    await _toggle_module_setting(callback, "party_enabled", "🎉 Трек вечеринки")


@router.callback_query(F.data == "toggle_party_fork_question")
async def toggle_party_fork_question(callback: types.CallbackQuery):
    # D-10: default OFF — an ordinary delegate sees no extra screen until an admin opts in.
    await _toggle_module_setting(callback, "party_fork_question", "🔀 Вопрос-развилка формата")


@router.callback_query(F.data == "toggle_payment_reminders")
async def toggle_payment_reminders(callback: types.CallbackQuery):
    # Default ON — preserves prior behaviour (reminders fired whenever a deadline was set).
    await _toggle_value_setting(
        callback, "payment_reminders_enabled", "on", "off", "on",
        "⏰ Автонапоминания об оплате включены", "⏰ Автонапоминания об оплате выключены",
    )


async def _toggle_value_setting(callback, key, val_a, val_b, default, title_a, title_b):
    """Generic two-value toggle (e.g. list↔text, on↔off) with a friendly alert."""
    # REG-02 (06-07): final-coverage sweep — every key routed through this helper
    # (reg_university_mode/edu_conditional/reg_show_progress/payment_reminders_enabled) is
    # in SETTINGS_SCHEMA with a registry default byte-identical to the `default` param.
    current = await get_setting_typed(key)
    new_val = val_b if current == val_a else val_a
    await set_setting(key, new_val)
    await callback.answer(title_a if new_val == val_a else title_b, show_alert=True)
    text = await render_settings_text(callback.from_user.id)
    await callback.message.edit_text(text, parse_mode="HTML", reply_markup=await build_settings_keyboard(callback.from_user.id))


@router.callback_query(F.data == "toggle_uni_mode")
async def toggle_uni_mode(callback: types.CallbackQuery):
    await _toggle_value_setting(
        callback, "reg_university_mode", "list", "text", "text",
        "🏫 ВУЗ: выбор из списка", "🏫 ВУЗ: свободный ввод",
    )


@router.callback_query(F.data == "toggle_edu_conditional")
async def toggle_edu_conditional(callback: types.CallbackQuery):
    await _toggle_value_setting(
        callback, "edu_conditional", "on", "off", "on",
        "🎓 ВУЗ/курс спрашиваются только у студентов", "🎓 ВУЗ/курс спрашиваются у всех",
    )


@router.callback_query(F.data == "toggle_show_progress")
async def toggle_show_progress(callback: types.CallbackQuery):
    await _toggle_value_setting(
        callback, "reg_show_progress", "on", "off", "off",
        "🔢 Нумерация вопросов включена", "🔢 Нумерация вопросов выключена",
    )


async def _apply_event_type_preset(event_type: str):
    """D-05: event type presets module flags; each is still manually overridable after.
    conference → payment+consent ON; forum → both OFF; custom → no change."""
    if event_type == "conference":
        await set_setting("payment_enabled", "on")
        await set_setting("consent_enabled", "on")
    elif event_type == "forum":
        await set_setting("payment_enabled", "off")
        await set_setting("consent_enabled", "off")
    # "custom" → no change (manual control)


@router.callback_query(F.data == "settings_toggle_notify")
async def toggle_notify_mode(callback: types.CallbackQuery):
    # REG-02 (06-05): current-value read migrated to the registry; write path unchanged.
    current = await get_setting_typed("pending_notify_mode")
    new_val = "batched" if current == "instant" else "instant"
    await set_setting("pending_notify_mode", new_val)
    await callback.answer(f"Уведомление: {'📨 Сразу' if new_val == 'instant' else '🕒 Пачкой'}", show_alert=True)
    text = await render_settings_text(callback.from_user.id)
    await callback.message.edit_text(text, parse_mode="HTML", reply_markup=await build_settings_keyboard(callback.from_user.id))


@router.callback_query(F.data == "settings_toggle_bonus")
async def toggle_bonus(callback: types.CallbackQuery):
    # REG-02 (06-05): current-value read migrated to the registry; write path unchanged.
    current = await get_setting_typed("reg_bonus_enabled")
    new_val = "on" if current == "off" else "off"
    await set_setting("reg_bonus_enabled", new_val)

    label = "✅ Вкл" if new_val == "on" else "❌ Выкл"
    await callback.answer(f"Бонус за регистрацию: {label}", show_alert=True)

    text = await render_settings_text(callback.from_user.id)
    await callback.message.edit_text(text, parse_mode="HTML", reply_markup=await build_settings_keyboard(callback.from_user.id))


@router.callback_query(F.data.startswith("settings_file:"))
async def settings_file_start(callback: types.CallbackQuery, state: FSMContext):
    prefix = callback.data.split(":", 1)[1]
    prompts = {p: (label, prompt) for p, label, prompt in FILE_FIELDS}
    label, prompt = prompts.get(prefix, ("Файл", "Отправьте файл."))

    photo = await get_setting(f"{prefix}_photo_file_id")
    doc = await get_setting(f"{prefix}_doc_file_id")
    status = "✅ загружен" if (photo or doc) else "<i>не загружен</i>"
    text = f"{label}: {status}\n\n{prompt}"

    cancel_kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="❌ Отмена", callback_data="settings_cancel")],
    ])
    await callback.message.edit_text(text, parse_mode="HTML", reply_markup=cancel_kb)
    await state.set_state(EditSetting.waiting_for_file)
    await state.update_data(file_setting=prefix)
    await callback.answer()


# ── Phase 09.2 (C, CITY-05): «🏙 Для города…» per-setting override sub-flow ────────────────
#
# One reusable screen for every per_city-flagged SETTINGS_SCHEMA key (text or enum) — reached
# from the editor (settings_edit_start below) and from the landing's «🏙 Форма по городам»
# shortcut for registration_mode, which has no settings_edit screen of its own. Mirrors the
# admin_city_switch/_roles_city_kb idiom (RESEARCH Pattern 3): city LABELS only, never codes.

async def _per_city_visible_codes(admin_id: int) -> list[str]:
    """Which city codes this admin may edit — a RIGHT, not a filter (Phase 07.2 terminology).
    Phase 09.3 (06, CITY-09): the only caller-facing question this answers now is "can this
    admin edit the city currently sitting in the header" (membership test), not "which cities
    should a picker list" — there is no picker left. Superadmins (config.ADMIN_IDS) see every
    city; a manager bound to a city (get_staff_city) sees exactly that one; an unbound manager
    sees all. This shapes the keyboard only — every write handler below (settings_edit_city /
    settings_reset_city_go) re-checks membership itself before writing anything (RESEARCH
    Pitfall 6: a hidden button is not access control)."""
    if admin_id in config.ADMIN_IDS:
        return city_codes()
    bound = await get_staff_city(admin_id)
    if bound:
        return [normalize_city(bound)]
    return city_codes()


async def _settings_edit_screen(key: str, header_code: str | None) -> tuple[str, InlineKeyboardMarkup]:
    """Phase 09.3 (06, CITY-09): single render helper for the per-key editor, relative to an
    ALREADY-RESOLVED header code (WR-05 — every caller resolves `admin_selected_city()` once
    and passes the result in here; this helper never calls it a second time). Reused by
    `settings_edit_start`, the `per_city_base` return path in `settings_edit_value`, and the
    reset confirm/go handlers below — one screen shape per branch, no separate picker screen.

    Three branches (CONTEXT B):
    (1) header is a real city AND `key` is per_city -> the city's OWN value or «как везде» +
        «✏️ Изменить для {город}» / «↩️ Как везде» (reset row only when an own value exists).
        No FSM is implied by this screen alone — the caller decides (branch (1) never starts
        one from here; only `settings_edit_city` does, on an explicit tap).
    (2) header is a real city AND `key` is NOT per_city -> today's prompt screen plus a
        global-only note (see the literal marker string in the branch below); the key is
        edited globally.
    (3) header is `None`/`ALL_CITIES` (module off or «все города») -> today's prompt screen,
        byte-identical to before the phase, minus the removed «🏙 Для города…» row."""
    prompts = {k: prompt for k, _, prompt in SETTINGS_FIELDS}
    # Phase 8 (ROLE-02): role_caps_<role> etc. ride this generic edit flow but aren't in
    # SETTINGS_FIELDS (D-18) — fall back to the registry itself for the prompt (Phase 6
    # D-13, registry-as-source) before the last-resort literal.
    prompt = prompts.get(key) or SETTINGS_SCHEMA.get(key, {}).get("prompt") or "Введите значение"
    per_city_ctx = bool(header_code and header_code != ALL_CITIES)

    if per_city_ctx and is_per_city(key):
        city_label_txt = await city_label(header_code)
        composed = per_city_key(key, header_code)
        own_value = await get_setting(composed) if composed else None
        lines = [f"🏙 {html_module.escape(city_label_txt)}"]
        if own_value:
            lines.append(f"Своё значение: <b>{html_module.escape(own_value)}</b>")
        else:
            global_value = await get_setting(key)
            global_txt = f"<b>{html_module.escape(global_value)}</b>" if global_value else "<i>по умолчанию</i>"
            lines.append(f"Как везде. Общий текст: {global_txt}")

        rows: list[list[InlineKeyboardButton]] = [
            [InlineKeyboardButton(
                text=f"✏️ Изменить для {city_label_txt}",
                callback_data=f"settings_edit_city:{key}",
            )],
        ]
        if own_value:
            rows.append([InlineKeyboardButton(text="↩️ Как везде", callback_data=f"settings_reset_city:{key}")])
        rows.append([InlineKeyboardButton(text="❌ Отмена", callback_data="settings_cancel")])
        return "\n".join(lines), InlineKeyboardMarkup(inline_keyboard=rows)

    # Branches (2)/(3): today's prompt screen — escape both the field description (may
    # contain literal <b>/<code> examples) and the current value (admin may have stored raw
    # HTML) — otherwise parse_mode=HTML breaks.
    current = await get_setting(key)
    text = f"{html_module.escape(prompt)}"
    if current:
        text = f"Сейчас задано:\n<b>{html_module.escape(current)}</b>\n\n{text}"
    text += "\n\n<i>Пришлите новое значение сообщением. Чтобы очистить поле — отправьте «-».</i>"

    if per_city_ctx:
        # Branch (2): real city header, but the key is not per_city — правится глобально,
        # marked so the manager never mistakes this for a city-scoped edit.
        text = (
            f"🏙 {html_module.escape(await city_label(header_code))}\n"
            f"Общая настройка (одна на все города)\n\n{text}"
        )
    elif is_per_city(key) and await cities_module_on():
        # Branch (3), city module on (header None only when module off — see
        # admin_selected_city — or explicit «все города»): keep the override summary, drop
        # the removed «🏙 Для города…» entry point (CONTEXT B).
        override_codes = await city_override_codes(key)
        if override_codes:
            names = ", ".join([await city_label(c) for c in override_codes])
            text += f"\n\nПереопределено для: {names}"

    rows = [[InlineKeyboardButton(text="❌ Отмена", callback_data="settings_cancel")]]
    return text, InlineKeyboardMarkup(inline_keyboard=rows)


@router.callback_query(F.data.startswith("settings_edit:"))
async def settings_edit_start(callback: types.CallbackQuery, state: FSMContext):
    key = callback.data.split(":", 1)[1]
    admin_id = callback.from_user.id
    # Phase 09.3 (06, CITY-09): WR-05 — single header read for this handler, passed into the
    # shared render helper so it never re-resolves the header itself.
    header_code = await admin_selected_city(admin_id)
    text, cancel_kb = await _settings_edit_screen(key, header_code)
    await callback.message.edit_text(text, parse_mode="HTML", reply_markup=cancel_kb)

    # Branch (1) (header = real city AND key is per_city) never starts the FSM from here —
    # «✏️ Изменить для …» (settings_edit_city below) is the only entry point that may start
    # it, otherwise a stray text message sent while just LOOKING at the screen would silently
    # overwrite the global value. state.clear() is defensive: re-entering this screen (e.g.
    # via the «❌ Отмена» button on the per-city input screen) must never leave a stale FSM
    # state pointing at the wrong composite key.
    own_city_context = bool(header_code and header_code != ALL_CITIES and is_per_city(key))
    await state.clear()
    if not own_city_context:
        await state.set_state(EditSetting.waiting_for_value)
        await state.update_data(setting_key=key)
    await callback.answer()


@router.callback_query(F.data.startswith("settings_edit_city:"))
async def settings_edit_city(callback: types.CallbackQuery, state: FSMContext):
    """Phase 09.3 (06, CITY-09): «✏️ Изменить для {город}» — the ONLY entry point that starts
    `EditSetting.waiting_for_value` for a per-city composite key; reuses 09.2-05's per-city
    text-entry mechanics verbatim (same FSM keys, same composed-key primitive), just reached
    from the header-aware editor screen instead of the deleted separate city picker."""
    key = callback.data.split(":", 1)[1]
    admin_id = callback.from_user.id
    # Fail-closed (RESEARCH Pattern 2): module off or a non-per_city key never starts an
    # edit, even if someone forges the callback_data directly.
    if not await cities_module_on() or not is_per_city(key):
        await callback.answer("Города выключены", show_alert=True)
        return
    header_code = await admin_selected_city(admin_id)
    if not header_code or header_code == ALL_CITIES:
        await callback.answer("Сначала выберите город в шапке", show_alert=True)
        return
    # T-093-19/21: RIGHT re-checked here, not just via a hidden button.
    visible = await _per_city_visible_codes(admin_id)
    if header_code not in visible:
        await callback.answer("Этот город правит суперадмин", show_alert=True)
        return
    # T-093-20: composed key comes ONLY from cities.per_city_key.
    composed = per_city_key(key, header_code)
    if composed is None:
        await callback.answer("Неизвестный город", show_alert=True)
        return

    entry = SETTINGS_SCHEMA.get(key, {})
    prompts = {k: prompt for k, _, prompt in SETTINGS_FIELDS}
    prompt = prompts.get(key) or entry.get("prompt") or "Введите значение"
    current = await get_setting(composed)
    city_txt = await city_label(header_code)
    text = f"🏙 {html_module.escape(city_txt)}\n\n"
    if current:
        text += f"Сейчас у города:\n<b>{html_module.escape(current)}</b>\n\n"
    else:
        text += "Сейчас у города: <i>как везде</i>\n\n"
    text += html_module.escape(prompt)
    text += "\n\n<i>Пришлите новое значение сообщением. Чтобы очистить поле — отправьте «-».</i>"

    rows: list[list[InlineKeyboardButton]] = []
    if current:
        rows.append([InlineKeyboardButton(text="↩️ Как везде", callback_data=f"settings_reset_city:{key}")])
    rows.append([InlineKeyboardButton(text="❌ Отмена", callback_data=f"settings_edit:{key}")])
    await callback.message.edit_text(text, parse_mode="HTML", reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))
    await state.set_state(EditSetting.waiting_for_value)
    await state.update_data(setting_key=composed, per_city_base=key)
    await callback.answer()


@router.callback_query(F.data.startswith("settings_reset_city:"))
async def settings_reset_city(callback: types.CallbackQuery):
    """Confirm screen for «↩️ Как везде» on the header-scoped per-key editor — same two-step
    confirm gate idiom as `settings_regmode_reset` above (09.2-05 lineage: names the value
    the city is about to fall back to before deleting anything)."""
    key = callback.data.split(":", 1)[1]
    admin_id = callback.from_user.id
    if not await cities_module_on() or not is_per_city(key):
        await callback.answer("Города выключены", show_alert=True)
        return
    header_code = await admin_selected_city(admin_id)
    if not header_code or header_code == ALL_CITIES:
        await callback.answer("Нет своего значения для сброса", show_alert=True)
        return
    visible = await _per_city_visible_codes(admin_id)
    if header_code not in visible:
        await callback.answer("Этот город правит суперадмин", show_alert=True)
        return
    composed = per_city_key(key, header_code)
    if composed is None or not await get_setting(composed):
        await callback.answer("Нет своего значения для сброса", show_alert=True)
        return

    city_txt = html_module.escape(await city_label(header_code))
    global_value = await get_setting(key)
    preview = f"<b>{html_module.escape(global_value)}</b>" if global_value else "<i>по умолчанию</i>"
    text = (
        f"Город {city_txt} снова будет использовать общий текст:\n{preview}\n\n"
        "Свой текст города будет удалён."
    )
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Да, как везде", callback_data=f"settings_reset_city_go:{key}:{header_code}")],
        [InlineKeyboardButton(text="← Отмена", callback_data=f"settings_edit:{key}")],
    ])
    await callback.message.edit_text(text, parse_mode="HTML", reply_markup=kb)
    await callback.answer()


@router.callback_query(F.data.startswith("settings_reset_city_go:"))
async def settings_reset_city_go(callback: types.CallbackQuery):
    rest = callback.data.split(":", 1)[1]
    if ":" not in rest:
        await callback.answer("Неизвестная кнопка", show_alert=True)
        return
    key, code = rest.rsplit(":", 1)
    admin_id = callback.from_user.id
    if not await cities_module_on() or not is_per_city(key):
        await callback.answer("Города выключены", show_alert=True)
        return
    # T-093-20: composed key comes ONLY from cities.per_city_key — refuse on an unknown code
    # before touching rights or freshness (same guard order as settings_regmode_reset_go above).
    composed = per_city_key(key, code)
    if composed is None:
        await callback.answer("Неизвестный город", show_alert=True)
        return
    # T-093-19: RIGHT check against the code carried in callback_data (not just the current
    # header) — this is what catches a bound manager's forged confirmation for another city,
    # since the freshness check below alone could never distinguish "forged" from "stale"
    # (a bound manager's OWN header can never actually become another city).
    visible = await _per_city_visible_codes(admin_id)
    if code not in visible:
        await callback.answer("Этот город правит суперадмин", show_alert=True)
        return
    # T-093-22: freshness — the confirm screen named the header's city; if the header moved
    # on since, refuse and re-render the editor for the NEW header instead of deleting.
    current = await admin_selected_city(admin_id)
    if code != current:
        await callback.answer("Город админки изменился — подтвердите заново.", show_alert=True)
        text, kb = await _settings_edit_screen(key, current)
        await callback.message.edit_text(text, parse_mode="HTML", reply_markup=kb)
        return

    await delete_setting(composed)  # idempotent — safe if already absent
    city_txt = await city_label(code)
    await callback.answer(f"Готово: {city_txt} — как везде", show_alert=True)
    text, kb = await _settings_edit_screen(key, current)
    await callback.message.edit_text(text, parse_mode="HTML", reply_markup=kb)


@router.callback_query(F.data.startswith("settings_photo:"))
async def settings_photo_start(callback: types.CallbackQuery, state: FSMContext):
    prefix = callback.data.split(":", 1)[1]
    prompts = {p: (label, prompt) for p, label, prompt in PHOTO_FIELDS}
    label, prompt = prompts.get(prefix, ("Фото", "Отправьте фото."))

    current = await get_setting(f"{prefix}_photo_file_id")
    text = f"{label}: {'✅ загружена' if current else '<i>не загружена</i>'}\n\n{prompt}"

    cancel_kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="❌ Отмена", callback_data="settings_cancel")],
    ])
    await callback.message.edit_text(text, parse_mode="HTML", reply_markup=cancel_kb)
    await state.set_state(EditSetting.waiting_for_photo)
    await state.update_data(photo_setting=prefix)
    await callback.answer()


@router.callback_query(F.data == "settings_cancel")
async def cancel_edit_setting_callback(callback: types.CallbackQuery, state: FSMContext):
    await state.clear()
    text = await render_settings_text(callback.from_user.id)
    await callback.message.edit_text(text, parse_mode="HTML", reply_markup=await build_settings_keyboard(callback.from_user.id))
    await callback.answer()


@router.callback_query(F.data == "admin_sync_sheet")
async def sync_sheet(callback: types.CallbackQuery):
    await callback.answer("🔄 Синхронизация...")
    await callback.message.edit_text("🔄 Получаю данные из таблицы...", parse_mode="HTML")

    try:
        headers = await active_sheet_headers()  # only enabled columns
        await ensure_sheet_header(headers)  # шапка таблицы, если её ещё нет
        existing_ids = await get_existing_sheet_ids()
        all_users = await get_all_users_dicts()

        missing = [u for u in all_users if u["telegram_id"] not in existing_ids]

        if not missing:
            await callback.message.edit_text(
                "✅ Таблица синхронизирована, пропущенных записей нет.",
                parse_mode="HTML",
                reply_markup=await admin_keyboard_for(callback.from_user.id),
            )
            return

        # Align each row to the active header order so columns match the sheet exactly.
        rows = [[_sheet_value_map(u).get(h, "-") for h in headers] for u in missing]
        count = await append_rows_to_sheet(rows)

        await callback.message.edit_text(
            f"✅ Синхронизация завершена!\n\n"
            f"Добавлено записей: <b>{count}</b>",
            parse_mode="HTML",
            reply_markup=await admin_keyboard_for(callback.from_user.id),
        )
    except Exception as e:
        logger.error(f"Sheet sync failed: {e}")
        await callback.message.edit_text(
            f"❌ Ошибка синхронизации:\n<code>{html_module.escape(str(e))}</code>",
            parse_mode="HTML",
            reply_markup=await admin_keyboard_for(callback.from_user.id),
        )


@router.callback_query(F.data == "admin_rebuild_sheet")
async def rebuild_sheet_confirm(callback: types.CallbackQuery):
    """Quick 260813-sdl: пересборка делает sheet.clear() и перезаписывает ВСЕ строки — то есть
    сносит любые ручные правки менеджеров на листе. До этого она запускалась одним тапом, без
    вопроса; соседняя destructive-кнопка «🧹 Убрать дубли» подтверждение имела всегда. Гейт
    зеркалит dedupe: сама работа переехала в admin_rebuild_sheet_go."""
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="♻️ Да, пересобрать", callback_data="admin_rebuild_sheet_go")],
        [InlineKeyboardButton(text="← Отмена", callback_data="admin_menu")],
    ])
    await callback.message.edit_text(
        "♻️ <b>Пересобрать таблицу?</b>\n\n"
        "Перезапишу на основной вкладке <b>шапку и все строки</b> из базы бота: колонки "
        "встанут в порядке анкеты, «Статус» получит выпадашку и цвета.\n\n"
        "⚠️ Лист очищается целиком и заполняется заново. <b>Любые ручные правки и заметки, "
        "которых нет в базе бота, пропадут безвозвратно.</b> Если менеджеры что-то дописывали "
        "прямо в таблице — сначала сохраните копию (Файл → Создать копию).",
        parse_mode="HTML",
        reply_markup=kb,
    )
    await callback.answer()


@router.callback_query(F.data == "admin_rebuild_sheet_go")
async def rebuild_sheet(callback: types.CallbackQuery):
    """Полная пересборка листа данных: перезаписать шапку + ВСЕ строки в текущем порядке
    колонок, применить выпадашку/цвета к «Статус». Выравнивает старые строки после смены
    порядка колонок (Таня п.1/п.5). Внимание: перезаписывает ручные правки на листе."""
    await callback.answer("♻️ Пересборка...")
    logger.info(f"admin={callback.from_user.id} action=rebuild_sheet start")
    await callback.message.edit_text("♻️ Пересобираю таблицу (перезапись всех строк)…", parse_mode="HTML")

    try:
        headers = await active_sheet_headers()  # only enabled columns
        all_users = await get_all_users_dicts()
        # UAT 17.08 (fast): the rebuild used to dump EVERY user into the main tab regardless of
        # city, so after one «Пересобрать» the main tab held СПб/Тюмень rows too while live
        # appends kept routing them to their own tabs -- the sheets drifted apart. Route each
        # row through the SAME resolver the live append uses (city_row_tab: default city / module
        # off -> None -> main tab; other city -> its named tab) and full-refresh every touched
        # city tab alongside the main one. Module off => city_row_tab is always None => byte-
        # identical to the old behaviour.
        main_rows: list[list] = []
        city_rows: dict[str, list[list]] = {}
        for u in all_users:
            row = [_sheet_value_map(u).get(h, "-") for h in headers]
            tab = await city_row_tab(u.get("event_city"), u.get("participant_type"))
            if tab is None:
                main_rows.append(row)
            else:
                city_rows.setdefault(tab, []).append(row)
        rows = main_rows
        count = await rebuild_main_sheet(headers, rows)
        city_synced: list[tuple[str, int]] = []
        if count >= 0:
            for tab, trows in city_rows.items():
                city_synced.append((tab, await sync_named_worksheet(tab, headers, trows)))
        if count == REFUSED_UNPINNED_TAB:
            await callback.message.edit_text(
                "⛔ Пересборка отключена: основная вкладка не задана.\n\n"
                "Без неё пересборка могла бы задеть не ту вкладку. Укажите вкладку в "
                "«⚙️ Настройки → 📄 Вкладки таблицы → 📄 Основная (регистрации)» — сработает "
                "сразу, без перезапуска. Вариант для разработчика — <code>GOOGLE_SHEET_TAB</code> "
                "в .env (тогда нужен перезапуск).",
                parse_mode="HTML",
                reply_markup=await admin_keyboard_for(callback.from_user.id),
            )
            return
        if count < 0:
            await callback.message.edit_text(
                "❌ Пересборка не выполнена (таблица не настроена или ошибка API). Смотри логи.",
                parse_mode="HTML",
                reply_markup=await admin_keyboard_for(callback.from_user.id),
            )
            return
        # CR-9: rebuild is the re-sync point — freeze the snapshot to the header just written
        # so subsequent registrations align to the rebuilt physical header.
        await set_sheet_schema(headers)
        city_line = ""
        if city_synced:
            parts = [f"{html_module.escape(t)}: <b>{n if n >= 0 else '❌'}</b>" for t, n in city_synced]
            city_line = "Городские вкладки: " + ", ".join(parts) + "\n"
        await callback.message.edit_text(
            f"✅ Таблица пересобрана!\n\n"
            f"Строк записано (основная): <b>{count}</b>\n"
            f"{city_line}"
            f"Колонки выстроены в порядке анкеты, «Статус» с выпадашкой и цветами.",
            parse_mode="HTML",
            reply_markup=await admin_keyboard_for(callback.from_user.id),
        )
    except Exception as e:
        logger.error(f"Sheet rebuild failed: {e}")
        await callback.message.edit_text(
            f"❌ Ошибка пересборки:\n<code>{html_module.escape(str(e))}</code>",
            parse_mode="HTML",
            reply_markup=await admin_keyboard_for(callback.from_user.id),
        )


@router.callback_query(F.data == "settings_back")
async def settings_back_to_admin(callback: types.CallbackQuery):
    await callback.message.edit_text(
        "👮‍♂️ <b>Панель администратора</b>",
        parse_mode="HTML",
        reply_markup=await admin_keyboard_for(callback.from_user.id),
    )
    await callback.answer()


def _parse_consent_list(raw: str) -> list[tuple[str, str]]:
    """consent_list ('Название|ключ' per line) → [(label, key)]."""
    items = []
    # ';' works as a separator too — see _consent_entries in registration.py: the
    # Telegram Enter=send trap can split multi-line input, so admins may enter all
    # consents on one line joined by ';'. Existing newline data still parses.
    for line in (raw or "").replace(";", "\n").strip().splitlines():
        line = line.strip()
        if not line or "|" not in line:
            continue
        label, key = line.split("|", 1)
        key = key.strip()
        if key:
            items.append((label.strip() or key, key))
    return items


@router.callback_query(F.data == "admin_consent_pdfs")
async def admin_consent_pdfs(callback: types.CallbackQuery):
    items = _parse_consent_list(await get_setting("consent_list") or "")
    if not items:
        await callback.answer()
        await callback.message.edit_text(
            "🧾 <b>PDF согласий</b>\n\n"
            "Здесь пусто, потому что ещё не задан список согласий.\n\n"
            "<b>Что сделать:</b>\n"
            "1. Зайди в «📋 Список согласий» и добавь согласия (каждое строкой "
            "<i>Название|ключ</i>).\n"
            "2. Вернись сюда — у каждого согласия появится кнопка для загрузки PDF.",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="📋 К списку согласий", callback_data="settings_edit:consent_list")],
                [InlineKeyboardButton(text="← Назад", callback_data="admin_settings")],
            ]),
        )
        return
    buttons = []
    for label, key in items:
        has_pdf = bool(await get_setting(f"consent_pdf_{key}"))
        mark = "✅" if has_pdf else "📎"
        buttons.append([InlineKeyboardButton(text=f"{mark} {label}", callback_data=f"consent_pdf_set:{key}")])
    buttons.append([InlineKeyboardButton(text="← Назад", callback_data="admin_settings")])
    await callback.message.edit_text(
        "🧾 <b>PDF согласий</b>\n\n"
        "Нажми на согласие и пришли PDF-файл — участник увидит его прикреплённым к этому согласию.\n\n"
        "✅ — PDF уже загружен · 📎 — ещё нет.",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons),
    )
    await callback.answer()


@router.callback_query(F.data.startswith("consent_pdf_set:"))
async def consent_pdf_set(callback: types.CallbackQuery, state: FSMContext):
    key = callback.data.split(":", 1)[1]
    cancel_kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="❌ Отмена", callback_data="settings_cancel")],
    ])
    await callback.message.edit_text(
        "📎 Пришли сюда <b>PDF-файл</b> этого согласия одним сообщением "
        "(перетащи файл или прикрепи через скрепку).\n\n"
        "Просто фото или ссылка не подойдут — нужен именно PDF-документ.",
        parse_mode="HTML",
        reply_markup=cancel_kb,
    )
    await state.set_state(EditSetting.waiting_for_file)
    await state.update_data(raw_file_key=f"consent_pdf_{key}")
    await callback.answer()


@router.message(StateFilter(EditSetting), Command("cancel"))
@router.message(StateFilter(EditSetting), F.text == "Отмена")
async def cancel_edit_setting(message: types.Message, state: FSMContext):
    await state.clear()
    text = await render_settings_text(message.from_user.id)
    await message.answer(text, parse_mode="HTML", reply_markup=await build_settings_keyboard(message.from_user.id))


@router.message(EditSetting.waiting_for_photo, F.photo)
async def settings_receive_photo(message: types.Message, state: FSMContext):
    data = await state.get_data()
    prefix = data.get("photo_setting", "program")

    file_id = message.photo[-1].file_id
    await set_setting(f"{prefix}_photo_file_id", file_id)

    if message.caption:
        await set_setting(f"{prefix}_caption", message.html_text)
    else:
        await delete_setting(f"{prefix}_caption")

    await state.clear()
    await message.answer("✅ Фото обновлено!")
    text = await render_settings_text(message.from_user.id)
    await message.answer(text, parse_mode="HTML", reply_markup=await build_settings_keyboard(message.from_user.id))


@router.message(EditSetting.waiting_for_photo)
async def settings_receive_photo_invalid(message: types.Message):
    await message.answer("Отправьте именно фото (не файлом).")


@router.message(EditSetting.waiting_for_file, F.photo)
async def settings_receive_file_photo(message: types.Message, state: FSMContext):
    data = await state.get_data()
    if data.get("raw_file_key"):
        await message.answer("Согласие принимается только PDF-документом, не фото.")
        return
    prefix = data.get("file_setting", "reg_bonus")

    file_id = message.photo[-1].file_id
    await set_setting(f"{prefix}_photo_file_id", file_id)
    await delete_setting(f"{prefix}_doc_file_id")

    if message.caption:
        await set_setting(f"{prefix}_caption", message.html_text)
    else:
        await delete_setting(f"{prefix}_caption")

    await state.clear()
    await message.answer("✅ Файл обновлён!")
    text = await render_settings_text(message.from_user.id)
    await message.answer(text, parse_mode="HTML", reply_markup=await build_settings_keyboard(message.from_user.id))


@router.message(EditSetting.waiting_for_file, F.document)
async def settings_receive_file_doc(message: types.Message, state: FSMContext):
    data = await state.get_data()

    # Consent PDF: store the document file_id directly into an arbitrary settings key.
    raw_key = data.get("raw_file_key")
    if raw_key:
        if (message.document.mime_type or "") != "application/pdf":
            await message.answer("Принимается только PDF-документ. Пришли PDF.")
            return
        await set_setting(raw_key, message.document.file_id)
        await state.clear()
        await message.answer("✅ PDF согласия сохранён!")
        text = await render_settings_text(message.from_user.id)
        await message.answer(text, parse_mode="HTML", reply_markup=await build_settings_keyboard(message.from_user.id))
        return

    prefix = data.get("file_setting", "reg_bonus")

    file_id = message.document.file_id
    await set_setting(f"{prefix}_doc_file_id", file_id)
    await delete_setting(f"{prefix}_photo_file_id")

    if message.caption:
        await set_setting(f"{prefix}_caption", message.html_text)
    else:
        await delete_setting(f"{prefix}_caption")

    await state.clear()
    await message.answer("✅ Файл обновлён!")
    text = await render_settings_text(message.from_user.id)
    await message.answer(text, parse_mode="HTML", reply_markup=await build_settings_keyboard(message.from_user.id))


@router.message(EditSetting.waiting_for_file)
async def settings_receive_file_invalid(message: types.Message):
    await message.answer("Отправьте фото или документ.")


HTML_SETTINGS = {"start_text", "start_text_registered", "start_text_returning", "reg_complete_text", "approve_text", "approve_text__party"}


def _base_setting_key(key: str) -> str:
    """Phase 09.2 (C, CITY-05): strips a `{key}__city__{code}` composite key down to the
    base registry key — used ONLY for the HTML_SETTINGS membership check in
    settings_edit_value, so per-city text saves get the same HTML parsing as the global
    save. `_SHEET_TAB_WRITE_MODE`/`_options` branches deliberately stay on the raw key
    (guarded by test_no_per_city_key_in_sheet_tab_write_mode_or_options_suffix)."""
    return key.split(PER_CITY_SEP)[0]


def _enum_human_label(key: str, value: str) -> str:
    """Human-readable alert text for a per-city enum toggle (CLAUDE.md: no raw values in
    admin-facing alerts)."""
    if key == "registration_mode":
        return {"short": "⚡ Краткая", "full": "📋 Полная"}.get(value, value)
    if value == "on":
        return "✅ Вкл"
    if value == "off":
        return "❌ Выкл"
    return value


# Quick 260815-3hw (Task 3): which Google Sheets tab-name keys the bot actually WRITES to, and
# HOW. "rewrite" = the sync path does ws.clear() + full rewrite (rebuild_main_sheet /
# sync_named_worksheet); "append" = only new rows are ever added (append_to_named_sheet), never
# a clear. preselect_tab (read-only — the bot never writes it) and the three
# city_tab_suffix__* keys (not full tab names, just suffixes) are deliberately ABSENT — the
# confirm-gate in settings_edit_value only fires for a key present in this dict.
_SHEET_TAB_WRITE_MODE = {
    "main_sheet_tab": "rewrite",
    "incomplete_sheet_tab": "rewrite",
    "game_matrix_tab": "rewrite",
    "game_history_tab": "rewrite",
    "short_sheet_tab": "append",
    "party_sheet_tab": "append",
}


async def _after_tab_setting_saved(key: str) -> None:
    """Called after EVERY save/clear of a _SHEET_TAB_WRITE_MODE key (plain save, gated
    save-after-confirm, and the "-" clear path) — resets the cached MAIN worksheet handle
    (services.sheets._sheet global) so a renamed main_sheet_tab takes effect on the very next
    write, no bot restart needed. Named-tab caches (short/party/game/incomplete) need no
    reset: they're keyed BY NAME (services.sheets._named_sheets), so a new name simply opens a
    new cache entry — the stale entry under the old name just goes unused, it isn't wrong."""
    if key == "main_sheet_tab":
        _reset_sheet_cache()


def _tab_confirm_text(key: str, value: str, rows: int) -> str:
    """Confirm-screen body for an EXISTING tab name — text differs by write mode (CLAUDE.md:
    a confirmation has to name the actual damage, and for an append-only tab nothing is
    actually lost)."""
    label = SETTINGS_SCHEMA.get(key, {}).get("label", key)
    safe_value = html_module.escape(value)
    mode = _SHEET_TAB_WRITE_MODE.get(key)
    if mode == "append":
        body = (
            f"Вкладка «{safe_value}» уже существует, в ней {rows} строк.\n\n"
            "Бот будет дописывать в неё строки заявок, к тому, что там уже есть — ничего не "
            "сотрётся."
        )
    else:
        body = (
            f"Вкладка «{safe_value}» уже существует, в ней {rows} строк.\n\n"
            "Бот будет перезаписывать её целиком при каждой синхронизации — <b>всё, что там "
            "сейчас есть, пропадёт.</b>"
        )
        if key == "main_sheet_tab":
            body += (
                "\n\nРегистрации будут дописываться в неё по одной; кнопка «♻️ Пересобрать "
                "таблицу» очистит её целиком и запишет заново."
            )
    return f"⚠️ <b>{html_module.escape(label)}</b>\n\n{body}"


def _tab_check_failed_warning(key: str) -> str:
    """Appended to the post-save confirmation text when tab_row_count() couldn't check the
    spreadsheet at all (Sheets down/unconfigured) — the value is saved regardless (a settings
    change must never depend on Sheets being reachable), but the manager needs to know the
    existing-tab check didn't run."""
    mode = _SHEET_TAB_WRITE_MODE.get(key)
    if mode == "append":
        tail = "если такая вкладка уже есть, бот будет дописывать в неё, ничего не потеряется."
    else:
        tail = "если такая вкладка уже есть, при следующей синхронизации она будет перезаписана."
    return f"\n\n⚠️ Значение сохранено, но проверить вкладку в Google-таблице не удалось — {tail}"


def _tab_confirm_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Да, сохранить это имя", callback_data="sheets_tab_confirm")],
        [InlineKeyboardButton(text="← Отмена", callback_data="sheets_tab_cancel")],
    ])


@router.message(EditSetting.waiting_for_value)
async def settings_edit_value(message: types.Message, state: FSMContext):
    data = await state.get_data()
    key = data["setting_key"]

    # Phase 09.2 (C, CITY-05): a per-city composite key (`{base}__city__{code}`) gets the
    # SAME HTML-parsing treatment as its base key — the check is against the base, not the
    # raw composite (which is never itself a HTML_SETTINGS member).
    if _base_setting_key(key) in HTML_SETTINGS:
        value = (message.html_text or message.text or "").strip()
    else:
        value = (message.text or "").strip()

    # Guard: a non-text message (sticker/photo/voice/forwarded media) or a whitespace-only
    # send yields value == "" here. Storing "" is never a meaningful value — the registry's
    # text branch would return "" instead of the default (so the settings screen shows
    # «по умолчанию» while a consumer actually resolves ""), and an empty Google Sheets tab
    # name breaks the allowlist read and every sync. Clearing a setting is the explicit "-"
    # sentinel, not an empty send. Reject and stay in the state so the admin can just retype.
    if not value:
        await message.answer(
            "Не понял значение — пришлите его <b>текстом</b> одним сообщением "
            "(например: <code>Реги бот</code>).\n\nЧтобы очистить настройку, отправьте «-».",
            parse_mode="HTML",
        )
        return

    # Quick 260815-3hw (Task 3): confirm-gate before silently overwriting an EXISTING Google
    # Sheets tab — only for keys the bot actually writes to (_SHEET_TAB_WRITE_MODE);
    # preselect_tab (read-only) and the city_tab_suffix__* keys never reach this branch, and
    # neither does clearing a value ("-") — there is nothing to protect when unsetting.
    tab_check_failed = False
    if key in _SHEET_TAB_WRITE_MODE and value and value != "-":
        probe = await tab_row_count(value)
        if probe is None:
            tab_check_failed = True
        elif probe[0]:
            _exists, rows = probe
            await state.update_data(pending_tab_key=key, pending_tab_value=value)
            await state.set_state(EditSetting.waiting_for_tab_confirm)
            await message.answer(
                _tab_confirm_text(key, value, rows),
                parse_mode="HTML",
                reply_markup=_tab_confirm_keyboard(),
            )
            return
        # probe == (False, 0): tab doesn't exist yet — fall through to the normal silent save.

    warning = ""
    if value == "-":
        await delete_setting(key)
    else:
        await set_setting(key, value)
        # Phase 4 (D-05): saving event_type applies the module-toggle preset.
        if key == "event_type":
            await _apply_event_type_preset(value.strip().lower())
        # WR-02: "Отмена"/"Другое"/"Пропустить" are reserved control words in the registration
        # flow — an option list whose line equals one becomes unreachable (it triggers cancel /
        # "type your own" instead of being recorded). Warn the admin (the value is still saved).
        if key.endswith("_options"):
            reserved = {"отмена", "другое", "пропустить"}
            clashes = sorted({
                ln.strip() for ln in value.splitlines() if ln.strip().lower() in reserved
            })
            if clashes:
                warning = (
                    "\n\n⚠️ Внимание: варианты "
                    + ", ".join(f"«{html_module.escape(c)}»" for c in clashes)
                    + " совпадают со служебными словами бота и будут недоступны для выбора. "
                    "Переименуйте их."
                )
        if tab_check_failed:
            warning += _tab_check_failed_warning(key)

    if key in _SHEET_TAB_WRITE_MODE:
        await _after_tab_setting_saved(key)

    per_city_base = data.get("per_city_base")
    await state.clear()
    if per_city_base:
        # Phase 09.3 (06, CITY-09): a per-city save/clear returns to the SAME header-aware
        # editor screen, not the general settings landing (RESEARCH Pattern 3 lineage — reuse
        # the FSM, but keep the caller on the screen they actually came from).
        header_code = await admin_selected_city(message.from_user.id)
        text, kb = await _settings_edit_screen(per_city_base, header_code)
        await message.answer(text + warning, parse_mode="HTML", reply_markup=kb)
        return
    text = await render_settings_text(message.from_user.id)
    await message.answer(text + warning, parse_mode="HTML", reply_markup=await build_settings_keyboard(message.from_user.id))


@router.callback_query(F.data == "sheets_tab_confirm")
async def sheets_tab_confirm_go(callback: types.CallbackQuery, state: FSMContext):
    """Confirmed overwrite of an existing tab name — saves the pending value (mirrors
    gtconfirm/gtcancel, 09-02: no StateFilter, FSM data is read directly). Returns to the
    «📄 Вкладки таблицы» screen, not the general settings landing — the manager came from there."""
    data = await state.get_data()
    key = data.get("pending_tab_key")
    value = data.get("pending_tab_value")
    await state.clear()
    if key and value is not None:
        await set_setting(key, value)
        if key in _SHEET_TAB_WRITE_MODE:
            await _after_tab_setting_saved(key)
    text = await render_settings_group_text("sheets", callback.from_user.id)
    await callback.message.edit_text(text, parse_mode="HTML", reply_markup=await build_settings_group_keyboard("sheets", callback.from_user.id))
    await callback.answer("✅ Сохранено")


@router.callback_query(F.data == "sheets_tab_cancel")
async def sheets_tab_cancel_go(callback: types.CallbackQuery, state: FSMContext):
    """Cancelled overwrite — nothing saved, prior value untouched."""
    await state.clear()
    text = await render_settings_group_text("sheets", callback.from_user.id)
    await callback.message.edit_text(text, parse_mode="HTML", reply_markup=await build_settings_group_keyboard("sheets", callback.from_user.id))
    await callback.answer("Отменено")


@router.callback_query(F.data == "admin_export_csv")
async def show_admin_export(callback: types.CallbackQuery):
    # Phase 07.2 (CITY-02): CSV export is SCOPED to the admin's selected city — the opposite
    # of the (intentionally unscoped) stats screen. Same resolver every other city-scoped
    # surface uses (_admin_city_view), so module-off collapses to the exact pre-Phase-07.2
    # unfiltered export, byte-identical filename and caption.
    # WR-05: ONE read — the filename must never name a different city than the caption.
    admin_id = callback.from_user.id
    scope, label = await _admin_city_view(admin_id)
    headers, rows = await export_users_csv(city_scope=scope)
    output = io.StringIO()
    writer = csv.writer(output, delimiter=';', quotechar='"', quoting=csv.QUOTE_MINIMAL)
    writer.writerow(headers)
    writer.writerows(rows)
    file_bytes = output.getvalue().encode('utf-8-sig')
    # Filename stays keyed on `scope` (there is no single city code in ALL_CITIES mode — scope
    # is None there too, same as module-off).
    filename = "users.csv" if scope is None else f"users_{scope[0]}.csv"
    # CR-01: the label is an admin-editable free-text setting (`city_label__{code}` in
    # bot_settings) and the bot runs with DefaultBotProperties(parse_mode=HTML), so a caption
    # sent without an explicit parse_mode is parsed as HTML. Escape it exactly like
    # _render_application_card / _render_receipt_card / render_stats_text already do.
    # Phase 09.3 (09.3-02, CITY-08): switched from `scope is None` to `label is None` — module
    # off still has no label (byte-identical caption); ALL_CITIES mode now has a non-None
    # label (ALL_CITIES_LABEL) even though scope is also None, so the caption names the mode.
    caption = (
        "База данных пользователей" if label is None
        else f"База данных пользователей — {html_module.escape(str(label))}"
    )
    document = BufferedInputFile(file_bytes, filename=filename)
    await callback.message.answer_document(document, caption=caption)
    await callback.answer()


@router.callback_query(F.data == "admin_export_incomplete")
async def export_incomplete(callback: types.CallbackQuery):
    await callback.answer("📝 Выгружаю…")
    # Phase 07.1 (CITY-04): incomplete_city_batches() is the SINGLE shared helper for both the
    # manual export and services/scheduler.py:sync_incomplete_sheet_job — headers are computed
    # once inside it (Google Sheets quota) and both callers MUST stay on this helper (WR-01
    # parity), otherwise the 2h auto-sync can silently narrow a tab back down.
    # Phase 07.2 (CITY-02): deliberately NOT scoped to the admin's selected city, unlike
    # show_admin_export above. incomplete_city_batches() writes ALL city tabs in one pass
    # (sync_named_worksheet = clear+rewrite per tab); narrowing to one city here would leave
    # every OTHER city's tab holding stale data after this run. This is already a per-city
    # surface (Phase 07.1, WR-01 parity) — just not filtered by the admin's current selection.
    batches = await incomplete_city_batches()
    total_rows = 0
    written_lines = []
    any_negative = False
    for tab, headers, sheet_rows in batches:
        written = await sync_named_worksheet(tab, headers, sheet_rows)
        total_rows += len(sheet_rows)
        if written < 0:
            any_negative = True
        else:
            written_lines.append(f"«{tab}» — {written}")

    # Aggregate: on which question do dropouts stall most? (works even if the sheet write failed)
    stats = await get_dropout_step_stats()
    total = sum(c for _s, c in stats) or 1
    top = "\n".join(
        f"• {dropout_step_label(step)} — <b>{cnt}</b> ({round(cnt * 100 / total)}%)"
        for step, cnt in stats[:8]
    )
    summary = f"\n\n📊 <b>Где отваливаются:</b>\n{top}" if stats else ""

    if any_negative:
        await callback.message.answer(
            "⚠️ Не удалось записать в таблицу (проверь доступ к Google Sheets). "
            f"Незавершённых регистраций в базе: <b>{total_rows}</b>.{summary}",
            parse_mode="HTML",
        )
        return
    await callback.message.answer(
        f"✅ Обновлено: {', '.join(written_lines)}.{summary}",
        parse_mode="HTML",
    )


# ── Phase 07.1 (CITY-04) / Phase 14 (14-07, CITY-07): «🏙 Города» admin screen ───────────────
# Phase 14-07 closes CITY-07 fully: add/rename/tab-base/default/delete are now all in-bot, no
# `.env` restart round-trip. The city list itself lives in the `cities` table (cities.py cache,
# `all_cities()`); a manager never types or sees a raw city CODE — only the human-facing label
# and the deep-link it produces (T-14-34).


async def _cities_screen_allowed(user_id: int) -> bool:
    """T-14-32: the registry is shared across every city, so only a manager NOT bound to a
    single city (a bootstrap superadmin, or a manager whose `staff.city` binding is empty) may
    write to it. Called from `show_admin_cities` AND every writing handler below — the gate
    belongs in the handler, not only the keyboard, because an old inline keyboard from before a
    binding existed lives forever in a chat."""
    if user_id in config.ADMIN_IDS:
        return True
    bound = await get_staff_city(user_id)
    return not bound


async def _deny_cities_screen(callback: types.CallbackQuery) -> None:
    bound = await get_staff_city(callback.from_user.id)
    await callback.answer(
        f"Этот экран — для менеджера всех городов. Ваш город: {await city_label(bound)}",
        show_alert=True,
    )


async def _deny_cities_screen_message(message: types.Message) -> None:
    bound = await get_staff_city(message.from_user.id)
    await message.answer(
        f"Этот экран — для менеджера всех городов. Ваш город: {await city_label(bound)}"
    )


async def render_cities_text() -> str:
    module_on = await cities_module_on()
    module_status = "✅ Вкл" if module_on else "❌ Выкл"
    cities = all_cities()
    default_code = default_city_code()
    lines = [
        "🏙 <b>Города мероприятия</b>",
        "",
        f"Модуль выбора города: {module_status}",
    ]
    if not module_on:
        lines.append(
            "Пока выключен — экран выбора города делегатам не показывается, "
            "все заявки идут в основной лист."
        )
    lines.append("")
    any_hidden_delete = False
    for c in cities:
        code = c["code"]
        enabled = await is_city_enabled(code)
        label = await city_label(code)
        tab = await city_row_tab(code, None) or "основной лист"
        icon = "✅" if enabled else "⛔"
        star = " ⭐" if code == default_code else ""
        lines.append(
            f"{icon} {html_module.escape(label)}{star} — "
            f"<code>?start=city_{html_module.escape(code)}</code> → {html_module.escape(tab)}"
        )
        if await count_users_by_city(code) > 0 or await count_tasks_by_city(code) > 0:
            any_hidden_delete = True
    lines.append("")
    lines.append("⭐ — город по умолчанию: в него попадают заявки без выбранного города.")
    if any_hidden_delete:
        lines.append(
            "🗑 У городов, где уже есть делегаты или задания, удаления нет — такой город можно "
            "только выключить (⛔): он исчезнет из выбора, а собранные заявки останутся на месте."
        )
    return "\n".join(lines)


async def build_cities_keyboard() -> InlineKeyboardMarkup:
    module_on = await cities_module_on()
    master_text = ("🏙 Выбор города: ✅ Вкл → ❌ Выкл" if module_on
                   else "🏙 Выбор города: ❌ Выкл → ✅ Вкл")
    buttons = [[InlineKeyboardButton(text=master_text, callback_data="toggle_event_city_enabled")]]
    cities = all_cities()
    default_code = default_city_code()
    for c in cities:
        code = c["code"]
        enabled = await is_city_enabled(code)
        label = await city_label(code)
        icon = "✅" if enabled else "⛔"
        row1 = [InlineKeyboardButton(text=f"{icon} {label}", callback_data=f"city_toggle:{code}")]
        if code != default_code:
            row1.append(InlineKeyboardButton(text="⭐", callback_data=f"city_default:{code}"))
        buttons.append(row1)
        row2 = [
            InlineKeyboardButton(text="✏️ Переименовать", callback_data=f"city_rename:{code}"),
            InlineKeyboardButton(text="📄 База вкладки", callback_data=f"city_tab:{code}"),
        ]
        if await count_users_by_city(code) == 0 and await count_tasks_by_city(code) == 0:
            row2.append(InlineKeyboardButton(text="🗑", callback_data=f"city_del:{code}"))
        buttons.append(row2)
    buttons.append([InlineKeyboardButton(text="➕ Добавить город", callback_data="city_add")])
    buttons.append([InlineKeyboardButton(text="◀️ Назад", callback_data="admin_menu")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


@router.callback_query(F.data == "admin_cities")
async def show_admin_cities(callback: types.CallbackQuery):
    if not await _cities_screen_allowed(callback.from_user.id):
        await _deny_cities_screen(callback)
        return
    text = await render_cities_text()
    await callback.message.edit_text(text, parse_mode="HTML", reply_markup=await build_cities_keyboard())
    await callback.answer()


@router.callback_query(F.data == "toggle_event_city_enabled")
async def toggle_event_city_enabled(callback: types.CallbackQuery):
    current = await get_setting_typed("event_city_enabled")
    new_val = "off" if current == "on" else "on"
    await set_setting("event_city_enabled", new_val)
    label = "✅ Вкл" if new_val == "on" else "❌ Выкл"
    await callback.answer(f"Выбор города: {label}", show_alert=True)

    text = await render_cities_text()
    await callback.message.edit_text(text, parse_mode="HTML", reply_markup=await build_cities_keyboard())


@router.callback_query(F.data.startswith("city_toggle:"))
async def city_toggle(callback: types.CallbackQuery):
    if not await _cities_screen_allowed(callback.from_user.id):
        await _deny_cities_screen(callback)
        return
    code = callback.data.split(":", 1)[1]
    if code not in {c["code"] for c in all_cities()}:
        await callback.answer("Неизвестный город", show_alert=True)
        return

    current = await is_city_enabled(code)
    new_val = 0 if current else 1
    await update_city(code, enabled=new_val)
    # Легаси-ключ (Phase 07.1) имеет приоритет в is_city_enabled — если его не убрать,
    # тумблер перестанет менять фактическое поведение после первого же переключения.
    await delete_setting(f"city_enabled__{code}")
    await reload_cities()
    label_text = "✅ Вкл" if new_val else "⛔ Выкл"
    await callback.answer(f"{await city_label(code)}: {label_text}", show_alert=True)

    text = await render_cities_text()
    await callback.message.edit_text(text, parse_mode="HTML", reply_markup=await build_cities_keyboard())


@router.callback_query(F.data.startswith("city_default:"))
async def city_default(callback: types.CallbackQuery):
    if not await _cities_screen_allowed(callback.from_user.id):
        await _deny_cities_screen(callback)
        return
    code = callback.data.split(":", 1)[1]
    cities = all_cities()
    if code not in {c["code"] for c in cities}:
        await callback.answer("Неизвестный город", show_alert=True)
        return
    await update_city(code, sort_order=0)
    others = [c["code"] for c in cities if c["code"] != code]
    for i, other_code in enumerate(others, start=1):
        await update_city(other_code, sort_order=i)
    await reload_cities()
    await callback.answer(f"Город по умолчанию: {await city_label(code)}", show_alert=True)

    text = await render_cities_text()
    await callback.message.edit_text(text, parse_mode="HTML", reply_markup=await build_cities_keyboard())


# ── Phase 14 (14-07, CITY-07): «➕ Добавить город» wizard + rename / tab-base edit ───────────
#
# The city CODE is NEVER human input — `city_add`'s add_tab step is the only call site of
# `make_city_code()` in this file. A manager only ever types a LABEL and a tab-base string.

def _dash_or_text(raw: str | None) -> str:
    """«—»/empty input -> "" (same tab as the main city); anything else -> `.strip()`ped
    text. Shared by the add-wizard's add_tab step and the edit_tab step so both accept the
    same «Enter/«—»» escape hatch the plan's copy promises."""
    text = (raw or "").strip()
    if not text or text == "—":
        return ""
    return text


@router.callback_query(F.data == "city_add")
async def city_add(callback: types.CallbackQuery, state: FSMContext):
    if not await _cities_screen_allowed(callback.from_user.id):
        await _deny_cities_screen(callback)
        return
    await state.set_data({})
    await callback.message.answer(
        "Как называется город? Напишите подпись, которую увидят делегаты — например: "
        "Казань, 14 ноября.",
        reply_markup=get_cancel_kb(),
    )
    await state.set_state(CityForm.add_label)
    await callback.answer()


# Cancel-mid-wizard, registered BEFORE the per-step handlers below (admin.router: first match
# wins) — same shape as cancel_game_task_create/cancel_edit_setting.
@router.message(StateFilter(CityForm), Command("cancel"))
@router.message(StateFilter(CityForm), F.text == "Отмена")
async def cancel_city_form(message: types.Message, state: FSMContext):
    await state.set_state(None)
    await message.answer("Отменено.", reply_markup=ReplyKeyboardRemove())
    text = await render_cities_text()
    await message.answer(text, parse_mode="HTML", reply_markup=await build_cities_keyboard())


@router.message(CityForm.add_label)
async def city_add_label_step(message: types.Message, state: FSMContext):
    if not await _cities_screen_allowed(message.from_user.id):
        await _deny_cities_screen_message(message)
        return
    label = (message.text or "").strip()
    if not label:
        await message.answer("Подпись не может быть пустой. Как называется город?")
        return
    await state.update_data(city_new_label=label)
    await message.answer(
        "На какую вкладку таблицы писать заявки этого города? Напишите базу имени вкладки — "
        "например: Казань. Если писать в ту же вкладку, что и основной город, пришлите «—» "
        "(или Enter).",
        reply_markup=get_cancel_kb(),
    )
    await state.set_state(CityForm.add_tab)


async def _materialize_new_city_tabs() -> None:
    """T-14-38: Sheets failure must never block the screen — this runs fire-and-forget
    (`_spawn`) with its own try/except, mirroring main.py's startup materialization. Local
    import only: `main.py` imports `handlers.*`, so a module-level `from main import ...`
    here would create an import cycle."""
    try:
        from main import _maybe_ensure_city_sheet_headers
        await _maybe_ensure_city_sheet_headers()
    except Exception as e:
        logger.warning("Не удалось материализовать вкладки нового города: %s", e)


@router.message(CityForm.add_tab)
async def city_add_tab_step(message: types.Message, state: FSMContext):
    if not await _cities_screen_allowed(message.from_user.id):
        await _deny_cities_screen_message(message)
        return
    data = await state.get_data()
    label = data.get("city_new_label", "")
    tab_base = _dash_or_text(message.text)
    existing = {c["code"] for c in all_cities()}
    code = make_city_code(label, existing=existing)
    sort_order = max((c.get("sort_order") or 0) for c in all_cities()) + 1 if existing else 1
    await insert_city(code, label, tab_base, sort_order, enabled=1)
    await reload_cities()
    _spawn(_materialize_new_city_tabs())

    await state.set_state(None)
    await message.answer(f"✅ Город добавлен: {label}", reply_markup=ReplyKeyboardRemove())
    text = await render_cities_text()
    await message.answer(text, parse_mode="HTML", reply_markup=await build_cities_keyboard())


@router.callback_query(F.data.startswith("city_rename:"))
async def city_rename_start(callback: types.CallbackQuery, state: FSMContext):
    if not await _cities_screen_allowed(callback.from_user.id):
        await _deny_cities_screen(callback)
        return
    code = callback.data.split(":", 1)[1]
    if code not in {c["code"] for c in all_cities()}:
        await callback.answer("Неизвестный город", show_alert=True)
        return
    await state.set_data({"city_code": code})
    label = await city_label(code)
    await callback.message.answer(
        f"Как теперь называть город «{label}»? Напишите новую подпись.",
        reply_markup=get_cancel_kb(),
    )
    await state.set_state(CityForm.edit_label)
    await callback.answer()


@router.callback_query(F.data.startswith("city_tab:"))
async def city_tab_start(callback: types.CallbackQuery, state: FSMContext):
    if not await _cities_screen_allowed(callback.from_user.id):
        await _deny_cities_screen(callback)
        return
    code = callback.data.split(":", 1)[1]
    if code not in {c["code"] for c in all_cities()}:
        await callback.answer("Неизвестный город", show_alert=True)
        return
    await state.set_data({"city_code": code})
    label = await city_label(code)
    await callback.message.answer(
        f"На какую вкладку писать заявки города «{label}»? Напишите базу имени вкладки, или "
        "«—» — писать в ту же вкладку, что и основной город.\n\n"
        "⚠️ Уже собранные заявки останутся в старой вкладке — бот переносит только новые.",
        reply_markup=get_cancel_kb(),
    )
    await state.set_state(CityForm.edit_tab)
    await callback.answer()


@router.message(CityForm.edit_label)
async def city_edit_label_step(message: types.Message, state: FSMContext):
    if not await _cities_screen_allowed(message.from_user.id):
        await _deny_cities_screen_message(message)
        return
    data = await state.get_data()
    code = data.get("city_code")
    label = (message.text or "").strip()
    if not label:
        await message.answer("Подпись не может быть пустой. Как теперь называть город?")
        return
    await update_city(code, label=label)
    await delete_setting(f"city_label__{code}")  # легаси-override иначе продолжит перекрывать колонку
    await reload_cities()

    await state.set_state(None)
    await message.answer(f"✅ Подпись обновлена: {label}", reply_markup=ReplyKeyboardRemove())
    text = await render_cities_text()
    await message.answer(text, parse_mode="HTML", reply_markup=await build_cities_keyboard())


@router.message(CityForm.edit_tab)
async def city_edit_tab_step(message: types.Message, state: FSMContext):
    if not await _cities_screen_allowed(message.from_user.id):
        await _deny_cities_screen_message(message)
        return
    data = await state.get_data()
    code = data.get("city_code")
    tab_base = _dash_or_text(message.text)
    await update_city(code, tab_base=tab_base)
    await delete_setting(f"city_tab__{code}")  # легаси-override иначе продолжит перекрывать колонку
    await reload_cities()

    await state.set_state(None)
    await message.answer("✅ База вкладки обновлена.", reply_markup=ReplyKeyboardRemove())
    text = await render_cities_text()
    await message.answer(text, parse_mode="HTML", reply_markup=await build_cities_keyboard())


# ── Phase 14 (14-07, CITY-07): удаление города — три независимые серверные проверки (T-14-35):
# кнопка в клавиатуре (build_cities_keyboard), это подтверждение, и исполнение (city_delete_go).

@router.callback_query(F.data.startswith("city_del:"))
async def city_delete_confirm(callback: types.CallbackQuery):
    if not await _cities_screen_allowed(callback.from_user.id):
        await _deny_cities_screen(callback)
        return
    code = callback.data.split(":", 1)[1]
    if code not in {c["code"] for c in all_cities()}:
        await callback.answer("Неизвестный город", show_alert=True)
        return
    users_n = await count_users_by_city(code)
    tasks_n = await count_tasks_by_city(code)
    if users_n > 0 or tasks_n > 0:
        await callback.answer(
            f"На этом городе уже есть делегаты ({users_n}) или задания ({tasks_n}) — его можно "
            "только выключить (⛔), тогда он пропадёт из выбора, а собранные заявки останутся "
            "на месте",
            show_alert=True,
        )
        text = await render_cities_text()
        await callback.message.edit_text(text, parse_mode="HTML", reply_markup=await build_cities_keyboard())
        return
    if code == default_city_code():
        await callback.answer(
            "Это город по умолчанию: в него попадают заявки без выбранного города. Сначала "
            "назначьте другой город ⭐, потом удаляйте",
            show_alert=True,
        )
        return
    label = await city_label(code)
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🗑 Да, удалить", callback_data=f"city_del_go:{code}")],
        [InlineKeyboardButton(text="← Отмена", callback_data="admin_cities")],
    ])
    await callback.message.edit_text(
        f"🗑 <b>Удалить город «{html_module.escape(label)}»?</b>\n\n"
        "Город пропадёт из выбора у делегатов и из фильтров рассылок; его ссылка-приглашение "
        "перестанет работать. Вкладка таблицы и её строки НЕ удаляются. Вернуть можно только "
        "заведя город заново.",
        parse_mode="HTML",
        reply_markup=kb,
    )
    await callback.answer()


@router.callback_query(F.data.startswith("city_del_go:"))
async def city_delete_go(callback: types.CallbackQuery):
    if not await _cities_screen_allowed(callback.from_user.id):
        await _deny_cities_screen(callback)
        return
    code = callback.data.split(":", 1)[1]
    users_n = await count_users_by_city(code)
    tasks_n = await count_tasks_by_city(code)
    if users_n > 0 or tasks_n > 0:
        await callback.answer(
            "На этом городе уже есть делегаты/задания — его можно только выключить (⛔)",
            show_alert=True,
        )
        text = await render_cities_text()
        await callback.message.edit_text(text, parse_mode="HTML", reply_markup=await build_cities_keyboard())
        return
    if code == default_city_code():
        await callback.answer(
            "Это город по умолчанию — сначала назначьте другой город ⭐, потом удаляйте",
            show_alert=True,
        )
        return
    # Никаких каскадных удалений: только строка реестра. Легаси-ключи city_*__{code} в
    # bot_settings НЕ трогаются — заведение города с тем же кодом заново подхватит их.
    if await delete_city_row(code):
        await reload_cities()
        await callback.answer("Город удалён", show_alert=True)
    else:
        await callback.answer("Город уже удалён", show_alert=True)
    text = await render_cities_text()
    await callback.message.edit_text(text, parse_mode="HTML", reply_markup=await build_cities_keyboard())


# ── Phase 07.3 (02, RET-01): «🔄 Новый сезон» wizard ──────────────────────────────────────────
# T-073-02-01 (Elevation of Privilege): the `settings` capability entry (admin_caps.py) gets a
# holder INTO this screen, but every single handler below re-checks `config.ADMIN_IDS` itself —
# a stale inline keyboard rendered before someone's rights changed lives in the chat forever
# (same posture as roles_city_start/roles_city_pick). T-073-02-02/03 (Tampering/Repudiation):
# a two-tap numbers confirm PLUS a typed passphrase (the old season's exact name, or the literal
# "НОВЫЙ СЕЗОН" if there was none) gate the actual bulk UPDATE, and the action is logged with
# the admin's id before the reply is sent.

@router.callback_query(F.data == "admin_season_reset")
async def season_reset_start(callback: types.CallbackQuery, state: FSMContext):
    # Positive-form idiom (byte-identical to roles_city_start/admin_city_switch) — D-01/T-08-18's
    # structural gate forbids a bare "not in config.ADMIN_IDS" anywhere in this file.
    is_superadmin = callback.from_user.id in config.ADMIN_IDS
    if not is_superadmin:
        await callback.answer("Новый сезон может начать только суперадмин.", show_alert=True)
        return
    await state.set_data({})
    old = (await get_setting("event_season") or "").strip()
    await state.update_data(season_old=old)
    old_label = html_module.escape(old) if old else "не задан"
    await callback.message.answer(
        f"🔄 <b>Новый сезон</b>\n\nСейчас сезон: <b>{old_label}</b>.\n\n"
        "Напиши название нового сезона — им будут помечаться все новые регистрации.\n"
        "Например: YL'26",
        parse_mode="HTML",
        reply_markup=get_cancel_kb(),
    )
    await state.set_state(SeasonReset.naming)
    await callback.answer()


# Cancel-mid-wizard, registered BEFORE the per-step handlers below (admin.router: first match
# wins) — same shape as cancel_city_form/cancel_game_task_create.
@router.message(StateFilter(SeasonReset), Command("cancel"))
@router.message(StateFilter(SeasonReset), F.text == "Отмена")
async def cancel_season_reset(message: types.Message, state: FSMContext):
    await state.set_state(None)
    await message.answer("Отменено. Сезон не менялся.", reply_markup=ReplyKeyboardRemove())


@router.message(SeasonReset.naming)
async def season_reset_name_step(message: types.Message, state: FSMContext):
    # T-073-02-01: the gate belongs in the handler, not only the button that opened it — a
    # message can arrive at any moment after the wizard was entered.
    is_superadmin = message.from_user.id in config.ADMIN_IDS
    if not is_superadmin:
        await message.answer("Новый сезон может начать только суперадмин.")
        return
    new = (message.text or "").strip()
    if not new:
        await message.answer("Название сезона не может быть пустым. Напиши, например: YL'26")
        return
    if len(new) > 64:
        await message.answer("Слишком длинно. Уложись в 64 символа.")
        return
    await state.update_data(season_new=new)
    data = await state.get_data()
    old = data.get("season_old") or ""
    n = await count_current_season_users(old or None)
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="➡️ Продолжить", callback_data="season_reset_go")],
        [InlineKeyboardButton(text="← Отмена", callback_data="settings_group:event")],
    ])
    # State stays SeasonReset.naming — the next step is a tap (season_reset_go), not text; a
    # repeated text message here just re-renders this same screen with the new name.
    await message.answer(
        f"🔄 <b>Начать сезон «{html_module.escape(new)}»?</b>\n\n"
        f"• {n} делегатов текущего сезона станут «прошлыми»\n"
        "• статусы, монеты и чеки не трогаем, база не чистится\n"
        "• они смогут обновить анкету по /start\n\n"
        "Продолжить?",
        parse_mode="HTML",
        reply_markup=kb,
    )


@router.callback_query(F.data == "season_reset_go")
async def season_reset_go(callback: types.CallbackQuery, state: FSMContext):
    # T-073-02-01: re-checked again, T-073-02-05: re-checked against a possibly-stale screen.
    is_superadmin = callback.from_user.id in config.ADMIN_IDS
    if not is_superadmin:
        await callback.answer("Новый сезон может начать только суперадмин.", show_alert=True)
        return
    data = await state.get_data()
    new = data.get("season_new")
    if not new:
        await callback.answer("Экран устарел, начни заново", show_alert=True)
        await state.set_state(None)
        return
    old = data.get("season_old") or ""
    phrase = old if old else "НОВЫЙ СЕЗОН"
    await state.update_data(season_phrase=phrase)
    await callback.message.answer(
        f"Чтобы подтвердить, напиши: <code>{html_module.escape(phrase)}</code>",
        parse_mode="HTML",
        reply_markup=get_cancel_kb(),
    )
    await state.set_state(SeasonReset.passphrase)
    await callback.answer()


@router.message(SeasonReset.passphrase)
async def season_reset_passphrase_step(message: types.Message, state: FSMContext):
    # T-073-02-01: re-checked a third time — this is the step that actually executes the write.
    is_superadmin = message.from_user.id in config.ADMIN_IDS
    if not is_superadmin:
        await message.answer("Новый сезон может начать только суперадмин.")
        return
    data = await state.get_data()
    phrase = (data.get("season_phrase") or "").strip()
    typed = (message.text or "").strip()
    if typed != phrase:
        await message.answer("Фраза не совпала, ничего не сделано.")
        await state.set_state(None)
        return
    old = data.get("season_old") or ""
    new = data.get("season_new") or ""
    # Order matters (T-073-02-02): mark old-season rows FIRST, while event_season still reads
    # as the old value, then flip event_season last — makes the switch atomic from a delegate's
    # point of view.
    affected = await mark_season_ended(old or None)
    await set_setting("event_season", new)
    logger.warning(
        f"SEASON RESET by admin {message.from_user.id}: '{old}' -> '{new}', marked {affected} users"
    )
    await message.answer(
        f"✅ Новый сезон: <b>{html_module.escape(new)}</b>\nПрошлыми отмечены: {affected}\n\n"
        "Статусы, монеты и чеки не тронуты.",
        parse_mode="HTML",
        reply_markup=ReplyKeyboardRemove(),
    )
    await state.set_state(None)


# ── Phase 07.3 (06, RET-04): «📥 Импорт прошлого события» wizard ─────────────────────────────
# T-073-06-01 (Tampering): the uploaded file is opened read-only, in ITS OWN sqlite3 connection
# — never config.DB_PATH, never the live DB's async driver. T-073-06-03 (DoS): the 20 MB guard runs BEFORE
# bot.download; the temp file is always removed in `finally` (T-073-06-05, Information
# Disclosure — nothing from a past event's personal data touches disk after this handler
# returns). T-073-06-02 (column-name injection) is mitigated inside
# database.db.bulk_insert_users_if_absent itself, not here.

_IMPORT_MAX_BYTES = 20 * 1024 * 1024


@router.callback_query(F.data == "admin_season_import")
async def season_import_start(callback: types.CallbackQuery, state: FSMContext):
    await state.set_data({})
    await callback.message.answer(
        "📥 <b>Импорт прошлого события</b>\n\n"
        "Пришли файл базы старого бота — я прочитаю из него делегатов и добавлю тех, кого "
        "ещё нет.\n\n"
        "Монеты, оплаты и рефералы не переносятся, существующие записи не меняются. Файл "
        "после импорта не храню.\n\n"
        "Размер — до 20 МБ.",
        parse_mode="HTML",
        reply_markup=get_cancel_kb(),
    )
    await state.set_state(SeasonImport.waiting_file)
    await callback.answer()


# Cancel-mid-wizard, registered BEFORE the per-step handlers below (admin.router: first match
# wins) — same shape as cancel_city_form/cancel_season_reset.
@router.message(StateFilter(SeasonImport), Command("cancel"))
@router.message(StateFilter(SeasonImport), F.text == "Отмена")
async def cancel_season_import(message: types.Message, state: FSMContext):
    await state.set_state(None)
    await message.answer("Отменено. Ничего не импортировано.", reply_markup=ReplyKeyboardRemove())


@router.message(SeasonImport.waiting_file, F.document)
async def season_import_file_step(message: types.Message, state: FSMContext, bot: Bot):
    if (message.document.file_size or 0) > _IMPORT_MAX_BYTES:
        await message.answer("Файл больше 20 МБ — столько я принять не могу.")
        return

    tmp_path = None
    con = None
    try:
        buf = await bot.download(message.document.file_id)
        buf.seek(0)
        content = buf.read()

        with tempfile.NamedTemporaryFile(delete=False, suffix=".db") as tmp:
            tmp.write(content)
            tmp_path = tmp.name

        try:
            # T-073-06-01: read-only, own connection — physically cannot write to the file.
            con = sqlite3.connect(f"file:{tmp_path}?mode=ro", uri=True)
            con.row_factory = sqlite3.Row
            table_check = con.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='users'"
            ).fetchone()
            if not table_check:
                await message.answer("В этом файле нет таблицы делегатов. Похоже, это база другого приложения.")
                return
            raw_rows = con.execute("SELECT * FROM users").fetchall()
        except sqlite3.DatabaseError:
            await message.answer(
                "Это не похоже на базу бота: не удалось её прочитать. Пришли файл базы "
                "старого бота (обычно называется forum.db)."
            )
            return

        rows = []
        for r in raw_rows:
            d = dict(r)
            try:
                tg_id = int(d.get("telegram_id"))
            except (TypeError, ValueError):
                continue
            if not tg_id:
                continue
            d["telegram_id"] = tg_id  # normalize to int — matches count_existing_telegram_ids
            rows.append(d)

        found = len(rows)
        if found == 0:
            await message.answer("В файле нет делегатов — импортировать нечего.")
            await state.set_state(None)
            return

        ids = [r["telegram_id"] for r in rows]
        existing = await count_existing_telegram_ids(ids)

        await state.update_data(import_rows=rows, import_found=found, import_existing=existing)
        await message.answer(
            f"📥 <b>Файл прочитан</b>\n\n"
            f"Найдено делегатов: <b>{found}</b>\n"
            f"Из них уже есть в базе: <b>{existing}</b> — их пропущу\n"
            f"Будет добавлено: <b>{found - existing}</b>\n\n"
            "Напиши название сезона для импортируемых — например: YL'25",
            parse_mode="HTML",
            reply_markup=get_cancel_kb(),
        )
        await state.set_state(SeasonImport.naming)
    finally:
        if con is not None:
            con.close()
        if tmp_path is not None:
            try:
                os.unlink(tmp_path)
            except Exception as e:
                logger.warning(f"season_import_file_step: не удалось удалить временный файл {tmp_path}: {e}")


@router.message(SeasonImport.waiting_file)
async def season_import_file_invalid(message: types.Message):
    await message.answer("Пришли файл базы документом (не фото и не архив).")


@router.callback_query(F.data == "admin_city_switch")
async def admin_city_switch(callback: types.CallbackQuery):
    """Phase 07.2 (CITY-02): pick the city the admin panel is currently scoped to. Disabled
    cities are still listed (their past applications still need moderating), marked ❌.

    Phase 09.1 (C, ROLE-03): a manager bound to a city (`get_staff_city`) can't switch at
    all — the picker is never shown, only an alert naming their city. Bootstrap superadmins
    (`config.ADMIN_IDS`, D-12) are never restricted."""
    # Phase 09.3 (CITY-08): the bound-manager lock above is the ONLY gate this screen needs --
    # it already computes is_superadmin/bound and returns early for anyone with a locked city,
    # so everyone who reaches the code below (superadmin or an unbound manager) is exactly who
    # is allowed to see the "🌍 Все города" row too. No separate capability check needed here;
    # ADMIN_CAPS/CapabilityMiddleware already gated entry to this handler at ANY_CAPABILITY.
    is_superadmin = callback.from_user.id in config.ADMIN_IDS
    bound = None if is_superadmin else await get_staff_city(callback.from_user.id)
    if bound:
        await callback.answer(
            f"Ваш город — {await city_label(bound)}, менять может суперадмин",
            show_alert=True,
        )
        return

    current = await admin_selected_city(callback.from_user.id)
    all_prefix = "✅ " if current == ALL_CITIES else ""
    buttons = [
        [InlineKeyboardButton(text=f"{all_prefix}{ALL_CITIES_LABEL}", callback_data=f"admin_city_pick:{ALL_CITIES}")],
    ]
    for c in CITIES:
        code = c["code"]
        label = await city_label(code)
        enabled = await is_city_enabled(code)
        prefix = "✅ " if code == current else ""
        suffix = "" if enabled else " ❌"
        buttons.append([InlineKeyboardButton(text=f"{prefix}{label}{suffix}", callback_data=f"admin_city_pick:{code}")])
    buttons.append([InlineKeyboardButton(text="◀️ Назад", callback_data="admin_menu")])
    text = (
        "🏙 <b>Город админки</b>\n\n"
        "Всё в админке — про выбранный город: заявки, чеки, выгрузка, гейма, тексты и кнопки меню.\n"
        "«🌍 Все города» — данные без фильтра и общие тексты (то, что видят города без своего значения)."
    )
    await callback.message.edit_text(text, parse_mode="HTML", reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons))
    await callback.answer()


@router.callback_query(F.data.startswith("admin_city_pick:"))
async def admin_city_pick(callback: types.CallbackQuery):
    code = callback.data.split(":", 1)[1]
    if not await set_admin_city(callback.from_user.id, code):
        await callback.answer("Неизвестный город", show_alert=True)
        return
    await callback.answer(f"Город: {await city_label(code)}", show_alert=True)
    await callback.message.edit_text(
        "👮‍♂️ <b>Панель администратора</b>",
        parse_mode="HTML",
        reply_markup=await admin_keyboard_for(callback.from_user.id),
    )


@router.callback_query(F.data == "admin_menu")
async def admin_menu_root(callback: types.CallbackQuery):
    """Back to the admin panel keyboard (also fixes the previously dead «Отмена» buttons
    that pointed at admin_menu without a handler)."""
    await callback.message.edit_text(
        "👮‍♂️ <b>Панель администратора</b>", parse_mode="HTML", reply_markup=await admin_keyboard_for(callback.from_user.id)
    )
    await callback.answer()


@router.callback_query(F.data == "admin_dedupe_sheet")
async def dedupe_sheet_confirm(callback: types.CallbackQuery):
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🧹 Да, убрать дубли", callback_data="admin_dedupe_sheet_go")],
        [InlineKeyboardButton(text="← Отмена", callback_data="admin_menu")],
    ])
    await callback.message.edit_text(
        "🧹 <b>Убрать дубли из таблицы?</b>\n\n"
        "Удалю повторные строки с одинаковым Telegram ID (от повторных регистраций / "
        "тестов админов), оставлю <b>самую свежую</b> по каждому.\n\n"
        "⚠️ Удаляются целые строки — если на старой строке-дубле были ручные заметки, "
        "они пропадут (на оставленной строке всё цело).",
        parse_mode="HTML",
        reply_markup=kb,
    )
    await callback.answer()


@router.callback_query(F.data == "admin_dedupe_sheet_go")
async def dedupe_sheet_run(callback: types.CallbackQuery):
    await callback.answer("🧹 Убираю дубли…")
    logger.info(f"admin={callback.from_user.id} action=dedupe_sheet start")
    removed = await dedupe_sheet_by_id()
    if removed == REFUSED_UNPINNED_TAB:
        text = (
            "⛔ Убрать дубли нельзя: основная вкладка не задана.\n\n"
            "Без неё удаление строк могло бы задеть не ту вкладку. Укажите вкладку в "
            "«⚙️ Настройки → 📄 Вкладки таблицы → 📄 Основная (регистрации)» — сработает сразу, "
            "без перезапуска. Вариант для разработчика — <code>GOOGLE_SHEET_TAB</code> в .env "
            "(тогда нужен перезапуск)."
        )
    elif removed < 0:
        text = "⚠️ Не удалось (проверь доступ к Google Sheets, подробности в логах)."
    elif removed == 0:
        text = "✅ Дублей не найдено — таблица чистая."
    else:
        text = f"✅ Удалено дублей: <b>{removed}</b>. Оставлены свежие строки."
    logger.info(f"admin={callback.from_user.id} action=dedupe_sheet removed={removed}")
    await callback.message.edit_text(text, parse_mode="HTML", reply_markup=await admin_keyboard_for(callback.from_user.id))


@router.callback_query(F.data == "admin_broadcast")
async def show_admin_broadcast(callback: types.CallbackQuery, state: FSMContext):
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📢 Все пользователи", callback_data="broadcast_all")],
        [InlineKeyboardButton(text="📄 По файлу в проекте", callback_data="broadcast_local")],
        [InlineKeyboardButton(text="🚫 Не подписаны на канал", callback_data="broadcast_unsubscribed")],
        [InlineKeyboardButton(text="📝 Не завершили регистрацию", callback_data="broadcast_incomplete")],
        [InlineKeyboardButton(text="🎯 По фильтру", callback_data="broadcast_filter")],
        [InlineKeyboardButton(text="🕓 Запланировать", callback_data="broadcast_schedule")],
        [InlineKeyboardButton(text="❌ Отмена", callback_data="broadcast_cancel")],
    ])
    await callback.message.edit_text("Выберите целевую аудиторию рассылки:", reply_markup=kb)
    await state.set_state(Broadcast.target_selection)
    await callback.answer()

@router.message(Command("export"))
async def cmd_export(message: types.Message):
    headers, rows = await export_users_csv()

    output = io.StringIO()
    writer = csv.writer(output, delimiter=';', quotechar='"', quoting=csv.QUOTE_MINIMAL)
    writer.writerow(headers)
    writer.writerows(rows)

    output.seek(0)
    file_bytes = output.getvalue().encode('utf-8-sig')
    document = BufferedInputFile(file_bytes, filename="users.csv")

    await message.answer_document(document, caption="База данных пользователей")

@router.message(Command("broadcast"))
async def cmd_broadcast(message: types.Message, state: FSMContext):
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📢 Все пользователи", callback_data="broadcast_all")],
        [InlineKeyboardButton(text="📄 По файлу в проекте", callback_data="broadcast_local")],
        [InlineKeyboardButton(text="🚫 Не подписаны на канал", callback_data="broadcast_unsubscribed")],
        [InlineKeyboardButton(text="📝 Не завершили регистрацию", callback_data="broadcast_incomplete")],
        [InlineKeyboardButton(text="🎯 По фильтру", callback_data="broadcast_filter")],
        [InlineKeyboardButton(text="🕓 Запланировать", callback_data="broadcast_schedule")],
        [InlineKeyboardButton(text="❌ Отмена", callback_data="broadcast_cancel")],
    ])
    await message.answer("Выберите целевую аудиторию рассылки:", reply_markup=kb)
    await state.set_state(Broadcast.target_selection)

@router.callback_query(F.data == "broadcast_all", Broadcast.target_selection)
async def process_broadcast_all(callback: types.CallbackQuery, state: FSMContext):
    await callback.answer()
    try:
        await callback.message.delete()
    except Exception:
        pass
    await callback.message.answer(
        "Отправьте сообщение (текст или фото с подписью) для рассылки всем пользователям.",
        reply_markup=get_cancel_kb(),
    )
    await state.update_data(target_type="all")
    await state.set_state(Broadcast.message)

@router.callback_query(F.data == "broadcast_local", Broadcast.target_selection)
async def process_broadcast_local_file(callback: types.CallbackQuery, state: FSMContext):
    file_path = "data/broadcast_target.txt"

    if not os.path.exists(file_path):
        await callback.message.edit_text(f"❌ Файл {file_path} не найден! Создайте его и добавьте ID пользователей.")
        await state.clear()
        return

    try:
        with open(file_path, 'r', encoding='utf-8') as f:
            content = f.read()

        user_ids = []
        for line in content.splitlines():
            line = line.strip()
            clean_line = line.replace(',', '').replace(';', '')

            if clean_line.isdigit():
                user_ids.append(int(clean_line))

        if not user_ids:
            await callback.message.edit_text("⚠️ Файл пуст или не содержит корректных ID.")
            await state.clear()
            return

        user_ids = list(set(user_ids))

        await state.update_data(target_type="list", target_users=user_ids)
        await callback.answer()
        try:
            await callback.message.delete()
        except Exception:
            pass
        await callback.message.answer(
            f"✅ Найдено {len(user_ids)} пользователей в файле.\nТеперь отправьте сообщение для рассылки.",
            reply_markup=get_cancel_kb(),
        )
        await state.set_state(Broadcast.message)

    except Exception as e:
        await callback.message.edit_text(f"Ошибка при чтении файла: {e}")
        await state.clear()

async def _start_segment_broadcast(callback: types.CallbackQuery, state: FSMContext, user_ids: list, prompt: str):
    user_ids = list(set(user_ids))
    if not user_ids:
        await callback.message.edit_text("В этом сегменте сейчас нет пользователей.")
        await state.clear()
        await callback.answer()
        return
    await state.update_data(target_type="list", target_users=user_ids)
    await callback.answer()
    try:
        await callback.message.delete()
    except Exception:
        pass
    await callback.message.answer(prompt, reply_markup=get_cancel_kb())
    await state.set_state(Broadcast.message)


# ── Phase 3 (COMM-04): pure flood-safe send helpers ──────────────────────────

def _retry_delay(retry_after: int) -> int:
    """Wait Telegram's told delay plus 1s of slack before retrying (D-07)."""
    return retry_after + 1


def _classify_outcome(first_ok: bool, retried_ok) -> tuple[int, int]:
    """(delivered_inc, blocked_inc). A 429 that succeeds on retry is delivered, NOT
    blocked (D-08); only a genuine/failed-retry outcome increments blocked."""
    if first_ok or retried_ok is True:
        return (1, 0)
    return (0, 1)


@router.callback_query(F.data == "broadcast_unsubscribed", Broadcast.target_selection)
async def process_broadcast_unsubscribed(callback: types.CallbackQuery, state: FSMContext):
    user_ids = await get_non_subscriber_ids()
    await _start_segment_broadcast(
        callback, state, user_ids,
        f"🚫 {len(set(user_ids))} пользователей не подписаны на канал, давайте пришлём им уведомление.\n"
        "Теперь отправьте сообщение для рассылки.",
    )


@router.callback_query(F.data == "broadcast_incomplete", Broadcast.target_selection)
async def process_broadcast_incomplete(callback: types.CallbackQuery, state: FSMContext):
    user_ids = await get_incomplete_user_ids()
    await _start_segment_broadcast(
        callback, state, user_ids,
        f"📝 {len(set(user_ids))} пользователей не завершили регистрацию.\n"
        "Теперь отправьте сообщение для рассылки.",
    )


@router.callback_query(F.data == "broadcast_cancel")
async def cancel_broadcast_callback(callback: types.CallbackQuery, state: FSMContext):
    await state.clear()
    await callback.message.edit_text("Рассылка отменена.")
    await callback.answer()


@router.message(StateFilter(Broadcast), Command("cancel"))
@router.message(StateFilter(Broadcast), F.text == "Отмена")
async def cancel_broadcast(message: types.Message, state: FSMContext):
    await state.clear()
    await message.answer("Рассылка отменена.", reply_markup=ReplyKeyboardRemove())


async def _wait_and_send_album(media_group_id: str, users_ids: list, bot: Bot, state: FSMContext, admin_id: int):
    await asyncio.sleep(0.8)
    album_data = pending_albums.pop(media_group_id, None)
    if not album_data:
        return

    messages = album_data["messages"]
    media = []

    for msg in messages:
        caption_text = msg.html_text if getattr(msg, "html_text", None) else None

        if msg.photo:
            media.append(types.InputMediaPhoto(
                media=msg.photo[-1].file_id,
                caption=caption_text,
                parse_mode="HTML"
            ))
        elif msg.video:
            media.append(types.InputMediaVideo(
                media=msg.video.file_id,
                caption=caption_text,
                parse_mode="HTML"
            ))
        elif msg.document:
            media.append(types.InputMediaDocument(
                media=msg.document.file_id,
                caption=caption_text,
                parse_mode="HTML"
            ))
        elif msg.audio:
            media.append(types.InputMediaAudio(
                media=msg.audio.file_id,
                caption=caption_text,
                parse_mode="HTML"
            ))

    if not media:
        # WR-04: unsupported-only media group — notify the admin and clear state instead of
        # silently leaving the FSM parked in Broadcast.message with no feedback.
        try:
            await bot.send_message(admin_id, "⚠️ Рассылка отменена: неподдерживаемый тип вложения.")
        except Exception:
            pass
        await state.clear()
        return

    count = 0
    blocked = 0
    for chat_id in users_ids:
        retried_ok = None
        try:
            await bot.send_media_group(chat_id, media=media)
            first_ok = True
        except TelegramRetryAfter as e:
            first_ok = False
            await asyncio.sleep(_retry_delay(e.retry_after))
            try:
                await bot.send_media_group(chat_id, media=media)
                retried_ok = True
            except Exception:
                retried_ok = "error"
        except Exception:
            first_ok = False
        delivered_inc, blocked_inc = _classify_outcome(first_ok, retried_ok)
        count += delivered_inc
        blocked += blocked_inc
        await asyncio.sleep(0.05)

    try:
        await bot.send_message(
            admin_id,
            f"Рассылка альбома завершена.\n✅ Успешно: {count}\n❌ Недоступно: {blocked}"
        )
    except Exception:
        pass

    await state.clear()

@router.message(Broadcast.message)
async def process_broadcast(message: types.Message, state: FSMContext, bot: Bot):
    data = await state.get_data()
    target_type = data.get("target_type", "all")

    if target_type == "list":
        users_ids = data.get("target_users", [])
        if not users_ids:
             await message.answer("Список пользователей пуст. Рассылка отменена.")
             await state.clear()
             return
    else:
        users_ids = await get_all_users_ids()

    mgid = message.media_group_id
    if mgid:
        if mgid not in pending_albums:
            pending_albums[mgid] = {"messages": [message]}
            await message.answer(f"Альбом получен. Начинаю рассылку на {len(users_ids)} пользователей...")
            _spawn(_wait_and_send_album(mgid, users_ids, bot, state, message.from_user.id))
        else:
            pending_albums[mgid]["messages"].append(message)
        return

    count = 0
    blocked = 0

    await message.answer(f"Начинаю рассылку на {len(users_ids)} пользователей...")  # IN-01: was assigned to unused status_msg

    for chat_id in users_ids:
        retried_ok = None
        try:
            await message.send_copy(chat_id)
            first_ok = True
        except TelegramRetryAfter as e:
            first_ok = False
            await asyncio.sleep(_retry_delay(e.retry_after))
            try:
                await message.send_copy(chat_id)
                retried_ok = True
            except Exception:
                retried_ok = "error"
        except Exception:
            first_ok = False
        delivered_inc, blocked_inc = _classify_outcome(first_ok, retried_ok)
        count += delivered_inc
        blocked += blocked_inc
        await asyncio.sleep(0.05)

    await message.answer(
        f"Рассылка завершена.\n"
        f"✅ Успешно: {count}\n"
        f"❌ Недоступно: {blocked}"
    )
    await state.clear()


# ── Phase 3 (SCHED-01): schedule-a-broadcast UI ──────────────────────────────

@router.callback_query(F.data == "broadcast_schedule", Broadcast.target_selection)
async def broadcast_schedule_start(callback: types.CallbackQuery, state: FSMContext):
    await callback.answer()
    try:
        await callback.message.delete()
    except Exception:
        pass
    await callback.message.answer(
        "🕓 Введите дату и время рассылки в формате <b>ДД.ММ.ГГГГ ЧЧ:ММ</b>\n"
        "Например: 01.07.2026 14:30",
        reply_markup=get_cancel_kb(),
    )
    await state.set_state(Broadcast.schedule_when)


@router.message(Broadcast.schedule_when)
async def broadcast_schedule_when(message: types.Message, state: FSMContext):
    when = _parse_schedule_dt(message.text)
    if when is None:
        await message.answer("❌ Не понял дату. Формат: ДД.ММ.ГГГГ ЧЧ:ММ (напр. 01.07.2026 14:30)")
        return
    # TZFIX-260816: admin input is Moscow wall-clock — compare against Moscow, not the
    # container clock (UTC), or a past-MSK time can slip through as "future" and fire instantly.
    if when <= _now_moscow_naive():
        await message.answer("❌ Это время уже прошло. Введите будущую дату.")
        return
    await state.update_data(schedule_dt=when)
    await message.answer(
        f"✅ Запланировано на {when.strftime('%d.%m.%Y %H:%M')}.\n"
        "Теперь отправьте сообщение (текст или фото с подписью) для рассылки.",
        reply_markup=get_cancel_kb(),
    )
    await state.set_state(Broadcast.schedule_message)


@router.message(Broadcast.schedule_message)
async def broadcast_schedule_message(message: types.Message, state: FSMContext):
    data = await state.get_data()
    when = data.get("schedule_dt")
    if not when:
        await message.answer("Сессия истекла, начните заново через /broadcast.")
        await state.clear()
        return

    photo = message.photo[-1].file_id if message.photo else None
    # WR-03: html_text already falls back to caption+caption_entities when .text is empty, so
    # it preserves bold/italic/link formatting for BOTH text and photo-caption broadcasts.
    # Using raw message.caption here silently stripped entities the admin applied.
    if message.text or message.caption:
        text = message.html_text
    else:
        text = None

    filters = data.get("filters")
    filter_spec = json.dumps(filters, ensure_ascii=False) if filters else None

    bid = await create_scheduled_broadcast(text, photo, filter_spec, _fmt_dt(when), message.from_user.id)
    schedule_broadcast_job(bid, when)

    scope = "по фильтру" if filters else "всем пользователям"
    await message.answer(
        f"✅ Рассылка #{bid} запланирована на {when.strftime('%d.%m.%Y %H:%M')} ({scope}).\n"
        "Управление: /scheduled"
    )
    await state.clear()


@router.message(Command("scheduled"))
async def cmd_scheduled(message: types.Message):
    rows = await list_pending_broadcasts()
    if not rows:
        await message.answer("Нет запланированных рассылок.")
        return
    for row in rows:
        # IN-02: row["text"] was stored as HTML (message.html_text at schedule time). Strip the
        # tags for a clean plain-text preview instead of escaping them into visible &lt;b&gt; noise.
        preview = re.sub(r"<[^>]+>", "", row.get("text") or "(фото)")[:60]
        kb = InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="❌ Отменить", callback_data=f"sched_cancel_{row['id']}")
        ]])
        await message.answer(
            f"#{row['id']} — {row['scheduled_at']}\n{html_module.escape(preview)}",
            reply_markup=kb,
        )


@router.callback_query(F.data.startswith("sched_cancel_"))
async def sched_cancel(callback: types.CallbackQuery):
    # WR-02: guard the int() parse like _parse_appr/_parse_rcpt do — malformed callback_data
    # (empty suffix) must degrade gracefully, not raise and leave the button spinning.
    try:
        bid = int(callback.data.rsplit("_", 1)[1])
    except ValueError:
        await callback.answer("Некорректные данные.", show_alert=True)
        return
    await cancel_scheduled_broadcast(bid)
    cancel_broadcast_job(bid)
    await callback.answer("Отменено")
    try:
        await callback.message.edit_text(f"#{bid} — отменено ❌")
    except Exception:
        pass


# ── Phase 3 (COMM-01/02/03): filtered-broadcast builder ──────────────────────

_FILTER_FIELD_LABELS = {
    "city": "Город", "university": "ВУЗ", "status": "Статус",
    "source": "Источник", "registration_date": "Дата регистрации",
    "payment_status": "Оплата",
    "local_committee": "Комитет AIESEC", "department": "Департамент",
    "aiesec_role": "Роль AIESEC", "education_status": "Образование",
    "course": "Курс", "study_field": "Направление",
    "position": "Позиция", "attendance_format": "Формат участия",
    "participant_type": "Трек",  # Phase 5 (D-19)
    # Phase 07.2 (CITY-02). NOT the same thing as "city": "Город" above — that one is the
    # DELEGATE's own home city (a registration question); this one is the CITY OF THE EVENT.
    # The two labels sit in the same menu and must stay visually distinguishable on screen —
    # confusing them is the most expensive mistake this feature can make.
    "event_city": "Город мероприятия",
}

# Fields whose value is chosen from a DB-distinct picker (buttons pulled from real data).
# Everything in the filter menu except «Дата регистрации» (a before/after threshold).
_PICKER_FIELDS = {
    "city", "university", "source", "status", "payment_status",
    "local_committee", "department", "aiesec_role", "education_status",
    "course", "study_field", "position", "attendance_format",
    "participant_type",  # Phase 5 (D-19) — must ALSO be in db._FILTER_COLUMNS or it's dropped
    # Phase 07.2 (CITY-02) — same двойная регистрация rule: also in db._FILTER_COLUMNS, else
    # the filter shows on screen and never reaches the SQL. No separate handler needed —
    # `filter_pick_field` is subscribed to `filter_f_{fld}` across this whole set, computed at
    # import time. Its values are the ONLY ones not sourced from a DB DISTINCT (see
    # `_show_value_picker`).
    "event_city",
}

# How many value buttons per picker page (long cyrillic values → 1 per row).
_FILTER_PAGE_SIZE = 8


def _value_picker_kb(field: str, options: list[str], page: int,
                     labels: dict | None = None) -> InlineKeyboardMarkup:
    """Paginated value picker. The value itself never goes in callback_data (cyrillic values
    blow past Telegram's 64-byte limit) — buttons carry the option INDEX; the full list lives
    in FSM state. payment_status shows human labels.

    `labels` (Phase 07.2, CITY-02) is an optional {option: display_text} map for fields whose
    stored value is a machine code — event_city stores "spb", the button must read
    "Санкт-Петербург…". When omitted, behavior is byte-identical to before.
    """
    total = len(options)
    pages = max(1, (total + _FILTER_PAGE_SIZE - 1) // _FILTER_PAGE_SIZE)
    page = max(0, min(page, pages - 1))
    start = page * _FILTER_PAGE_SIZE
    rows = []
    for i, v in enumerate(options[start:start + _FILTER_PAGE_SIZE], start=start):
        if labels:
            label = str(labels.get(v, v))
        else:
            label = _PAYMENT_STATUS_LABELS.get(v, v) if field == "payment_status" else v
        rows.append([InlineKeyboardButton(text=label[:60], callback_data=f"filter_opt:{i}")])
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton(text="◀", callback_data=f"filter_optpage:{page - 1}"))
    if page < pages - 1:
        nav.append(InlineKeyboardButton(text="▶", callback_data=f"filter_optpage:{page + 1}"))
    if nav:
        rows.append(nav)
    rows.append([InlineKeyboardButton(text="← Назад", callback_data="filter_back")])
    return InlineKeyboardMarkup(inline_keyboard=rows)

# Human labels for payment_status values (shown in the filter summary / value picker).
_PAYMENT_STATUS_LABELS = {
    "not_paid": "Не оплатил", "overdue": "Просрочил",
    "receipt_sent": "Чек на проверке", "paid": "Оплатил",
}


def _filter_summary(filters: list[dict]) -> str:
    if not filters:
        return "Фильтры пока не выбраны."
    parts = []
    for f in filters:
        label = _FILTER_FIELD_LABELS.get(f["field"], f["field"])
        val = html_module.escape(str(f.get("value")))
        if f["field"] == "registration_date":
            opl = "после" if f.get("op") == "after" else "до"
            parts.append(f"{label} {opl} {val}")
        elif f["field"] == "payment_status":
            parts.append(f"{label} = {_PAYMENT_STATUS_LABELS.get(f.get('value'), val)}")
        elif f.get("label"):
            # Phase 07.2 (CITY-02): a filter may carry its own human display text (a city code
            # is unreadable in the summary). Generic — not a city special case. Escaped like
            # every other value; the label comes from bot_settings and is admin-editable.
            parts.append(f"{label} = {html_module.escape(str(f['label']))}")
        else:
            parts.append(f"{label} = {val}")
    return " И ".join(parts)


def _filter_menu_kb(filters: list[dict], *, show_city: bool = False) -> InlineKeyboardMarkup:
    kb = [
        [InlineKeyboardButton(text="Комитет AIESEC", callback_data="filter_f_local_committee"),
         InlineKeyboardButton(text="Департамент", callback_data="filter_f_department")],
        [InlineKeyboardButton(text="Роль AIESEC", callback_data="filter_f_aiesec_role"),
         InlineKeyboardButton(text="Позиция", callback_data="filter_f_position")],
        [InlineKeyboardButton(text="Город", callback_data="filter_f_city"),
         InlineKeyboardButton(text="ВУЗ", callback_data="filter_f_university")],
        [InlineKeyboardButton(text="Образование", callback_data="filter_f_education_status"),
         InlineKeyboardButton(text="Курс", callback_data="filter_f_course")],
        [InlineKeyboardButton(text="Направление", callback_data="filter_f_study_field"),
         InlineKeyboardButton(text="Формат участия", callback_data="filter_f_attendance_format")],
        [InlineKeyboardButton(text="Статус", callback_data="filter_f_status"),
         InlineKeyboardButton(text="Источник", callback_data="filter_f_source")],
        [InlineKeyboardButton(text="Дата регистрации", callback_data="filter_f_date"),
         InlineKeyboardButton(text="💰 Оплата", callback_data="filter_f_payment_status")],
        [InlineKeyboardButton(text="🎉 Трек", callback_data="filter_f_participant_type")],
    ]
    # Phase 07.2 (CITY-02): only with the cities module on. Default False keeps the keyboard
    # byte-identical to the pre-phase one when the module is off.
    if show_city:
        kb.append([InlineKeyboardButton(text="🏙 Город мероприятия",
                                        callback_data="filter_f_event_city")])
    if filters:
        kb.append([InlineKeyboardButton(text="📊 Показать и отправить", callback_data="filter_count")])
    kb.append([InlineKeyboardButton(text="❌ Отмена", callback_data="broadcast_cancel")])
    return InlineKeyboardMarkup(inline_keyboard=kb)


async def _render_filter_menu(target, filters: list[dict], *, edit: bool):
    text = (
        "🎯 <b>Рассылка по фильтру</b>\n"
        f"Текущие условия (AND): {_filter_summary(filters)}\n\n"
        "Добавьте поле фильтра или покажите количество."
    )
    # The menu deliberately opens EMPTY — the admin's selected city does NOT pre-fill this
    # filter (07.2-04 decision). A broadcast has an asymmetric risk: a condition the manager
    # never set is exactly how a message reaches a third of the base while they believe it
    # reached everyone. Moderation/export can't fail that way (an empty screen is visible).
    kb = _filter_menu_kb(filters, show_city=await cities_module_on())
    if edit:
        await target.edit_text(text, reply_markup=kb)
    else:
        await target.answer(text, reply_markup=kb)


@router.callback_query(F.data == "broadcast_filter", Broadcast.target_selection)
async def broadcast_filter_start(callback: types.CallbackQuery, state: FSMContext):
    await state.update_data(filters=[])
    await callback.answer()
    await _render_filter_menu(callback.message, [], edit=True)
    await state.set_state(Broadcast.filter_field)


# Phase 14 (CFG-02, IN-01): human RU labels for the «Трек» filter picker's buttons. The
# callback_data / value stored in FSM (and in the resulting filter, sent to the DB query) is
# still the raw code — this dict ONLY changes what the button text says. Deliberately separate
# from `_render_application_card`'s own `track_label` dict a few hundred lines down: that one
# renders a pending-application card and its literals are pinned by existing tests — not
# touched here.
_TRACK_LABELS = {
    "full": "Полный",
    "party_overnight": "🎉 Вечеринка с ночёвкой",
    "party_noovernight": "🎉 Вечеринка без ночёвки",
    "short": "⚡ Краткая анкета (акция)",
}


async def _show_value_picker(callback: types.CallbackQuery, state: FSMContext, field: str, prompt: str):
    """Load distinct DB values for `field`, stash them in FSM, render the paginated picker."""
    if field == "event_city":
        # WR-04: гейт живёт В ХЭНДЛЕРЕ, а не только в отрисовке клавиатуры. _render_filter_menu
        # лишь ПРЯЧЕТ кнопку при выключенном модуле, но `filter_pick_field` подписан на
        # множество, вычисленное из _PICKER_FIELDS на импорте, а инлайн-кнопки не истекают:
        # меню фильтров, нарисованное при включённом модуле, после выключения тумблера
        # оставалось рабочим, и рассылка молча сужалась по городу. Контракт module-off
        # («фаза не изменила поведение, пока менеджер не включил модуль») требует отказа здесь.
        if not await cities_module_on():
            await callback.answer("Модуль городов выключен.", show_alert=True)
            return
        # Phase 07.2 (CITY-02): the ONE field whose values come from the REGISTRY, not from a
        # DISTINCT over the column. `get_distinct_filter_values` filters out NULLs by
        # construction — and every application registered before the cities module has
        # event_city NULL, so the DEFAULT city (where all of them live) would simply not be
        # offered. A city with zero applications so far would be unofferable too.
        options = [c["code"] for c in CITIES]
        labels = {code: await city_label(code) for code in options}
    elif field == "participant_type":
        # Phase 14 (CFG-02, IN-01): RU labels instead of raw codes (party_noovernight etc.);
        # fail-soft for a value not in _TRACK_LABELS — falls back to the raw code as the label
        # (dict.get default), never crashes the picker.
        options = await get_distinct_filter_values(field)
        labels = {code: _TRACK_LABELS.get(code, code) for code in options}
    else:
        options = await get_distinct_filter_values(field)
        labels = None
    if not options:
        await callback.answer("В базе нет значений для этого поля.", show_alert=True)
        return
    # `filter_option_labels` rides along in FSM so pagination redraws keep the human labels.
    await state.update_data(filter_options=options, filter_page=0, filter_option_labels=labels)
    await callback.answer()
    await callback.message.edit_text(
        prompt, reply_markup=_value_picker_kb(field, options, 0, labels)
    )


@router.callback_query(F.data.in_({f"filter_f_{fld}" for fld in _PICKER_FIELDS}), Broadcast.filter_field)
async def filter_pick_field(callback: types.CallbackQuery, state: FSMContext):
    """Every attribute field → a DB-distinct value picker (no free-text typing)."""
    field = callback.data[len("filter_f_"):]
    await state.update_data(filter_pending_field=field, filter_pending_op=None)
    await _show_value_picker(callback, state, field, f"Выберите значение — «{_FILTER_FIELD_LABELS.get(field, field)}»:")


@router.callback_query(F.data == "filter_f_date", Broadcast.filter_field)
async def filter_pick_date(callback: types.CallbackQuery, state: FSMContext):
    await callback.answer()
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="После", callback_data="filter_d_after"),
         InlineKeyboardButton(text="До", callback_data="filter_d_before")],
        [InlineKeyboardButton(text="← Назад", callback_data="filter_back")],
    ])
    await callback.message.edit_text("Зарегистрированы…", reply_markup=kb)


@router.callback_query(F.data.in_({"filter_d_after", "filter_d_before"}), Broadcast.filter_field)
async def filter_pick_date_op(callback: types.CallbackQuery, state: FSMContext):
    op = "after" if callback.data.endswith("after") else "before"
    await state.update_data(filter_pending_field="registration_date", filter_pending_op=op)
    opl = "после" if op == "after" else "до"
    await _show_value_picker(callback, state, "registration_date", f"Зарегистрированы {opl} даты:")


@router.callback_query(F.data.startswith("filter_optpage:"), Broadcast.filter_field)
async def filter_page_nav(callback: types.CallbackQuery, state: FSMContext):
    data = await state.get_data()
    field = data.get("filter_pending_field")
    options = data.get("filter_options", [])
    if not field or not options:
        await callback.answer("Список устарел, начните заново.", show_alert=True)
        return
    try:
        page = int(callback.data.split(":", 1)[1])
    except ValueError:
        await callback.answer()
        return
    await state.update_data(filter_page=page)
    await callback.answer()
    await callback.message.edit_reply_markup(
        reply_markup=_value_picker_kb(field, options, page, data.get("filter_option_labels"))
    )


@router.callback_query(F.data.startswith("filter_opt:"), Broadcast.filter_field)
async def filter_pick_value(callback: types.CallbackQuery, state: FSMContext):
    data = await state.get_data()
    field = data.get("filter_pending_field")
    options = data.get("filter_options", [])
    try:
        idx = int(callback.data.split(":", 1)[1])
    except ValueError:
        await callback.answer("Некорректный выбор.", show_alert=True)
        return
    if not field or not (0 <= idx < len(options)):
        await callback.answer("Значение больше не доступно, начните заново.", show_alert=True)
        return
    value = options[idx]
    filters = data.get("filters", [])
    if field == "registration_date":
        filters.append({"field": field, "op": data.get("filter_pending_op"), "value": value})
    elif field == "event_city":
        # `exclude` is computed by cities.city_scope — the SAME function that scopes both
        # moderation queues and the CSV export, so "which codes count as this city" has
        # exactly one definition. It travels inside the filter dict because database/db.py
        # may not import cities (cycle), and it must survive the JSON round-trip a scheduled
        # broadcast's spec goes through. `label` renders the summary in human words.
        labels = data.get("filter_option_labels") or {}
        scope = city_scope(value)
        filters.append({
            "field": field,
            "value": value,
            "exclude": list(scope[1]) if scope else [],
            "label": labels.get(value, value),
        })
    else:
        filters.append({"field": field, "value": value})
    await state.update_data(
        filters=filters, filter_pending_field=None, filter_pending_op=None,
        filter_options=[], filter_page=0, filter_option_labels=None,
    )
    await callback.answer()
    await _render_filter_menu(callback.message, filters, edit=True)


@router.callback_query(F.data == "filter_back", Broadcast.filter_field)
async def filter_back(callback: types.CallbackQuery, state: FSMContext):
    """Abandon the in-progress field pick, return to the filter menu."""
    data = await state.get_data()
    await state.update_data(filter_pending_field=None, filter_pending_op=None, filter_options=[],
                            filter_page=0, filter_option_labels=None)
    await callback.answer()
    await _render_filter_menu(callback.message, data.get("filters", []), edit=True)


@router.callback_query(F.data == "filter_count", Broadcast.filter_field)
async def filter_count(callback: types.CallbackQuery, state: FSMContext):
    data = await state.get_data()
    filters = data.get("filters", [])
    ids = await count_and_list_filtered(filters)
    await callback.answer()
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📨 Отправить сейчас", callback_data="filter_send_now")],
        [InlineKeyboardButton(text="🕓 Запланировать", callback_data="filter_schedule")],
        [InlineKeyboardButton(text="❌ Отмена", callback_data="broadcast_cancel")],
    ])
    await callback.message.edit_text(
        f"🎯 Условия: {_filter_summary(filters)}\n"
        f"Под фильтр попадает <b>{len(ids)}</b> пользователей.",
        reply_markup=kb,
    )


@router.callback_query(F.data == "filter_send_now", Broadcast.filter_field)
async def filter_send_now(callback: types.CallbackQuery, state: FSMContext):
    data = await state.get_data()
    filters = data.get("filters", [])
    ids = await count_and_list_filtered(filters)
    await _start_segment_broadcast(
        callback, state, ids,
        f"🎯 {len(set(ids))} получателей по фильтру.\nТеперь отправьте сообщение для рассылки.",
    )


@router.callback_query(F.data == "filter_schedule", Broadcast.filter_field)
async def filter_schedule(callback: types.CallbackQuery, state: FSMContext):
    # filters stay in FSM state; the schedule flow reads them as filter_spec
    await callback.answer()
    await callback.message.edit_text(
        "🕓 Введите дату и время рассылки в формате ДД.ММ.ГГГГ ЧЧ:ММ (напр. 01.07.2026 14:30):"
    )
    await state.set_state(Broadcast.schedule_when)


# ── Phase 3 (VERIF): manual allowlist refresh ────────────────────────────────

@router.message(Command("refresh_allowlist"))
async def cmd_refresh_allowlist(message: types.Message):
    await refresh_allowlist()
    size = allowlist_size()
    if size == 0:
        await message.answer(
            "⚠️ Allowlist пуст. Если предотбор включён — сейчас впускаются ВСЕ (fail-open). "
            "Проверьте Google-таблицу (вкладка «Отобранные»)."
        )
    else:
        await message.answer(f"✅ Allowlist обновлён: {size} username в списке.")


# --- Registration Question Toggles ---

async def _is_question_on(setting_key: str) -> bool:
    # REG-02 (06-04): delegates entirely to the registry's typed toggle accessor — byte-
    # identical to the prior manual (val == "on") if val is not None else REG_DEFAULTS.get(...)
    # idiom (get_setting_typed's toggle branch reproduces it exactly), single get_setting call.
    return await get_setting_typed(setting_key)


# Phase 5 (D-04): party question tri-state helpers. These are PURE — they operate on the
# raw get_setting(f"{key}__party") return value (None | "on" | "off") and must never route
# through _is_question_on, which collapses None into a resolved boolean and would make
# "inherit" indistinguishable from "off".
def _party_tri_state_label(raw: str | None) -> str:
    if raw is None:
        return "➕ Наследует"
    return "✅ Вкл" if raw == "on" else "❌ Выкл"


def _party_tri_state_advance(raw: str | None) -> str | None:
    """Cycle: None (inherit) -> "on" -> "off" -> None (inherit). The None return value
    signals the caller to delete_setting (key-absence IS the inherit state, D-04)."""
    if raw is None:
        return "on"
    if raw == "on":
        return "off"
    return None


def _track_switcher_row(active: str) -> list[InlineKeyboardButton]:
    """D-06: first row of the questions keyboard — switches the whole screen between the
    full-track view (existing 2-state toggles) and the party-track view (tri-state).

    Phase 7 (SHORT-03): third button for the short (promo) track. Its own screen is
    2-state (✅/❌), not tri-state like party — see render_questions_text/
    build_questions_keyboard for the rationale."""
    return [
        InlineKeyboardButton(text=("• " if active == "full" else "") + "Полный", callback_data="reg_q_track:full"),
        InlineKeyboardButton(text=("• " if active == "party" else "") + "Party", callback_data="reg_q_track:party"),
        InlineKeyboardButton(text=("• " if active == "short" else "") + "⚡ Краткая", callback_data="reg_q_track:short"),
    ]


def _categorized_question_keys() -> list[tuple[str, str]]:
    """(header, setting_key) rows in category order. Any REG_FLOW key not placed in a
    REG_CATEGORIES bucket lands in a trailing «Прочие» group so nothing is ever hidden."""
    seen = set()
    rows: list[tuple[str, str]] = []
    for header, keys in REG_CATEGORIES:
        for k in keys:
            rows.append((header, k))
            seen.add(k)
    leftover = [sk for _, sk, *_ in REG_FLOW if sk not in seen]
    for k in leftover:
        rows.append(("📦 Прочие", k))
    return rows


async def render_questions_text(track: str = "full") -> str:
    lines = ["📋 <b>Вопросы регистрации</b>", ""]
    if track == "party":
        lines.append(
            "<i>Действуют в режиме «🎉 Party». ➕ Наследует — берётся общая настройка, "
            "✅/❌ — переопределено для этого трека.</i>"
        )
    elif track == "short":
        # Phase 7 (SHORT-03): 2-state, not tri-state. Absent __short key means "не спрашивается"
        # (07-01's is-not-None gate resolves absence to False, no fallback to the global toggle) —
        # visually there is nothing to "inherit", so a third inherit-labelled button would look
        # identical to "off" and just confuse the manager ("нажимаю, ничего не меняется").
        lines.append(
            "<i>Действуют в режиме «⚡ Краткая». По умолчанию вопрос не задаётся — включай "
            "только нужные.</i>"
        )
    else:
        lines.append("<i>Действуют в режиме «📋 Полная регистрация». Сгруппированы по типу события.</i>")
    lines.append("")
    current = None
    for header, setting_key in _categorized_question_keys():
        if header != current:
            lines.append(f"\n<b>{header}</b>")
            current = header
        label = REG_LABELS.get(setting_key, setting_key)
        if track == "party":
            raw = await get_setting(f"{setting_key}__party")
            status = _party_tri_state_label(raw)
        elif track == "short":
            raw = await get_setting(f"{setting_key}__short")
            status = "✅ Вкл" if raw == "on" else "❌ Выкл"
        else:
            status = "✅" if await _is_question_on(setting_key) else "❌"
        lines.append(f"{status} {label}")
    return "\n".join(lines)


async def build_questions_keyboard(track: str = "full"):
    buttons = [_track_switcher_row(track)]
    current = None
    for header, setting_key in _categorized_question_keys():
        if header != current:
            # Non-actionable section header (noop callback).
            buttons.append([InlineKeyboardButton(text=f"── {header} ──", callback_data="reg_q_noop")])
            current = header
        label = REG_LABELS.get(setting_key, setting_key)
        if track == "party":
            raw = await get_setting(f"{setting_key}__party")
            toggle_text = f"{_party_tri_state_label(raw)} {label}"
            buttons.append([InlineKeyboardButton(text=toggle_text, callback_data=f"reg_q_ptoggle:{setting_key}")])
        elif track == "short":
            # Phase 7 (SHORT-03): read the RAW __short value (never _is_question_on/
            # get_setting_typed) — only the literal "on" counts as enabled; absence and any
            # other value both render as ❌ Выкл, matching the 2-state model above.
            raw = await get_setting(f"{setting_key}__short")
            toggle_text = f"{'✅ Вкл' if raw == 'on' else '❌ Выкл'} {label}"
            buttons.append([InlineKeyboardButton(text=toggle_text, callback_data=f"reg_q_stoggle:{setting_key}")])
        else:
            toggle_text = f"{'✅' if await _is_question_on(setting_key) else '❌'} {label}"
            buttons.append([InlineKeyboardButton(text=toggle_text, callback_data=f"reg_q_toggle:{setting_key}")])
    buttons.append([InlineKeyboardButton(text="← Назад к настройкам", callback_data="reg_q_back")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


@router.callback_query(F.data == "admin_reg_questions")
async def show_reg_questions(callback: types.CallbackQuery):
    text = await render_questions_text()
    await callback.message.edit_text(text, parse_mode="HTML", reply_markup=await build_questions_keyboard())
    await callback.answer()


async def _refresh_sheet_header() -> None:
    """Regenerate the Google-sheet header after a question toggle so newly enabled
    questions show up as columns right away. The header is otherwise built only at
    startup (main.py), so mid-session toggles left the sheet missing enabled columns —
    the reported bug. Fail-soft (ensure_sheet_header swallows API/credential errors) and
    backgrounded so the admin UI stays snappy.

    NOTE: a column inserted mid-list only aligns rows appended AFTER the toggle; rows
    already in the sheet keep their original positions. Set the event type before
    delegates start registering to avoid mid-event drift."""
    try:
        headers = await active_sheet_headers()
    except Exception as e:
        logger.warning(f"_refresh_sheet_header: could not compute headers: {e}")
        return
    _spawn(ensure_sheet_header(headers))


async def _refresh_party_sheet_header() -> None:
    """MEDIUM-01: resync the party tab's physical header after a party-question toggle/preset so
    party rows appended afterwards align to the same columns as row 1. party_sheet_row recomputes
    party_sheet_headers() live per append, so a mid-event __party override otherwise shifts every
    subsequent row against the once-written startup header — a silent column misalignment.
    Mirrors _refresh_sheet_header for the main tab. GATED on party_enabled='on' (like the startup
    _maybe_ensure_party_sheet_header) so toggling a party override while the track is OFF never
    materializes the tab (D-15). Fail-soft + backgrounded."""
    from handlers.registration import party_sheet_headers, PARTY_SHEET_TAB_DEFAULT
    from services.sheets import ensure_named_sheet_header
    try:
        # REG-02 (06-05): gate read migrated to the registry; behavior unchanged.
        if (await get_setting_typed("party_enabled")) != "on":
            return
        tab = await get_setting("party_sheet_tab") or PARTY_SHEET_TAB_DEFAULT
        headers = await party_sheet_headers()
    except Exception as e:
        logger.warning(f"_refresh_party_sheet_header: could not compute headers: {e}")
        return
    _spawn(ensure_named_sheet_header(tab, headers))


async def _refresh_short_sheet_header() -> None:
    """Phase 7 (SHORT-03): resync the short (promo) tab's physical header after a __short
    question toggle/preset — mirrors _refresh_party_sheet_header exactly. GATED on
    registration_mode == 'short' (gate #5) so a tap on the short-track questions screen while
    the manager is still on «Полная» never materializes an empty promo tab. Fail-soft +
    backgrounded, local import to avoid a circular import (same idiom as the party sibling)."""
    from handlers.registration import short_sheet_headers, SHORT_SHEET_TAB_DEFAULT
    from services.sheets import ensure_named_sheet_header
    try:
        if (await get_setting_typed("registration_mode")) != "short":
            return
        tab = await get_setting("short_sheet_tab") or SHORT_SHEET_TAB_DEFAULT
        headers = await short_sheet_headers()
    except Exception as e:
        logger.warning(f"_refresh_short_sheet_header: could not compute headers: {e}")
        return
    _spawn(ensure_named_sheet_header(tab, headers))


@router.callback_query(F.data.startswith("reg_q_toggle:"))
async def toggle_reg_question(callback: types.CallbackQuery):
    setting_key = callback.data.split(":", 1)[1]

    # REG-02 (06-04): registry-driven resolution, byte-identical to the prior manual
    # (val == "on") if val is not None else REG_DEFAULTS.get(setting_key, "on") == "on" idiom.
    current_on = await get_setting_typed(setting_key)

    new_val = "off" if current_on else "on"
    await set_setting(setting_key, new_val)

    label = REG_LABELS.get(setting_key, setting_key)
    status = "✅ Вкл" if new_val == "on" else "❌ Выкл"
    await callback.answer(f"{label}: {status}", show_alert=True)

    text = await render_questions_text()
    await callback.message.edit_text(text, parse_mode="HTML", reply_markup=await build_questions_keyboard())
    await _refresh_sheet_header()  # keep the sheet header in sync with enabled questions


@router.callback_query(F.data.startswith("reg_q_track:"))
async def reg_q_track_switch(callback: types.CallbackQuery):
    """D-06: track switcher row — re-renders the SAME «📋 Вопросы регистрации» message in
    the requested track context. No FSM state — the requested track lives entirely in the
    callback_data of the tapped button."""
    track = callback.data.split(":", 1)[1]
    if track not in ("full", "party", "short"):
        track = "full"
    text = await render_questions_text(track)
    await callback.message.edit_text(text, parse_mode="HTML", reply_markup=await build_questions_keyboard(track))
    await callback.answer()


@router.callback_query(F.data.startswith("reg_q_ptoggle:"))
async def toggle_party_question(callback: types.CallbackQuery):
    """D-04: tri-state cycle inherit(absent) -> on -> off -> inherit for the party-track
    override of one question. Reads/writes the RAW f"{setting_key}__party" value — never
    routes through _is_question_on, which would collapse None and make "inherit"
    indistinguishable from "off". delete_setting is the "back to inherit" primitive."""
    setting_key = callback.data.split(":", 1)[1]
    # T-05-03-02: validate setting_key against REG_FLOW before ever suffixing/writing it —
    # an unknown key from a crafted callback is rejected, never turned into a bot_settings write.
    valid_keys = {sk for _, sk, *_ in REG_FLOW}
    if setting_key not in valid_keys:
        await callback.answer("Неизвестный вопрос.", show_alert=True)
        return

    party_key = f"{setting_key}__party"
    current = await get_setting(party_key)  # None | "on" | "off" — do NOT collapse
    new_val = _party_tri_state_advance(current)
    if new_val is None:
        await delete_setting(party_key)  # back to inherit — key ABSENCE is the inherit state
    else:
        await set_setting(party_key, new_val)
    label = _party_tri_state_label(new_val)

    await _refresh_party_sheet_header()  # MEDIUM-01: keep the party tab header aligned with the toggle
    await callback.answer(f"{REG_LABELS.get(setting_key, setting_key)} (party): {label}", show_alert=True)
    text = await render_questions_text("party")
    await callback.message.edit_text(text, parse_mode="HTML", reply_markup=await build_questions_keyboard("party"))


@router.callback_query(F.data.startswith("reg_q_stoggle:"))
async def toggle_short_question(callback: types.CallbackQuery):
    """Phase 7 (SHORT-03): 2-state toggle (on/off) for the short-track __short override of
    one question. Deliberately 2-state, not tri-state like toggle_party_question — see
    render_questions_text's short branch for the rationale (absent __short key already means
    "off" per 07-01, so a separate "inherit" state would be indistinguishable from "off" and
    only confuse the manager). delete_setting is never used here — every tap writes an
    explicit "on"/"off", unlike the party cycle's "back to inherit" step."""
    setting_key = callback.data.split(":", 1)[1]
    # T-07-09: validate setting_key against REG_FLOW before ever suffixing/writing it — a
    # crafted "reg_q_stoggle:party_enabled" (or any non-REG_FLOW key) is rejected, never
    # turned into a bot_settings write.
    valid_keys = {sk for _, sk, *_ in REG_FLOW}
    if setting_key not in valid_keys:
        await callback.answer("Неизвестный вопрос.", show_alert=True)
        return

    short_key = f"{setting_key}__short"
    current = await get_setting(short_key)  # None | "on" | "off"
    new_val = "off" if current == "on" else "on"
    await set_setting(short_key, new_val)  # always an explicit write, never delete_setting
    label = "✅ Вкл" if new_val == "on" else "❌ Выкл"

    await _refresh_short_sheet_header()  # keep the short tab header aligned with the toggle
    await callback.answer(f"{REG_LABELS.get(setting_key, setting_key)} (краткая): {label}", show_alert=True)
    text = await render_questions_text("short")
    await callback.message.edit_text(text, parse_mode="HTML", reply_markup=await build_questions_keyboard("short"))


@router.callback_query(F.data == "reg_q_noop")
async def reg_q_noop(callback: types.CallbackQuery):
    # Section-header button in the categorized question view — not actionable.
    await callback.answer()


@router.callback_query(F.data == "reg_q_back")
async def reg_questions_back(callback: types.CallbackQuery):
    text = await render_settings_text(callback.from_user.id)
    await callback.message.edit_text(text, parse_mode="HTML", reply_markup=await build_settings_keyboard(callback.from_user.id))
    await callback.answer()


# --- Event-type presets (one-tap bulk toggle) ---

async def _apply_event_preset(preset_key: str) -> None:
    """Bulk-write reg_q_* + payment_enabled for the chosen preset. Every REG_DEFAULTS
    key is set explicitly (on if in the preset's list, else off) so the result is
    deterministic regardless of prior per-question overrides."""
    preset = REG_PRESETS[preset_key]
    on_set = set(preset["on"])
    for key in REG_DEFAULTS:
        await set_setting(key, "on" if key in on_set else "off")
    await set_setting("payment_enabled", preset["payment_enabled"])


@router.callback_query(F.data == "admin_event_preset")
async def admin_event_preset(callback: types.CallbackQuery):
    buttons = [
        [InlineKeyboardButton(text=p["label"], callback_data=f"preset_apply:{key}")]
        for key, p in REG_PRESETS.items()
    ]
    buttons.append([InlineKeyboardButton(text="← Назад к настройкам", callback_data="reg_q_back")])
    await callback.message.edit_text(
        "🎛 <b>Тип события</b>\n\n"
        "Выбери пресет — он одним махом включит нужные вопросы и выключит остальные "
        "(+ настроит модуль оплаты). Экстра-вопросы потом докинешь вручную в «📋 Вопросы регистрации».\n\n"
        "⚠️ Перезатрёт текущие тумблеры вопросов.",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons),
    )
    await callback.answer()


@router.callback_query(F.data.startswith("preset_apply:"))
async def preset_apply(callback: types.CallbackQuery):
    key = callback.data.split(":", 1)[1]
    preset = REG_PRESETS.get(key)
    if not preset:
        await callback.answer("Неизвестный пресет.", show_alert=True)
        return
    on_labels = ", ".join(REG_LABELS.get(k, k) for k in preset["on"])
    # D-07: the party preset carries no "payment_enabled" key (party pricing is plan 05-05's
    # concern, not this preset's). preset.get(...) avoids a KeyError that the global
    # @dp.errors() handler would otherwise swallow silently (the admin sees nothing happen).
    payment_enabled = preset.get("payment_enabled")
    pay_line = ""
    if payment_enabled is not None:
        pay = "включится" if payment_enabled == "on" else "выключится"
        pay_line = f" Модуль оплаты <b>{pay}</b>."
    # D-07: __party/__short keys never overlap the globals a live full-form admin is looking
    # at, so neither the party nor the short (Phase 7) preset needs the "перезатрёт текущие
    # настройки" warning the forum/conf presets carry — nothing existing gets overwritten.
    warn = "" if key in ("party", "short") else "\n\n⚠️ Текущие настройки вопросов будут перезаписаны."
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Применить", callback_data=f"preset_confirm:{key}")],
        [InlineKeyboardButton(text="← Отмена", callback_data="admin_event_preset")],
    ])
    await callback.message.edit_text(
        f"Применить пресет <b>{preset['label']}</b>?\n\n"
        f"<b>Включатся:</b> {on_labels}\n"
        f"Остальные вопросы выключатся.{pay_line}"
        f"{warn}",
        parse_mode="HTML",
        reply_markup=kb,
    )
    await callback.answer()


@router.callback_query(F.data.startswith("preset_confirm:"))
async def preset_confirm(callback: types.CallbackQuery):
    key = callback.data.split(":", 1)[1]
    preset = REG_PRESETS.get(key)
    if not preset:
        await callback.answer("Неизвестный пресет.", show_alert=True)
        return
    if key == "party":
        # D-07: route to the isolated __party-only bulk writer. _apply_event_preset writes
        # GLOBAL reg_q_* keys for every REG_DEFAULTS entry — routing the party key there
        # would erase the live full-delegate question set, exactly what D-07 exists to prevent.
        await _apply_party_preset()
        await callback.answer(f"Пресет применён: {preset['label']}", show_alert=True)
        text = await render_questions_text("party")
        await callback.message.edit_text(
            text, parse_mode="HTML", reply_markup=await build_questions_keyboard("party")
        )
        # No _refresh_sheet_header(): the party preset changes no global setting, so the main
        # sheet header cannot have drifted. MEDIUM-01: but the party preset DOES change the
        # __party question set, so resync the PARTY tab's own header (plan 05-06).
        await _refresh_party_sheet_header()
        return
    if key == "short":
        # Phase 7 (SHORT-03): route to the isolated __short-only bulk writer, same reasoning
        # as the party branch above — _apply_event_preset writes GLOBAL reg_q_* keys, which
        # would erase the live full-delegate question set. The promo preset changes no global
        # setting, so the MAIN sheet header cannot have drifted — only the SHORT tab's own
        # header needs a resync.
        await _apply_short_preset()
        await callback.answer(f"Пресет применён: {preset['label']}", show_alert=True)
        text = await render_questions_text("short")
        await callback.message.edit_text(
            text, parse_mode="HTML", reply_markup=await build_questions_keyboard("short")
        )
        await _refresh_short_sheet_header()
        return
    await _apply_event_preset(key)
    await callback.answer(f"Пресет применён: {preset['label']}", show_alert=True)
    text = await render_questions_text()
    await callback.message.edit_text(text, parse_mode="HTML", reply_markup=await build_questions_keyboard())
    await _refresh_sheet_header()  # preset flips many questions → resync the sheet header


# --- Editable question prompts (YL'26: per-event wording, 0 хардкода) ---

def _prompt_steps() -> list[tuple[str, str]]:
    """(step_key, human label) for every question whose wording can be overridden."""
    steps = [("full_name", "🪪 Фамилия и Имя")]
    for step_key, setting_key, *_ in REG_FLOW:
        steps.append((step_key, REG_LABELS.get(setting_key, step_key)))
    return steps


def _prompt_track_switcher_row(active: str) -> list[InlineKeyboardButton]:
    """Quick 260724-cfn (WR-02b): mirrors _track_switcher_row for the «Тексты вопросов»
    screen — switches between editing the global (full) prompt overrides and the
    party-track (__party) prompt overrides. Own callback namespace (reg_prompt_track:)
    so it never collides with the questions-toggle screen's reg_q_track: switcher."""
    return [
        InlineKeyboardButton(text=("• " if active == "full" else "") + "Полный", callback_data="reg_prompt_track:full"),
        InlineKeyboardButton(text=("• " if active == "party" else "") + "Party", callback_data="reg_prompt_track:party"),
    ]


async def render_prompts_text(track: str = "full") -> str:
    text = (
        "✏️ <b>Тексты вопросов</b>\n\nВыбери вопрос и пришли свой текст. ✅ — текст переопределён, "
        "✏️ — стандартный. Чтобы вернуть стандартный, отправь «-»."
    )
    if track == "party":
        text += (
            "\n\n<i>Действуют в режиме 🎉 Party. ✏️ — берётся общий текст вопроса, "
            "✅ — переопределено для party. «-» — сброс к общему.</i>"
        )
    return text


async def build_prompts_keyboard(track: str = "full"):
    buttons = [_prompt_track_switcher_row(track)]
    for step_key, label in _prompt_steps():
        if track == "party":
            key = f"reg_prompt_{step_key}__party"
            callback_data = f"reg_prompt_edit:{step_key}:party"
        else:
            key = f"reg_prompt_{step_key}"
            callback_data = f"reg_prompt_edit:{step_key}"
        custom = await get_setting(key)
        mark = "✅" if custom else "✏️"
        buttons.append([InlineKeyboardButton(text=f"{mark} {label}", callback_data=callback_data)])
    buttons.append([InlineKeyboardButton(text="← Назад к настройкам", callback_data="admin_settings")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


@router.callback_query(F.data == "admin_reg_prompts")
async def admin_reg_prompts(callback: types.CallbackQuery):
    text = await render_prompts_text("full")
    await callback.message.edit_text(text, parse_mode="HTML", reply_markup=await build_prompts_keyboard("full"))
    await callback.answer()


@router.callback_query(F.data.startswith("reg_prompt_track:"))
async def reg_prompt_track_switch(callback: types.CallbackQuery):
    """Quick 260724-cfn (WR-02b): re-renders the SAME «✏️ Тексты вопросов» message in the
    requested track context. No FSM state — mirrors reg_q_track_switch."""
    track = callback.data.split(":", 1)[1]
    if track not in ("full", "party"):
        track = "full"
    text = await render_prompts_text(track)
    await callback.message.edit_text(text, parse_mode="HTML", reply_markup=await build_prompts_keyboard(track))
    await callback.answer()


@router.callback_query(F.data.startswith("reg_prompt_edit:"))
async def reg_prompt_edit(callback: types.CallbackQuery, state: FSMContext):
    # Quick 260724-cfn (WR-02b): optional trailing ":party" track suffix. step_keys (full_name
    # + REG_FLOW) never contain ":", so this split is safe. Any suffix other than the literal
    # "party" falls back to "full" (closed whitelist, mirrors reg_q_track_switch).
    parts = callback.data.split(":")
    step_key = parts[1]
    track = "party" if len(parts) > 2 and parts[2] == "party" else "full"
    key = f"reg_prompt_{step_key}__party" if track == "party" else f"reg_prompt_{step_key}"
    current = await get_setting(key)
    text = "Пришли новый текст вопроса."
    if current:
        text = f"Текущий текст: <b>{html_module.escape(current)}</b>\n\n{text}"
    text += "\n\n<i>«-» — вернуть стандартный текст.</i>"
    cancel_kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="❌ Отмена", callback_data="settings_cancel")],
    ])
    await callback.message.edit_text(text, parse_mode="HTML", reply_markup=cancel_kb)
    await state.set_state(EditSetting.waiting_for_value)
    await state.update_data(setting_key=key)
    await callback.answer()


# --- Menu Button Toggles ---
#
# Phase 09.3 (07, CITY-09): header-aware, single screen. Folds in 09.2-06's separate
# per-city entry point and its whole picker sub-flow (list screen, per-city text/keyboard
# builders, pick/toggle/clear/clear-go handlers — all deleted) — mirrors the exact merge
# shape 09.3-06 already applied to the per-key settings editor (_settings_edit_screen): one
# render helper branching on an ALREADY-RESOLVED header (WR-05), no separate picker screen.
# The has-override helper below is kept (still needed by the merged keyboard's «↩️ Все как
# везде» row).

async def render_menu_text(admin_id: int | None = None) -> str:
    """Header = real city -> title names the city, every row shows the city's EFFECTIVE
    value (`get_setting_typed_for_city`) plus a «(своё)»/«(как везде)» mark. Header = None
    (module off, or no admin_id passed) / ALL_CITIES («все города») -> today's global
    screen, byte-identical (09.2-06: menu_* is a registry `enum` key, options ["on","off"],
    default "on" — the enum branch of `_parse_setting` is `raw if raw else default`, so
    None/"" -> "on", any other value (including "off"/garbage) -> not "on", matching the
    pre-registry `val is None or val == "on"` idiom)."""
    header_code = await admin_selected_city(admin_id) if admin_id is not None else None
    if header_code and header_code != ALL_CITIES:
        city_txt = await city_label(header_code)
        lines = [f"🔘 <b>Кнопки главного меню — {html_module.escape(city_txt)}</b>", ""]
        for key, text in MENU_BUTTONS:
            is_on = await get_setting_typed_for_city(key, header_code) == "on"
            override_key = per_city_key(key, header_code)
            own = bool(override_key and await get_setting(override_key))
            status = "✅" if is_on else "❌"
            mark = " <i>(своё)</i>" if own else " <i>(как везде)</i>"
            lines.append(f"{status} {text}{mark}")
        return "\n".join(lines)

    lines = ["🔘 <b>Кнопки главного меню</b>", ""]
    for key, text in MENU_BUTTONS:
        is_on = await get_setting_typed(key) == "on"
        status = "✅" if is_on else "❌"
        lines.append(f"{status} {text}")
    return "\n".join(lines)


async def build_menu_keyboard(admin_id: int | None = None):
    """Same header-aware branch as `render_menu_text` (WR-05: this function resolves the
    header itself, ONCE — every caller below passes its own already-known `admin_id`, never
    a pre-resolved code, matching the settings-group-screen precedent)."""
    header_code = await admin_selected_city(admin_id) if admin_id is not None else None
    per_city_ctx = bool(header_code and header_code != ALL_CITIES)

    buttons = []
    for key, text in MENU_BUTTONS:
        if per_city_ctx:
            is_on = await get_setting_typed_for_city(key, header_code) == "on"
        else:
            is_on = await get_setting_typed(key) == "on"
        toggle_text = f"{'✅' if is_on else '❌'} {text}"
        buttons.append([InlineKeyboardButton(text=toggle_text, callback_data=f"menu_toggle:{key}")])

    if per_city_ctx and await _menu_city_has_override(header_code):
        buttons.append([InlineKeyboardButton(text="↩️ Все как везде", callback_data="menu_reset_city")])

    buttons.append([InlineKeyboardButton(text="← Назад к настройкам", callback_data="menu_back")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


@router.callback_query(F.data == "admin_menu_buttons")
async def show_menu_buttons(callback: types.CallbackQuery):
    admin_id = callback.from_user.id
    text = await render_menu_text(admin_id)
    await callback.message.edit_text(text, parse_mode="HTML", reply_markup=await build_menu_keyboard(admin_id))
    await callback.answer()


@router.callback_query(F.data.startswith("menu_toggle:"))
async def toggle_menu_button(callback: types.CallbackQuery):
    key = callback.data.split(":", 1)[1]
    admin_id = callback.from_user.id
    # WR-05: single header read for this handler; both the write branch and the redraw
    # below reuse `admin_id`, never a second `admin_selected_city` call.
    header_code = await admin_selected_city(admin_id)
    per_city_ctx = bool(header_code and header_code != ALL_CITIES)

    if per_city_ctx:
        # T-093-24: RIGHT re-checked here, not just via a hidden button.
        visible = await _per_city_visible_codes(admin_id)
        if header_code not in visible:
            await callback.answer("Этот город правит суперадмин", show_alert=True)
            return
        if key not in {k for k, _ in MENU_BUTTONS}:
            await callback.answer("Неизвестная кнопка", show_alert=True)
            return
        # T-093-25: composed key comes ONLY from cities.per_city_key.
        composed = per_city_key(key, header_code)
        if composed is None:
            await callback.answer("Неизвестный город", show_alert=True)
            return
        current_on = await get_setting_typed_for_city(key, header_code) == "on"
        new_val = "off" if current_on else "on"
        await set_setting(composed, new_val)
        label = dict(MENU_BUTTONS).get(key, key)
        city_txt = await city_label(header_code)
        status = "✅ Вкл" if new_val == "on" else "❌ Выкл"
        await callback.answer(f"{label} — {city_txt}: {status}", show_alert=True)
    else:
        current_on = await get_setting_typed(key) == "on"
        new_val = "off" if current_on else "on"
        await set_setting(key, new_val)
        label = dict(MENU_BUTTONS).get(key, key)
        status = "✅ Вкл" if new_val == "on" else "❌ Выкл"
        await callback.answer(f"{label}: {status}", show_alert=True)

    text = await render_menu_text(admin_id)
    await callback.message.edit_text(text, parse_mode="HTML", reply_markup=await build_menu_keyboard(admin_id))


@router.callback_query(F.data == "menu_back")
async def menu_buttons_back(callback: types.CallbackQuery):
    text = await render_settings_text(callback.from_user.id)
    await callback.message.edit_text(text, parse_mode="HTML", reply_markup=await build_settings_keyboard(callback.from_user.id))
    await callback.answer()


async def _menu_city_has_override(code: str) -> bool:
    """True if the city has at least one of the 9 menu_* keys overridden. Still needed by
    the merged keyboard's «↩️ Все как везде» row (09.2-06 lineage, kept verbatim)."""
    for key, _ in MENU_BUTTONS:
        override_key = per_city_key(key, code)
        if override_key and await get_setting(override_key):
            return True
    return False


@router.callback_query(F.data == "menu_reset_city")
async def menu_reset_city(callback: types.CallbackQuery):
    """Confirm screen for «↩️ Все как везде» on the header-scoped menu-buttons screen — same
    two-step confirm gate idiom as `settings_reset_city`/09.2-06's now-deleted per-city
    reset confirm screen: names the number of buttons about to lose their own setting
    before deleting anything."""
    admin_id = callback.from_user.id
    if not await cities_module_on():
        await callback.answer("Города выключены", show_alert=True)
        return
    header_code = await admin_selected_city(admin_id)
    if not header_code or header_code == ALL_CITIES:
        await callback.answer("Нет своих настроек для сброса", show_alert=True)
        return
    visible = await _per_city_visible_codes(admin_id)
    if header_code not in visible:
        await callback.answer("Этот город правит суперадмин", show_alert=True)
        return

    override_count = 0
    for key, _ in MENU_BUTTONS:
        override_key = per_city_key(key, header_code)
        if override_key and await get_setting(override_key):
            override_count += 1
    if override_count == 0:
        await callback.answer("Нет своих настроек для сброса", show_alert=True)
        return

    city_txt = await city_label(header_code)
    text = (
        f"Город {html_module.escape(city_txt)} снова будет показывать общий набор кнопок;\n"
        f"свои настройки {override_count} кнопок пропадут."
    )
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Да, как везде", callback_data=f"menu_reset_city_go:{header_code}")],
        [InlineKeyboardButton(text="← Отмена", callback_data="admin_menu_buttons")],
    ])
    await callback.message.edit_text(text, parse_mode="HTML", reply_markup=kb)
    await callback.answer()


@router.callback_query(F.data.startswith("menu_reset_city_go:"))
async def menu_reset_city_go(callback: types.CallbackQuery):
    code = callback.data.split(":", 1)[1]
    admin_id = callback.from_user.id
    if not await cities_module_on():
        await callback.answer("Города выключены", show_alert=True)
        return
    if code not in city_codes():
        await callback.answer("Неизвестный город", show_alert=True)
        return
    # T-093-24: RIGHT check against the code carried in callback_data (not just the current
    # header) — catches a bound manager's forged confirmation for another city, same
    # ordering as `settings_reset_city_go` (right check before freshness).
    visible = await _per_city_visible_codes(admin_id)
    if code not in visible:
        await callback.answer("Этот город правит суперадмин", show_alert=True)
        return
    # T-093-26: freshness — the confirm screen named the header's city; if the header moved
    # on since, refuse and re-render the menu screen for the NEW header instead of deleting.
    current = await admin_selected_city(admin_id)
    if code != current:
        await callback.answer("Город админки изменился — подтвердите заново.", show_alert=True)
        text = await render_menu_text(admin_id)
        await callback.message.edit_text(text, parse_mode="HTML", reply_markup=await build_menu_keyboard(admin_id))
        return

    # Idempotent -- deleting an already-absent key is a no-op, safe to repeat.
    for key, _ in MENU_BUTTONS:
        override_key = per_city_key(key, code)
        if override_key:
            await delete_setting(override_key)

    city_txt = await city_label(code)
    await callback.answer(f"Готово: {city_txt} — как везде", show_alert=True)
    text = await render_menu_text(admin_id)
    await callback.message.edit_text(text, parse_mode="HTML", reply_markup=await build_menu_keyboard(admin_id))


# Phase 07.2 (CITY-02): the ONE resolver both moderation queues (applications + receipts)
# read the selected city through — no handler calls admin_selected_city/city_scope directly.
# If a third moderation queue is ever added (gamification, Phase 9), it plugs in here too.
#
# WR-05: a screen must resolve the city ONCE and derive both the scope and the label from that
# single read. `_admin_city_scope` + `_admin_city_label` called back-to-back are two independent
# coroutines, each re-reading `admin_city__{id}` (cities_module_on() + get_setting = two more
# SQLite connections). aiogram processes updates concurrently, so the setting can change between
# the two awaits — the queue would then be FILTERED by one city and HEADED by another, exactly
# the confusion the header exists to prevent. Every render-time call site uses this helper.
async def _admin_city_view(admin_id: int) -> tuple[tuple[str, tuple[str, ...]] | None, str | None]:
    """(city_scope, label) for one admin, resolved from a SINGLE `admin_selected_city` read.
    Three states now (Phase 09.3, CITY-08):
    - `(None, None)` = no scope (module off) — the unfiltered, pre-CITY-02 behaviour.
    - `(None, ALL_CITIES_LABEL)` = module on, admin explicitly chose «Все города» — the SAME
      unfiltered SQL scope as module-off, but the label is NOT None. Every existing call site
      that branches on `label is None` to mean "module off, no city text" now gets a real
      (non-None) label for this mode instead — it will NOT crash or raise, but it WILL print
      the wrong copy (or the raw "*" literal, pre-this-fix) unless that branch is re-read and,
      where the text differs by mode, given a third branch. Re-check every `label is None`
      ternary you touch downstream of this function.
    - a real city code returns its own scoped tuple + label, unchanged."""
    code = await admin_selected_city(admin_id)
    if code is None:
        return None, None
    if code == ALL_CITIES:
        return None, await city_label(code)
    return city_scope(code), await city_label(code)


# Thin single-value views over _admin_city_view — kept because "what city is this admin scoped
# to" is the seam Phase 8 (staff/capabilities) plugs into, and the module-off parity contract
# asserts on them directly.
async def _admin_city_scope(admin_id: int) -> tuple[str, tuple[str, ...]] | None:
    return (await _admin_city_view(admin_id))[0]


async def _admin_city_label(admin_id: int) -> str | None:
    return (await _admin_city_view(admin_id))[1]


# WR-03: the QUEUE is scoped, but the per-card actions (appr_approve / appr_reject /
# rcpt_confirm / rcpt_reject) address a row by telegram_id from the callback data and knew
# nothing about the city. Cards stay in the chat history and inline buttons never expire, so a
# card rendered for city A and tapped after switching to city B acted OUTSIDE the current
# scope — breaking the "the panel shows and changes only the selected city" guarantee the
# ADMIN_GUIDE makes. These are single-record actions (the card shows the name), so one check is
# enough; unlike CR-02's mass approval, nothing here is redesigned.
async def _card_out_of_scope(admin_id: int, tid: int | None) -> bool:
    """True when `tid` does not belong to the admin's currently selected city. Always False
    with the module off (scope None = «ничего не фильтруется», the pre-CITY-02 path)."""
    if tid is None:
        return False
    scope = await _admin_city_scope(admin_id)
    if scope is None:
        return False
    user = await get_user(tid)
    # normalize_city collapses NULL/unknown into the default city — the SAME resolver the
    # queue's SQL scope uses, so «заявка без города» lands in Moscow here too, not «nowhere».
    return normalize_city((user or {}).get("event_city")) != scope[0]


_OUT_OF_SCOPE_ALERT = "Эта заявка из другого города — переключите город."


# CR-03 (09.1-REVIEW.md): the submission-level twin of `_card_out_of_scope` above. The QUEUE
# (`get_pending_submissions(city_scope=...)`) was already scoped by the DELEGATE's city — this
# checks the same thing for the per-card DECISION handlers (grev_approve /
# grev_approve_custom_start / grev_reject_start), which used to act on `submission_id` from
# callback_data with zero city check and credit coins / notify the delegate regardless. Checks
# the SUBMITTER's city, not the task's city (a task can be city-scoped or "all cities" — WR-06
# covers that at submit time; here we only care whether the DELEGATE is inside the admin's
# current scope, exactly the resolver `get_pending_submissions` itself uses).
async def _submission_out_of_scope(admin_id: int, submission: dict | None) -> bool:
    """True when the submission's delegate is outside the admin's currently selected city.
    Always False with the module off (scope None = «ничего не фильтруется», same parity
    contract as `_card_out_of_scope`)."""
    if submission is None:
        return False
    scope = await _admin_city_scope(admin_id)
    if scope is None:
        return False
    user = await get_user(submission["user_id"])
    return normalize_city((user or {}).get("event_city")) != scope[0]


_SUBMISSION_OUT_OF_SCOPE_ALERT = "Эта сдача из другого города — переключите город."


# ── Phase 2: application review queue ("Заявки", tinder UI) ───────────────────

def _parse_appr(data: str) -> tuple[str, int | None]:
    """'appr_approve:123' -> ('appr_approve', 123); 'appr_all' -> ('appr_all', None)."""
    if ":" in data:
        prefix, tid = data.split(":", 1)
        try:
            return prefix, int(tid)
        except ValueError:
            return prefix, None
    return data, None


def _render_application_card(user: dict, position: int, total: int, city_label_text: str | None = None) -> str:
    """HTML card for one pending application; all free-text escaped. `city_label_text` (Phase
    07.2, CITY-02) appends «· 🏙 {label}» to the header when an admin city is selected; None
    keeps the header byte-identical to the pre-CITY-02 line (module off / no city chosen)."""
    def esc(v):
        return html_module.escape(str(v)) if v not in (None, "", "-") else None

    header = f"📋 <b>Заявка {position}/{total}</b>"
    if city_label_text is not None:
        header += f" · 🏙 {html_module.escape(str(city_label_text))}"
    lines = [header, ""]
    name = esc(user.get("full_name")) or "—"
    uname = esc(user.get("username"))
    lines.append(f"👤 {name}" + (f" ({uname})" if uname else ""))
    # Phase 5 (D-14): shared queue — one extra line for a non-full track, no second queue,
    # no track predicate anywhere in get_pending_users/get_pending_count. Unrecognised values
    # are HTML-escaped (T-05-03-03): the raw DB column value can never inject markup here.
    track = user.get("participant_type") or "full"
    if track != "full":
        track_label = {
            "party_overnight": "🎉 Трек: вечеринка с ночёвкой",
            "party_noovernight": "🎉 Трек: вечеринка без ночёвки",
            "short": "⚡ Трек: краткая анкета (акция)",
        }.get(track, f"🎉 Трек: {html_module.escape(str(track))}")
        lines.append(track_label)
    # Phase 07.3 (05, RET-03): prev_season — сырая строка из БД, пришедшая изначально из
    # текстовой настройки event_season (см. threat T-073-05-01) — обязана пройти то же
    # экранирование, что и неопознанный track выше. Служебный литерал "legacy" (плана 01/04,
    # означает «регистрация до эпохи сезонов») менеджеру не показываем текстом — CLAUDE.md
    # запрещает показывать коды человеку (T-073-05-02).
    prev_season_raw = (user.get("prev_season") or "").strip()
    if prev_season_raw:
        if prev_season_raw == "legacy":
            lines.append("🔁 Повторный: был(а) на прошлом событии")
        else:
            lines.append(f"🔁 Повторный: был(а) в {html_module.escape(prev_season_raw)}")
    edu = esc(user.get("university")) or esc(user.get("education_status"))
    if edu:
        course = esc(user.get("course"))
        lines.append(f"🎓 {edu}" + (f", {course}" if course else ""))
    if esc(user.get("city")):
        lines.append(f"📍 {esc(user.get('city'))}")
    if esc(user.get("local_committee")):
        lines.append(f"🏢 {esc(user.get('local_committee'))}")
    if esc(user.get("position")):
        lines.append(f"👔 {esc(user.get('position'))}")
    if esc(user.get("alumni_status")):
        lines.append(f"🏷 {esc(user.get('alumni_status'))}")
    if user.get("age"):
        lines.append(f"🎂 {esc(user.get('age'))}")
    # Резюме: файлом, текстом или нет. Текст показываем прямо в карточке (Таня п.4),
    # обрезая длинные — полный текст доступен по кнопке «📎 Резюме».
    if user.get("resume_file_id"):
        lines.append("📎 Резюме: файлом (кнопка ниже)")
    elif esc(user.get("resume_text")):
        rt = str(user.get("resume_text"))
        preview = html_module.escape(rt[:300] + ("…" if len(rt) > 300 else ""))
        lines.append(f"📎 Резюме (текст): {preview}")
    else:
        lines.append("📎 Резюме: нет")
    return "\n".join(lines)


def _appr_card_kb(tid: int, has_resume: bool, total: int) -> InlineKeyboardMarkup:
    rows = [
        [
            InlineKeyboardButton(text="✅ Одобрить", callback_data=f"appr_approve:{tid}"),
            InlineKeyboardButton(text="❌ Отклонить", callback_data=f"appr_reject:{tid}"),
        ],
    ]
    third = []
    if has_resume:
        third.append(InlineKeyboardButton(text="📎 Резюме", callback_data=f"appr_resume:{tid}"))
    third.append(InlineKeyboardButton(text="⏭ Пропустить", callback_data=f"appr_skip:{tid}"))
    rows.append(third)
    rows.append([InlineKeyboardButton(text=f"✅ Одобрить все ({total})", callback_data="appr_all")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def _show_current_card(target: types.Message, state: FSMContext):
    """Render the oldest non-skipped pending card (DB-driven, restart-safe). Phase 07.2
    (CITY-02): city-scoped through _admin_city_view (_admin_city_scope's single-read form) —
    the admin id comes from state.key.user_id because `target` may be the bot's own message
    (callback.message), whose from_user is the bot, not the admin."""
    admin_id = state.key.user_id
    # WR-05: ONE read — the rows shown and the city named in the header must agree.
    scope, label = await _admin_city_view(admin_id)
    skipped = set((await state.get_data()).get("appr_skipped", []))
    total = await get_pending_count(city_scope=scope)
    offset = 0
    visible: list[dict] = []
    while not visible and offset < total:
        batch = await get_pending_users(limit=50, offset=offset, city_scope=scope)
        if not batch:
            break
        visible = [u for u in batch if u["telegram_id"] not in skipped]
        offset += len(batch)
    if not visible:
        # CR-01: admin-editable label + global HTML parse_mode → escape, or an «<» in the
        # setting makes Telegram reject the message and the empty-queue screen never opens.
        empty_text = (
            "✅ Заявок нет." if label is None
            else f"✅ Заявок нет — «{html_module.escape(str(label))}»."
        )
        await target.answer(empty_text, reply_markup=await admin_keyboard_for(admin_id))
        return
    current = visible[0]
    # M-02: position = how many the admin has already skipped + 1 (the shown card is the first
    # not-yet-skipped pending item). The old total - len(visible) + 1 returned e.g. 51/100 for
    # the first card whenever a full 50-row batch was unskipped. Cap at total for safety.
    position = min(len(skipped) + 1, total)
    # Phase 09.3 (09.3-02, CITY-08): in ALL_CITIES mode the queue holds every city at once — one
    # shared "🌍 Все города" header on every card would tell the manager nothing about WHICH
    # delegate they're approving. Resolve the CARD's own city instead of the header label, only
    # in this one mode; the normal city-selected/module-off cases pass `label` through unchanged.
    card_label = label
    if label == ALL_CITIES_LABEL:
        card_label = await city_label(normalize_city(current.get("event_city")))
    await target.answer(
        _render_application_card(current, position, total, city_label_text=card_label),
        parse_mode="HTML",
        reply_markup=_appr_card_kb(
            current["telegram_id"],
            bool(current.get("resume_file_id") or current.get("resume_text")),
            total,
        ),
    )


@router.callback_query(F.data == "admin_applications")
async def show_applications(callback: types.CallbackQuery, state: FSMContext):
    await state.update_data(appr_skipped=[])  # session-only skip set (D-07)
    await callback.answer()
    await _show_current_card(callback.message, state)


@router.callback_query(F.data.startswith("appr_skip:"))
async def appr_skip(callback: types.CallbackQuery, state: FSMContext):
    _, tid = _parse_appr(callback.data)
    data = await state.get_data()
    skipped = list(data.get("appr_skipped", []))
    if tid is not None and tid not in skipped:
        skipped.append(tid)
    await state.update_data(appr_skipped=skipped)
    try:
        await callback.message.delete()
    except Exception:
        pass
    await callback.answer("Пропущено")
    await _show_current_card(callback.message, state)


@router.callback_query(F.data.startswith("appr_resume:"))
async def appr_resume(callback: types.CallbackQuery):
    _, tid = _parse_appr(callback.data)
    user = await get_user(tid) if tid is not None else None
    if user and user.get("resume_file_id"):
        try:
            await callback.message.answer_document(user["resume_file_id"])
        except Exception as e:
            logger.error(f"Failed to re-send resume for {tid}: {e}")
            await callback.message.answer("Не удалось открыть резюме.")
    elif user and user.get("resume_text"):
        await callback.message.answer(
            f"📄 Резюме (текст):\n\n{html_module.escape(str(user['resume_text']))}",
            parse_mode="HTML",
        )
    else:
        await callback.message.answer("Резюме не приложено.")
    await callback.answer()


@router.callback_query(F.data.startswith("appr_approve:"))
async def appr_approve(callback: types.CallbackQuery, state: FSMContext):
    _, tid = _parse_appr(callback.data)
    # WR-03: карточка могла быть отрисована для другого города (кнопки не истекают).
    if await _card_out_of_scope(callback.from_user.id, tid):
        await callback.answer(_OUT_OF_SCOPE_ALERT, show_alert=True)
        return
    won = await approve_user_atomic(tid) if tid is not None else False
    if won:
        await approve_user(callback.bot, tid)  # welcome exactly once (D-10)
        # Автосинк статуса в таблицу (Таня п.5), fire-and-forget fail-soft.
        _spawn(update_status_in_sheet(tid, STATUS_LABELS["approved"]))
        logger.info(f"admin={callback.from_user.id} action=approve user={tid}")
        await callback.answer("Одобрено")
    else:
        await callback.answer("Уже обработано")
    try:
        await callback.message.delete()
    except Exception:
        pass
    await _show_current_card(callback.message, state)


@router.callback_query(F.data.startswith("appr_reject:"))
async def appr_reject_start(callback: types.CallbackQuery, state: FSMContext):
    _, tid = _parse_appr(callback.data)
    # WR-03: проверяем ДО запроса причины — иначе менеджер напишет причину впустую.
    if await _card_out_of_scope(callback.from_user.id, tid):
        await callback.answer(_OUT_OF_SCOPE_ALERT, show_alert=True)
        return
    await state.update_data(appr_reject_id=tid)
    await callback.message.answer("Укажи причину отклонения:", reply_markup=get_cancel_kb())
    await state.set_state(Approval.reason)
    await callback.answer()


# WR-03: admin.router is first, so appr_reject_reason (the Approval.reason catch-all below)
# would otherwise SWALLOW any /command typed mid-rejection as the rejection reason — the
# rejection fires with a garbage reason and the command never runs. Catch «Отмена» AND any
# «/...» command here first, aborting the rejection cleanly so the admin can re-issue it.
@router.message(Approval.reason, F.text.in_({"Отмена"}) | F.text.startswith("/"))
async def appr_reject_cancel(message: types.Message, state: FSMContext):
    await state.set_state(None)
    text = (message.text or "").strip()
    if text not in ("Отмена", "/cancel"):
        note = "Отклонение отменено (введена команда). При необходимости повторите её."
    else:
        note = "Отклонение отменено."
    await message.answer(note, reply_markup=ReplyKeyboardRemove())
    await _show_current_card(message, state)


@router.message(Approval.reason)
async def appr_reject_reason(message: types.Message, state: FSMContext):
    data = await state.get_data()
    tid = data.get("appr_reject_id")
    reason = message.text or "-"
    ok = await reject_user(tid) if tid is not None else False
    if ok:
        _spawn(update_status_in_sheet(tid, STATUS_LABELS["rejected"]))
        logger.info(f"admin={message.from_user.id} action=reject user={tid} reason={reason!r}")
        try:
            prefix = await get_setting("reject_text") or "К сожалению, твоя заявка отклонена."
            # WR-05: escape the admin-set prefix symmetrically with reason — an unescaped
            # &/< in the setting would otherwise break every rejection message under HTML mode.
            await message.bot.send_message(
                tid, f"{html_module.escape(prefix)}\n\n{html_module.escape(reason)}", parse_mode="HTML"
            )
        except Exception as e:
            logger.error(f"Failed to notify rejected user {tid}: {e}")
        await message.answer("Заявка отклонена.", reply_markup=ReplyKeyboardRemove())
    else:
        await message.answer("Заявка уже обработана.", reply_markup=ReplyKeyboardRemove())
    await state.set_state(None)
    await _show_current_card(message, state)


@router.callback_query(F.data == "appr_all")
async def appr_all_confirm(callback: types.CallbackQuery, state: FSMContext):
    # T-072-07 (Repudiation): the confirmation text must name BOTH the city and the count —
    # this is an irreversible mass operation, and a global-looking button at 3 cities means
    # one tap flips a city the admin never chose.
    code = await admin_selected_city(callback.from_user.id)  # None = модуль выключен
    scope = city_scope(code)
    label = None if code is None else await city_label(code)
    total = await get_pending_count(city_scope=scope)
    if total == 0:
        await callback.answer("Заявок нет")
        await _show_current_card(callback.message, state)
        return
    # CR-02: «Да» обязана подтверждать ИМЕННО тот город, который назван в тексте выше.
    # Код города едет в callback_data, и appr_all_yes сверяет его с текущим выбором —
    # иначе переключение города в соседнем сообщении (выбор живёт в bot_settings и
    # переживает перезапуск, кнопки не истекают) необратимо одобряет чужую очередь.
    kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="✅ Да", callback_data=f"appr_all_yes:{code or ''}"),
        InlineKeyboardButton(text="❌ Отмена", callback_data="appr_all_no"),
    ]])
    # CR-01: escape the admin-editable label — this screen is the LAST thing shown before an
    # irreversible mass approval; a broken parse here means it cannot be opened at all.
    # Phase 09.3 (CITY-08, T-093-10): THREE branches now, not two — `label` is non-None both
    # for a real city AND for ALL_CITIES mode (city_label("*") -> ALL_CITIES_LABEL, plan 01),
    # so a plain `label is None` ternary would silently print the per-city phrasing on a
    # cross-city mass approval. The ALL_CITIES branch must honestly say "по всем городам" —
    # this IS the irreversible-scope disclosure T-093-10/T-093-11 rely on.
    if label is None:
        text = f"Одобрить все {total} заявок?"
    elif label == ALL_CITIES_LABEL:
        text = (
            f"Одобрить все {total} заявок по всем городам? "
            "Будут затронуты заявки всех городов."
        )
    else:
        text = (
            f"Одобрить все {total} заявок в городе «{html_module.escape(str(label))}»? "
            "Заявки других городов не будут затронуты."
        )
    await callback.message.edit_text(text, reply_markup=kb)
    await callback.answer()


@router.callback_query(F.data == "appr_all_no")
async def appr_all_no(callback: types.CallbackQuery, state: FSMContext):
    await callback.answer("Отменено")
    await _show_current_card(callback.message, state)


async def _welcome_flipped(bot, ids: list):
    """Drain welcome sends for a mass approval, handling Telegram 429 (D-11)."""
    for tid in ids:
        try:
            await approve_user(bot, tid)
        except TelegramRetryAfter as e:
            await asyncio.sleep(e.retry_after + 1)
            try:
                await approve_user(bot, tid)
            except Exception as e2:
                logger.error(f"Mass-approve welcome retry failed for {tid}: {e2}")
        except Exception as e:
            logger.error(f"Mass-approve welcome failed for {tid}: {e}")
        await asyncio.sleep(0.05)


@router.callback_query(F.data.startswith("appr_all_yes"))
async def appr_all_yes(callback: types.CallbackQuery, state: FSMContext):
    # CR-02: массовое одобрение необратимо, поэтому оно fail-closed. Город берётся из
    # callback_data (тот, что назван в тексте подтверждения), а не из текущего выбора
    # админа, и должен ему СОВПАДАТЬ. Если админ переключил город после показа диалога —
    # отказываем и просим подтвердить заново, а не одобряем «то, что выбрано сейчас».
    # Старая (до CR-02) кнопка без двоеточия даёт confirmed=None: при включённом модуле
    # это гарантированно не совпадёт с выбранным городом и будет отвергнуто; при
    # выключенном модуле current тоже None — это и есть путь module-off (скоуп None).
    raw = callback.data.split(":", 1)[1].strip() if ":" in callback.data else ""
    confirmed = raw or None
    current = await admin_selected_city(callback.from_user.id)
    if confirmed != current:
        await callback.answer("Город админки изменился — подтвердите заново.", show_alert=True)
        await _show_current_card(callback.message, state)
        return
    # Phase 09.3 (CITY-08, Pitfall 1 / T-093-10): "*" is the ALL_CITIES marker, not a member
    # of the closed city registry — without this exception every confirmed ALL_CITIES mass
    # approval would hit "Неизвестный город" here even though the roundtrip check above just
    # passed. `city_scope(confirmed)` below already resolves "*" to None (no filter, plan 01);
    # only THIS membership guard needed the extra branch.
    if confirmed is not None and confirmed != ALL_CITIES and confirmed not in city_codes():
        await callback.answer("Неизвестный город", show_alert=True)
        return
    # T-072-03/T-072-07: city condition lives in the WHERE of this SAME atomic
    # UPDATE ... RETURNING — structurally cannot flip another city's rows.
    ids = await approve_all_pending(city_scope=city_scope(confirmed))  # atomic flip first (D-11)
    # WR-04: a stale confirm dialog re-clicked (buttons never expire) hits approve_all_pending
    # again — atomic, so it returns [] the second time. Don't run the drain or claim a count;
    # tell the admin it's already done and refresh the card.
    if not ids:
        try:
            await callback.message.edit_text("Заявки уже обработаны.", reply_markup=await admin_keyboard_for(callback.from_user.id))
        except Exception:
            pass
        await callback.answer("Уже обработано")
        await _show_current_card(callback.message, state)
        return
    # WR-01: schedule the welcome drain (and status sync) BEFORE the fragile edit_text. Inline
    # buttons never expire, but Telegram rejects editing a message >48h old, and the card may
    # have been deleted — if the edit threw first, the N just-approved users would be left
    # `approved` in DB with no welcome/menu/payment requisites (violates D-11 "welcome exactly
    # once"). Ordering the background sends first makes delivery independent of the edit.
    _spawn(_welcome_flipped(callback.bot, ids))  # drain sends in background
    # Массовый автосинк статуса в таблицу (Таня п.5) — один batch, fail-soft.
    if ids:
        _spawn(
            bulk_update_status_in_sheet({str(t): STATUS_LABELS["approved"] for t in ids})
        )
    try:
        await callback.message.edit_text(
            f"✅ Одобрено: {len(ids)}. Рассылаю приветствия…",
            reply_markup=await admin_keyboard_for(callback.from_user.id),
        )
    except Exception as e:
        logger.warning(f"appr_all_yes: confirm edit failed (welcome drain already scheduled): {e}")
    await callback.answer()


# ── Phase 4: receipt verification queue ("Чеки", tinder UI, D-12) ─────────────

def _parse_rcpt(data: str) -> tuple[str, int | None]:
    """'rcpt_confirm:123' -> ('rcpt_confirm', 123)."""
    if ":" in data:
        prefix, uid = data.split(":", 1)
        try:
            return prefix, int(uid)
        except ValueError:
            return prefix, None
    return data, None


def _render_receipt_card(user: dict, position: int, total: int, city_label_text: str | None = None) -> str:
    """Phase 07.2 (CITY-02): same header-suffix convention as _render_application_card —
    `city_label_text` appends «· 🏙 {label}» only when an admin city is selected; None keeps
    the header byte-identical to the pre-CITY-02 line."""
    header = f"🧾 <b>Чек {position}/{total}</b>"
    if city_label_text is not None:
        header += f" · 🏙 {html_module.escape(str(city_label_text))}"
    lines = [header, ""]
    lines.append(f"👤 {html_module.escape(str(user.get('full_name') or '—'))}")
    lines.append(f"💳 Вариант: {html_module.escape(str(user.get('payment_option') or '—'))}")
    lines.append(f"📎 Чек: {'загружен' if user.get('receipt_file_id') else 'нет'}")
    return "\n".join(lines)


def _rcpt_card_kb(uid: int, has_receipt: bool, total: int) -> InlineKeyboardMarkup:
    rows = [[
        InlineKeyboardButton(text="✅ Подтвердить", callback_data=f"rcpt_confirm:{uid}"),
        InlineKeyboardButton(text="❌ Отклонить", callback_data=f"rcpt_reject:{uid}"),
    ]]
    third = []
    if has_receipt:
        third.append(InlineKeyboardButton(text="🧾 Чек", callback_data=f"rcpt_view:{uid}"))
    third.append(InlineKeyboardButton(text="⏭ Следующий", callback_data=f"rcpt_skip:{uid}"))
    rows.append(third)
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def _show_current_receipt_card(target: types.Message, state: FSMContext):
    """Phase 07.2 (CITY-02): the SECOND moderation queue — reads the selected city through the
    same _admin_city_view / _admin_city_scope resolver as _show_current_card. If a third queue
    is ever added it plugs in here too (see the comment anchor next to _admin_city_view)."""
    admin_id = state.key.user_id
    # WR-05: ONE read — the rows shown and the city named in the header must agree.
    scope, label = await _admin_city_view(admin_id)
    skipped = set((await state.get_data()).get("rcpt_skipped", []))
    total = await get_receipt_pending_count(city_scope=scope)
    offset = 0
    visible: list[dict] = []
    while not visible and offset < total:
        batch = await get_receipt_pending_users(limit=50, offset=offset, city_scope=scope)
        if not batch:
            break
        visible = [u for u in batch if u["telegram_id"] not in skipped]
        offset += len(batch)
    if not visible:
        # CR-01: same escaping as the applications queue above.
        empty_text = (
            "✅ Чеков на проверке нет." if label is None
            else f"✅ Чеков на проверке нет — «{html_module.escape(str(label))}»."
        )
        await target.answer(empty_text, reply_markup=await admin_keyboard_for(admin_id))
        return
    current = visible[0]
    # M-02: position = skipped-so-far + 1 (the shown card is the first not-yet-skipped receipt).
    # The old total - len(visible) + 1 returned e.g. 51/100 for the first card on a >50 queue.
    position = min(len(skipped) + 1, total)
    # Phase 09.3 (09.3-02, CITY-08): same per-card resolve as the applications queue above — in
    # ALL_CITIES mode the shared header label would misname every card but its own delegate.
    card_label = label
    if label == ALL_CITIES_LABEL:
        card_label = await city_label(normalize_city(current.get("event_city")))
    await target.answer(
        _render_receipt_card(current, position, total, city_label_text=card_label),
        parse_mode="HTML",
        reply_markup=_rcpt_card_kb(current["telegram_id"], bool(current.get("receipt_file_id")), total),
    )


@router.callback_query(F.data == "admin_receipts")
async def show_receipts(callback: types.CallbackQuery, state: FSMContext):
    await state.update_data(rcpt_skipped=[])
    await callback.answer()
    await _show_current_receipt_card(callback.message, state)


@router.callback_query(F.data.startswith("rcpt_confirm:"))
async def rcpt_confirm(callback: types.CallbackQuery, state: FSMContext):
    _, uid = _parse_rcpt(callback.data)
    # WR-03: та же проверка, что и в очереди заявок — карточка чека тоже не истекает.
    if await _card_out_of_scope(callback.from_user.id, uid):
        await callback.answer(_OUT_OF_SCOPE_ALERT, show_alert=True)
        return
    rows = await update_payment_status(uid, "paid") if uid is not None else 0
    if rows == 0:
        # Atomic guard (T-04-05-02): another manager already confirmed.
        await callback.answer("Чек уже обработан.")
        await _show_current_receipt_card(callback.message, state)
        return
    # H-01: disable this card's buttons now that it's confirmed, so scrolling back and
    # tapping ❌ Отклонить on it can't fire a stale reject (the db guard also blocks it).
    try:
        await callback.message.edit_reply_markup(reply_markup=None)
    except Exception:
        pass
    from services.scheduler import cancel_payment_reminders
    cancel_payment_reminders(uid)  # cancel BEFORE notifying — no reminder after paid
    try:
        await callback.bot.send_message(
            uid,
            "✅ <b>Оплата подтверждена!</b>\n\nСпасибо, ваш взнос получен.",
            parse_mode="HTML",
            reply_markup=await get_main_menu_kb(uid),  # first menu after the payment journey
        )
        # WR-04: payment-confirm must mirror the non-payment approval path — deliver the
        # configured completion text + registration bonus. Menu already sent above.
        # WR-01: resolve the track before calling send_completion_and_bonus, mirroring the
        # pattern in approve_user (registration.py) — this is the primary PAID party path, so
        # without this a paying party delegate always got the full-track approve_text.
        from handlers.registration import send_completion_and_bonus
        try:
            user_row = await get_user(uid)
            participant_type = (user_row or {}).get("participant_type") or "full"
        except Exception as e2:
            logger.error(f"rcpt_confirm: failed to resolve participant_type for {uid}, defaulting to 'full': {e2}")
            participant_type = "full"
        await send_completion_and_bonus(callback.bot, uid, with_menu=False, participant_type=participant_type)
    except Exception as e:
        logger.error(f"Failed to notify user {uid} of payment confirmation: {e}")
    await callback.answer("Оплата подтверждена")
    await _show_current_receipt_card(callback.message, state)


@router.callback_query(F.data.startswith("rcpt_reject:"))
async def rcpt_reject_start(callback: types.CallbackQuery, state: FSMContext):
    _, uid = _parse_rcpt(callback.data)
    # WR-03: проверяем ДО запроса причины.
    if await _card_out_of_scope(callback.from_user.id, uid):
        await callback.answer(_OUT_OF_SCOPE_ALERT, show_alert=True)
        return
    await state.update_data(rcpt_reject_uid=uid)
    await state.set_state(ReceiptReview.reject_reason)
    await callback.message.answer("Укажи причину отклонения (или «-» без объяснений):", reply_markup=get_cancel_kb())
    await callback.answer()


@router.message(ReceiptReview.reject_reason, F.text.in_({"Отмена", "/cancel"}))
async def rcpt_reject_cancel(message: types.Message, state: FSMContext):
    await state.set_state(None)
    await message.answer("Отклонение отменено.", reply_markup=ReplyKeyboardRemove())
    await _show_current_receipt_card(message, state)


@router.message(ReceiptReview.reject_reason)
async def rcpt_reject_reason(message: types.Message, state: FSMContext):
    data = await state.get_data()
    uid = data.get("rcpt_reject_uid")
    if uid is not None:
        # H-01: guard the reset — a stale/already-confirmed card tapped ❌ must NOT flip a
        # 'paid' user back to 'not_paid'. Only a row still in 'receipt_sent' is rejectable.
        rows = await update_payment_status(uid, "not_paid", require_status="receipt_sent")
        if rows == 0:
            await state.set_state(None)
            await message.answer(
                "Чек уже обработан (оплата подтверждена или чек не в очереди) — отклонение пропущено.",
                reply_markup=ReplyKeyboardRemove(),
            )
            await _show_current_receipt_card(message, state)
            return
        reason_text = (message.text or "").strip()
        user_msg = "❌ Чек отклонён."
        if reason_text and reason_text != "-":
            user_msg += f" Причина: {html_module.escape(reason_text)}"
        user_msg += "\n\nЗагрузи чек повторно через бота."
        try:
            await message.bot.send_message(uid, user_msg, parse_mode="HTML")
        except Exception as e:
            logger.error(f"Failed to notify user {uid} of receipt rejection: {e}")
    await state.set_state(None)
    await message.answer("Готово.", reply_markup=ReplyKeyboardRemove())
    await _show_current_receipt_card(message, state)


@router.callback_query(F.data.startswith("rcpt_skip:"))
async def rcpt_skip(callback: types.CallbackQuery, state: FSMContext):
    _, uid = _parse_rcpt(callback.data)
    data = await state.get_data()
    skipped = list(data.get("rcpt_skipped", []))
    if uid is not None and uid not in skipped:
        skipped.append(uid)
    await state.update_data(rcpt_skipped=skipped)
    try:
        await callback.message.delete()
    except Exception:
        pass
    await callback.answer()
    await _show_current_receipt_card(callback.message, state)


@router.callback_query(F.data.startswith("rcpt_view:"))
async def rcpt_view(callback: types.CallbackQuery):
    _, uid = _parse_rcpt(callback.data)
    user = await get_user(uid) if uid is not None else None
    if user and user.get("receipt_file_id"):
        file_id = user["receipt_file_id"]
        try:
            await callback.message.answer_document(file_id, caption=f"Чек пользователя {uid}")
        except Exception:
            # receipt may be a photo file_id, not a document — fall back to photo send
            try:
                await callback.message.answer_photo(file_id, caption=f"Чек пользователя {uid}")
            except Exception as e:
                logger.error(f"Failed to show receipt for {uid}: {e}")
                await callback.answer("Не удалось открыть чек.", show_alert=True)
                return
        await callback.answer()
    else:
        await callback.answer("Чек не найден.", show_alert=True)


# ── Phase 2: self-documenting settings command (D-16) ────────────────────────
# Quick 260726-0bc: the guide used to dump raw bot_settings keys ("pending_notify_mode —
# instant/batched"). Managers do not read keys — they read screens. It now renders as
# grouped plain-Russian cards: what the setting does, what it is set to RIGHT NOW in words,
# and which admin button changes it. `entries` carry the raw key only for the DB read.

_GUIDE_ONOFF = {"on": "✅ включено", "off": "❌ выключено"}


def _guide_text_value(raw):
    """Human 'current value' for free-text settings — managers care whether a custom text is
    set at all, not about its full body (which can be several screens long)."""
    if raw is None or not str(raw).strip():
        return None
    text = " ".join(str(raw).split())
    return f"свой текст («{text[:60]}…»)" if len(text) > 60 else f"свой текст («{text}»)"


# Each entry: key + label + what it does + how to read its value + where to change it.
# `values` maps a stored value to a human phrase; missing/unknown values fall through to
# `default` (shown with the «по умолчанию» marker) or to _guide_text_value for text settings.
SETTINGS_GUIDE_SECTIONS = [
    (
        "📝 Приём заявок",
        "Какая анкета показывается и нужно ли одобрение менеджера.",
        [
            {
                "key": "registration_mode",
                "label": "Форма регистрации",
                "what": "Краткая — отдельная настраиваемая анкета (акция) со своей вкладкой "
                        "Google-таблицы. Полная — обычная анкета, свой лист. Набор вопросов "
                        "краткой формы настраивается отдельно (⚙️ Настройки → «📋 Вопросы "
                        "регистрации» → «⚡ Краткая»), пресет «⚡ Акция: 6 вопросов» включает "
                        "ФИО, телефон, ВК, город, образование, курс.",
                "values": {"short": "⚡ краткая (акционная анкета)", "full": "📋 полная анкета"},
                "default": "short",
                "where": "⚙️ Настройки → «📝 Регистрация»",
            },
            {
                "key": "short_sheet_tab",
                "label": "Вкладка для краткой формы",
                "what": "Куда падают заявки краткой (акционной) формы — отдельная вкладка "
                        "Google-таблицы, основной лист не трогает.",
                "default": "Краткая",
                "where": "⚙️ Настройки → «📄 Вкладки таблицы»",
            },
            {
                "key": "short_approval",
                "label": "Одобрение для краткой формы",
                "what": "Авто — участник сразу попадает в меню. Вручную — ждёт менеджера.",
                "values": {"auto": "автоматически", "manual": "вручную (через «📋 Заявки»)"},
                "default": "auto",
                "where": "⚙️ Настройки → «✅ Модерация (краткая форма)»",
            },
            {
                "key": "full_approval",
                "label": "Одобрение для полной формы",
                "what": "Авто — участник сразу попадает в меню. Вручную — ждёт менеджера.",
                "values": {"auto": "автоматически", "manual": "вручную (через «📋 Заявки»)"},
                "default": "manual",
                "where": "⚙️ Настройки → «✅ Модерация (полная форма)»",
            },
            {
                "key": "reg_q_resume",
                "label": "Просить резюме",
                "what": "Участник прикладывает PDF/DOCX или пишет резюме текстом.",
                "values": _GUIDE_ONOFF,
                "default": "off",
                "where": "⚙️ Настройки → «📋 Вопросы регистрации» → «📄 Резюме»",
            },
            {
                "key": "reject_text",
                "label": "Текст при отклонении заявки",
                "what": "Что участник прочитает, если менеджер нажал «❌ Отклонить».",
                "default": "стандартный «К сожалению, твоя заявка отклонена»",
                "where": "⚙️ Настройки → «📝 Регистрация» → «🚫 При отклонении»",
            },
        ],
    ),
    (
        "🔔 Уведомления менеджерам о заявках",
        "Как часто бот дёргает вас, когда приходят новые заявки.",
        [
            {
                "key": "pending_notify_mode",
                "label": "Когда сообщать о новой заявке",
                "what": "Сразу — сообщение на каждую заявку. Пачкой — одна сводка «Заявок: N».",
                "values": {"instant": "сразу по каждой заявке", "batched": "пачкой, сводкой"},
                "default": "batched",
                "where": "⚙️ Настройки → «🔔 Уведомление о заявке»",
            },
            {
                "key": "pending_reminder_enabled",
                "label": "Напоминать о нерассмотренных заявках",
                "what": "Бот периодически пишет, сколько заявок ждёт решения.",
                "values": _GUIDE_ONOFF,
                "default": "on",
                "where": "просите разработчика — кнопки нет",
            },
            {
                "key": "pending_reminder_interval",
                "label": "Как часто напоминать",
                "what": "Интервал сводки. 900 = 15 мин, 1800 = 30 мин, 3600 = 1 час.",
                "default": "1800 (30 минут)",
                "unit": " сек.",
                "where": "⚙️ Настройки → «📝 Регистрация» → «🕒 Тайминг батчей заявок»",
            },
        ],
    ),
    (
        "⏰ Напоминания тем, кто бросил анкету",
        "Человек начал регистрацию и не дошёл до конца — бот сам его вернёт.",
        [
            {
                "key": "nudge_enabled",
                "label": "Догонять брошенные анкеты",
                "what": "Одно напоминание на человека, повторно бот не пишет.",
                "values": _GUIDE_ONOFF,
                "default": "on",
                "where": "просите разработчика — кнопки нет",
            },
            {
                "key": "nudge_after_minutes",
                "label": "Через сколько напомнить",
                "what": "Сколько минут молчания участника ждать до напоминания.",
                "default": "120 (2 часа)",
                "unit": " мин.",
                "where": "меняет разработчик по вашей просьбе",
            },
            {
                "key": "nudge_text",
                "label": "Текст напоминания",
                "what": "Что получит человек, бросивший анкету.",
                "default": "стандартный текст",
                "where": "меняет разработчик по вашей просьбе",
            },
        ],
    ),
    (
        "🎯 Предотбор по Google-таблице",
        "Пускать в бота только тех, кто уже отобран вручную (список @username в таблице). "
        "Если таблица недоступна — бот пускает всех и пишет админу, регистрация не встаёт.",
        [
            {
                "key": "preselect_enabled",
                "label": "Предотбор",
                "what": "Проверять @username по вкладке с отобранными на входе.",
                "values": _GUIDE_ONOFF,
                "default": "off",
                "where": "просите разработчика — кнопки нет",
            },
            {
                "key": "preselect_tab",
                "label": "Вкладка со списком отобранных",
                "what": "Имя вкладки в вашей Google-таблице, где лежат @username.",
                "default": "Отобранные",
                "where": "⚙️ Настройки → «📄 Вкладки таблицы»",
            },
            {
                "key": "preselect_fail_text",
                "label": "Текст не прошедшим отбор",
                "what": "Что увидит человек, которого нет в списке.",
                "default": "«Отбор не пройден.»",
                "where": "меняет разработчик по вашей просьбе",
            },
            {
                "key": "preselect_link",
                "label": "Ссылка не прошедшим отбор",
                "what": "Куда отправить человека, если он не в списке (канал, сайт).",
                "default": "нет",
                "where": "меняет разработчик по вашей просьбе",
            },
        ],
    ),
    (
        "👥 Роли и доступы",
        "Кто, кроме суперадминов из .env, имеет доступ к админке, и что именно каждой роли "
        "можно (подробности — ADMIN_GUIDE.md, §22).",
        [
            {
                "key": "role_reg_manager_enabled",
                "label": "Роль «Менеджер регистраций»",
                "what": "Выключенная роль не даёт прав никому из её носителей, но сами люди "
                        "остаются в списке.",
                "values": _GUIDE_ONOFF,
                "default": "on",
                "where": "⚙️ Настройки → «👥 Роли и доступы»",
            },
            {
                "key": "role_caps_reg_manager",
                "label": "Права роли «Менеджер регистраций»",
                "what": "Список прав, по одному на строке (или через «;»): moderate_reg, "
                        "moderate_receipts, moderate_game, broadcast, settings, stats, checkin.",
                "default": "moderate_reg, moderate_receipts",
                "where": "⚙️ Настройки → «👥 Роли и доступы» → «✏️ Права роли: 🛂 Менеджер регистраций»",
            },
            {
                "key": "role_game_manager_enabled",
                "label": "Роль «Менеджер геймификации»",
                "what": "Выключенная роль не даёт прав никому из её носителей, но сами люди "
                        "остаются в списке.",
                "values": _GUIDE_ONOFF,
                "default": "on",
                "where": "⚙️ Настройки → «👥 Роли и доступы»",
            },
            {
                "key": "role_caps_game_manager",
                "label": "Права роли «Менеджер геймификации»",
                "what": "Список прав, по одному на строке (или через «;»): moderate_reg, "
                        "moderate_receipts, moderate_game, broadcast, settings, stats, checkin.",
                "default": "moderate_game",
                "where": "⚙️ Настройки → «👥 Роли и доступы» → «✏️ Права роли: 🎮 Менеджер геймификации»",
            },
        ],
    ),
]

# Every key the guide needs to read from bot_settings (single source for the DB fetch).
SETTINGS_GUIDE_KEYS = [
    entry["key"] for _, __, entries in SETTINGS_GUIDE_SECTIONS for entry in entries
]

_GUIDE_CHUNK_LIMIT = 3500  # Telegram hard limit is 4096; leave room for HTML tags


def _render_guide_entry(entry: dict, raw) -> str:
    """One settings card: label, what it does, what it is set to now, where to change it."""
    values = entry.get("values") or {}
    if raw is not None and str(raw).strip():
        stored = str(raw).strip()
        if values:
            shown = values.get(stored, stored)
        else:
            unit = entry.get("unit")
            shown = f"{stored}{unit}" if unit and stored.isdigit() else _guide_text_value(raw)
    else:
        # Unset key → show what the bot actually does today, in the same human wording.
        default = entry["default"]
        shown = f"{values.get(default, default)} — по умолчанию"

    return (
        f"<b>{html_module.escape(entry['label'])}</b>\n"
        f"{html_module.escape(entry['what'])}\n"
        f"Сейчас: <b>{html_module.escape(str(shown))}</b>\n"
        f"Где менять: {html_module.escape(entry['where'])}"
    )


def _render_settings_guide(sections: list, current: dict) -> list[str]:
    """Render the guide as a list of Telegram-sized messages (one or more sections each)."""
    blocks = ["📖 <b>Справка: что где настраивается</b>"]
    for title, subtitle, entries in sections:
        block = [f"<b>{html_module.escape(title)}</b>\n{html_module.escape(subtitle)}"]
        block += [_render_guide_entry(e, current.get(e["key"])) for e in entries]
        blocks.append("\n\n".join(block).rstrip())
    blocks.append(
        "Остальное — кнопками в ⚙️ Настройки форума: тексты, даты, фото, вопросы анкеты, "
        "оплата, согласия, Party.\nПодробный гайд — файл ADMIN_GUIDE.md, короткая "
        "версия — ADMIN_CHEATSHEET.md."
    )

    messages, buf = [], ""
    for block in blocks:
        candidate = f"{buf}\n\n{block}" if buf else block
        if len(candidate) > _GUIDE_CHUNK_LIMIT and buf:
            messages.append(buf)
            buf = block
        else:
            buf = candidate
    if buf:
        messages.append(buf)
    return messages


async def _send_settings_guide(target: types.Message):
    current = {key: await get_setting(key) for key in SETTINGS_GUIDE_KEYS}
    for chunk in _render_settings_guide(SETTINGS_GUIDE_SECTIONS, current):
        await target.answer(chunk, parse_mode="HTML")


@router.message(Command("settings_guide"))
async def cmd_settings_guide(message: types.Message):
    await _send_settings_guide(message)


@router.callback_query(F.data == "admin_settings_guide")
async def show_admin_settings_guide(callback: types.CallbackQuery):
    await _send_settings_guide(callback.message)
    await callback.answer()


# ── Phase 8 (ROLE-02, D-18): роли и доступы ─────────────────────────────────────────────
# Bespoke settings sub-screen (analog: show_reg_questions/render_questions_text/
# build_questions_keyboard) — reached from a hardcoded row in build_settings_keyboard, exactly
# like «📋 Вопросы регистрации». `staff` isn't a SETTINGS_SCHEMA row (D-11), so its CRUD needs
# real handlers; `role_caps_<role>` list editing rides the existing generic settings_edit flow
# for free (see settings_edit_start's registry-prompt fallback above).

async def render_roles_text() -> str:
    lines = ["👥 <b>Роли и доступы</b>", ""]
    for role, meta in ROLES.items():
        enabled = await get_setting_typed(role_enabled_key(role))
        state_label = "✅ Вкл" if enabled == "on" else "❌ Выкл"
        lines.append(f"{meta['label']}: <b>{state_label}</b>")
        # _known_caps отбрасывает сентинел пустого набора и мусор от старого текстового ввода —
        # показывать «—» надо ровно тогда, когда реальных прав нет.
        caps = _known_caps(await get_setting_typed(role_caps_key(role)))
        cap_text = ", ".join(CAP_LABELS.get(c, c) for c in caps) or "—"
        lines.append(f"　Права: {cap_text}")

    lines.append("")
    lines.append("<b>Люди</b>")
    staff = await list_staff()
    if not staff:
        lines.append("<i>Пока никто не назначен.</i>")
    show_city = await cities_module_on()  # Phase 09.1 (C, ROLE-03)
    for row in staff:
        tid = row["telegram_id"]
        role_label = ROLES.get(row["role"], {}).get("label", row["role"])
        # Manager isn't necessarily a registered delegate (Task 1 read_first note) — fall back
        # to the bare id when `users` has no matching row.
        user = await get_user(tid)
        name = (user.get("full_name") or user.get("username")) if user else None
        name = html_module.escape(str(name or tid))
        line = f"• {name} — {role_label} (добавил {row.get('added_by')}, {row.get('added_at')})"
        if show_city:
            city = row.get("city")
            city_text = await city_label(city) if city else "🌍 Все города"
            line += f" · 🏙 {city_text}"
        lines.append(line)

    lines.append("")
    admins_text = ", ".join(str(a) for a in config.ADMIN_IDS)
    lines.append(f"<i>Суперадмины из .env ({admins_text}) имеют все права всегда и не снимаются из бота.</i>")
    lines.append("⚠️ Право «⚙️ Настройки» включает управление ролями — выдавайте его как равнозначное админскому.")
    return "\n".join(lines)


async def build_roles_keyboard(viewer_id: int | None = None) -> InlineKeyboardMarkup:
    """`viewer_id=None` (default, back-compat for existing call sites/tests) = always show the
    🏙 city-edit button. WR-02 (09.1-REVIEW.md): the real enforcement lives in the
    `roles_city_start`/`roles_city_pick` handler gates below — this is only UX (CLAUDE.md: не
    показывать кнопку, которая заведомо откажет)."""
    buttons = []
    for role, meta in ROLES.items():
        enabled = await get_setting_typed(role_enabled_key(role))
        toggle_text = (
            f"{meta['label']}: ✅ Вкл → ❌ Выкл" if enabled == "on"
            else f"{meta['label']}: ❌ Выкл → ✅ Вкл"
        )
        buttons.append([InlineKeyboardButton(text=toggle_text, callback_data=f"roles_toggle:{role}")])
        buttons.append([InlineKeyboardButton(
            text=f"✏️ Права роли: {meta['label']}",
            callback_data=f"roles_caps:{role}",
        )])

    show_city = await cities_module_on()  # Phase 09.1 (C, ROLE-03)
    for row in await list_staff():
        tid = row["telegram_id"]
        role = row["role"]
        role_label = ROLES.get(role, {}).get("label", role)
        user = await get_user(tid)
        name = (user.get("full_name") or user.get("username")) if user else None
        name = str(name or tid)
        row_buttons = [InlineKeyboardButton(
            text=f"➖ {name} — {role_label}", callback_data=f"roles_del:{tid}:{role}",
        )]
        if show_city and (viewer_id is None or viewer_id in config.ADMIN_IDS):
            city = row.get("city")
            city_text = await city_label(city) if city else "🌍 Все города"
            row_buttons.append(InlineKeyboardButton(
                text=f"🏙 {city_text}", callback_data=f"roles_city:{tid}",
            ))
        buttons.append(row_buttons)

    buttons.append([InlineKeyboardButton(text="➕ Добавить менеджера", callback_data="roles_add")])
    buttons.append([InlineKeyboardButton(text="← Назад к настройкам", callback_data="admin_settings")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


@router.callback_query(F.data == "admin_roles")
async def show_roles(callback: types.CallbackQuery):
    text = await render_roles_text()
    kb = await build_roles_keyboard(callback.from_user.id)
    await callback.message.edit_text(text, parse_mode="HTML", reply_markup=kb)
    await callback.answer()


@router.callback_query(F.data.startswith("roles_toggle:"))
async def toggle_role_enabled(callback: types.CallbackQuery):
    role = callback.data.split(":", 1)[1]
    if role not in ROLES:
        await callback.answer("Неизвестная роль", show_alert=True)
        return

    # Same body as _toggle_module_setting, but redraws the ROLES screen, not admin_settings.
    key = role_enabled_key(role)
    current = await get_setting_typed(key)
    new_val = "off" if current == "on" else "on"
    await set_setting(key, new_val)
    label = "✅ Вкл" if new_val == "on" else "❌ Выкл"
    await callback.answer(f"{ROLES[role]['label']}: {label}", show_alert=True)

    text = await render_roles_text()
    kb = await build_roles_keyboard(callback.from_user.id)
    await callback.message.edit_text(text, parse_mode="HTML", reply_markup=kb)


# ── Права роли: экран с чекбоксами вместо ввода кодов текстом (quick 260813) ───────────────
#
# Раньше «✏️ Права роли» вела в generic settings_edit: менеджеру показывали подсказку
# «moderate_reg, moderate_receipts, moderate_game, broadcast, settings, stats, checkin» и
# просили НАБРАТЬ нужные коды через «;». Это ровно тот случай, который запрещает главное
# правило проекта (CLAUDE.md): настройка из фиксированного набора обязана быть кнопками, а
# кодовые значения человеку показывать нельзя.
#
# Хранение не меняется: тот же ключ role_caps_<role>, тот же type:"list" в SETTINGS_SCHEMA —
# запись идёт «по одному коду на строке», что _parse_setting уже понимает. Значит откат к
# старому экрану не требует миграции данных.
#
# Пустой набор пишется СЕНТИНЕЛОМ, а не пустой строкой: _parse_setting для type:"list"
# возвращает `default` на falsy raw (settings_schema.py), то есть пустая строка молча вернула
# бы роли права по умолчанию — противоположность тому, что нажал менеджер. Сентинел не входит
# в ALL_CAPABILITIES, а resolve_capabilities отбрасывает всё, чего там нет
# (handlers/admin_caps.py) — на выходе честный нулевой набор прав.
_CAPS_EMPTY_SENTINEL = "—"


def _known_caps(raw_caps) -> list[str]:
    """Отфильтровать сентинел и любой мусор, оставшийся от прежнего текстового ввода."""
    return [c for c in (raw_caps or []) if c in ALL_CAPABILITIES]


async def render_role_caps_text(role: str) -> str:
    meta = ROLES[role]
    caps = _known_caps(await get_setting_typed(role_caps_key(role)))
    lines = [
        f"🛂 <b>Права роли: {meta['label']}</b>",
        "",
        "Отметьте, что этой роли можно делать. Нажатие сразу сохраняется.",
        "",
    ]
    if caps:
        lines.append("Сейчас разрешено: " + ", ".join(CAP_LABELS.get(c, c) for c in caps))
    else:
        lines.append("Сейчас роль <b>не может ничего</b> — ни одно право не отмечено.")
    lines.append("")
    lines.append(
        "⚠️ «⚙️ Настройки» — это и управление ролями тоже: у кого есть это право, тот может "
        "выдать права кому угодно, включая себя."
    )
    return "\n".join(lines)


def build_role_caps_keyboard(role: str, caps: list[str]) -> InlineKeyboardMarkup:
    buttons = [
        [InlineKeyboardButton(
            text=("✅ " if cap in caps else "☐ ") + CAP_LABELS.get(cap, cap),
            callback_data=f"roles_cap:{role}:{cap}",
        )]
        for cap in ALL_CAPABILITIES
    ]
    buttons.append([InlineKeyboardButton(text="← К ролям", callback_data="admin_roles")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


async def _show_role_caps(callback: types.CallbackQuery, role: str):
    caps = _known_caps(await get_setting_typed(role_caps_key(role)))
    await callback.message.edit_text(
        await render_role_caps_text(role),
        parse_mode="HTML",
        reply_markup=build_role_caps_keyboard(role, caps),
    )


@router.callback_query(F.data.startswith("roles_caps:"))
async def show_role_caps(callback: types.CallbackQuery):
    role = callback.data.split(":", 1)[1]
    if role not in ROLES:
        await callback.answer("Неизвестная роль", show_alert=True)
        return
    await _show_role_caps(callback, role)
    await callback.answer()


@router.callback_query(F.data.startswith("roles_cap:"))
async def toggle_role_cap(callback: types.CallbackQuery):
    parts = callback.data.split(":")
    if len(parts) != 3:
        await callback.answer("Неизвестная кнопка", show_alert=True)
        return
    _, role, cap = parts
    if role not in ROLES or cap not in ALL_CAPABILITIES:
        await callback.answer("Неизвестное право", show_alert=True)
        return

    caps = _known_caps(await get_setting_typed(role_caps_key(role)))
    if cap in caps:
        caps.remove(cap)
        toast = f"{CAP_LABELS.get(cap, cap)}: снято"
    else:
        # Порядок как в ALL_CAPABILITIES, а не «в порядке нажатий» — чтобы строка прав в
        # render_roles_text не прыгала между перерисовками.
        caps = [c for c in ALL_CAPABILITIES if c == cap or c in caps]
        toast = f"{CAP_LABELS.get(cap, cap)}: разрешено"

    await set_setting(role_caps_key(role), "\n".join(caps) if caps else _CAPS_EMPTY_SENTINEL)
    await callback.answer(toast)
    await _show_role_caps(callback, role)


_STAFF_INPUT_ERROR = (
    "Не понял, кого добавить. Пришлите пересланное сообщение от человека, "
    "@username или числовой id."
)


def _resolve_staff_input(message) -> tuple[int | None, str | None]:
    """Synchronous, DB-free parse of the admin's "who to add" input (CONVENTIONS.md
    `_private`-helper unit-testability idiom — mirrors `_parse_coins_amount`).

    Returns (telegram_id, marker_or_error):
    - (id, None) — resolved directly (forward or numeric id); ready for role assignment.
    - (None, "@username") — needs an async `get_user_by_username` lookup by the caller
      (kept out of this function so it stays sync + DB-free, per CONVENTIONS.md).
    - (None, "<human error text>") — nothing usable; caller shows this text verbatim.
    """
    # Bot API 7.0 (январь 2024) убрал forward_from/forward_date/forward_sender_name из Message
    # и заменил их одним полем forward_origin. Телеграм больше НЕ присылает старые поля — код,
    # который смотрел только на них, видел None у любой пересылки, проваливался в разбор текста
    # и отвечал «Не понял, кого добавить». Тесты этого не ловили: фейковые сообщения ставили
    # forward_from вручную. Поэтому forward_origin проверяется ПЕРВЫМ.
    origin = getattr(message, "forward_origin", None)
    if origin is not None:
        sender = getattr(origin, "sender_user", None)  # MessageOriginUser — единственный
        if sender is not None:                          # вариант, где есть настоящий telegram_id
            return sender.id, None
        # MessageOriginHiddenUser (приватность «скрывать аккаунт при пересылке»),
        # MessageOriginChat / MessageOriginChannel (переслали из чата или канала, а не от
        # человека) — id человека в таких апдейтах отсутствует физически.
        return None, (
            "В этой пересылке нет аккаунта человека — либо у него включена приватность "
            "пересылок, либо сообщение переслано из чата/канала. Попросите его @username "
            "или числовой id."
        )

    # Легаси-ветка для старых апдейтов (и для тестов, писавших forward_from напрямую).
    forwarded = getattr(message, "forward_from", None)
    if forwarded is not None:
        return forwarded.id, None
    if getattr(message, "forward_sender_name", None) or getattr(message, "forward_date", None):
        return None, (
            "У этого человека скрыт аккаунт при пересылке — попросите его @username или "
            "числовой id."
        )

    body = (message.text or "").strip()
    if not body:
        return None, _STAFF_INPUT_ERROR
    if body.startswith("@"):
        return None, body  # marker: caller resolves via get_user_by_username
    if body.isascii() and body.isdigit():  # same unicode-digit guard as _parse_coins_amount
        return int(body), None
    return None, _STAFF_INPUT_ERROR


def _parse_staff_role_callback(data: str) -> tuple[int | None, str | None]:
    """'roles_addrole:900802:reg_manager' -> (900802, 'reg_manager'); malformed -> (None, None).
    Same `_parse_*`-style as `_parse_appr` — never raises on crooked callback_data."""
    parts = data.split(":")
    if len(parts) != 3:
        return None, None
    _, tid_str, role = parts
    if not (tid_str.isascii() and tid_str.isdigit()):
        return None, None
    return int(tid_str), role


# ── Phase 09.1 (C, ROLE-03): manager <-> city binding step ─────────────────────────────────

async def _roles_city_kb(tid: int) -> InlineKeyboardMarkup:
    """«🌍 Все города» + one row per known city (disabled ones marked ❌, same suffix shape as
    `admin_city_switch`), plus a cancel-back-to-roles row. Shown after assigning a role (the
    add-manager flow) and from the per-person «🏙» edit button on the roles screen."""
    buttons = [[InlineKeyboardButton(text="🌍 Все города", callback_data=f"roles_city_pick:{tid}:all")]]
    for c in CITIES:
        code = c["code"]
        label = await city_label(code)
        enabled = await is_city_enabled(code)
        suffix = "" if enabled else " ❌"
        buttons.append([InlineKeyboardButton(
            text=f"{label}{suffix}", callback_data=f"roles_city_pick:{tid}:{code}",
        )])
    buttons.append([InlineKeyboardButton(text="❌ Отмена", callback_data="admin_roles")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


_CITY_BINDING_SUPERADMIN_ONLY_ALERT = "Привязку к городу меняет только суперадмин"


@router.callback_query(F.data.startswith("roles_city:"))
async def roles_city_start(callback: types.CallbackQuery):
    if not await cities_module_on():
        await callback.answer()
        return
    # WR-02 (09.1-REVIEW.md): admin_city_switch already promises the human "менять может
    # суперадмин" -- this handler is the entry point that opens the picker, so the gate goes
    # here first (before rendering a screen that would refuse anyway, per CLAUDE.md).
    is_superadmin = callback.from_user.id in config.ADMIN_IDS
    if not is_superadmin:
        await callback.answer(_CITY_BINDING_SUPERADMIN_ONLY_ALERT, show_alert=True)
        return
    parts = callback.data.split(":")
    if len(parts) != 2 or not (parts[1].isascii() and parts[1].isdigit()):
        await callback.answer("Неизвестная кнопка", show_alert=True)
        return
    tid = int(parts[1])
    text = (
        "🏙 Из какого города этот менеджер? Он будет видеть заявки, чеки и гейму "
        "только своего города."
    )
    await callback.message.edit_text(text, reply_markup=await _roles_city_kb(tid))
    await callback.answer()


@router.callback_query(F.data.startswith("roles_city_pick:"))
async def roles_city_pick(callback: types.CallbackQuery):
    # WR-02 (09.1-REVIEW.md): the guarantee admin_city_switch makes the human ("менять может
    # суперадмин") was enforced only in cities.py's own set_admin_city/admin_selected_city --
    # this handler itself was gated by nothing but the `settings` capability, so any holder of
    # that capability could rebind ANY person's city, including their own, and thereby unlock
    # every other city's queues. Positive-form idiom, byte-identical to admin_city_switch:1927.
    is_superadmin = callback.from_user.id in config.ADMIN_IDS
    if not is_superadmin:
        await callback.answer(_CITY_BINDING_SUPERADMIN_ONLY_ALERT, show_alert=True)
        return
    parts = callback.data.split(":")
    if len(parts) != 3 or not (parts[1].isascii() and parts[1].isdigit()):
        await callback.answer("Неизвестная кнопка", show_alert=True)
        return
    tid = int(parts[1])
    code = parts[2]
    if code == "all":
        ok = await set_staff_city(tid, None)
        toast = "Город: все города"
    elif code in city_codes():
        ok = await set_staff_city(tid, code)
        toast = f"Город: {await city_label(code)}"
    else:
        await callback.answer("Неизвестный город", show_alert=True)
        return

    # WR-02: set_staff_city returns False when there is no staff row for tid (person removed
    # meanwhile / never staff) -- the handler used to discard the return value and toast
    # success anyway, lying to the human about a permission change actually happening.
    if not ok:
        await callback.answer("Этого менеджера уже нет в списке — обновите экран", show_alert=True)
        return

    await callback.answer(toast, show_alert=True)
    text = await render_roles_text()
    kb = await build_roles_keyboard(callback.from_user.id)
    await callback.message.edit_text(text, parse_mode="HTML", reply_markup=kb)


@router.callback_query(F.data == "roles_add")
async def roles_add_start(callback: types.CallbackQuery, state: FSMContext):
    text = (
        "👥 Кого добавить менеджером?\n\n"
        "Пришлите одним сообщением:\n"
        "• пересланное сообщение от этого человека,\n"
        "• его @username,\n"
        "• или числовой telegram id."
    )
    cancel_kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="❌ Отмена", callback_data="admin_roles")],
    ])
    await callback.message.edit_text(text, reply_markup=cancel_kb)
    await state.set_state(StaffAdd.waiting_for_person)
    await callback.answer()


@router.message(StaffAdd.waiting_for_person, F.text.in_({"Отмена", "/cancel"}))
async def roles_add_cancel(message: types.Message, state: FSMContext):
    await state.clear()
    text = await render_roles_text()
    kb = await build_roles_keyboard(message.from_user.id)
    await message.answer(text, parse_mode="HTML", reply_markup=kb)


@router.message(StaffAdd.waiting_for_person)
async def roles_add_person(message: types.Message, state: FSMContext):
    telegram_id, marker = _resolve_staff_input(message)
    if telegram_id is None and marker is not None and marker.startswith("@"):
        user = await get_user_by_username(marker)
        if user is None:
            await message.answer(
                f"Пользователь {html_module.escape(marker)} не найден в базе бота — "
                "попросите числовой id."
            )
            return
        telegram_id = user["telegram_id"]
        marker = None

    if telegram_id is None:
        await message.answer(marker or _STAFF_INPUT_ERROR)
        return

    await state.clear()
    user = await get_user(telegram_id)
    display_name = (user.get("full_name") or user.get("username")) if user else None
    display_name = html_module.escape(str(display_name or telegram_id))

    buttons = [
        [InlineKeyboardButton(text=meta["label"], callback_data=f"roles_addrole:{telegram_id}:{role}")]
        for role, meta in ROLES.items()
    ]
    buttons.append([InlineKeyboardButton(text="❌ Отмена", callback_data="admin_roles")])
    await message.answer(
        f"Кого назначить: {display_name}",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons),
    )


@router.callback_query(F.data.startswith("roles_addrole:"))
async def roles_assign(callback: types.CallbackQuery):
    tid, role = _parse_staff_role_callback(callback.data)
    if tid is None or role not in ROLES:
        await callback.answer("Неизвестная роль", show_alert=True)
        return

    created = await add_staff(tid, role, callback.from_user.id)
    await callback.answer("Добавлен" if created else "Уже был в этой роли", show_alert=True)

    # WR-02: same superadmin-only gate as roles_city_start/roles_city_pick -- a non-superadmin
    # settings holder lands straight on the roster instead of a picker that would refuse them.
    is_superadmin = callback.from_user.id in config.ADMIN_IDS
    if is_superadmin and await cities_module_on():  # Phase 09.1 (C, ROLE-03): city step
        text = (
            "🏙 Из какого города этот менеджер? Он будет видеть заявки, чеки и гейму "
            "только своего города."
        )
        await callback.message.edit_text(text, reply_markup=await _roles_city_kb(tid))
        return

    text = await render_roles_text()
    kb = await build_roles_keyboard(callback.from_user.id)
    await callback.message.edit_text(text, parse_mode="HTML", reply_markup=kb)


@router.callback_query(F.data.startswith("roles_del:"))
async def roles_remove(callback: types.CallbackQuery):
    tid, role = _parse_staff_role_callback(callback.data)
    if tid is None or role not in ROLES:
        await callback.answer("Неизвестная роль", show_alert=True)
        return
    if tid in config.ADMIN_IDS:  # D-12: bootstrap superadmin, never revocable from the bot
        await callback.answer("Это суперадмин из .env, снять из бота нельзя", show_alert=True)
        return

    await remove_staff(tid, role)
    text = await render_roles_text()
    kb = await build_roles_keyboard(callback.from_user.id)
    await callback.message.edit_text(text, parse_mode="HTML", reply_markup=kb)


# ── Phase 9 (GAME-01, D-08, wave 2, 09-02): «📋 Задания» screen + creation wizard ───────────
#
# game_manager-only surface (moderate_game, ADMIN_CAPS pre-registered by 09-01). Task list is
# ~10-20 rows a season (D-08 table), no pagination needed here — unlike the tinder-pattern
# moderation queue wave 4 will add. T-09-05: task text is shown to every delegate later
# (wave 3, parse_mode="HTML") — escaped on EVERY render, not just once at creation.

def _game_task_line(t: dict) -> str:
    """Byte-identical row format shared by both the active-tasks screen and the archive
    screen — the ONE place a task row is rendered (Task 1, 14-03: replaces the old
    `_render_game_tasks_text`'s inline loop body)."""
    try:
        deadline = datetime.strptime(t["deadline_at"], "%Y-%m-%d %H:%M:%S").strftime("%d.%m.%Y %H:%M")
    except (TypeError, ValueError):
        deadline = str(t["deadline_at"] or "—")
    preview = html_module.escape(str(t["text"])[:60])
    category = html_module.escape(str(t["category"]))
    return f"«{category}» {preview} · {t['coins']}🪙 · до {deadline}"


async def _game_tasks_screen() -> tuple[str, InlineKeyboardMarkup]:
    """«Функция возвращает (text, kb)» idiom (same shape as this file's other render-helper
    functions) — replaces the old two-function pair (Phase 9's list-text renderer + its
    keyboard builder) so the four
    existing re-render call sites (`show_game_tasks`, `cancel_game_task_create`,
    `game_task_confirm`, `game_task_create_cancel`) can never drift on format.
    Task 1 (14-03, GAME-08).

    Only ACTIVE tasks are listed here — archived tasks live on the separate «🗄 Архив» screen
    (`_game_archive_screen`). Per task: «🗄 В архив» always, «🗑 Удалить» only if the task has
    zero submissions of any status (T-14-11/T-14-12: the SQL-level gate in `delete_task` is the
    real defense, this is only the UX hint of when the button is even offered)."""
    all_tasks = await list_all_tasks()
    active = [t for t in all_tasks if not t.get("archived_at")]
    archived_count = sum(1 for t in all_tasks if t.get("archived_at"))

    buttons: list[list[InlineKeyboardButton]] = []
    hidden_delete = False
    for t in active:
        name = str(t["text"])[:20]
        row = [InlineKeyboardButton(text=f"🗄 В архив: {name}", callback_data=f"gtarchive:{t['id']}")]
        if await count_task_submissions(t["id"]) == 0:
            row.append(InlineKeyboardButton(text="🗑 Удалить", callback_data=f"gtdelete:{t['id']}"))
        else:
            hidden_delete = True
        buttons.append(row)

    if not active:
        text = "Заданий пока нет."
    else:
        lines = ["📋 <b>Задания</b>\n"] + [_game_task_line(t) for t in active]
        if hidden_delete:
            lines.append(
                "\n🗑 У заданий со сдачами удаления нет — по ним уже есть история. Такое "
                "задание можно только убрать в архив."
            )
        if archived_count:
            lines.append(f"\n🗄 В архиве: {archived_count}")
        text = "\n".join(lines)

    buttons.append([InlineKeyboardButton(text="➕ Новое задание", callback_data="gtnew")])
    if archived_count:
        buttons.append([InlineKeyboardButton(
            text=f"🗄 Архив ({archived_count})", callback_data="admin_game_archive",
        )])
    return text, InlineKeyboardMarkup(inline_keyboard=buttons)


async def _game_archive_screen() -> tuple[str, InlineKeyboardMarkup]:
    """«🗄 Архив» screen: archived tasks + «↩️ Вернуть» per row. Return-from-archive is a SAFE
    operation (CONTEXT.md decision A) — no confirm step, unlike archive/delete. Task 1
    (14-03, GAME-08)."""
    all_tasks = await list_all_tasks()
    archived = [t for t in all_tasks if t.get("archived_at")]

    if not archived:
        text = "Архив пуст."
    else:
        lines = ["🗄 <b>Архив</b>\n"]
        for t in archived:
            try:
                archived_at = datetime.strptime(t["archived_at"], "%Y-%m-%d %H:%M:%S").strftime("%d.%m.%Y")
            except (TypeError, ValueError):
                archived_at = str(t["archived_at"] or "—")
            lines.append(f"{_game_task_line(t)} · 🗄 в архиве с {archived_at}")
        text = "\n".join(lines)

    buttons = [
        [InlineKeyboardButton(
            text=f"↩️ Вернуть: {str(t['text'])[:20]}", callback_data=f"gtunarchive:{t['id']}",
        )]
        for t in archived
    ]
    buttons.append([InlineKeyboardButton(text="← К заданиям", callback_data="admin_game_tasks")])
    return text, InlineKeyboardMarkup(inline_keyboard=buttons)


@router.callback_query(F.data == "admin_game_tasks")
async def show_game_tasks(callback: types.CallbackQuery, state: FSMContext):
    text, kb = await _game_tasks_screen()
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
    name = html_module.escape(str(task["text"])[:60])
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
    name = html_module.escape(str(task["text"])[:60])
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
    await callback.message.answer("Введите текст задания:", reply_markup=get_cancel_kb())
    await state.set_state(GameTaskCreate.text)
    await callback.answer()


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


# Human-readable labels for GAME_PROOF_TYPES (D-08/CLAUDE.md «для людей, не для прогеров»):
# the manager taps a labeled button, never types a proof-type code.
_GAME_PROOF_LABELS = {
    "photo": "📷 Скриншот/фото",
    "pdf": "📄 PDF",
    "text": "✍️ Текст",
    "link": "🔗 Ссылка",
}


def _game_task_category_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=cat, callback_data=f"gtcat:{cat}")] for cat in GAME_CATEGORIES
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


def _game_task_confirm_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Создать", callback_data="gtconfirm")],
        [InlineKeyboardButton(text="❌ Отмена", callback_data="gtcancel")],
    ])


def _render_game_task_confirm_card(data: dict) -> str:
    """T-09-05: the task text is shown to every delegate later (wave 3) with
    `parse_mode="HTML"` -- escaped here too, not just once at the eventual delegate render."""
    deadline_raw = data.get("gt_deadline")
    try:
        deadline_display = datetime.strptime(deadline_raw, "%Y-%m-%d %H:%M:%S").strftime("%d.%m.%Y %H:%M")
    except (TypeError, ValueError):
        deadline_display = str(deadline_raw or "—")
    proof_label = _proof_types_label(data.get("gt_proof_type"))
    # Phase 09.1 (B): the "Кому:" line only appears when the city step was actually shown
    # (gt_city_step_shown, set in game_task_proof_done below) -- this function is synchronous
    # and cannot itself call cities_module_on(), so the caller resolves that once and hands
    # the result down via state data, same "resolve once, derive everything from it" shape
    # as _admin_city_view.
    city_line = ""
    if data.get("gt_city_step_shown"):
        city_label_text = data.get("gt_event_city_label") or "🌍 Все города"
        city_line = f"Кому: {html_module.escape(str(city_label_text))}\n"
    return (
        "📋 <b>Проверьте задание</b>\n\n"
        f"Текст: {html_module.escape(str(data.get('gt_text')))}\n"
        f"Категория: {html_module.escape(str(data.get('gt_category')))}\n"
        f"Коины: {data.get('gt_coins')}\n"
        f"Подтверждение: {proof_label}\n"
        f"{city_line}"
        f"Дедлайн: {deadline_display}"
    )


@router.message(GameTaskCreate.text)
async def game_task_text_step(message: types.Message, state: FSMContext):
    text = (message.text or "").strip()
    if not text:
        await message.answer("Текст не может быть пустым. Введите текст задания:")
        return
    await state.update_data(gt_text=text)
    await message.answer("Выберите категорию:", reply_markup=_game_task_category_kb())
    await state.set_state(GameTaskCreate.category)


@router.callback_query(F.data.startswith("gtcat:"))
async def game_task_category_step(callback: types.CallbackQuery, state: FSMContext):
    cat = callback.data.split(":", 1)[1]
    if cat not in GAME_CATEGORIES:
        await callback.answer("Некорректная категория", show_alert=True)
        return
    await state.update_data(gt_category=cat)
    await callback.message.answer("Сколько монет за это задание?", reply_markup=get_cancel_kb())
    await state.set_state(GameTaskCreate.coins)
    await callback.answer()


@router.message(GameTaskCreate.coins)
async def game_task_coins_step(message: types.Message, state: FSMContext):
    value = _parse_positive_int(message.text)
    if value is None:
        await message.answer("Введите положительное целое число монет:")
        return
    await state.update_data(gt_coins=value, gt_proof_types=[])
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
    """Shared by game_task_proof_done (module off) and game_task_city_step (module on) --
    same prompt/state either way, CONTEXT.md B: "выключен → шаг пропущен... ни одной новой
    кнопки"."""
    await target.answer(
        "Дедлайн сдачи? Формат ДД.ММ.ГГГГ ЧЧ:ММ (например 25.08.2026 23:59):",
        reply_markup=get_cancel_kb(),
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
        await message.answer("❌ Это время уже прошло. Введите будущую дату.")
        return
    await state.update_data(gt_deadline=_fmt_dt(when))
    data = await state.get_data()
    await message.answer(
        _render_game_task_confirm_card(data), parse_mode="HTML", reply_markup=_game_task_confirm_kb(),
    )
    await state.set_state(GameTaskCreate.confirm)


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
    lines = [header, "", esc(row.get("task_text")) or "—"]
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
    def _matrix_col_header(t: dict) -> str:
        prefix = "🗄 " if t.get("archived_at") else ""
        return (prefix + (t["text"] or ""))[:40]

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
    def _history_task_label(s: dict) -> str:
        prefix = "🗄 " if s.get("task_archived_at") else ""
        return (prefix + (s.get("task_text") or "-"))[:60]

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
    matrix_tab = await get_setting_typed("game_matrix_tab")
    history_tab = await get_setting_typed("game_history_tab")
    raw_synced_at = await get_setting("game_sheet_last_synced_at")
    sync_phrase = _last_sync_phrase(raw_synced_at, _now_moscow_naive())
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔄 Да, пересобрать вкладки", callback_data="admin_game_sync_sheet_go")],
        [InlineKeyboardButton(text="← Отмена", callback_data="admin_menu")],
    ])
    await callback.message.edit_text(
        "🔄 <b>Пересобрать вкладки геймификации?</b>\n\n"
        f"Заново соберу из базы бота две вкладки: <b>«{html_module.escape(matrix_tab)}»</b> "
        f"(матрица участники × задания) и <b>«{html_module.escape(history_tab)}»</b>.\n\n"
        f"Вкладки уже обновляются сами после каждого задания/решения/правки монет — {sync_phrase}.\n\n"
        "⚠️ Обе вкладки очищаются целиком и заполняются заново. <b>Заметки, которые вы писали "
        "руками прямо в этих листах, пропадут</b> — в базе бота их нет. Остальные вкладки "
        "таблицы не затрагиваются.",
        parse_mode="HTML",
        reply_markup=kb,
    )
    await callback.answer()


async def rebuild_game_sheets() -> tuple[int, int]:
    """Phase 09.1 (D, GAME-07): the ONE place that actually rebuilds the two gamification
    sheet tabs — shared by the "🔄 Да, пересобрать вкладки" button (sync_game_sheets below,
    which calls this directly, bypassing the debounce -- CONTEXT.md D's "пересобрать сейчас"
    escape hatch) and services.game_sync's debounced background resync (registered via
    set_rebuild() at the bottom of this module). No callback/keyboard args -- the button's
    own handler renders the report from the returned pair, the background caller only needs
    the pair to decide whether to warn superadmins."""
    # list_all_tasks() is created_at DESC (moderation-friendly, newest-first); the matrix wants
    # columns oldest-first (left to right) -- reversed by an explicit sort here, not a new
    # db.py accessor, per the plan's own instruction.
    # Phase 14 (14-03, GAME-08): list_all_tasks()/list_all_submissions() are DELIBERATELY
    # unfiltered by archive status -- archived tasks stay in both tabs (marked «🗄 » by the
    # builders above), deleted tasks vanish on their own because they no longer exist in
    # either table. Do NOT add an archived_at filter here.
    tasks = sorted(await list_all_tasks(), key=lambda t: t["created_at"])
    submissions = await list_all_submissions()

    matrix_headers, matrix_rows = _build_game_matrix(tasks, submissions)
    history_headers, history_rows = _build_game_history(submissions)

    # Quick 260815-3hw: tab names resolved from the registry (game_matrix_tab/game_history_tab)
    # instead of the literals "Гейма"/"История сдач" — same defaults, admin-renameable.
    matrix_tab = await get_setting_typed("game_matrix_tab")
    history_tab = await get_setting_typed("game_history_tab")
    matrix_written = await sync_named_worksheet(matrix_tab, matrix_headers, matrix_rows)
    history_written = await sync_named_worksheet(history_tab, history_headers, history_rows)

    # T-091-17: the "обновлено N мин назад" timestamp must never lie about a partially-failed
    # sync -- only written when BOTH tabs actually wrote successfully. game_sheet_last_synced_at
    # is a service key, not a SETTINGS_SCHEMA entry (see sync_game_sheets_confirm's comment).
    if matrix_written >= 0 and history_written >= 0:
        await set_setting("game_sheet_last_synced_at", _now_moscow_naive().strftime("%Y-%m-%d %H:%M:%S"))

    return matrix_written, history_written


@router.callback_query(F.data == "admin_game_sync_sheet_go")
async def sync_game_sheets(callback: types.CallbackQuery):
    await callback.answer("🔄 Синхронизация...")
    logger.info(f"admin={callback.from_user.id} action=game_sync_sheet start")

    matrix_written, history_written = await rebuild_game_sheets()

    matrix_tab = await get_setting_typed("game_matrix_tab")
    history_tab = await get_setting_typed("game_history_tab")
    matrix_report = f"{matrix_written} строк" if matrix_written >= 0 else "⚠️ ошибка синхронизации (см. лог)"
    history_report = f"{history_written} строк" if history_written >= 0 else "⚠️ ошибка синхронизации (см. лог)"

    await callback.message.answer(
        f"✅ {html_module.escape(matrix_tab)}: {matrix_report}.\n"
        f"{html_module.escape(history_tab)}: {history_report}.",
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


# ── ROLE-01 (D-16): «/admin auto-opens the one available section» ──────────────────────────
#
# Placed at file end (not next to `cmd_admin_help`, up near line ~277) because
# `_AUTO_OPEN_SECTIONS` binds real handler function OBJECTS by name at module-load time -- every
# name it references (`show_admin_stats`, `show_applications`, ...) must already exist in this
# module's namespace when this dict literal executes. `cmd_admin_help` itself only needs these
# names to exist by the time it's CALLED (ordinary Python global lookup), not by the time it's
# defined, so its own position in the file is unaffected.

class _MessageAsCallback:
    """D-16: lets `/admin`'s auto-open call an EXISTING callback-query handler (e.g.
    `show_applications`) while only a `Message` is on hand -- avoids copying the body of every
    one of the 8 `_AUTO_OPEN_SECTIONS` handlers. `data` is set exactly once, by the caller
    (`cmd_admin_help`), from a callback_data that already passed `_visible_menu_rows(caps)` on a
    freshly resolved capability set (T-08-24) -- this class itself has no path for arbitrary
    user input to reach `data`."""

    def __init__(self, message: types.Message, data: str):
        self.data = data
        self.from_user = message.from_user
        self.message = _MessageAsCallback._EditProxy(message)

    async def answer(self, text=None, show_alert=False):
        return None  # no real callback_query to acknowledge -- no-op

    class _EditProxy:
        """Proxies the real `Message` for the handler's `callback.message.*` calls: there is no
        message to EDIT yet (this is a fresh `/admin`, not a re-render), so `edit_text` becomes
        a plain `answer` and `edit_reply_markup` is a no-op; everything else (including
        `answer`/`answer_document`) is delegated straight through."""

        def __init__(self, message: types.Message):
            self._message = message

        async def edit_text(self, text, parse_mode=None, reply_markup=None):
            await self._message.answer(text, parse_mode=parse_mode, reply_markup=reply_markup)

        async def edit_reply_markup(self, reply_markup=None):
            return None

        def __getattr__(self, name):
            return getattr(self._message, name)


# Closed whitelist (T-08-25): only rows that open a SCREEN are eligible for auto-open. Action
# rows (`admin_export_csv`/`admin_export_incomplete`/`admin_sync_sheet`/`admin_rebuild_sheet`/
# `admin_dedupe_sheet`) are deliberately absent -- auto-running "♻️ Пересобрать таблицу" off a
# bare `/admin` would be destructive with no confirmation. Value: (handler, needs_state) --
# `needs_state` is a fixed flag per handler's own real signature (no `inspect.signature` probing
# needed, per 08-05-PLAN.md's own guidance).
_AUTO_OPEN_SECTIONS: dict[str, tuple] = {
    "admin_stats": (show_admin_stats, False),
    "admin_monthly_stats": (show_admin_monthly_stats, False),
    "admin_source_stats": (show_admin_source_stats, False),
    "admin_applications": (show_applications, True),
    "admin_receipts": (show_receipts, True),
    "admin_broadcast": (show_admin_broadcast, True),
    "admin_settings": (show_admin_settings, False),
    "admin_settings_guide": (show_admin_settings_guide, False),
}


def _pick_auto_open(rows: list[tuple[str, str]]):
    """Pure D-16 decision, isolated for unit testing without any capability/DB wiring: exactly
    one visible row AND its callback_data is in the closed `_AUTO_OPEN_SECTIONS` whitelist ->
    return that entry; anything else (0 or 2+ rows, or a single ACTION-only row) -> `None`, and
    the caller falls back to the ordinary menu (possibly a one-button one for the action case)."""
    if len(rows) != 1:
        return None
    return _AUTO_OPEN_SECTIONS.get(rows[0][1])
