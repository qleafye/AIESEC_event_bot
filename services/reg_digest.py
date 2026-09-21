"""Квик 260916: уведомления менеджеру о новых заявках — каждую отдельно или пачкой.

Родной брат `services/game_digest.py` (квик 260822), тот же разрез, та же механика:

- `each` (по умолчанию, прежнее поведение) — на каждую новую заявку одно сообщение
  держателям `moderate_reg` города делегата;
- `digest` — заявка кладётся в `reg_submit_digest_queue` (БД, переживает рестарт), а на
  APScheduler ставится/перевзводится ОДНА date-джоба на город (`reg_digest:{city}` или
  `reg_digest:all` без модуля городов). Джоба срабатывает через `reg_submit_digest_minutes`
  тишины: каждая новая заявка двигает её вперёд (`replace_existing=True`). На старте бота
  `rearm_pending_digests()` взводит джобы для всего, что осталось неотправленным.

ЕДИНСТВЕННАЯ точка входа уведомления менеджерам о поданной анкете — `notify_application`.
Её зовёт `services/reg_finalize.py::post_finalize`, а это общий финал И чата, И Mini App
(её submit приезжает сюда же через `miniapp_outbox`), поэтому второй двери нет и третьего
места, где решается режим, тоже. Правка уже поданной анкеты (`is_new=False`) уходит СРАЗУ и
мимо очереди: это не «новая заявка», в счётчик «Новые заявки: N» ей нельзя, а менеджеру она
нужна с текстом «что именно изменилось».

Чего этот модуль НЕ трогает: гейт `pending_notify_mode` («сообщать ли вообще о заявке на
модерацию») остаётся в `post_finalize` — он решает ЧТО отправлять, а этот модуль ТОЛЬКО как
(по одной или пачкой).

Импорт `handlers.admin_caps` — ленивый, внутри функций: `handlers/registration.py`
импортирует `reg_finalize` на верхнем уровне, поэтому верхнеуровневый импорт обратно в
`handlers` замкнул бы цикл при загрузке пакета (тот же приём, что в game_digest.py).
"""
import html
import logging
from datetime import datetime, timedelta

from cities import cities_module_on, normalize_city
from database.db import (
    enqueue_reg_digest, get_user, list_unsent_reg_digest, mark_reg_digest_sent,
)
from settings_schema import REG_SUBMIT_NOTIFY_MODE_LABELS, get_setting_typed
from services import scheduler as _sched
from services.timeutil import msk_now

logger = logging.getLogger(__name__)

CAP = "moderate_reg"
JOB_PREFIX = "reg_digest:"

# Сколько имён влезает в одну строку дайджеста, прежде чем она превращается в простыню.
# Остальные сворачиваются в «и ещё K» — сводка зовёт открыть «📋 Заявки», а не заменяет их.
MAX_NAMES = 15


# ── Pure helpers (unit-test surface) ──────────────────────────────────────────

def digest_job_id(city: str | None) -> str:
    return f"{JOB_PREFIX}{city or 'all'}"


def notify_mode_label(mode) -> str:
    """Человеческая подпись режима; незнакомое/пустое значение читается как «each»."""
    return REG_SUBMIT_NOTIFY_MODE_LABELS.get(mode, REG_SUBMIT_NOTIFY_MODE_LABELS["each"])


def _auto_reject_suffix(count: int) -> str:
    """D-17: «, из них 🤖 N автоотказов» — ТОЛЬКО при ненулевом счётчике (пустой хвост
    оставляет `build_digest_text` байт-в-байт прежним для событий без правил автоотказа).
    Русское склонение (1/2-4/5-20) — тот же стандартный приём, что `services.proxy_session.
    _plural_ru`, своя копия здесь (мелкая чистая функция, второй общий модуль не заводим)."""
    if not count:
        return ""
    n = abs(count) % 100
    if 11 <= n <= 14:
        word = "автоотказов"
    else:
        tail = n % 10
        if tail == 1:
            word = "автоотказ"
        elif 2 <= tail <= 4:
            word = "автоотказа"
        else:
            word = "автоотказов"
    return f", из них 🤖 {count} {word}"


def build_digest_text(names: list[str], auto_reject_count: int = 0) -> str:
    """«📥 Новые заявки: N — Иванова, Петров → 📋 Заявки[, из них 🤖 K автоотказов]». `names` —
    уже в нужном порядке; HTML-экранирование здесь, не у вызывающего. Хвост длиннее MAX_NAMES
    сворачивается. `auto_reject_count` (Phase 31, 31-06, D-17) — хвостовой kwarg с дефолтом 0,
    существующие вызывающие получают байт-в-байт прежний текст."""
    total = len(names)
    shown = [html.escape(str(n)) for n in names[:MAX_NAMES]]
    people = ", ".join(shown)
    rest = total - len(shown)
    if rest > 0:
        people = f"{people} и ещё {rest}"
    return f"📥 Новые заявки: {total} — {people} → 📋 Заявки{_auto_reject_suffix(auto_reject_count)}"


# ── Async helpers ─────────────────────────────────────────────────────────────

async def reg_submit_notify_button_text() -> str:
    """Подпись тумблера в разделе «📋 Заявки» — текущее состояние словами, без кодов."""
    mode = await get_setting_typed("reg_submit_notify_mode")
    return f"📥 Заявки менеджеру: {notify_mode_label(mode)}"


