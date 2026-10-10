"""Форум-ночь п.9 (идея №15 бэклога чек-ина, `.planning/IDEAS-CHECKIN-BACKLOG-260924.md`;
D-24 `.planning/FORUM-CHECKIN.md`): «⭐ Отзыв о сессии одним тапом» — через N минут после конца
сессии делегатам, ОТМЕЧЕННЫМ на ней (итоговая отметка слота, D-20), уходит вопрос с оценкой
1–5 и необязательным комментарием.

Домен вынесен из `handlers/session_feedback.py` по правилу проекта «своего Router() нет — домен
в services/, хендлеры — тонкий шов» (та же форма, что `services/sos.py`/`services/program.py`).

aiogram-free НА УРОВНЕ ИМПОРТА (тот же инвариант, что `services/program.py`/`services/sos.py`)
— `services.scheduler` (тянет aiogram `Bot`) и `services.quiet_hours` (aiogram-free сам, но
зовёт `services.scheduler` лениво только у бота, не у веба) подтягиваются ЛЕНИВО внутри функций,
которые вызывает ТОЛЬКО бот (планирование джоб, сама доставка) — не на пути импорта модуля.

Идемпотентность рассылки — на УРОВНЕ БД (`database.db.create_session_feedback_prompt`,
`UNIQUE(telegram_id, session_id)` + `INSERT OR IGNORE`), не на уровне джобы: повторный тик
джобы (перепланирование при правке сессии, reconcile на рестарте, редкая гонка) не шлёт
делегату второе приглашение — вставка просто возвращает `False`."""
from __future__ import annotations

import logging
from datetime import datetime, timedelta

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from cities import get_setting_typed_for_city
from database.db import (
    create_session_feedback_prompt,
    get_program_session,
    get_session_feedback,
    is_marked_for_session,
    list_marked_telegram_ids_for_session,
    list_session_feedback_comments,
    session_feedback_stats,
    session_feedback_stats_bulk,
    set_session_feedback_comment,
    set_session_feedback_rating,
)
from domain.settings.schema import get_setting_typed

logger = logging.getLogger(__name__)

DEFAULT_DELAY_MINUTES = 10
# Джоба, тикнувшая, когда конец сессии уже больше этого числа часов в прошлом (бот был выключен
# дольше, чем окно отзыва + запас), НЕ досылает приглашение задним числом — форум мог смениться
# днём, делегат уже забыл сессию. Тот же баланс, что у `schedule_task_deadline_reminder`
# (прошедший момент не воскрешается), но с запасом на «реконсиляция после короткого рестарта
# должна досылать», а не молчаливо терять окно.
_CATCHUP_GRACE_HOURS = 12
_RATING_MIN, _RATING_MAX = 1, 5


def feedback_job_id(session_id: int) -> str:
    return f"session_feedback_{session_id}"


async def is_enabled_for_city(city: str | None) -> bool:
    return await get_setting_typed_for_city("session_feedback_enabled", city) == "on"


async def _delay_minutes_for_city(city: str | None) -> int:
    raw = await get_setting_typed_for_city("session_feedback_delay_minutes", city)
    try:
        return int(raw) if raw else DEFAULT_DELAY_MINUTES
    except (TypeError, ValueError):
        return DEFAULT_DELAY_MINUTES


def _session_end_dt(session: dict) -> datetime | None:
    try:
        return datetime.strptime(f"{session['day']} {session['end_time']}", "%Y-%m-%d %H:%M")
    except (KeyError, ValueError):
        return None


async def _run_at_for_session(session: dict) -> datetime | None:
    end_dt = _session_end_dt(session)
    if end_dt is None:
        return None
    delay = await _delay_minutes_for_city(session.get("city"))
    # Время сессии — местное время города, а планировщик живёт по Москве: переводим момент
    # отправки в МСК (Тюмень МСК+2: конец 11:00 местного = 09:00 МСК).
    from services.timeutil import city_offset_hours
    offset = await city_offset_hours(session.get("city"))
    return end_dt + timedelta(minutes=delay) - timedelta(hours=offset)


