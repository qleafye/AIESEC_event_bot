"""Quick 260822: уведомления менеджеру о сданных заданиях — каждое отдельно или дайджестом.

Режим — настройка `game_submit_notify_mode` (SETTINGS_SCHEMA, тумблер на экране
«⚙️ Настройки → 🎮 Геймификация»):

- `each` (по умолчанию, прежнее поведение) — на каждую сдачу одно сообщение держателям
  `moderate_game`;
- `digest` — сдача кладётся в `game_submit_digest_queue` (БД, переживает рестарт), а на
  APScheduler ставится/перевзводится ОДНА date-джоба на город (`game_digest:{city}` или
  `game_digest:all` без модуля городов). Джоба срабатывает через `game_submit_digest_minutes`
  тишины: каждая новая сдача двигает её вперёд (`replace_existing=True`). На старте бота
  `rearm_pending_digests()` взводит джобы для всего, что осталось неотправленным.

В обоих режимах рассылка идёт через `notify_by_capability(..., city=city)` — город сдавшего
делегата, как у заявок: менеджер, привязанный к городу, видит только свой город, без привязки —
всё (семантика `capability_holders`). Уведомления делегату (одобрено/отклонено) этот модуль не
трогает — они персональные.

Вынесено из handlers/user_actions.py и services/scheduler.py: оба у своих потолков
(tests/test_module_size_convention_260816.py), хуки там — по одной строке. Импорт
`handlers.admin_caps` — ленивый, внутри функций: модуль импортируется из user_actions при
инициализации пакета handlers, верхнеуровневый импорт обратно в handlers замкнул бы цикл.
"""
import html
import logging
from datetime import datetime, timedelta

from cities import cities_module_on, normalize_city
from database.db import (
    enqueue_game_digest, get_user, list_unsent_game_digest, mark_game_digest_sent,
)
from settings_schema import GAME_SUBMIT_NOTIFY_MODE_LABELS, get_setting_typed
from services import scheduler as _sched

logger = logging.getLogger(__name__)

CAP = "moderate_game"
JOB_PREFIX = "game_digest:"


# ── Pure helpers (unit-test surface) ──────────────────────────────────────────

def digest_job_id(city: str | None) -> str:
    return f"{JOB_PREFIX}{city or 'all'}"


def notify_mode_label(mode) -> str:
    """Человеческая подпись режима; незнакомое/пустое значение читается как «each»."""
    return GAME_SUBMIT_NOTIFY_MODE_LABELS.get(mode, GAME_SUBMIT_NOTIFY_MODE_LABELS["each"])


def build_digest_text(entries: list[tuple[str, int]]) -> str:
    """«📥 Новые сдачи: N — Иванов ×3, Петрова ×2 → 🎮 Проверка». `entries` — (имя, сколько),
    уже в нужном порядке; имена HTML-экранируются здесь, не у вызывающего."""
    total = sum(n for _, n in entries)
    people = ", ".join(
        f"{html.escape(str(name))} ×{n}" if n > 1 else html.escape(str(name))
        for name, n in entries
    )
    return f"📥 Новые сдачи: {total} — {people} → 🎮 Проверка"


def aggregate_rows(rows: list[dict], names: dict[int, str]) -> list[tuple[str, int]]:
    """Группировка строк очереди по делегату в порядке первого появления."""
    counts: dict[int, int] = {}
    for row in rows:
        counts[row["user_id"]] = counts.get(row["user_id"], 0) + 1
    return [(names.get(uid, str(uid)), n) for uid, n in counts.items()]


# ── Async helpers ─────────────────────────────────────────────────────────────

async def game_submit_notify_button_text() -> str:
    """Подпись тумблера на экране «🎮 Геймификация» — текущее состояние словами."""
    mode = await get_setting_typed("game_submit_notify_mode")
    return f"📥 Сдачи менеджеру: {notify_mode_label(mode)}"


async def resolve_submitter_city(user_id: int) -> str | None:
    """Город делегата для маршрутизации — тот же идиом, что у заявок (user_actions
    ask_question): модуль городов выключен -> None (глобальная рассылка); ошибка -> None."""
    if not await cities_module_on():
        return None
    try:
        user = await get_user(user_id)
        return normalize_city(user.get("event_city") if user else None)
    except Exception as e:
        logger.error(f"game_digest: failed to resolve city for {user_id}: {e}")
        return None


async def _display_name(user_id: int) -> str:
    try:
        user = await get_user(user_id)
    except Exception:
        user = None
    if user and user.get("full_name"):
        return user["full_name"]
    return str(user_id)


def arm_digest_job(city: str | None, minutes: int) -> None:
    """Поставить/перевзвести джобу дайджеста для города на now+minutes (окно тишины)."""
    run_at = datetime.now(_sched.MOSCOW_TZ) + timedelta(minutes=minutes)
    _sched.get_scheduler().add_job(
        send_game_digest, "date", run_date=run_at, args=[city],
        id=digest_job_id(city), replace_existing=True,
    )


async def notify_submission(bot, *, submission_id: int, user_id: int, task_id: int,
                            task_text: str, submitter_name: str) -> None:
    """Точка входа из finalize_game_submission: выбрать режим и отправить/отложить."""
    from handlers.admin_caps import notify_by_capability  # lazy: см. докстринг модуля
    city = await resolve_submitter_city(user_id)
    mode = await get_setting_typed("game_submit_notify_mode")
    if mode == "digest":
        await enqueue_game_digest(
            submission_id, user_id, task_id, city,
            datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        )
        minutes = await get_setting_typed("game_submit_digest_minutes")
        arm_digest_job(city, minutes)
        return
    await notify_by_capability(
        bot, CAP,
        f"🎮 Новая сдача по заданию «{html.escape(str(task_text)[:60])}» от "
        f"{html.escape(str(submitter_name))}",
        parse_mode="HTML", city=city,
    )


async def send_game_digest(city: str | None) -> int:
    """Date-job target (аргумент — только строка города, picklable; Bot — из
    services.scheduler._bot). Пустая очередь -> без сообщения. Возвращает число отправок."""
    from handlers.admin_caps import notify_by_capability  # lazy: см. докстринг модуля
    try:
        rows = await list_unsent_game_digest(city)
        if not rows:
            return 0
        names = {}
        for uid in dict.fromkeys(r["user_id"] for r in rows):
            names[uid] = await _display_name(uid)
        text = build_digest_text(aggregate_rows(rows, names))
        sent = await notify_by_capability(_sched._bot, CAP, text, parse_mode="HTML", city=city)
        await mark_game_digest_sent(
            [r["id"] for r in rows], datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        )
        return sent
    except Exception as e:
        logger.error(f"send_game_digest({city!r}) failed: {e}")
        return 0


async def rearm_pending_digests() -> list[str | None]:
    """На старте: для каждого города с неотправленными строками взвести джобу (если её нет в
    jobstore — сохранившаяся date-джоба остаётся как есть). Fail-soft, не блокирует старт."""
    armed: list[str | None] = []
    try:
        rows = await list_unsent_game_digest(all_cities=True)
        if not rows:
            return armed
        minutes = await get_setting_typed("game_submit_digest_minutes")
        sched = _sched.get_scheduler()
        for city in dict.fromkeys(r["city"] for r in rows):
            if sched.get_job(digest_job_id(city)) is None:
                arm_digest_job(city, minutes)
                armed.append(city)
        if armed:
            logger.warning(f"game_digest: re-armed {len(armed)} digest job(s) after restart: {armed}")
    except Exception as e:
        logger.error(f"rearm_pending_digests failed: {e}")
    return armed
