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
# The «По городам» block below is a DELIBERATE EXCEPTION to city scoping (07.2-CONTEXT.md) in
# one very specific sense: `_admin_city_scope` — the admin's own UI toggle in the shapka,
# changeable per-session — never filters THIS screen. That part still holds and is regression-
# tested (test_render_stats_text_identical_regardless_of_selected_admin_city).
#
# Phase 15 (D-10, owner decision 22.08) narrows the SAME screen by a DIFFERENT axis: the
# manager's BOUND city (`staff.city`, set once by an admin assigning them, not chosen by the
# viewer). `admin_id=None` (both call sites below always pass a real id; None only remains for
# the pre-Phase-15 test callers) reproduces the exact byte-identical unscoped text. A bound
# manager sees ONE city row (their own) and narrowed totals/ВУЗы; a superadmin
# (`config.ADMIN_IDS`) is NEVER narrowed even if they happen to carry a binding — same D-12
# convention `capability_holders` already applies. Do not resurrect this as an
# `_admin_city_scope` read — that would silently let the shapka toggle leak into this screen.
async def render_stats_text(admin_id: int | None = None) -> str:
    city_scope_val = None
    own_city_code = None
    own_city_label = None
    if admin_id is not None and await cities_module_on():
        # D-12 convention (mirrors capability_holders in admin_caps.py): a superadmin's own
        # screen is never narrowed, even if they happen to also carry a staff.city binding.
        is_superadmin = admin_id in config.ADMIN_IDS
        if not is_superadmin:
            bound_city = await get_staff_city(admin_id)
            if bound_city:
                own_city_code = normalize_city(bound_city)
                city_scope_val = city_scope(bound_city)
                own_city_label = html_module.escape(await city_label(own_city_code))

    total, top_unis = await get_stats(city_scope=city_scope_val)

    header_suffix = f" — {own_city_label}" if own_city_label else ""
    text = (
        f"📊 <b>Статистика{header_suffix}:</b>\n"
        f"Всего регистраций: {total}\n"
        f"🏆 <b>Топ-3 ВУЗа:</b>\n"
    )

    for i, (uni, count) in enumerate(top_unis, 1):
        text += f"{i}. {html_module.escape(str(uni))} — {count}\n"

    # Phase 07.3 (05, RET-03): счётчик повторных делегатов — глобальный (без городского
    # разреза) в НЕсуженном режиме; в суженном режиме (D-10) считается по тому же city_scope.
    text += f"🔁 Повторных: {await get_returning_count(city_scope=city_scope_val)}\n"

    if own_city_code is not None:
        # D-10 scoped mode: ровно ОДНА строка города (привязка менеджера), без «Итого» — она
        # дублировала бы единственную строку. get_city_counts() остаётся нефильтрованным
        # (небольшой датасет) — коллапс NULL/неизвестного кода в дефолтный город делается
        # здесь же, тем же способом, что и в нессуженной ветке ниже.
        rows = await get_city_counts()
        t = p = a = 0
        for raw_city, cnt, pending, approved in rows:
            if normalize_city(raw_city) == own_city_code:
                t += cnt or 0
                p += pending or 0
                a += approved or 0
        text += "\n🏙 <b>По городам:</b>\n"
        text += f"• {own_city_label} — всего {t}, на модерации {p}, одобрено {a}\n"
        return text

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


# Phase 15 (D-18): kept adjacent to render_stats_text — the ONE keyboard both cmd_stats and
# show_admin_stats attach. Prepends the dashboard-entry button (own row, first) on top of the
# ordinary capability-filtered admin panel whenever a public URL is configured (bootstrap-only,
# config.DASHBOARD_PUBLIC_URL — never a bot_settings key, D-05). No new callback_data is
# introduced (it's a `url=` button), so ADMIN_CAPS needs no new entry.
async def _stats_keyboard_for(user_id: int, callback_data: str | None = None) -> InlineKeyboardMarkup:
    # Ревью фазы 20: экран зовут и кнопкой раздела «📊 Данные», и командой /stats. Кнопка
    # называет себя (callback_data) и получает клавиатуру своего раздела; у команды экрана-
    # источника нет вовсе, поэтому там по-прежнему корень.
    if callback_data:
        from handlers.admin_sections import op_return_keyboard  # ленивый шов
        base = await op_return_keyboard(user_id, callback_data)
    else:
        base = await admin_keyboard_for(user_id)
    if not config.DASHBOARD_PUBLIC_URL:
        return base
    dashboard_row = [InlineKeyboardButton(text="🌐 Открыть дашборд", url=config.DASHBOARD_PUBLIC_URL)]
    return InlineKeyboardMarkup(inline_keyboard=[dashboard_row] + base.inline_keyboard)


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
    admin_id = message.from_user.id
    await message.answer(
        await render_stats_text(admin_id), parse_mode="HTML",
        reply_markup=await _stats_keyboard_for(admin_id),
    )