def cancel_for_session(session_id: int) -> None:
    """Fail-soft снятие джобы (сессия удалена/правка сдвинула время — `schedule_for_session`
    сама переставляет по тому же id, но удаление сессии джобу нигде больше не переставит)."""
    try:
        from services.scheduler import get_scheduler

        get_scheduler().remove_job(feedback_job_id(session_id))
    except Exception:
        pass  # не стояла или уже сработала — оба случая ОК (форма cancel_escalation у SOS)


async def schedule_for_session(session_id: int) -> bool:
    """Ставит/переставляет джобу отзыва сессии — вызывается после ЛЮБОГО создания/правки
    сессии (`handlers/admin_program.py`), идемпотентно (`replace_existing=True`, тот же id
    что при прошлой постановке). Правка, изменившая день/время конца, просто переставляет джобу
    на новый момент — отдельного дифа «что именно изменилось» не считает, пересчёт от нуля
    дешевле дифа и не может разойтись с фактическим состоянием сессии.

    Момент уже далеко в прошлом (`_CATCHUP_GRACE_HOURS`) -> джоба НЕ ставится вовсе (тот же
    баланс, что `schedule_task_deadline_reminder`) — возвращает `False`. Момент в прошлом, но
    в пределах запаса (короткий рестарт бота во время окна отзыва) -> джоба ставится «почти
    сейчас» (through `max(1, ...)`, тот же приём, что `sos.schedule_escalation`) — приглашение
    досылается с опозданием, а не теряется вовсе."""
    session = await get_program_session(session_id)
    if session is None:
        cancel_for_session(session_id)
        return False
    run_at = await _run_at_for_session(session)
    if run_at is None:
        return False
    from services.scheduler import _now_moscow_naive, get_scheduler

    now = _now_moscow_naive()
    if run_at < now - timedelta(hours=_CATCHUP_GRACE_HOURS):
        cancel_for_session(session_id)
        return False
    if run_at <= now:
        run_at = now + timedelta(minutes=1)
    try:
        get_scheduler().add_job(
            deliver_feedback_prompts, "date", run_date=run_at, args=[session_id],
            id=feedback_job_id(session_id), replace_existing=True,
        )
        return True
    except Exception as e:
        logger.warning("session_feedback.schedule_for_session(%s) failed: %s: %s", session_id, type(e).__name__, e)
        return False


async def reconcile_all() -> None:
    """Вызывается из `services.scheduler.init_scheduler` рядом с остальными реконсиляциями —
    перевзводит джобы отзыва ВСЕХ сессий на старте бота (пересозданный `jobs.sqlite`, простой
    дольше `_MISFIRE_GRACE_SECONDS`). Идемпотентно (`schedule_for_session` сама решает, ставить
    ли и на какой момент) — повторный вызов на уже полностью взведённом хранилище не плодит
    дублей и не портит будущие сессии."""
    try:
        from database.db import list_all_program_sessions

        for session in await list_all_program_sessions():
            await schedule_for_session(int(session["id"]))
    except Exception as e:
        logger.error("session_feedback.reconcile_all failed: %s: %s", type(e).__name__, e)


async def reconcile_city(code: str) -> None:
    """Перестановка джоб отзыва ВСЕХ сессий ОДНОГО города — вызывается после смены тумблера
    `session_feedback_enabled` или задержки `session_feedback_delay_minutes` на экране
    «⭐ Отзывы о сессиях» (`handlers/session_feedback.py`). Тумблер сам по себе джобу не трогает
    (`deliver_feedback_prompts` перечитывает его на тике, докстринг выше), а вот задержка —
    да: у уже стоящей джобы `run_at` посчитан со СТАРЫМ значением, отдельного диффа не считает
    (`schedule_for_session` и так пересчитывает от нуля дешевле дифа), поэтому реконсиляция
    вызывается на ОБА события — цена одинаковая, а не звать её на тумблер значило бы держать
    в голове, что «эта перестановка на самом деле не нужна», пока это не перестанет быть правдой.
    Fail-soft, тот же приём, что `reconcile_all`."""
    try:
        from database.db import list_all_program_sessions

        for session in await list_all_program_sessions():
            if session.get("city") == code:
                await schedule_for_session(int(session["id"]))
    except Exception as e:
        logger.error("session_feedback.reconcile_city(%s) failed: %s: %s", code, type(e).__name__, e)