async def resolve_city(raw_city) -> str | None:
    """Город заявки для маршрутизации: модуль городов выключен ИЛИ город в анкете пуст ->
    None (глобальная рассылка), иначе нормализованный код. Для `notify_by_capability` это
    РАВНОСИЛЬНО прежней передаче сырого значения (`capability_holders` сам игнорирует город
    при выключенном модуле и сам нормализует обе стороны сравнения при включённом) — но
    очереди дайджеста нужен один стабильный ключ города, а не то, что лежало в анкете.

    Пустой город НЕ схлопывается в город по умолчанию (в отличие от `normalize_city`): сейчас
    такая заявка уходит всем держателям `moderate_reg`, и режим уведомлений не имеет права
    молча сузить их круг."""
    try:
        if not raw_city or not await cities_module_on():
            return None
        return normalize_city(raw_city)
    except Exception as e:
        logger.error(f"reg_digest: failed to resolve city {raw_city!r}: {e}")
        return None


async def _display_name(telegram_id: int) -> str:
    try:
        user = await get_user(telegram_id)
    except Exception:
        user = None
    if user and user.get("full_name"):
        return user["full_name"]
    return str(telegram_id)


def arm_digest_job(city: str | None, minutes: int) -> None:
    """Поставить/перевзвести джобу дайджеста для города на now+minutes (окно тишины)."""
    run_at = datetime.now(_sched.MOSCOW_TZ) + timedelta(minutes=minutes)
    _sched.get_scheduler().add_job(
        send_reg_digest, "date", run_date=run_at, args=[city],
        id=digest_job_id(city), replace_existing=True,
    )


async def notify_application(bot, *, telegram_id: int, admin_text: str, city_raw=None,
                             is_new: bool = True, auto_rejected: bool = False) -> None:
    """Точка входа из post_finalize: выбрать режим и отправить/отложить.

    `is_new=False` (правка/переподача уже поданной анкеты) уходит немедленно ВСЕГДА — см.
    докстринг модуля. `auto_rejected` (Phase 31, 31-06, D-17) — хвостовой kwarg с дефолтом
    `False`, штампуется в очередь дайджеста НА ПОСТАНОВКЕ (не выводится из `users.status` при
    отправке — к моменту отправки менеджер мог вернуть заявку из журнала, и счётчик соврал
    бы)."""
    from handlers.admin_caps import notify_by_capability  # lazy: см. докстринг модуля
    city = await resolve_city(city_raw)
    mode = await get_setting_typed("reg_submit_notify_mode") if is_new else "each"
    if mode == "digest":
        await enqueue_reg_digest(
            telegram_id, city, msk_now().strftime("%Y-%m-%d %H:%M:%S"),
            auto_rejected=1 if auto_rejected else 0,
        )
        minutes = await get_setting_typed("reg_submit_digest_minutes")
        arm_digest_job(city, minutes)
        return
    await notify_by_capability(bot, CAP, admin_text, parse_mode="HTML", city=city)


async def send_reg_digest(city: str | None) -> int:
    """Date-job target (аргумент — только строка города, picklable; Bot — из
    services.scheduler._bot). Пустая очередь -> без сообщения. Возвращает число отправок.

    `auto_reject_count` (D-17) считается по полю СТРОК ОЧЕРЕДИ (`auto_rejected`, штампуется на
    постановке `notify_application`), а НЕ перечитыванием `users.status` — к моменту отправки
    статус мог смениться (менеджер вернул заявку из журнала автоотказов), и сводка соврала
    бы."""
    from handlers.admin_caps import notify_by_capability  # lazy: см. докстринг модуля
    try:
        rows = await list_unsent_reg_digest(city)
        if not rows:
            return 0
        names = [await _display_name(r["telegram_id"]) for r in rows]
        auto_reject_count = sum(1 for r in rows if r.get("auto_rejected"))
        text = build_digest_text(names, auto_reject_count)
        sent = await notify_by_capability(_sched._bot, CAP, text, parse_mode="HTML", city=city)
        await mark_reg_digest_sent(
            [r["id"] for r in rows], msk_now().strftime("%Y-%m-%d %H:%M:%S")
        )
        return sent
    except Exception as e:
        logger.error(f"send_reg_digest({city!r}) failed: {e}")
        return 0


async def rearm_pending_digests() -> list[str | None]:
    """На старте: для каждого города с неотправленными строками взвести джобу (если её нет в
    jobstore — сохранившаяся date-джоба остаётся как есть). Fail-soft, не блокирует старт."""
    armed: list[str | None] = []
    try:
        rows = await list_unsent_reg_digest(all_cities=True)
        if not rows:
            return armed
        minutes = await get_setting_typed("reg_submit_digest_minutes")
        sched = _sched.get_scheduler()
        for city in dict.fromkeys(r["city"] for r in rows):
            if sched.get_job(digest_job_id(city)) is None:
                arm_digest_job(city, minutes)
                armed.append(city)
        if armed:
            logger.warning(f"reg_digest: re-armed {len(armed)} digest job(s) after restart: {armed}")
    except Exception as e:
        logger.error(f"reg_digest.rearm_pending_digests failed: {e}")
    return armed
