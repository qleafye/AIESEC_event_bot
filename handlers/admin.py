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
from handlers.reg_schema import REG_FLOW, REG_DEFAULTS, REG_LABELS, REG_PRESETS, REG_CATEGORIES, SHEET_HEADERS, STATUS_LABELS, _build_sheet_row, active_sheet_headers, set_sheet_schema, _sheet_value_map, approve_user, dropout_step_label, _apply_party_preset, _apply_short_preset, city_row_tab, incomplete_city_batches
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
from handlers.admin_core import (  # Phase 13 (13-04, REFAC-01): shared aggregator-core helpers
    _ADMIN_MENU_ROWS,
    _visible_menu_rows,
    build_admin_keyboard,
    admin_keyboard_for,
    _admin_city_view,
    _admin_city_scope,
    _admin_city_label,
    _card_out_of_scope,
    _OUT_OF_SCOPE_ALERT,
    _submission_out_of_scope,
    _SUBMISSION_OUT_OF_SCOPE_ALERT,
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
    # Quick 260819-gtl (CONTEXT.md decision 8): title/photo wizard step prompts.
    "game_task_title_prompt", "game_task_photo_prompt",
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
    # Phase 13 (13-05): _refresh_short_sheet_header moved to admin_reg_config.py; local import
    # (same idiom _refresh_party_sheet_header/_refresh_short_sheet_header themselves use for
    # handlers.registration) avoids triggering admin_reg_config's own back-import of this
    # module before admin.py has finished defining the names admin_reg_config imports back.
    from handlers.admin_reg_config import _refresh_short_sheet_header
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


from handlers import admin_cities  # noqa: E402


from handlers import admin_broadcasts  # noqa: E402
from handlers.admin_broadcasts import show_admin_broadcast  # noqa: E402


from handlers import admin_reg_config  # noqa: E402



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


# Phase 13 (13-04, REFAC-01): shared-router seam import for the guide+roles seam, placed HERE
# (immediately before the tail auto-open cluster, exactly where the guide+roles block itself
# used to sit) rather than in the bottom seam-import list -- `_AUTO_OPEN_SECTIONS` below binds
# `show_admin_settings_guide` as a function OBJECT at module-load time, so the name must already
# be bound in THIS module's namespace before that dict literal executes. This import also
# registers admin_roles.py's handlers on the shared router at exactly the position guide+roles
# occupied in the original (pre-split) file -- immediately before gamification, which the bottom
# `from handlers import admin_gamification` import still reproduces (13-01 snapshot order).
from handlers.admin_roles import show_admin_settings_guide  # noqa: E402


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


# Phase 13 (13-04, REFAC-01): shared-router seam imports, in ORIGINAL top-to-bottom handler
# order (gamification was last in the file before this split) -- decorating THIS module's
# `router` object, never a second Router() instance. main.py is unaffected: it still includes
# `admin.router` by reference, unaware any handler now physically lives in a seam file.
from handlers import admin_gamification  # noqa: E402