def rating_keyboard(session_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="⭐" * n, callback_data=f"sfb:r:{session_id}:{n}")
        for n in range(_RATING_MIN, _RATING_MAX + 1)
    ]])


def comment_offer_keyboard(
    session_id: int, lang: str = "ru", tr_map: dict | None = None,
) -> InlineKeyboardMarkup:
    """Кнопка «✍️ Написать» — на языке получателя. Ленивый импорт: модуль aiogram-free на
    уровне импорта `handlers.*` (см. докстринг)."""
    from handlers.reg_i18n import tr_text

    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text=tr_text("✍️ Написать", lang, tr_map or {}), callback_data=f"sfb:c:{session_id}"),
    ]])


async def deliver_feedback_prompts(session_id: int) -> None:
    """Тело джобы (пункт 1 плана): перечитывает сессию/тумблер ПЕРЕД отправкой (та же идиома,
    что `sos.escalation_job`) — правка/выключение фичи/удаление сессии между постановкой джобы
    и тиком гасят рассылку без отдельной отмены. Приглашение уходит ТОЛЬКО отмеченным на
    сессии (D-24, `is_marked_for_session`/`list_marked_telegram_ids_for_session` — итоговая
    отметка слота, D-20); идемпотентно на делегата (`create_session_feedback_prompt`).

    Тихие часы — тот же приём, что у остальных уведомлений делегату (16.09, `services.
    quiet_hours.send_or_queue_text`, `respect_quiet_hours` не отдельным флагом — эта рассылка
    ВСЕГДА через очередь тихих часов, ни один вызывающий не просит немедленную отправку)."""
    try:
        session = await get_program_session(session_id)
        if session is None:
            return
        if not await is_enabled_for_city(session.get("city")):
            return
        title = (session.get("title") or "").strip() or "сессия"
        template = await get_setting_typed("session_feedback_prompt_text") or "Как тебе «{title}»?"
        kb = rating_keyboard(session_id)

        # Ленивые импорты — модуль aiogram-free на уровне импорта (докстринг), эта функция
        # выполняется ТОЛЬКО ботом. `handlers.reg_i18n.tr_fmt` — тот же порядок «шаблон
        # переводится СНАЧАЛА, {title} подставляется ПОСЛЕ», что LANG-02 уже закрепила везде
        # в чате (Часть А ревью SOS — тот же класс бага, если поменять местами).
        from services.scheduler import _now_moscow_naive, get_bot
        from services import quiet_hours
        from services import i18n as i18n_service
        from handlers import reg_i18n

        bot = get_bot()
        now = _now_moscow_naive()
        recipients = await list_marked_telegram_ids_for_session(session_id)
        tr_maps: dict[str, dict] = {}
        for telegram_id in recipients:
            stamp = now.strftime("%Y-%m-%d %H:%M:%S")
            inserted = await create_session_feedback_prompt(telegram_id, session_id, stamp)
            if not inserted:
                continue  # уже приглашали (повторный тик джобы/reconcile) — не дублируем
            lang, tr_map = await i18n_service.context_cached(telegram_id, tr_maps)
            text = reg_i18n.tr_fmt(template, lang, tr_map, title=title)
            await quiet_hours.send_or_queue_text(
                now, telegram_id, text,
                sender=lambda tid=telegram_id, t=text: bot.send_message(tid, t, reply_markup=kb),
                reply_markup=kb,
            )
    except Exception as e:
        logger.error("session_feedback.deliver_feedback_prompts(%s) failed: %s: %s", session_id, type(e).__name__, e)


