"""«✅ Готовность к форуму» — реестр проверок «что ещё не настроено» и напоминание менеджерам.

Зачем: 23.10 регистрацию Москвы никто не закрыл — дату закрытия не поставили, и пришло
57 лишних заявок; 14.10 форма делегаций была подключена при выключенных делегациях — 11
автоотказов. Каждое такое «полувключённое» состояние видно только тому, кто знает, куда
смотреть. Здесь бот сам перечисляет, чего не хватает.

Устройство:

- ОДНА декларация `READINESS_ITEMS`. Экран (`handlers/settings/admin_readiness.py`), счётчик в
  корне `/admin` (`pending_count_for_admin`) и ежедневное напоминание (`readiness_remind_job`)
  читают только её — второго списка пунктов нигде нет, новый пункт добавляется одной записью.
- Пункт — `ReadyItem`: `scope` 'city' (зависит от города) или 'global' (одно на весь бот);
  `check(city) -> 'ok' | 'todo' | 'na'`; `fix_callback` — экран, где это исправляется.
  Каждая проверка fail-soft: сбой -> 'na' (сломанная проверка не показывает ложный ⚠️ и не
  роняет `/admin`).
- «🙈 Не нужно для этого события» — сырой ключ `bot_settings`
  `ready_skip__{id}__{город или '_'}` (служебное состояние экрана, как `admin_city__*`, в
  `SETTINGS_SCHEMA` не заводится). Имя ключа живёт только в этом модуле.
- Напоминание: cron-джоба `readiness_remind` в 10:00 МСК; по каждому городу за
  `ready_remind_days` дней до даты форума — список ненастроенного держателям права settings,
  не чаще раза в сутки на город (`ready_reminded__{город или '_'}` = дата отправки). Тихие
  часы не применяются: это сообщение сотрудникам, как «📊 Итоги дня».

Тексты экрана и напоминания — для менеджеров, не для делегатов, поэтому литералы здесь.

Импорт `handlers.access.admin_caps` — ленивый, внутри функций (цикл через пакет `handlers`),
тот же приём, что в `services/daily_digest.py`.
"""
from __future__ import annotations

import html
import inspect
import logging
from dataclasses import dataclass
from typing import Awaitable, Callable

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from database import db as _db
from domain.settings.schema import get_setting_typed
from services.infra.timeutil import msk_now

logger = logging.getLogger(__name__)

JOB_ID = "readiness_remind"
READY_REMIND_HOUR = 10  # МСК; время не настройка — утро, когда менеджер успеет поправить
SCREEN_TITLE = "✅ Готовность к форуму"

OK, TODO, NA, SKIPPED = "ok", "todo", "na", "skipped"


@dataclass(frozen=True)
class ReadyItem:
    id: str
    title: str
    short: str
    scope: str  # 'city' | 'global'
    check: Callable[[str | None], Awaitable[str]]
    fix_callback: str


# ── проверки ──────────────────────────────────────────────────────────────────────────────

async def _check_forum_date(city: str | None) -> str:
    from services.forum.program import own_forum_date
    return TODO if await own_forum_date(city) is None else OK


async def _check_reg_close(city: str | None) -> str:
    if city is None:
        return NA  # модуль городов выключен — закрывать нечего
    from domain.cities import city_reg_close_date
    return TODO if await city_reg_close_date(city) is None else OK


async def _delegation_forms() -> list[dict]:
    """Активные формы, похожие на форму делегаций: подключённая в модуле или с вопросом про вуз
    (правило «полувключённых делегаций»)."""
    from database.ext_forms_db import list_columns, list_forms
    from services.delegations.delegations import delegation_form_id, guess_delegation_questions

    dfid = await delegation_form_id()
    out = []
    for form in await list_forms():
        if form.get("status") != "active":
            continue
        if form.get("id") == dfid:
            out.append(form)
            continue
        cols = await list_columns(form["id"])
        guess = guess_delegation_questions([(c["qkey"], c["label"]) for c in cols])
        if guess.get("university") is not None:
            out.append(form)
    return out


