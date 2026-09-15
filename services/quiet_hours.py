"""Quick 260904-dq1: «🌙 Тихие часы» — не будить делегата ночью.

Три вещи, которые обязаны оставаться верными при любой правке этого модуля:

1. **Модуль aiogram-free — и на уровне импорта, И на уровне вызова.** Его импортирует и
   ВЫЗЫВАЕТ веб-процесс Mini App (`miniapp/routers/review.py`, `miniapp/routers/applications.py`
   — см. `miniapp/deps.py`: «Модуль aiogram-free»). Всё, что тянет aiogram
   (`services.scheduler`, `services.application_effects`, `services.game_digest` — оно само
   тянет `services.scheduler`) импортируется ЛЕНИВО, внутри функций, которые вызывает ТОЛЬКО
   бот (`flush_due` и её приватные помощники `_rebuild_markup`/`_flush_*_row`) — никогда на
   пути, которым идёт веб-процесс (`window_for_city`/`defer_until`/вся семья
   `send_or_queue_*`/`serialize_markup`/`manager_notice`/`queued_count` намеренно НЕ
   импортируют `game_digest`/`scheduler` даже лениво — см. `_resolve_delegate_city`).
   Клавиатуру веб кладёт в очередь обычным словарём, объекты aiogram собирает `_rebuild_markup`
   уже у бота.
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
# payload {"text": str, "parse_mode": str, "reply_markup": dict | None} — произвольный текст
# (гейма/монеты/напоминания/ответ организаторов). `reply_markup` — сериализованная клавиатура
# (см. `serialize_markup`), None/отсутствует — сообщение без клавиатуры (форма до 16.09).
KIND_TEXT = "text_html"
# payload {"method": "send_photo"|"send_document", "file_id": str, "caption": str | None,
# "parse_mode": str | None} — файл по `file_id` (бонус за регистрацию и подобное). Телеграм
# хранит файл у себя вечно, поэтому в очереди лежит только идентификатор (см. CLAUDE.md:
# «Storing files on disk … Use file_id»).
KIND_MEDIA = "media"
# payload {"from_chat_id": int, "message_id": int, "caption": str | None} — копия чужого
# сообщения (`bot.copy_message`): так уходит не-текстовый ответ организаторов, где менеджер
# прислал голосовое/фото/кружок, а пересылать его «как есть» нельзя (copy, не forward —
# делегат не должен видеть чат менеджеров).
KIND_COPY = "copy"
# payload {"question": str, "options": list[str], "is_anonymous": bool,
# "allows_multiple_answers": bool, "intro_text": str | None, "poll_id": int | None} — нативный
# опрос Telegram. `poll_id` — id строки `polls` в НАШЕЙ базе: по нему `flush_due` допишет
# чекпоинт `poll_messages` (карта «ответ -> наш опрос» и цель для stop_poll), который при
# немедленной отправке пишет `services/polls.py::deliver_poll`.
KIND_POLL = "poll"

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


def serialize_markup(markup) -> dict | None:
    """Клавиатура -> JSON-словарь очереди: `{"type": "inline"|"reply", "data": {...}}`.

    Инвариант 1 докстринга модуля соблюдён: aiogram здесь НЕ импортируется — объект
    сериализует себя сам (`.model_dump()` — утиный вызов по атрибуту, а не импорт), а тип
    определяется по форме дампа (`inline_keyboard` / `keyboard`), а не по `isinstance`.
    Обратную сборку (`model_validate`) делает `_rebuild_markup` — она живёт на стороне бота,
    в хвосте `flush_due`. Уже готовый словарь пропускается как есть (веб-процесс кладёт в
    очередь обычные dict'ы). Всё непонятное -> None: сообщение уйдёт без клавиатуры, но
    уйдёт."""
    if markup is None:
        return None
    if isinstance(markup, dict):
        data = markup
    else:
        dump = getattr(markup, "model_dump", None)
        if dump is None:
            return None
        data = dump(exclude_none=True)
    if not isinstance(data, dict):
        return None
    if "inline_keyboard" in data:
        return {"type": "inline", "data": data}
    if "keyboard" in data:
        return {"type": "reply", "data": data}
    return None


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


async def _send_or_queue(now: datetime, user_id: int, kind: str, payload: dict, sender) -> datetime | None:
    """Общий хвост всей семьи `send_or_queue_*`: `None` — отправлено сейчас, иначе — момент,
    на который доставка отложена. Инвариант 3 докстринга модуля держит `defer_until`: при
    выключенном тумблере она выходит первым же чтением, и `payload` не собирается зря."""
    due = await defer_until(now, user_id)
    if due is None:
        await sender()
        return None
    await enqueue(user_id, kind, payload, due, now)
    return due


async def send_or_queue_text(now: datetime, user_id: int, text: str, *, sender,
                             parse_mode: str = "HTML", reply_markup=None) -> bool:
    """`sender` — асинхронный колбэк без аргументов (бот передаёт `lambda: bot.send_message(...)`,
    веб — свой `telegram_api`-путь). `True` — отправлено сейчас, `False` — положено в очередь.

    `reply_markup` (16.09) — клавиатура, которую обязано нести и отложенное сообщение: после
    подтверждения оплаты делегат получает главное меню, и утром оно должно приехать вместе с
    текстом, а не потеряться. Передаётся объектом aiogram (бот) или словарём (веб) —
    сериализует `serialize_markup`, aiogram сюда не тянется. `sender` по-прежнему отвечает за
    немедленную отправку САМ: клавиатуру в него кладёт вызывающий."""
    return await send_or_queue_text_due(
        now, user_id, text, sender=sender, parse_mode=parse_mode, reply_markup=reply_markup,
    ) is None


async def send_or_queue_text_due(now: datetime, user_id: int, text: str, *, sender,
                                 parse_mode: str = "HTML", reply_markup=None) -> datetime | None:
    """То же, что `send_or_queue_text`, но возвращает МОМЕНТ доставки (`None` — отправлено
    сейчас). Нужен там, где интерфейс показывает человеку «доставим утром в 09:00»: иначе
    вызывающему пришлось бы вторым запросом дёргать `defer_until` ради того же ответа."""
    payload = {"text": text, "parse_mode": parse_mode}
    markup = serialize_markup(reply_markup)
    if markup is not None:
        payload["reply_markup"] = markup
    return await _send_or_queue(now, user_id, KIND_TEXT, payload, sender)


async def send_or_queue_media(now: datetime, user_id: int, *, sender, method: str, file_id: str,
                              caption: str | None = None, parse_mode: str | None = "HTML") -> bool:
    """Файл по `file_id` (`send_photo`/`send_document`). `True` — отправлено сейчас."""
    payload = {"method": method, "file_id": file_id, "caption": caption, "parse_mode": parse_mode}
    return await _send_or_queue(now, user_id, KIND_MEDIA, payload, sender) is None


async def send_or_queue_copy(now: datetime, user_id: int, *, sender, from_chat_id: int,
                             message_id: int, caption: str | None = None) -> bool:
    """Копия сообщения (`bot.copy_message`). `True` — отправлено сейчас."""
    payload = {"from_chat_id": from_chat_id, "message_id": message_id, "caption": caption}
    return await _send_or_queue(now, user_id, KIND_COPY, payload, sender) is None


async def send_or_queue_poll(now: datetime, user_id: int, *, sender, question: str,
                             options: list, is_anonymous: bool, allows_multiple_answers: bool,
                             intro_text: str | None = None, poll_id: int | None = None) -> bool:
    """Нативный опрос Telegram (+ вступление перед ним, если задано). `True` — отправлено
    сейчас. `poll_id` — id строки `polls`: по нему `flush_due` допишет чекпоинт
    `poll_messages` за отложенную доставку."""
    payload = {
        "question": question, "options": list(options),
        "is_anonymous": bool(is_anonymous),
        "allows_multiple_answers": bool(allows_multiple_answers),
        "intro_text": intro_text, "poll_id": poll_id,
    }
    return await _send_or_queue(now, user_id, KIND_POLL, payload, sender) is None


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
                    reply_markup=_rebuild_markup(payload.get("reply_markup")),
                )
                await mark_delayed_notification_sent(row_id, now_str)
            elif kind == KIND_MEDIA:
                await _flush_media_row(row_id, user_id, payload, now_str)
            elif kind == KIND_COPY:
                await _sched._bot.copy_message(
                    chat_id=user_id,
                    from_chat_id=payload.get("from_chat_id"),
                    message_id=payload.get("message_id"),
                    **({"caption": payload["caption"]} if payload.get("caption") else {}),
                )
                await mark_delayed_notification_sent(row_id, now_str)
            elif kind == KIND_POLL:
                await _flush_poll_row(row_id, user_id, payload, now_str)
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


def _rebuild_markup(raw: dict | None):
    """Обратная сторона `serialize_markup` — ТОЛЬКО на стороне бота (инвариант 1 докстринга
    модуля: aiogram импортируется внутри функции, которую зовёт исключительно `flush_due`).
    Мусор/незнакомый тип -> None: сообщение уйдёт без клавиатуры, но уйдёт."""
    if not isinstance(raw, dict) or not raw.get("data"):
        return None
    from aiogram.types import InlineKeyboardMarkup, ReplyKeyboardMarkup

    cls = InlineKeyboardMarkup if raw.get("type") == "inline" else ReplyKeyboardMarkup
    try:
        return cls.model_validate(raw["data"])
    except Exception as e:
        logger.error(f"quiet_hours: не удалось собрать клавиатуру из очереди ({e}) — шлём без неё")
        return None


_MEDIA_METHODS = frozenset({"send_photo", "send_document"})


async def _flush_media_row(row_id: int, user_id: int, payload: dict, now_str: str) -> None:
    """`file_id` -> `bot.send_photo`/`bot.send_document`. Метод — из закрытого набора
    (`_MEDIA_METHODS`), как и `kind` в самой `flush_due`: строка из БД никогда не превращается
    в произвольный вызов атрибута бота."""
    from database.db import mark_delayed_notification_sent
    from services import scheduler as _sched

    method = payload.get("method")
    if method not in _MEDIA_METHODS:
        logger.error(f"quiet_hours: unknown media method={method!r} for row id={row_id} — never executed")
        await mark_delayed_notification_sent(row_id, now_str, error=f"unknown media method: {method}")
        return
    await getattr(_sched._bot, method)(
        user_id, payload.get("file_id"),
        caption=payload.get("caption"), parse_mode=payload.get("parse_mode"),
    )
    await mark_delayed_notification_sent(row_id, now_str)


async def _flush_poll_row(row_id: int, user_id: int, payload: dict, now_str: str) -> None:
    """Вступление (украшение — его сбой опрос не отменяет, как в `polls._send_one`) + сам
    опрос, затем чекпоинт `poll_messages`: без него ответ делегата некуда замапить, а
    `stop_poll` некуда послать. Сбой самого `send_poll` уходит наружу — `flush_due` пометит
    строку ошибкой."""
    from database.db import mark_delayed_notification_sent, record_poll_message
    from services import scheduler as _sched

    poll_id = payload.get("poll_id")
    intro = payload.get("intro_text")
    if intro:
        try:
            await _sched._bot.send_message(user_id, intro)
        except Exception as e:
            logger.warning(f"quiet_hours: вступление к опросу (row id={row_id}) не ушло: {e}")
    try:
        msg = await _sched._bot.send_poll(
            user_id,
            question=payload.get("question"),
            options=list(payload.get("options") or []),
            is_anonymous=bool(payload.get("is_anonymous")),
            allows_multiple_answers=bool(payload.get("allows_multiple_answers")),
        )
    except Exception:
        if poll_id is not None:
            # Как в deliver_poll: недоставленный чат фиксируется, чтобы дошлёт не долбил его снова.
            await record_poll_message(poll_id, user_id, None, None, False)
        raise
    if poll_id is not None:
        await record_poll_message(
            poll_id, user_id,
            getattr(getattr(msg, "poll", None), "id", None), getattr(msg, "message_id", None), True,
        )
    await mark_delayed_notification_sent(row_id, now_str)


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