async def record_rating(telegram_id: int, session_id: int, rating: int) -> bool:
    """`False` — делегат не отмечен на сессии (защита D-24 на входе, а не только на выдаче
    приглашения — чужой/подделанный callback_data не должен списать оценку) ИЛИ строки-
    приглашения не существует вовсе (никогда не получал его). Иначе — оценка проставлена/
    заменена (правило плана: одна оценка на делегата на сессию, повторный тап меняет)."""
    if rating < _RATING_MIN or rating > _RATING_MAX:
        return False
    if not await is_marked_for_session(telegram_id, session_id):
        return False
    from services.timeutil import msk_now

    stamp = msk_now().strftime("%Y-%m-%d %H:%M:%S")
    return await set_session_feedback_rating(telegram_id, session_id, rating, stamp)


async def record_comment(telegram_id: int, session_id: int, comment: str) -> bool:
    if not await is_marked_for_session(telegram_id, session_id):
        return False
    from services.timeutil import msk_now

    stamp = msk_now().strftime("%Y-%m-%d %H:%M:%S")
    return await set_session_feedback_comment(telegram_id, session_id, comment.strip(), stamp)


async def my_feedback(telegram_id: int, session_id: int) -> dict | None:
    return await get_session_feedback(telegram_id, session_id)


def stats_line(stats: dict) -> str:
    """«⭐ 4.6 (38 оценок) · 12 комментариев» (карточка сессии, `handlers.admin_program.
    render_session_card`) — «Пока нет оценок», если `rating_count == 0` (не «⭐ 0.0»)."""
    count = stats.get("rating_count", 0)
    if not count:
        return "Пока нет оценок"
    avg = stats.get("avg") or 0.0
    comments = stats.get("comment_count", 0)
    line = f"⭐ {avg:.1f} ({count} {_ratings_word(count)})"
    if comments:
        line += f" · {comments} {_comments_word(comments)}"
    return line


def _ratings_word(n: int) -> str:
    n = abs(n) % 100
    n1 = n % 10
    if 11 <= n <= 14:
        return "оценок"
    if n1 == 1:
        return "оценка"
    if 2 <= n1 <= 4:
        return "оценки"
    return "оценок"


def _comments_word(n: int) -> str:
    n = abs(n) % 100
    n1 = n % 10
    if 11 <= n <= 14:
        return "комментариев"
    if n1 == 1:
        return "комментарий"
    if 2 <= n1 <= 4:
        return "комментария"
    return "комментариев"


async def day_stats(city: str, day: str) -> list[dict]:
    """Экран «📊 Оценки сессий» дня (пункт 2 плана): сессии дня + статистика, сортировка по
    средней оценке (сессии без единой оценки — в конец, по времени начала)."""
    from services.program import sessions_for_city_day

    sessions = await sessions_for_city_day(city, day)
    if not sessions:
        return []
    stats_by_id = await session_feedback_stats_bulk([s["id"] for s in sessions])
    marked_counts = {
        s["id"]: len(await list_marked_telegram_ids_for_session(s["id"])) for s in sessions
    }
    rows = []
    for s in sessions:
        stats = stats_by_id.get(s["id"], {"avg": None, "rating_count": 0, "comment_count": 0})
        rows.append({**s, "stats": stats, "marked_count": marked_counts.get(s["id"], 0)})
    rows.sort(key=lambda r: (r["stats"]["avg"] is None, -(r["stats"]["avg"] or 0), r["start_time"]))
    return rows


async def comments_page(session_id: int, *, page: int = 0, page_size: int = 10) -> tuple[list[dict], int]:
    stats = await session_feedback_stats(session_id)
    total = stats.get("comment_count", 0)
    rows = await list_session_feedback_comments(session_id, limit=page_size, offset=page * page_size)
    return rows, total
