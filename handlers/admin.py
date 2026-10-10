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
from domain.settings.schema import SETTINGS_SCHEMA, get_setting_typed  # REG-02/REG-03: registry + typed accessor
from services.infra.ru_plural import points_word  # «1 балл», «5 баллов» в текстах менеджеру
from database.db import (
    get_stats,
    get_all_users_ids,
    get_all_users_dicts,
    export_users_csv,
    get_user,
    get_user_by_username,
    get_reg_started_by_username,  # Phase 33 (задача 2): фоллбэк /find на «только /start»
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
    count_past_season_users,
    # Phase 07.3 (06, RET-04): импорт делегатов прошлого события
    bulk_insert_users_if_absent,
    count_existing_telegram_ids,
)
from aiogram.exceptions import TelegramRetryAfter, TelegramForbiddenError, TelegramBadRequest
from services.sheets.sheets import get_existing_sheet_ids, append_rows_to_sheet, ensure_sheet_header, sync_named_worksheet, dedupe_sheet_by_id, update_status_in_sheet, bulk_update_status_in_sheet, rebuild_main_sheet, REFUSED_UNPINNED_TAB, _reset_sheet_cache, tab_row_count
from services.scheduler import (
    _parse_schedule_dt,
    _fmt_dt,
    _now_moscow_naive,
    schedule_broadcast_job,
    cancel_broadcast_job,
)
from services.access.allowlist import refresh_allowlist, allowlist_size
from services import source_links
from services.infra.background import spawn as _spawn
from services.applications import decision_delivery
from services.game.game_sync import request_resync as _request_game_resync, set_rebuild as _set_game_rebuild
from handlers.states import Broadcast, EditSetting, Approval, ReceiptReview, StaffAdd, GameTaskCreate, GameReview, CoinsManual, CityForm, SeasonReset, SeasonImport, clear_admin_flow_state
from handlers.access.admin_caps import ALL_CAPABILITIES, CAP_LABELS, ROLES, role_caps_key, role_enabled_key, CapabilityMiddleware, required_capability, has_capability, resolve_capabilities, ANY_CAPABILITY, capability_holders, _holds
from keyboards.builders import get_cancel_kb, MENU_BUTTONS, get_main_menu_kb
from handlers.reg.reg_schema import REG_FLOW, REG_DEFAULTS, REG_LABELS, REG_PRESETS, REG_CATEGORIES, SHEET_HEADERS, STATUS_LABELS, _build_sheet_row, active_sheet_headers, set_sheet_schema, _sheet_value_map, approve_user, dropout_step_label, _apply_party_preset, _apply_short_preset, city_row_tab, incomplete_city_batches
from domain.cities import (  # Phase 07.1 (CITY-04): admin city screen; Phase 07.2 (CITY-02): admin city switcher + scoping
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
from handlers.settings.admin_core import (  # Phase 13 (13-04, REFAC-01): shared aggregator-core helpers
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
# handlers/access/admin_caps.py for the map + resolver + the class itself.
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

    # Счётчики — только текущий сезон (`event_season`); сезон не задан — все строки, как раньше.
    # Прошлые сезоны (в т.ч. импортированные делегаты) отдельной строкой ниже, в «Всего» не идут.
    season = (await get_setting("event_season") or "").strip() or None
    total, top_unis = await get_stats(city_scope=city_scope_val, season=season)

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
    text += f"🔁 Повторных: {await get_returning_count(city_scope=city_scope_val, season=season)}\n"
    past_n = await count_past_season_users(season, city_scope=city_scope_val)
    if past_n:
        text += f"Прошлые сезоны: {past_n}\n"

    if own_city_code is not None:
        # D-10 scoped mode: ровно ОДНА строка города (привязка менеджера), без «Итого» — она
        # дублировала бы единственную строку. get_city_counts() остаётся нефильтрованным
        # (небольшой датасет) — коллапс NULL/неизвестного кода в дефолтный город делается
        # здесь же, тем же способом, что и в нессуженной ветке ниже.
        rows = await get_city_counts(season=season)
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
        rows = await get_city_counts(season=season)
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
        from handlers.settings.admin_sections import op_return_keyboard  # ленивый шов
        base = await op_return_keyboard(user_id, callback_data)
    else:
        base = await admin_keyboard_for(user_id)
    if not config.DASHBOARD_PUBLIC_URL:
        return base
    dashboard_row = [InlineKeyboardButton(text="🌐 Открыть дашборд", url=config.DASHBOARD_PUBLIC_URL)]
    rows = [dashboard_row]
    # Запасной вход менеджера: Mini App открывается и в обычном браузере — там экран «Откройте
    # через бота» с входом через Telegram (miniapp/static/js/app.js, /login дашборда). Кнопка
    # есть и при выключенном приложении: оно выключено только для делегатов, не для персонала.
    rows.append([InlineKeyboardButton(
        text="📱 Приложение в браузере", url=config.DASHBOARD_PUBLIC_URL.rstrip("/") + "/app",
    )])
    return InlineKeyboardMarkup(inline_keyboard=rows + base.inline_keyboard)


_ADMIN_HELP_LINES = [
    ("stats", "/stats - Статистика регистраций"),
    ("stats_monthly", "/stats_monthly - Регистрации по месяцам"),
    ("create_link", "/create_link &lt;название&gt; - Создать ссылку с меткой"),
    ("export", "/export - Скачать базу пользователей (CSV)"),
    ("broadcast", "/broadcast - Рассылка сообщения всем"),
    ("find", "/find @username - Найти пользователя по юзернейму"),
    ("coins", "/coins @username +N причина - Начислить/списать баллы"),
    ("scheduled", "/scheduled - Запланированные рассылки"),
    ("refresh_allowlist", "/refresh_allowlist - Обновить список отобранных"),
    ("settings_guide", "/settings_guide - 📖 Справка по всем настройкам бота"),
]


@router.message(Command("admin"))
async def cmd_admin_help(message: types.Message, state: FSMContext):
    await clear_admin_flow_state(state)  # брошенный админский мастер не ловит следующее сообщение
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

    # Только команды, которые этому человеку откроются: маркетологу незачем видеть /broadcast.
    lines = [line for cmd, line in _ADMIN_HELP_LINES if _holds(caps, required_capability(command=cmd))]
    text = "👮‍♂️ <b>Панель администратора</b>\n\nВыберите раздел кнопкой ниже." + (  # 09.10: команды свёрнуты
        "\n\n<blockquote expandable>Команды — для тех, кто привык:\n" + "\n".join(lines) + "</blockquote>" if lines else "")
    await message.answer(text, parse_mode="HTML", reply_markup=await admin_keyboard_for(message.from_user.id))


# Phase 14 (GAME-09) -> 16.09: сама функция переехала в `services/game/coins_notify.py` — ту же
# формулировку теперь зовёт и разборщик outbox'а Mini App (ручные монеты из приложения
# делегату не приходили вовсе). Здесь — реэкспорт под прежним приватным именем: `/coins`
# ниже, `coinsman_confirm` в admin_gamification.py и тесты импортируют его отсюда как раньше.
from services.game.coins_notify import notify_manual_coins as _notify_manual_coins  # noqa: E402


@router.message(Command("coins"))
async def cmd_coins(message: types.Message, bot: Bot):
    args = (message.text or "").split(maxsplit=3)
    # GAME-09: причина обязательна на обоих путях -- «журнал монет должен отвечать на вопрос
    # «кто, кому, за что»» (owner, CONTEXT.md B). Quick path stays for people used to it, but
    # follows the same rule as the button wizard.
    hint = (
        "⚠️ Формат: /coins @username +N причина — причину нужно указать: журнал баллов "
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
        f"🪙 {sign} {abs(amount)} {points_word(abs(amount))} для {safe_username}.\n"
        f"Новый баланс: <b>{balance}</b>.{notify_suffix}",
        parse_mode="HTML",
    )

def _find_email_line(user) -> str:
    """Строка «Email» карточки /find — только когда он есть: прочерк «-» (заглушка финала анкеты
    на событиях без вопроса про почту) и пустое значение не показываем."""
    email = str(user['email'] or '').strip()
    if email in ('', '-'):
        return ""
    return f"Email: {html_module.escape(email)}\n"


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
            f"{_find_email_line(user)}"
            f"Регистрация: {user['registration_date']}"
        )
        from services.applications.delegate_card import ext_forms_card_lines, status_city_season_lines  # 01.10
        text += await status_city_season_lines(user)
        forms_line, has_forms = await ext_forms_card_lines(user['telegram_id'])
        text += forms_line
        # Форум-ночь B1 (идея №10): перевыпуск QR — подтверждение/сама операция живут в
        # handlers/forum/admin_checkin.py (checkin_reissue*), здесь только кнопка на карточке.
        # Phase 33 (delegate-card admin actions): рядом — «Перевести в город», сама операция и
        # подтверждение живут в handlers/cities/admin_city_move.py (citymv_*), здесь тоже только кнопка.
        rows = [
            [InlineKeyboardButton(
                text="🔄 Перевыпустить QR", callback_data=f"checkin_reissue:{user['telegram_id']}",
            )],
            [InlineKeyboardButton(
                text="🏙 Перевести в город", callback_data=f"citymv_start:{user['telegram_id']}",
            )],
        ]
        if has_forms:
            rows.append([InlineKeyboardButton(
                text="📝 Ответы форм", callback_data=f"extf_view:{user['telegram_id']}",
            )])
        # Phase 33: «↩️ Вернуть в ожидание» — видна только для решённой заявки (одобрена/
        # отклонена), для ожидающей возвращать не с чего (services/applications/revert_pending.py
        # REVERTIBLE_STATUSES). Подтверждение и сама операция — handlers/applications/admin_revert_pending.py.
        if user.get("status") in ("approved", "rejected"):
            rows.append([InlineKeyboardButton(
                text="↩️ Вернуть в ожидание", callback_data=f"revertp_start:{user['telegram_id']}",
            )])
        # «📨 Отправить решение заново» — для решённой заявки; если письмо не дошло, причина
        # строкой в карточке (services/applications/decision_delivery.py::failure_line). Шов — handlers/
        # admin_resend_decision.py.
        if user.get("status") in ("approved", "rejected"):
            text += decision_delivery.failure_line(user)
            rows.append([InlineKeyboardButton(
                text="📨 Отправить решение заново", callback_data=f"decresend_start:{user['telegram_id']}",
            )])
        # Phase 33 (задача 2): «🔁 Разрешить повторную подачу» — видна только для отклонённой
        # заявки (services/reg_edit_policy.resubmit_gate — единственный гейт, которому это
        # исключение вообще что-то меняет). Уже активное исключение — строкой в тексте карточки
        # + кнопка «отозвать» вместо кнопки выдачи (не обе разом).
        from services.applications import delegate_overrides
        if user.get("status") == "rejected":
            resubmit_override = await delegate_overrides.active_override(
                user["telegram_id"], delegate_overrides.KIND_RESUBMIT,
            )
            if resubmit_override:
                granted_by_user = await get_user(resubmit_override["granted_by"])
                granted_by_label = html_module.escape(str(
                    (granted_by_user or {}).get("full_name") or resubmit_override["granted_by"]
                ))
                text += (
                    f"\n\n🔁 Разрешена повторная подача — выдал {granted_by_label}, "
                    f"{resubmit_override['granted_at']}"
                )
                rows.append([InlineKeyboardButton(
                    text="🔁 Отозвать разрешение", callback_data=f"resubg_revoke:{user['telegram_id']}",
                )])
            else:
                rows.append([InlineKeyboardButton(
                    text="🔁 Разрешить повторную подачу", callback_data=f"resubg_start:{user['telegram_id']}",
                )])
        # Phase 33 (задача 3): «✏️ Открыть правку после решения» — видна только для одобренной
        # заявки (services/reg_edit_policy.edit_gate гейтит ТОЛЬКО status == "approved", см. её
        # докстринг Р-1/Р-2). Та же пара «строка + кнопка отозвать» / «кнопка выдачи».
        if user.get("status") == "approved":
            edit_override = await delegate_overrides.active_override(
                user["telegram_id"], delegate_overrides.KIND_EDIT,
            )
            if edit_override:
                granted_by_user = await get_user(edit_override["granted_by"])
                granted_by_label = html_module.escape(str(
                    (granted_by_user or {}).get("full_name") or edit_override["granted_by"]
                ))
                text += (
                    f"\n\n✏️ Открыта правка — выдал {granted_by_label}, "
                    f"{edit_override['granted_at']}"
                )
                rows.append([InlineKeyboardButton(
                    text="✏️ Отозвать разрешение", callback_data=f"editg_revoke:{user['telegram_id']}",
                )])
            else:
                rows.append([InlineKeyboardButton(
                    text="✏️ Открыть правку после решения", callback_data=f"editg_start:{user['telegram_id']}",
                )])
        # Phase 33 (задача 1): «🧹 Сбросить зависшую анкету» — видна, только когда есть
        # незавершённый черновик (services/reg_stuck_reset.py::preview_stuck_reset); для
        # одобренной/отклонённой заявки БЕЗ открытой правки черновика нет — кнопка не
        # показывается вовсе (незачем звать экран подтверждения, который тут же откажет).
        from services.reg_stuck_reset import preview_stuck_reset
        if await preview_stuck_reset(user["telegram_id"]) is not None:
            rows.append([InlineKeyboardButton(
                text="🧹 Сбросить зависшую анкету", callback_data=f"regreset_start:{user['telegram_id']}",
            )])
        # Phase 33 (задача 3): «📎 Заменить резюме» — только для поданной анкеты (users-строка
        # уже есть); человека из reg_started (ветка ниже) резюме ещё не касалось вовсе.
        rows.append([InlineKeyboardButton(
            text="📎 Заменить резюме", callback_data=f"resumerep_start:{user['telegram_id']}",
        )])
        kb = InlineKeyboardMarkup(inline_keyboard=rows)
        await message.answer(text, parse_mode="HTML", reply_markup=kb)
        return

    # Phase 33 (задача 2): фоллбэк на reg_started — человек нажал /start, но анкету не подал
    # (users_row_only_on_submit, память проекта), поэтому его не было в users, но он всё
    # равно существует в базе бота. `services/access/person_search.py` это уже умеет для мастера
    # выдачи ролей (`handlers/access/admin_roles.py::roles_add_person`) — здесь та же фактическая
    # проверка, только напрямую по username (без части ФИО — /find сам всегда искал только
    # @username).
    started = await get_reg_started_by_username(username)
    if started:
        city_code = started.get("event_city")
        city_text = await city_label(city_code) if city_code else "-"
        text = (
            f"👤 <b>Пользователь найден (анкету не подавал(а)):</b>\n"
            f"ID: <code>{started['telegram_id']}</code>\n"
            f"Username: {html_module.escape(str(started.get('username') or ''))}\n"
            f"Начал(а) регистрацию: {started.get('started_at') or '-'}\n"
            f"Город (по анкете): {html_module.escape(str(city_text))}\n\n"
            "<i>Нажимал(а) /start, анкету пока не подавал(а) — записи делегата в базе нет.</i>"
        )
        # Ревью part2: «roles_addfor:*» требует `settings` (ADMIN_CAPS) — модератор без этого
        # права раньше видел кнопку, которая в ответ на тап отказывала бы (CapabilityMiddleware
        # молча съедает нажатие «не туда»); показываем кнопку только тем, кто реально может ею
        # воспользоваться (CLAUDE.md: ошибка объясняет, что делать — лучший вариант «объяснения»
        # тут просто не показать бесполезную кнопку).
        rows = []
        if await has_capability(message.from_user.id, "settings"):
            rows.append([InlineKeyboardButton(
                text="👥 Выдать роль", callback_data=f"roles_addfor:{started['telegram_id']}",
            )])
        from services.reg_stuck_reset import preview_stuck_reset
        if await preview_stuck_reset(started["telegram_id"]) is not None:
            rows.append([InlineKeyboardButton(
                text="🧹 Сбросить зависшую анкету", callback_data=f"regreset_start:{started['telegram_id']}",
            )])
        kb = InlineKeyboardMarkup(inline_keyboard=rows)
        await message.answer(text, parse_mode="HTML", reply_markup=kb)
        return

    await message.answer(f"❌ Пользователь {username} не найден в базе данных.")


@router.message(Command("create_link"))
async def cmd_create_link(message: types.Message, bot: Bot):
    args = message.text.split(maxsplit=1)
    if len(args) < 2 or not args[1].strip():
        await message.answer("⚠️ Используйте формат: /create_link &lt;название&gt;\nПример: /create_link vk_poster", parse_mode="HTML")
        return
    tag = source_links.clean_tag(args[1])  # проверка и тексты — services/source_links.py
    if not source_links.is_valid_tag(tag):
        await message.answer(source_links.bad_tag_text(args[1], "/create_link vk_poster"), parse_mode="HTML")
        return
    link = source_links.build_link((await bot.get_me()).username, tag)
    await message.answer(source_links.link_reply_text(tag, link), parse_mode="HTML")


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


async def _deliver_question_reply(message: types.Message, bot: Bot, user_id: int, admin_name: str,
                                  *, on_dispatched=None, on_part_sent=None, question_ref=None):
    """Shared delivery: send the reply (text or a copy of the admin's message) to the
    delegate, ack the replying admin, and fan out «who answered» to other moderate_reg
    holders. Raises on delivery failure -- callers decide what happens to a claim, if any.

    16.09 («все уведомления делегатам подходят под правило тихого часа»): ответ организаторов
    — такое же уведомление, как решение по заявке. Текстовый идёт в очередь kind text_html,
    не-текстовый (голосовое/фото/кружок менеджера) — kind copy: утром бот скопирует ТО ЖЕ
    сообщение из чата менеджеров (`copy_message`, не forward — делегат не должен видеть чат).
    Менеджер в ответ получает приписку `manager_notice` — «отправлено» без неё было бы
    полуправдой.

    `on_dispatched`/`on_part_sent`/`question_ref` (10.10) — см. handlers/comms/admin_question_delivery.py."""
    from services import questions as questions_service, quiet_hours
    from services.scheduler import _now_moscow_naive
    now = _now_moscow_naive()
    if message.text:
        reply_text = f"{await questions_service.org_reply_header_html(user_id)}\n\n{message.html_text}"
        await quiet_hours.send_or_queue_text(
            now, user_id, reply_text,
            sender=lambda: bot.send_message(user_id, reply_text, parse_mode="HTML"),
            question_ref=question_ref,
        )
    else:
        header = await questions_service.org_reply_header_html(user_id)
        await quiet_hours.send_or_queue_text(
            now, user_id, header,
            sender=lambda: bot.send_message(user_id, header, parse_mode="HTML"),
            question_ref=question_ref and {**question_ref, "question_part": "header"},
        )
        if on_part_sent is not None:
            await on_part_sent()
        await quiet_hours.send_or_queue_copy(
            now, user_id, sender=lambda: message.send_copy(user_id),
            from_chat_id=message.chat.id, message_id=message.message_id,
            question_ref=question_ref,
        )
    if on_dispatched is not None:
        await on_dispatched()
    notice = await quiet_hours.manager_notice(now, user_id)
    await message.reply(
        f"✅ Ответ отправлен пользователю. {notice}" if notice
        else "✅ Ответ отправлен пользователю."
    )
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


from handlers.comms.admin_question_delivery import _attempt_question_delivery  # noqa: E402


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
    from handlers.settings.admin_sections import op_return_keyboard  # ленивый шов (цикл на уровне модуля)
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

    from handlers.settings.admin_sections import op_return_keyboard  # ленивый шов
    await callback.message.edit_text(text, parse_mode="HTML", reply_markup=await op_return_keyboard(callback.from_user.id, callback.data))
    await callback.answer()


# T-08-33 (quick task), part D: claimed-but-never-delivered delegate questions.
#
# Quick 260904-2cj: экран-однострочник поглощён журналом «❓ Вопросы делегатов»
# (handlers/comms/admin_questions.py) — видит все три статуса, не только «в работе», и умеет
# отвечать прямо со страницы. Кнопки «🔒 Залипшие вопросы» на главном экране больше нет
# (см. handlers/settings/admin_core.py::_ADMIN_MENU_ROWS), но этот callback остаётся жить: клавиатуры,
# отправленные ДО этого квика, лежат в чатах менеджеров вечно и должны продолжать работать.
# Имя функции и декоратор НЕ трогаем — они зафиксированы золотым снимком
# `tests/test_refac_snapshot_260816.py`.
@router.callback_query(F.data == "admin_stuck_questions")
async def show_stuck_questions(callback: types.CallbackQuery):
    from handlers.comms.admin_questions import render_questions_screen  # ленивый шов
    text, kb = await render_questions_screen(callback.from_user.id, status="in_work")
    await callback.message.edit_text(text, parse_mode="HTML", reply_markup=kb)
    await callback.answer()


# Phase 13 (13-06, REFAC-01): shared-router seam import for the settings seam, inserted AT
# THE BLOCK'S ORIGINAL POSITION (13-05's established idiom) -- settings was the first seam
# after the 307-658 core in original top-to-bottom order, immediately before cities. Also
# re-wires `show_admin_settings` so the `_AUTO_OPEN_SECTIONS` dict (tail of this file) binds
# a real function object at module-load time.
from handlers.settings import admin_settings  # noqa: E402
from handlers.settings.admin_settings import show_admin_settings  # noqa: E402


# Module-size convention split (tests/test_module_size_convention_260816.py): «🔄 Синхронизация»/
# «♻️ Пересобрать таблицу» moved out of admin_settings.py into their own seam file, registered
# on the shared router right after it -- these two handlers were the last ones in the original
# file to rewrite Sheets by tab, so they now register right after everything else that stayed in
# admin_settings.py, instead of their old mid-file position (golden snapshot reorder, reviewed).
from handlers.sheets import admin_sheets  # noqa: E402


from handlers.cities import admin_cities  # noqa: E402


from handlers.comms import admin_broadcasts  # noqa: E402
from handlers.comms.admin_broadcasts import show_admin_broadcast  # noqa: E402


from handlers.regform import admin_reg_config  # noqa: E402


# Module-size convention split (tests/test_module_size_convention_260816.py): the per-city
# question/prompt screens moved out of admin_reg_config.py into their own seam file, registered
# on the SAME shared router right after it. admin_reg_percity.py imports the three
# `_refresh_*_sheet_header` helpers back from admin_reg_config at module level, so this seam
# MUST load after admin_reg_config -- the reorder this causes on the golden snapshot below
# (event-preset/menu-button handlers now register before question/prompt handlers, instead of
# being interleaved as in the original single file) is intentional and reviewed, not a residual
# artifact of the split.
from handlers.regform import admin_reg_percity  # noqa: E402


# Quick 260904-2cj (QJRN-01..04): shared-router seam import for the delegate-questions journal
# screen («❓ Вопросы делегатов») — registers admin_questions/aq:*/aq_answer:*/QuestionAnswer.*
# on the shared router right after the reg-config seam.
from handlers.comms import admin_questions  # noqa: E402


# Quick 260906-8uq (FAQ-01..06): shared-router seam import for the manager FAQ screen
# («❓ Частые вопросы») — registers admin_faq/afaq_*/FaqItem.* right after the questions
# journal seam (golden snapshot: a clean insertion, no reorder of admin_questions handlers).
from handlers.forum import admin_faq  # noqa: E402

# Phase 27 (27-06, LANG-05/LANG-09): shared-router seam import for the manager screen
# «🌐 Английские тексты» — registers admin_i18n/admin_i18n_*/AdminI18nEdit.* right after the
# FAQ seam (golden snapshot: a clean insertion, no reorder of admin_faq handlers).
from handlers.i18n import admin_i18n  # noqa: E402


# Phase 13 (13-06, REFAC-01): shared-router seam import for the moderation seam, inserted AT
# THE BLOCK'S ORIGINAL POSITION -- appr_*/rcpt_* sat immediately after reg-question config and
# before the guide+roles seam in original top-to-bottom order. Also re-wires
# `show_applications`/`show_receipts` so the `_AUTO_OPEN_SECTIONS` dict binds real function
# objects at module-load time.
from handlers.applications import admin_moderation  # noqa: E402
from handlers.applications.admin_moderation import show_applications, show_receipts  # noqa: E402



# Phase 13 (13-04, REFAC-01): shared-router seam import for the guide+roles seam, placed HERE
# (immediately before the tail auto-open cluster, exactly where the guide+roles block itself
# used to sit) rather than in the bottom seam-import list -- `_AUTO_OPEN_SECTIONS` below binds
# `show_admin_settings_guide` as a function OBJECT at module-load time, so the name must already
# be bound in THIS module's namespace before that dict literal executes. This import also
# registers admin_roles.py's handlers on the shared router at exactly the position guide+roles
# occupied in the original (pre-split) file -- immediately before gamification, which the bottom
# `from handlers import admin_gamification` import still reproduces (13-01 snapshot order).
from handlers.access.admin_roles import show_admin_settings_guide  # noqa: E402


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
from handlers.game import admin_gamification  # noqa: E402
# «📊 Опросы»: список/карточка (admin_polls) + мастер (admin_poll_wizard, импортируется из
# хвоста admin_polls — тот же приём, что admin_gamification → admin_game_tasks ниже).
from handlers.comms import admin_polls  # noqa: E402
# Phase 16 (16-03, GAME-UI-03): the manager task-management seam handlers/game/admin_game_tasks.py
# (point-edit card actions, deadline presets, wizard «✏️ Изменить», «👁 Как видит делегат»)
# is imported at the TAIL of admin_gamification.py, not here (16-04): a `from handlers import
# admin_gamification` that runs BEFORE this module (~20 test files do that) re-enters this
# seam list while admin_gamification is still half-initialised -- importing admin_game_tasks
# from here at that moment registered its 15 handlers BEFORE admin_gamification's own
# (cancel_game_task_edit would then lose first-match to game_task_editdesc_step). Chaining the
# import off admin_gamification's last line makes the order identical for every import order.

# Квик 260910-ro7 (DELU-01..08): shared-router seam import for the hidden superadmin command
# «/delete_user» — registers cmd_delete_user/delu_go:*/delu_no on the shared router right
# after the gamification+polls tail (golden snapshot: a clean append, no reorder of anything
# above). Command is intentionally invisible everywhere else — see handlers/access/admin_purge.py.
from handlers.access import admin_purge  # noqa: E402

# Phase 12 (FORUM-CHECKIN.md): shared-router seam import for «✅ Отметки на форуме»
# (handlers/forum/admin_checkin.py) — registers show_admin_checkin/checkin_upload_start/
# checkin_import_file_step/checkin_import_file_invalid/cancel_checkin_import/
# checkin_point_pick in the very tail of admin.router (golden snapshot: a clean append).
from handlers.forum import admin_checkin  # noqa: E402

# Форум-ночь п.8 (идея №19, SOS): shared-router seam import for «🆘 SOS»
# (handlers/forum/admin_sos.py) — registers admin_sos/asos_page/asos_bind_start/asos_bind_cancel/
# asos_bind_step/sos_claim/sos_resolve/admin_reply_to_sos in the very tail of admin.router
# (golden snapshot: a clean append).
from handlers.forum import admin_sos  # noqa: E402

# D-36 (24.09, аудит форумных тумблеров): shared-router seam import for «🎪 Форум: функции»
# (handlers/forum/admin_forum_functions.py) — registers admin_forum_functions_entry/
# admin_forum_functions_city_pick/checkinvol_cfg_screen/checkinvol_toggle_go/
# checkinvol_time_start/cancel_checkinvol_time_edit/checkinvol_time_step in the very tail of
# admin.router (golden snapshot: a clean append, right after admin_sos).
from handlers.forum import admin_forum_functions  # noqa: E402

# D-29 (24.09, «одна кнопка программы у делегата»): shared-router seam import for the
# table/photo view cycle button (handlers/forum/admin_program_view.py) — registers
# prog_view_toggle_go in the very tail of admin.router (golden snapshot: a clean append,
# right after admin_forum_functions). Not in admin_program.py itself (that module is at its
# own size ceiling) and not in admin_forum_functions.py (the button is shared by BOTH
# screens, one render function, not duplicated).
from handlers.forum import admin_program_view  # noqa: E402

# Бэклог чек-ина п.10: shared-router seam import for «📊 Статистика прихода»
# (handlers/forum/admin_checkin_stats.py) — registers checkin_stats_open/checkin_stats_refresh/
# checkin_stats_csv in the very tail of admin.router (golden snapshot: a clean append).
from handlers.forum import admin_checkin_stats  # noqa: E402

# Бэклог чек-ина №25: shared-router seam import for «🚦 Готовность к форуму»
# (handlers/forum/admin_forum_ready.py) — registers forum_ready_open/forum_ready_refresh in the very
# tail of admin.router (golden snapshot: a clean append, right after admin_checkin_stats).
from handlers.forum import admin_forum_ready  # noqa: E402


# Phase 33 (delegate-card admin actions): shared-router seam import for «🏙 Перевести в город»
# (handlers/cities/admin_city_move.py) — registers citymove_start/citymove_pick_city/citymove_apply/
# citymove_cancel in the very tail of admin.router (golden snapshot: a clean append, right
# after admin_program_view). Not a forum toggle — no hub row, see that module's docstring.
from handlers.cities import admin_city_move  # noqa: E402

# Бэклог чек-ина №12: shared-router seam import for «📍 Сейчас на площадке»
# (handlers/forum/admin_checkin_floor.py) — registers checkin_floor_open/checkin_floor_refresh in the
# very tail of admin.router (golden snapshot: a clean append, right after admin_forum_ready).
from handlers.forum import admin_checkin_floor  # noqa: E402



# Идея №5 бэклога чек-ина (приглашение волонтёров ссылкой): shared-router seam import for
# «🔗 Пригласить волонтёров» (handlers/forum/admin_volunteer_invite.py) — registers
# volinvite_entry/volinvite_city_pick/volinvite_cfg_screen/volinvite_toggle_go/
# volinvite_new_start/volinv_link_expiry_pick/volunteer_invite_wizard_cancel/
# volinv_link_date_step/volinv_rights_expiry_pick/volinv_rights_date_step/
# volinv_limit_pick_and_create/volinv_revoke_confirm/volinv_revoke_go/volinv_revoke_no/
# volinv_users_list/volinv_remove_user in the very tail of admin.router (golden snapshot: a
# clean append, right after admin_program_view).
from handlers.forum import admin_volunteer_invite  # noqa: E402

# Идея №20 бэклога чек-ина (бюро находок): shared-router seam import for «🧳 Нашли вещь»
# (handlers/forum/admin_lost_found.py) — registers lost_found_new_entry/lostfound_city_pick/
# lost_found_cmd/lost_found_cancel_wizard/lost_found_photo_step/lost_found_photo_invalid/
# lost_found_where_step/lost_found_cancel_preview/lost_found_publish/lostfound_return/
# lostfound_cfg_screen/lostfound_toggle_go in the very tail of admin.router (golden
# snapshot: a clean append, right after admin_volunteer_invite).
from handlers.forum import admin_lost_found  # noqa: E402

# Phase 33 (delegate-card admin actions): shared-router seam import for «↩️ Вернуть в ожидание»
# (handlers/applications/admin_revert_pending.py) — registers revertp_start/revertp_toggle/revertp_apply/
# revertp_cancel in the very tail of admin.router (golden snapshot: a clean append, right
# after admin_lost_found). Not a forum toggle — no hub row, same posture as admin_city_move.
from handlers.applications import admin_revert_pending  # noqa: E402

# Phase 33 (задача 2): shared-router seam import for «🔁 Разрешить повторную подачу»
# (handlers/applications/admin_resubmit_grant.py) — registers resubg_start/resubg_toggle/resubg_apply/
# resubg_cancel/resubg_revoke in the very tail of admin.router (golden snapshot: a clean
# append, right after admin_revert_pending). Not a forum toggle — no hub row.
from handlers.applications import admin_resubmit_grant  # noqa: E402

# Phase 33 (задача 3): shared-router seam import for «✏️ Открыть правку после решения»
# (handlers/applications/admin_edit_grant.py) — registers editg_start/editg_toggle/editg_apply/
# editg_cancel/editg_revoke in the very tail of admin.router (golden snapshot: a clean
# append, right after admin_resubmit_grant). Not a forum toggle — no hub row.
from handlers.applications import admin_edit_grant  # noqa: E402

# Phase 33 (delegate-card admin actions, задача 1): shared-router seam import for «🧹 Сбросить
# зависшую анкету» (handlers/applications/admin_reg_reset.py) — registers regreset_start/regreset_toggle/
# regreset_apply/regreset_cancel in the very tail of admin.router (golden snapshot: a clean
# append, right after admin_edit_grant). Not a forum toggle — no hub row.
from handlers.applications import admin_reg_reset  # noqa: E402

# Phase 33 (delegate-card admin actions, задача 3): shared-router seam import for «📎 Заменить
# резюме» (handlers/applications/admin_resume_replace.py) — registers resumerep_start/resumerep_cancel/
# resumerep_cancel_text/resumerep_receive_file/resumerep_receive_other in the very tail of
# admin.router (golden snapshot: a clean append, right after admin_reg_reset). Not a forum
# toggle — no hub row.
from handlers.applications import admin_resume_replace  # noqa: E402

# Phase 33 (delegate-card admin actions): shared-router seam import for «🔍 Сверить с БД»
# (handlers/sheets/admin_sheet_reconcile.py) — registers admin_sheet_reconcile/sheetrec_csv/
# sheetrec_append_confirm/sheetrec_append_go/sheetrec_status_confirm/sheetrec_status_go in the
# very tail of admin.router (golden snapshot: a clean append, right after admin_resume_replace).
# «📊 Данные» hub row: handlers/settings/admin_core.py right after «♻️ Пересобрать таблицу».
from handlers.sheets import admin_sheet_reconcile  # noqa: E402

# Идея №29 бэклога чек-ина («Твой Юлид в цифрах»): shared-router seam import for the
# forum-stats-card broadcast screen (handlers/forum/admin_forum_stats_card.py) — registers
# forumstats_cfg/forumstats_toggle/forumstats_preview/forumstats_pick/forumstats_send_go in
# the very tail of admin.router (golden snapshot: a clean append, right after
# admin_sheet_reconcile). Hub row: handlers/forum/admin_forum_functions.py, right after «🚌 Перенос
# неявившихся на форум в Москве».
from handlers.forum import admin_forum_stats_card  # noqa: E402

# Квик 260927 (рейтинг чата): shared-router seam import экрана «🏆 Рейтинг чата»
# (handlers/chat/admin_chat_rating.py) — admin_chat_rating/chrate:* и ввод ChatRatingEdit в самом
# хвосте admin.router (golden snapshot: чистое добавление после admin_forum_stats_card).
# Строка раздела: handlers/settings/admin_sections.py, «🔧 Управление», после тумблера учёта чата.
from handlers.chat import admin_chat_rating  # noqa: E402,F401
# Квик 260927: экран «🧹 Служебные сообщения в чате» (handlers/chat/admin_chat_cleanup.py) — сразу
# после admin_chat_rating (golden append: admin_chat_cleanup/chclean:* и ввод ChatCleanupEdit).
from handlers.chat import admin_chat_cleanup  # noqa: E402,F401
# Квик 260927: экран «📣 Публикация рейтинга в чат» (handlers/chat/admin_chat_rating_post.py) —
# golden append после admin_chat_cleanup: chpost:* и ввод ChatRatingPostEdit.
from handlers.chat import admin_chat_rating_post  # noqa: E402,F401
# D-41 (регистрация на месте): экран «📝 Регистрация на месте» (handlers/forum/admin_onsite_reg.py) —
# onsitereg_cfg_screen/onsitereg_toggle_go/onsitereg_qr_send в самом хвосте admin.router
# (golden snapshot: чистое добавление после admin_chat_cleanup). Строка хаба —
# handlers/forum/admin_forum_functions.py, сразу после «🧳 Бюро находок».
from handlers.forum import admin_onsite_reg  # noqa: E402,F401
# Приёмка 03.10: хаб «🎪 Форум: функции» — подтверждение общего тумблера «🎟 Вход по QR» и
# возврат в хаб с экранов, открытых из него (handlers/forum/admin_forum_hub_nav.py). Golden snapshot:
# чистое добавление в хвост admin.router.
from handlers.forum import admin_forum_hub_nav  # noqa: E402,F401
# Роль «📣 Маркетинг (метки)»: экран «🔗 Ссылки с метками» и мастер новой ссылки
# (handlers/applications/admin_source_links.py) — golden snapshot: чистое добавление в хвост admin.router.
from handlers.applications import admin_source_links  # noqa: E402,F401
# Раздел «📝 Внешние формы» (подключение Яндекс/Google форм).
from handlers.ext_forms import admin_ext_forms  # noqa: E402,F401
# Экран «🏫 Делегации» в «📋 Заявки» (handlers/delegations/admin_delegations.py) — golden append в хвост.
from handlers.delegations import admin_delegations  # noqa: E402,F401
# Экран «🕐 Часовой пояс» города (handlers/forum/admin_forum_tz.py) — golden append в хвост.
from handlers.forum import admin_forum_tz  # noqa: E402,F401
# «📥 Перенос баллов из таблицы» в «🎮 Геймификации» (handlers/game/admin_coins_transfer.py) — golden append в хвост.
from handlers.game import admin_coins_transfer  # noqa: E402,F401
# Enum-настройки кнопками в общем редакторе (handlers/settings/admin_settings_enum.py) — golden append в хвост.
from handlers.settings import admin_settings_enum  # noqa: E402,F401
# «👥 Список участников» в «📊 Данные» (handlers/applications/admin_participants.py) — golden append в хвост.
from handlers.applications import admin_participants  # noqa: E402,F401
# Треки, компетенции и запись у сессии (handlers/forum/admin_enroll.py) — golden append в хвост.
from handlers.forum import admin_enroll  # noqa: E402,F401
# Список записей, выгрузка и настройки записи (handlers/forum/admin_enroll_list.py) — golden append в хвост.
from handlers.forum import admin_enroll_list  # noqa: E402,F401
# Тест компетенций: настройки, вопросы, баллы (handlers/forum/admin_quiz.py) — golden append в хвост.
from handlers.forum import admin_quiz  # noqa: E402,F401
# «🖼 Аватар бота» в «🎪 Событие» (handlers/settings/admin_bot_avatar.py) — golden append в хвост.
from handlers.settings import admin_bot_avatar  # noqa: E402,F401

# Переотправка решения одному делегату: shared-router seam import «📨 Отправить решение заново»
# (handlers/applications/admin_resend_decision.py) — decresend_start/decresend_go/decresend_cancel в самом
# хвосте admin.router (golden snapshot: чистая вставка после admin_chat_rating).
from handlers.applications import admin_resend_decision  # noqa: E402,F401
from handlers.settings import admin_settings_search  # noqa: E402,F401  -- «🔎 Найти настройку», golden append в хвост
from handlers.sheets import admin_sheet_target  # noqa: E402,F401 — «🔗 Какая таблица» в «📊 Данные», golden append
from handlers.settings import admin_setup_wizard  # noqa: E402,F401 — «🚀 Первая настройка» в «🔧 Управление», golden append