async def _check_delegations(city: str | None) -> str:
    from services.delegations.delegations import is_armed
    try:  # готовое правило алерта «делегации выключены», если оно есть в этой сборке
        from services.delegations.delegations_off_alert import form_delegation_state
    except ImportError:
        form_delegation_state = None
    if form_delegation_state is not None:
        from database.ext_forms_db import list_forms
        for form in await list_forms():
            state = form_delegation_state(form)
            if inspect.isawaitable(state):
                state = await state
            if (state or {}).get("half_on") is True:
                return TODO
        return OK if await is_armed() else NA
    if not await _delegation_forms():
        return NA
    return OK if await is_armed() else TODO


async def _check_scanner(city: str | None) -> str:
    if await get_setting_typed("checkin_qr_enabled") != "on":
        return NA
    from config import config
    from handlers.access.admin_caps import capability_holders
    holders = await capability_holders("checkin", city=city)
    # суперадмины не считаются: пункт про волонтёров у двери
    return OK if any(uid not in config.ADMIN_IDS for uid in holders) else TODO


async def _check_shop(city: str | None) -> str:
    from database import shop_db
    if not await shop_db.is_open():
        return NA
    return OK if await shop_db.list_items(active_only=True) else TODO


READINESS_ITEMS: tuple[ReadyItem, ...] = (
    ReadyItem("forum_date", "🗓 Не задана дата начала форума", "Дата форума",
              "city", _check_forum_date, "settings_edit:forum_date"),
    ReadyItem("reg_close", "🔒 Не задана дата закрытия регистрации города", "Закрытие регистрации",
              "city", _check_reg_close, "settings_edit:city_reg_close_date"),
    ReadyItem("delegations", "🏫 Форма делегаций подключена, а делегации выключены", "Делегации",
              "global", _check_delegations, "admin_delegations"),
    ReadyItem("scanner", "📷 QR на вход включён, а права сканера нет ни у кого", "Сканер",
              "city", _check_scanner, "admin_roles"),
    ReadyItem("shop", "🛍 Магазин открыт, а товаров в нём нет", "Магазин",
              "global", _check_shop, "admin_shop"),
)


def get_item(item_id: str) -> ReadyItem | None:
    """Точное сравнение с реестром — строка из callback_data дальше не идёт."""
    for item in READINESS_ITEMS:
        if item.id == item_id:
            return item
    return None


# ── «не нужно» ────────────────────────────────────────────────────────────────────────────

def skip_key(item: ReadyItem, city: str | None) -> str:
    suffix = "_" if item.scope == "global" or not city else city
    return f"ready_skip__{item.id}__{suffix}"


async def is_skipped(item: ReadyItem, city: str | None) -> bool:
    return (await _db.get_setting(skip_key(item, city))) == "1"


async def set_skipped(item_id: str, city: str | None, on: bool) -> bool:
    """Отметить / снять «не нужно». Неизвестный id -> False, ничего не пишется."""
    item = get_item(item_id)
    if item is None:
        return False
    await _db.set_setting(skip_key(item, city), "1" if on else "")
    return True


# ── статусы и счётчик ─────────────────────────────────────────────────────────────────────

async def item_status(item: ReadyItem, city: str | None) -> str:
    try:
        if await is_skipped(item, city):
            return SKIPPED
        result = await item.check(city)
        return result if result in (OK, TODO, NA) else NA
    except Exception as exc:  # noqa: BLE001 — сломанная проверка не роняет экран
        logger.warning("readiness: проверка %s упала: %s", item.id, type(exc).__name__)
        return NA


async def item_statuses(city: str | None, scope: str | None = None) -> list[tuple[ReadyItem, str]]:
    """Статусы пунктов для города (`scope` — только пункты этой области)."""
    out = []
    for item in READINESS_ITEMS:
        if scope is not None and item.scope != scope:
            continue
        out.append((item, await item_status(item, city)))
    return out


async def pending_count(city: str | None, scope: str | None = None) -> int:
    return sum(1 for _item, st in await item_statuses(city, scope) if st == TODO)


