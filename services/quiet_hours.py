"""Quick 260904-dq1: «🌙 Тихие часы» — не будить делегата ночью.

Три вещи, которые обязаны оставаться верными при любой правке этого модуля:

1. **Модуль aiogram-free — и на уровне импорта, И на уровне вызова.** Его импортирует и
   ВЫЗЫВАЕТ веб-процесс Mini App (`miniapp/routers/review.py`, `miniapp/routers/applications.py`
   — см. `miniapp/deps.py`: «Модуль aiogram-free»). Всё, что тянет aiogram
   (`services.scheduler`, `services.application_effects`, `services.game_digest` — оно само
   тянет `services.scheduler`) импортируется ЛЕНИВО, внутри функций, которые вызывает ТОЛЬКО
   бот (`flush_due` и её приватные помощники) — никогда на пути, которым идёт веб-процесс
   (`window_for_city`/`defer_until`/`send_or_queue_text`/`manager_notice`/`queued_count`
   намеренно НЕ импортируют `game_digest`/`scheduler` даже лениво — см. `_resolve_delegate_city`).
   `database.db`/`settings_schema`/`cities` — безопасны на уровне модуля (сами aiogram-free).

2. **`now` всегда приходит АРГУМЕНТОМ.** Модуль не заводит свой литерал часового пояса
   (TZFIX-260816: пояс называется в одном месте на процесс). У бота это
   `services.scheduler._now_moscow_naive()`, у веба — `miniapp.timeutil.now_msk_naive()`.

3. **Тумблер выключен -> «слать сразу» РАНЬШЕ любого другого чтения.** Каждая асинхронная
   функция ниже, что решает «отложить или нет», проверяет `quiet_hours_enabled` ПЕРВЫМ
   действием — до резолва города делегата, до чтения часов, до чего угодно ещё. При
   дефолте (выключено) поведение бота обязано остаться прежним байт-в-байт: ни одной лишней
   записи в БД, ни одного лишнего чтения настроек.
"""
from __future__ import annotations

import logging
from datetime import datetime, time, timedelta

from settings_schema import get_setting_typed
from cities import cities_module_on, get_setting_typed_for_city, normalize_city
from database.db import get_user

logger = logging.getLogger(__name__)

# payload {"status": str, "reason": str | None} — решение по заявке (одобрено/отклонено).
KIND_APPLICATION_DECISION = "application_decision"
# payload {"text": str, "parse_mode": str} — произвольный текст (гейма/монеты/напоминания).
KIND_TEXT = "text_html"

# Дедуп-заменой («последнее решение выигрывает») живёт ТОЛЬКО application_decision. Результаты
# проверки заданий и монеты копятся списком (REPLACEABLE_KINDS их не содержит) — схлопывать их
# значило бы потерять уведомление о втором задании, сданном той же ночью.
REPLACEABLE_KINDS = frozenset({KIND_APPLICATION_DECISION})


# ── Чистые функции (тест-поверхность без БД) ────────────────────────────────────────────

def parse_hhmm(raw: str | None) -> time | None:
    """«22:00»/«9:00» -> `time`; мусор/None/пусто -> None."""
    if not raw or not isinstance(raw, str):
        return None
    parts = raw.strip().split(":")
    if len(parts) != 2:
        return None
    try:
        hours, minutes = int(parts[0]), int(parts[1])
    except ValueError:
        return None
    if not (0 <= hours <= 23 and 0 <= minutes <= 59):
        return None
    return time(hours, minutes)


def is_quiet(now: datetime, start: time, end: time) -> bool:
    """Окно `[start, end)`. `start > end` — окно через полночь (22:00-09:00 истинно и в
    23:30, и в 02:00, ложно ровно в 09:00 и в 12:00). `start == end` — окна нет вовсе (не
    «сутки тишины» — иначе случайно сравнявший поля менеджер запер бы все уведомления
    навсегда, T-dq1-05)."""
    if start == end:
        return False
    now_t = now.time()
    if start < end:
        return start <= now_t < end
    return now_t >= start or now_t < end


def next_window_end(now: datetime, start: time, end: time) -> datetime:
    """Ближайший момент окончания окна: сегодня, если `end` ещё впереди относительно `now`,
    иначе завтра. Naive datetime в том же поясе, что `now`."""
    end_today = datetime.combine(now.date(), end)
    if end_today > now:
        return end_today
    return end_today + timedelta(days=1)


# ── Асинхронные ──────────────────────────────────────────────────────────────────────────

async def window_for_city(city_code: str | None) -> tuple[time, time] | None:
    """`None` — окна нет вовсе (тумблер выключен, часы не заданы/сломаны, или `start == end`).
    Ранний выход на тумблере — ни одного лишнего чтения при выключенной фиче."""
    if await get_setting_typed("quiet_hours_enabled") != "on":
        return None
    start_raw = await get_setting_typed_for_city("quiet_hours_start", city_code)
    end_raw = await get_setting_typed_for_city("quiet_hours_end", city_code)
    start = parse_hhmm(start_raw)
    end = parse_hhmm(end_raw)
    if start is None or end is None or start == end:
        return None
    return start, end


async def _resolve_delegate_city(user_id: int) -> str | None:
    """Та же идиома, что `services.game_digest.resolve_submitter_city` («город делегата или
    None»), но ПРОДУБЛИРОВАНА здесь намеренно, а не импортирована: `game_digest.py` тянет
    `services.scheduler` (aiogram) на уровне СВОЕГО модуля, и даже ленивый импорт ВНУТРИ
    функции этого файла заставил бы веб-процесс исполнить тот импорт при первом вызове
    `defer_until`/`manager_notice` из `miniapp/routers/*` — ровно то ребро, которого
    инвариант 1 докстринга модуля запрещает. Модуль городов выключен -> None (глобальные
    значения); ошибка чтения -> None и лог (fail-soft = «слать сразу»)."""
    if not await cities_module_on():
        return None
    try:
        user = await get_user(user_id)
        return normalize_city(user.get("event_city") if user else None)
    except Exception as e:
        logger.error(f"quiet_hours: failed to resolve city for user_id={user_id}: {e}")
        return None