@router.message(Command("stats_monthly"))
async def cmd_stats_monthly(message: types.Message):
    await message.answer(await render_monthly_stats(), parse_mode="HTML")


@router.callback_query(F.data == "admin_stats")
async def show_admin_stats(callback: types.CallbackQuery):
    admin_id = callback.from_user.id
    text = await render_stats_text(admin_id)
    await callback.message.edit_text(text, parse_mode="HTML", reply_markup=await _stats_keyboard_for(admin_id, callback.data))
    await callback.answer()


@router.callback_query(F.data == "admin_monthly_stats")
async def show_admin_monthly_stats(callback: types.CallbackQuery):
    from handlers.admin_sections import op_return_keyboard  # ленивый шов (цикл на уровне модуля)
    await callback.message.edit_text(await render_monthly_stats(), parse_mode="HTML", reply_markup=await op_return_keyboard(callback.from_user.id, callback.data))
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

    from handlers.admin_sections import op_return_keyboard  # ленивый шов
    await callback.message.edit_text(text, parse_mode="HTML", reply_markup=await op_return_keyboard(callback.from_user.id, callback.data))
    await callback.answer()


# T-08-33 (quick task), part D: claimed-but-never-delivered delegate questions.
#
# Quick 260904-2cj: экран-однострочник поглощён журналом «❓ Вопросы делегатов»
# (handlers/admin_questions.py) — видит все три статуса, не только «в работе», и умеет
# отвечать прямо со страницы. Кнопки «🔒 Залипшие вопросы» на главном экране больше нет
# (см. handlers/admin_core.py::_ADMIN_MENU_ROWS), но этот callback остаётся жить: клавиатуры,
# отправленные ДО этого квика, лежат в чатах менеджеров вечно и должны продолжать работать.
# Имя функции и декоратор НЕ трогаем — они зафиксированы золотым снимком
# `tests/test_refac_snapshot_260816.py`.
@router.callback_query(F.data == "admin_stuck_questions")
async def show_stuck_questions(callback: types.CallbackQuery):
    from handlers.admin_questions import render_questions_screen  # ленивый шов
    text, kb = await render_questions_screen(callback.from_user.id, status="in_work")
    await callback.message.edit_text(text, parse_mode="HTML", reply_markup=kb)
    await callback.answer()


# Phase 13 (13-06, REFAC-01): shared-router seam import for the settings seam, inserted AT
# THE BLOCK'S ORIGINAL POSITION (13-05's established idiom) -- settings was the first seam
# after the 307-658 core in original top-to-bottom order, immediately before cities. Also
# re-wires `show_admin_settings` so the `_AUTO_OPEN_SECTIONS` dict (tail of this file) binds
# a real function object at module-load time.
from handlers import admin_settings  # noqa: E402
from handlers.admin_settings import show_admin_settings  # noqa: E402


from handlers import admin_cities  # noqa: E402


from handlers import admin_broadcasts  # noqa: E402
from handlers.admin_broadcasts import show_admin_broadcast  # noqa: E402


from handlers import admin_reg_config  # noqa: E402


# Quick 260904-2cj (QJRN-01..04): shared-router seam import for the delegate-questions journal
# screen («❓ Вопросы делегатов») — registers admin_questions/aq:*/aq_answer:*/QuestionAnswer.*
# on the shared router right after the reg-config seam.
from handlers import admin_questions  # noqa: E402


# Phase 13 (13-06, REFAC-01): shared-router seam import for the moderation seam, inserted AT
# THE BLOCK'S ORIGINAL POSITION -- appr_*/rcpt_* sat immediately after reg-question config and
# before the guide+roles seam in original top-to-bottom order. Also re-wires
# `show_applications`/`show_receipts` so the `_AUTO_OPEN_SECTIONS` dict binds real function
# objects at module-load time.
from handlers import admin_moderation  # noqa: E402
from handlers.admin_moderation import show_applications, show_receipts  # noqa: E402



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
# «📊 Опросы»: список/карточка (admin_polls) + мастер (admin_poll_wizard, импортируется из
# хвоста admin_polls — тот же приём, что admin_gamification → admin_game_tasks ниже).
from handlers import admin_polls  # noqa: E402
# Phase 16 (16-03, GAME-UI-03): the manager task-management seam handlers/admin_game_tasks.py
# (point-edit card actions, deadline presets, wizard «✏️ Изменить», «👁 Как видит делегат»)
# is imported at the TAIL of admin_gamification.py, not here (16-04): a `from handlers import
# admin_gamification` that runs BEFORE this module (~20 test files do that) re-enters this
# seam list while admin_gamification is still half-initialised -- importing admin_game_tasks
# from here at that moment registered its 15 handlers BEFORE admin_gamification's own
# (cancel_game_task_edit would then lose first-match to game_task_editdesc_step). Chaining the
# import off admin_gamification's last line makes the order identical for every import order.