async def pending_count_for_admin(admin_id: int) -> int:
    """N для кнопки в корне: шапка города админа. Модуль выключен / реальный город — пункты
    города + общие; «🌍 Все города» — сумма пунктов города по включённым городам + общие один
    раз. Сбой -> 0 (корень `/admin` не падает из-за счётчика)."""
    try:
        from domain.cities import ALL_CITIES, admin_selected_city, enabled_cities
        city = await admin_selected_city(admin_id)
        if city != ALL_CITIES:
            return await pending_count(city)
        total = await pending_count(None, "global")
        for c in await enabled_cities():
            total += await pending_count(c["code"], "city")
        return total
    except Exception as exc:  # noqa: BLE001
        logger.warning("readiness: счётчик не посчитан: %s", type(exc).__name__)
        return 0


# ── напоминание ───────────────────────────────────────────────────────────────────────────

def _reminded_key(city: str | None) -> str:
    return f"ready_reminded__{city or '_'}"


async def remind_cities() -> list[str | None]:
    """Та же резолюция, что у «📊 Итогов дня»: модуль городов выключен -> [None]."""
    from domain.cities import cities_module_on, enabled_cities
    if not await cities_module_on():
        return [None]
    return [c["code"] for c in await enabled_cities()]


def build_reminder_text(days_left: int, city_title: str | None, items: list[ReadyItem]) -> str:
    when = "сегодня" if days_left == 0 else f"{days_left} дн."
    head = f"⚠️ <b>До форума {when}</b>"
    if city_title:
        head += f" — {html.escape(str(city_title))}"
    lines = [head, f"Не настроено: {len(items)}"]
    lines += [f"• {item.title}" for item in items]
    lines.append("")
    lines.append(f"Откройте «{SCREEN_TITLE}» — там кнопки «🛠 Исправить» и «🙈 Не нужно».")
    return "\n".join(lines)


async def send_city_reminder(bot, city: str | None) -> int:
    """Напоминание по одному городу; число доставленных сообщений."""
    from config import config
    from handlers.access.admin_caps import capability_holders
    from services.forum.program import own_forum_date

    forum_date = await own_forum_date(city)
    if forum_date is None:
        return 0  # её отсутствие само видно пунктом «Дата форума» на экране
    today = msk_now().date()
    days_left = (forum_date.date() - today).days
    window = await get_setting_typed("ready_remind_days")
    try:
        window = int(window)
    except (TypeError, ValueError):
        window = 3
    if not 0 <= days_left <= window:
        return 0
    today_iso = today.isoformat()
    if await _db.get_setting(_reminded_key(city)) == today_iso:
        return 0
    todo = [item for item, st in await item_statuses(city) if st == TODO]
    if not todo:
        return 0

    title = None
    if city:
        from domain.cities import city_label
        try:
            title = await city_label(city)
        except Exception:  # noqa: BLE001
            title = city
    text = build_reminder_text(days_left, title, todo)
    kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(
        text=SCREEN_TITLE, callback_data=f"ready_city:{city}" if city else "ready_list")]])
    recipients = await capability_holders("settings", city=city) or list(config.ADMIN_IDS)
    sent = 0
    for uid in recipients:
        try:
            await bot.send_message(uid, text, parse_mode="HTML", reply_markup=kb)
            sent += 1
        except Exception as exc:  # noqa: BLE001 — один сломанный чат не блокирует остальных
            logger.warning("readiness: не доставлено %s: %s", uid, type(exc).__name__)
    if sent:
        await _db.set_setting(_reminded_key(city), today_iso)
    return sent


async def readiness_remind_job() -> int:
    """Cron-джоба: без аргументов, бот — из `services.scheduler._bot`, тумблер -> ранний выход."""
    from services import scheduler as _sched
    try:
        if await get_setting_typed("ready_remind_enabled") != "on":
            return 0
        total = 0
        for city in await remind_cities():
            total += await send_city_reminder(_sched._bot, city)
        return total
    except Exception as exc:  # noqa: BLE001
        logger.error("readiness_remind_job упала: %s", exc)
        return 0
