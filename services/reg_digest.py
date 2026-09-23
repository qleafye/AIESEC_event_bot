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
    auto_reject_summary, enqueue_reg_digest, get_user, list_unsent_reg_digest,
    mark_reg_digest_sent,
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


def _bullets(names: list[str]) -> list[str]:
    """Имена столбиком «• Имя»; хвост длиннее MAX_NAMES сворачивается в «• и ещё N»."""
    lines = [f"• {html.escape(str(n))}" for n in names[:MAX_NAMES]]
    rest = len(names) - MAX_NAMES
    if rest > 0:
        lines.append(f"• и ещё {rest}")
    return lines


def _period_label(period: tuple[str, str] | None) -> str:
    """(«15:20», «15:42») -> « · 15:20–15:42»; одинаковые края -> « · 15:42»; None -> ""."""
    if not period:
        return ""
    start, end = period
    return f" · {end}" if start == end else f" · {start}–{end}"


def build_digest_text(pending_names: list[str], auto_names: list[str] | None = None,
                      rule_counts: list[tuple[str, int]] | None = None,
                      period: tuple[str, str] | None = None) -> str:
    """Текст пачки уведомлений о заявках (владелец 23.09: «уродская строка» -> столбик).

    Два блока: «📥 Новые заявки» (ушли на модерацию) и «🤖 Автоотказ» (отклонены правилами,
    с разбивкой по правилам). Пустой блок не печатается. Период пачки («15:20–15:42») — в
    заголовке первого блока, чтобы было видно, за какое время собрано. Каждый блок кончается
    строкой «куда идти» — путём кнопками в админке. HTML-экранирование имён — здесь."""
    auto_names = auto_names or []
    period_text = _period_label(period)
    blocks: list[list[str]] = []
    if pending_names:
        blocks.append([
            f"📥 <b>Новые заявки: {len(pending_names)}</b>{period_text}",
            *_bullets(pending_names),
            "Открыть: 📋 Заявки",
        ])
        period_text = ""
    if auto_names:
        block = [f"🤖 <b>Автоотказ: {len(auto_names)}</b>{period_text}", *_bullets(auto_names)]
        if rule_counts:
            if len(rule_counts) == 1:
                block.append(f"Правило «{html.escape(str(rule_counts[0][0]))}»")
            else:
                parts = ", ".join(
                    f"«{html.escape(str(name))}» — {count}" for name, count in rule_counts
                )
                block.append(f"По правилам: {parts}")
        block.append("Журнал и возврат на модерацию: 🚫 Правила автоотказа → 🤖 Автоотказы")
        blocks.append(block)
    return "\n\n".join("\n".join(b) for b in blocks)


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


def arm_digest_job(city: str | None, minutes: int, *, first_queued_at: str | None = None,
                    cap_minutes: int = 0) -> None:
    """Поставить/перевзвести джобу дайджеста для города на now+minutes (окно тишины).

    Квик 260923 (D-C): `first_queued_at`/`cap_minutes` — хвостовые kwargs, `cap_minutes=0`
    (дефолт) не меняет расчёт вовсе (байт-в-байт прежнее `now+minutes`). При `cap_minutes>0` и
    заданном `first_queued_at` (created_at первой ещё НЕ отправленной строки очереди города,
    МСК-строка) — потолок: сводка уходит не позже `first_queued_at + cap_minutes`, даже если
    поток заявок не стихает и окно тишины постоянно откладывается. `run_at` никогда не уходит
    в прошлое (`max(..., now)`) — потолок может сработать «уже пора», а не «через N минут»."""
    now = datetime.now(_sched.MOSCOW_TZ)
    run_at = now + timedelta(minutes=minutes)
    if cap_minutes and first_queued_at:
        try:
            first_dt = datetime.strptime(first_queued_at, "%Y-%m-%d %H:%M:%S").replace(
                tzinfo=_sched.MOSCOW_TZ
            )
        except (TypeError, ValueError):
            first_dt = None
        if first_dt is not None:
            capped = first_dt + timedelta(minutes=cap_minutes)
            if capped < run_at:
                run_at = capped
            if run_at < now:
                run_at = now
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
        # Квик 260923 (D-C): потолок читает первую ещё НЕ отправленную строку очереди этого
        # города — она и есть точка отсчёта «не позже N минут» (список отсортирован по id, т.е.
        # по порядку постановки). cap_minutes=0 (дефолт) -> запрос не идёт в лишнюю сторону,
        # arm_digest_job сам не трогает расчёт при cap_minutes=0.
        cap_minutes = await get_setting_typed("reg_submit_digest_max_minutes")
        first_queued_at = None
        if cap_minutes:
            queue_rows = await list_unsent_reg_digest(city)
            if queue_rows:
                first_queued_at = queue_rows[0]["created_at"]
        arm_digest_job(city, minutes, first_queued_at=first_queued_at, cap_minutes=cap_minutes)
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
        pending_names = [
            await _display_name(r["telegram_id"]) for r in rows if not r.get("auto_rejected")
        ]
        auto_names = [await _display_name(r["telegram_id"]) for r in rows if r.get("auto_rejected")]
        auto_reject_count = len(auto_names)
        # Квик 260923 (D-D): разбивка по правилам — по СНИМКУ строк ЭТОЙ пачки (live_only=False,
        # тот же принцип, что и сам auto_reject_count выше: к моменту отправки менеджер мог уже
        # вернуть заявку из журнала, пачка описывает то, что произошло на постановке).
        rule_counts: list[tuple[str, int]] = []
        if auto_reject_count:
            auto_ids = [r["telegram_id"] for r in rows if r.get("auto_rejected")]
            _, rule_counts = await auto_reject_summary(telegram_ids=auto_ids, live_only=False)
        stamps = [str(r.get("created_at") or "") for r in rows]
        period = (stamps[0][11:16], stamps[-1][11:16]) if all(len(t) >= 16 for t in stamps) else None
        text = build_digest_text(pending_names, auto_names, rule_counts, period)
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
        cap_minutes = await get_setting_typed("reg_submit_digest_max_minutes")
        sched = _sched.get_scheduler()
        for city in dict.fromkeys(r["city"] for r in rows):
            if sched.get_job(digest_job_id(city)) is None:
                # Тот же расчёт потолка, что у notify_application (D-C) — это восстановление
                # расписания после рестарта, а не новая логика: первая строка ЭТОГО города
                # (rows уже упорядочены по id).
                first_queued_at = next(
                    (r["created_at"] for r in rows if r["city"] == city), None
                )
                arm_digest_job(
                    city, minutes, first_queued_at=first_queued_at, cap_minutes=cap_minutes
                )
                armed.append(city)
        if armed:
            logger.warning(f"reg_digest: re-armed {len(armed)} digest job(s) after restart: {armed}")
    except Exception as e:
        logger.error(f"reg_digest.rearm_pending_digests failed: {e}")
    return armed