async def defer_until(now: datetime, user_id: int) -> datetime | None:
    """`None` — слать сразу. Иначе — момент конца окна тишины по городу делегата.

    Тумблер выключен -> `None` РАНЬШЕ резолва города (инвариант 3 докстринга модуля) — сам
    резолв города это чтение БД (`database.db.get_user`), которого при выключенной фиче быть
    не должно вовсе."""
    if await get_setting_typed("quiet_hours_enabled") != "on":
        return None
    city_code = await _resolve_delegate_city(user_id)
    window = await window_for_city(city_code)
    if window is None:
        return None
    start, end = window
    if not is_quiet(now, start, end):
        return None
    return next_window_end(now, start, end)


async def enqueue(user_id: int, kind: str, payload: dict, due_at: datetime, now: datetime) -> int:
    """Тонкая обёртка над `database.db.enqueue_delayed_notification`. `replace` — только для
    `REPLACEABLE_KINDS` (см. модульный комментарий у константы)."""
    from database.db import enqueue_delayed_notification
    return await enqueue_delayed_notification(
        user_id, kind, payload,
        due_at.strftime("%Y-%m-%d %H:%M:%S"), now.strftime("%Y-%m-%d %H:%M:%S"),
        replace=kind in REPLACEABLE_KINDS,
    )


async def send_or_queue_text(now: datetime, user_id: int, text: str, *, sender,
                             parse_mode: str = "HTML") -> bool:
    """`sender` — асинхронный колбэк без аргументов (бот передаёт `lambda: bot.send_message(...)`,
    веб — свой `telegram_api`-путь). `True` — отправлено сейчас, `False` — положено в очередь."""
    due = await defer_until(now, user_id)
    if due is None:
        await sender()
        return True
    await enqueue(user_id, KIND_TEXT, {"text": text, "parse_mode": parse_mode}, due, now)
    return False


async def manager_notice(now: datetime, user_id: int) -> str:
    """«» — не в окне (или тумблер выключен). В окне — текст ключа
    `quiet_hours_manager_notice_text` с `{time}`, заменённым на конец окна (`.replace`, не
    `.format` — менеджер может оставить в шаблоне лишнюю фигурную скобку)."""
    due = await defer_until(now, user_id)
    if due is None:
        return ""
    template = await get_setting_typed("quiet_hours_manager_notice_text") or ""
    return template.replace("{time}", due.strftime("%H:%M"))


async def queued_count() -> int:
    from database.db import count_pending_delayed_notifications
    return await count_pending_delayed_notifications()


async def flush_due(now: datetime) -> int:
    """Цель джобы: забирает строки с `due_at <= now`, закрытый диспетчер по `kind` (форма
    `services/miniapp_outbox.py::_handle_row`) — неизвестный `kind` не исполняется никогда,
    строка помечается ошибкой. Логи несут только `id`/`kind`/`user_id`, никогда payload
    целиком (T-dq1-04/T-19-57). Возвращает число разобранных строк."""
    from database.db import list_due_delayed_notifications, mark_delayed_notification_sent
    from services import scheduler as _sched

    now_str = now.strftime("%Y-%m-%d %H:%M:%S")
    rows = await list_due_delayed_notifications(now_str)
    count = 0
    for row in rows:
        row_id = row["id"]
        kind = row.get("kind")
        user_id = row.get("user_id")
        payload = row.get("payload") or {}
        try:
            if kind == KIND_TEXT:
                await _sched._bot.send_message(
                    user_id, payload.get("text", ""),
                    parse_mode=payload.get("parse_mode", "HTML"),
                )
                await mark_delayed_notification_sent(row_id, now_str)
            elif kind == KIND_APPLICATION_DECISION:
                await _flush_application_decision_row(row_id, user_id, payload, now_str)
            else:
                logger.error(f"quiet_hours: unknown kind={kind!r} for row id={row_id} — marking as error, never executed")
                await mark_delayed_notification_sent(row_id, now_str, error=f"unknown kind: {kind}")
        except Exception as e:
            logger.error(f"quiet_hours: row id={row_id} kind={kind!r} user_id={user_id} failed: {e}")
            await mark_delayed_notification_sent(row_id, now_str, error=str(e))
        count += 1
    return count


async def _flush_application_decision_row(row_id: int, user_id: int, payload: dict, now_str: str) -> None:
    """Task 3 (services/application_effects.py): перечитать `users.status`, сравнить с
    `payload["status"]` — разошлись -> НЕ слать (менеджер передумал ночью); совпал -> доставить
    ровно одно последнее решение."""
    from database.db import get_user, mark_delayed_notification_sent
    from services.application_effects import apply_decision_effects
    from services import scheduler as _sched

    user = await get_user(user_id)
    live_status = (user or {}).get("status")
    wanted_status = payload.get("status")
    if live_status != wanted_status:
        await mark_delayed_notification_sent(
            row_id, now_str,
            error=f"статус изменился на {live_status!r}, уведомление не отправлено",
        )
        return
    await apply_decision_effects(
        _sched._bot, user_id, wanted_status, payload.get("reason"), notify=True, sheet=False,
    )
    await mark_delayed_notification_sent(row_id, now_str)
