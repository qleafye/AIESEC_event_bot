"""Квик 260916: «📊 Итоги дня» — одна вечерняя сводка менеджерам про прошедший день.

Что уходит (пример):

    📊 Итоги дня, 16.09 — Москва
    📋 Заявки: новых 23 · одобрено 18 · отклонено 2 · ждут 41
       Марина Иванова — 11 ✅ / 1 ❌
       Пётр Сидоров — 7 ✅ / 1 ❌
    🎮 Геймификация: сдач 14 · проверено 12 · монет начислено 340
       Марина Иванова — 9
       Иван Петров — 3

Устройство:

- ОДНА cron-джоба на весь бот (`daily_digest`, `replace_existing=True` на каждом старте),
  время — `daily_digest_time` (ЧЧ:ММ МСК, валидируется как `format: "time"`). Смена времени
  действует сразу (`on_setting_written` ниже) — как и у интервалов фоновых джоб
  (`services.scheduler.on_setting_written`).
- По городу: модуль городов включён -> одна сводка на каждый ВКЛЮЧЁННЫЙ город со своими
  цифрами; выключен -> одна общая (`city=None`). Ровно та же резолюция, что у
  `services/chat_tracking.py::bound_chats`.
- Получатели: держатели `moderate_reg` И `moderate_game` этого города, объединённые ПО
  ЧЕЛОВЕКУ — менеджер с обоими правами получает сводку ОДИН раз, а не два (два вызова
  `notify_by_capability` подряд дали бы ему два одинаковых сообщения).
- Раздел без активности схлопывается в одну строку («🎮 Геймификация: сегодня тихо»). Оба
  раздела пусты -> сводка НЕ отправляется вовсе: пустое письмо каждый вечер учит менеджера
  не открывать сообщения бота.

Тихие часы здесь НЕ применяются и применяться не должны: `services/quiet_hours.py` бережёт
СОН ДЕЛЕГАТА, а это сводка сотрудникам, которую менеджер сам себе включил на выбранный им
час.

День — московский (`services/timeutil.msk_now`), окно `[00:00, момент отправки)`: сводка,
ушедшая в 21:00, рассказывает про уже прожитые 21 час, а не про вчера.

Импорт `handlers.admin_caps` — ленивый, внутри функций (цикл через пакет `handlers`), тот же
приём, что в `services/game_digest.py` и `services/reg_digest.py`.
"""
import html
import logging
from datetime import timedelta

from domain.cities import cities_module_on, city_label, city_scope, enabled_cities
from database.db import auto_reject_summary, daily_digest_stats, get_display_names
from domain.settings.schema import get_setting_typed
from services.timeutil import msk_now
from services import staff_reach

logger = logging.getLogger(__name__)

JOB_ID = "daily_digest"
CAPS = ("moderate_reg", "moderate_game")
DEFAULT_TIME = "21:00"
# Сколько менеджеров показываем поимённо в каждом разделе. Хвост не сворачивается в «и ещё» —
# он просто не нужен: сводка отвечает «кто тянул день», а не ведёт бухгалтерию.
MAX_ROWS = 10


# ── Pure helpers (unit-test surface) ──────────────────────────────────────────

async def on_setting_written(key: str) -> None:
    """Новое время «📊 Итоги дня» действует сразу, без перезапуска бота."""
    if key != "daily_digest_time":
        return
    from services.scheduler import get_scheduler
    from domain.settings.schema import get_setting_typed

    hour, minute = parse_time(await get_setting_typed("daily_digest_time"))
    try:
        sched = get_scheduler()
    except RuntimeError:
        return  # планировщик ещё не поднят (веб-процесс, тесты) — время возьмёт старт бота
    if sched.get_job(JOB_ID) is not None:
        sched.reschedule_job(JOB_ID, trigger="cron", hour=hour, minute=minute)
    else:
        sched.add_job(daily_digest_job, "cron", hour=hour, minute=minute, id=JOB_ID)
    logger.info("daily_digest: время сводки перенесено на %02d:%02d", hour, minute)


def parse_time(raw) -> tuple[int, int]:
    """«ЧЧ:ММ» -> (часы, минуты). Мусор/пусто -> 21:00 — та же терпимость, что у
    `services.scheduler._int_or_default`: джоба обязана встать даже если в ключе ерунда,
    иначе одна опечатка тихо отменяет всю фичу."""
    try:
        hh, mm = str(raw).strip().split(":")
        hours, minutes = int(hh), int(mm)
    except (AttributeError, TypeError, ValueError):
        hours, minutes = -1, -1
    if not (0 <= hours <= 23 and 0 <= minutes <= 59):
        return tuple(int(p) for p in DEFAULT_TIME.split(":"))  # type: ignore[return-value]
    return hours, minutes


def manager_name(manager_id: int, names: dict[int, str]) -> str:
    """ФИО из `users`, иначе «менеджер #id» — менеджер не обязан быть делегатом, а голый
    числовой id в сводке человек не читает."""
    name = names.get(manager_id)
    return str(name) if name else f"менеджер #{manager_id}"


def _walkin_suffix(stats: dict) -> str:
    """D-41: регистрация на месте без решения — не очередь менеджера, отдельная цифра."""
    n = stats.get("apps_walkin_pending") or 0
    return f" · ждут на стойке {n}" if n else ""


def build_digest_text(stats: dict, names: dict[int, str], *, day_label: str,
                      city_title: str | None = None) -> str | None:
    """Готовый HTML сводки, или None, если оба раздела пусты (тогда не шлём вовсе).

    Имена экранируются здесь, не у вызывающего. Квик 260923 (D-A): `stats["auto_rejected"]`/
    `stats["auto_reject_rules"]` — хвостовые ключи с дефолтом 0/[] (`.get`, не `[...]`),
    существующие вызывающие без них получают байт-в-байт прежний текст. Блок заявок считается
    активным и когда день целиком состоял из одних автоотказов (менеджеру важно узнать, что
    правила поработали, даже если ни одного решения человек не принимал)."""
    auto_rejected = stats.get("auto_rejected", 0)
    apps_active = any(stats[k] for k in ("apps_new", "apps_approved", "apps_rejected")) or bool(auto_rejected)
    game_active = any(stats[k] for k in ("game_submissions", "game_reviewed", "coins_awarded"))
    if not apps_active and not game_active:
        return None

    header = f"📊 Итоги дня, {day_label}"
    if city_title:
        header = f"{header} — {html.escape(str(city_title))}"
    lines = [header]

    if apps_active:
        lines.append(
            f"📋 Заявки: новых {stats['apps_new']} · одобрено {stats['apps_approved']} · "
            f"отклонено {stats['apps_rejected']} · ждут {stats['apps_pending']}"
            + _walkin_suffix(stats)
        )
        for manager_id, approved, rejected in stats["app_managers"][:MAX_ROWS]:
            who = html.escape(manager_name(manager_id, names))
            lines.append(f"   {who} — {approved} ✅ / {rejected} ❌")
        if auto_rejected:
            lines.append(f"🤖 Автоотказ: {auto_rejected}")
            for rule_name, count in stats.get("auto_reject_rules", [])[:MAX_ROWS]:
                lines.append(f"   «{html.escape(str(rule_name))}» — {count}")
    else:
        lines.append(f"📋 Заявки: сегодня тихо, ждут {stats['apps_pending']}" + _walkin_suffix(stats))

    if game_active:
        lines.append(
            f"🎮 Геймификация: сдач {stats['game_submissions']} · "
            f"проверено {stats['game_reviewed']} · баллов начислено {stats['coins_awarded']}"
        )
        for manager_id, reviewed in stats["game_managers"][:MAX_ROWS]:
            who = html.escape(manager_name(manager_id, names))
            lines.append(f"   {who} — {reviewed}")
    else:
        lines.append("🎮 Геймификация: сегодня тихо")

    return "\n".join(lines)


def unreachable_line(names: list[str]) -> str | None:
    """29.09: одна строка «кому из получателей сводки бот не может написать» — или None.
    Имена экранируются здесь. Кто именно и с какого числа — на экране «👥 Роли и доступы»."""
    if not names:
        return None
    who = ", ".join(html.escape(str(n)) for n in names)
    return (
        f"⚠️ Не получают уведомления бота: {who} — заблокировали бота или не нажали /start. "
        "Подробнее — «👥 Роли и доступы»."
    )


# ── Async helpers ─────────────────────────────────────────────────────────────

async def digest_enabled() -> bool:
    return await get_setting_typed("daily_digest_enabled") == "on"


async def digest_cities() -> list[str | None]:
    """Города, по которым идёт сводка. Модуль городов выключен -> `[None]` (одна общая).
    Та же резолюция, что у `services.chat_tracking.bound_chats`. Fail-soft: любой сбой
    реестра городов схлопывается в одну общую сводку, а не отменяет её."""
    try:
        if not await cities_module_on():
            return [None]
        codes = [c["code"] for c in await enabled_cities()]
        return codes or [None]
    except Exception as e:
        logger.error(f"daily_digest: failed to resolve cities: {e}")
        return [None]


async def digest_recipients(city: str | None) -> list[int]:
    """Держатели ОБОИХ прав по этому городу, объединённые по человеку с сохранением порядка.

    Почему не два `notify_by_capability` подряд: менеджер с обоими правами (обычный случай у
    DXP) получил бы две одинаковые сводки. Пустой результат (никто не держит ни одного права)
    достаётся тем же фоллбэком на `config.ADMIN_IDS`, что внутри `notify_by_capability` —
    он живёт в `capability_holders`/вызывающем ниже, не здесь."""
    from handlers.admin_caps import capability_holders  # lazy: см. докстринг модуля
    out: list[int] = []
    seen: set[int] = set()
    for cap in CAPS:
        try:
            holders = await capability_holders(cap, city=city)
        except Exception as e:
            logger.error(f"daily_digest: capability_holders({cap}, city={city!r}) failed: {e}")
            continue
        for uid in holders:
            if uid not in seen:
                seen.add(uid)
                out.append(uid)
    return out


async def send_city_digest(bot, city: str | None) -> int:
    """Собрать и отправить сводку по одному городу. Возвращает число доставленных сообщений
    (0 — и когда день пустой, и когда всё упало: сводка не имеет права ронять джобу)."""
    from config import config
    now = msk_now()
    day = now.strftime("%Y-%m-%d")
    try:
        stats = await daily_digest_stats(day, city_scope=city_scope(city))
    except Exception as e:
        logger.error(f"daily_digest: stats for {city!r} failed: {e}")
        return 0

    # Квик 260923 (D-A): честные цифры автоотказа — отдельно от decided_by. Гейт
    # reject_rules_enabled — событие без модуля автоотказа не тратит запрос впустую, и стата
    # остаётся дефолтной (0/[]) для build_digest_text.
    if await get_setting_typed("reject_rules_enabled"):
        try:
            since = f"{day} 00:00:00"
            until_day = (now + timedelta(days=1)).strftime("%Y-%m-%d")
            count, rules = await auto_reject_summary(
                since=since, until=f"{until_day} 00:00:00", city_scope=city_scope(city),
            )
            stats["auto_rejected"] = count
            stats["auto_reject_rules"] = rules
        except Exception as e:
            logger.warning(f"daily_digest: auto_reject_summary for {city!r} failed: {e}")

    manager_ids = [m[0] for m in stats["app_managers"]] + [m[0] for m in stats["game_managers"]]
    try:
        names = await get_display_names(manager_ids)
    except Exception as e:
        logger.warning(f"daily_digest: name lookup failed: {e}")
        names = {}

    title = None
    if city:
        try:
            title = await city_label(city)
        except Exception as e:
            logger.warning(f"daily_digest: city_label({city!r}) failed: {e}")
            title = city

    text = build_digest_text(
        stats, names, day_label=now.strftime("%d.%m"), city_title=title,
    )
    if text is None:
        logger.info(f"daily_digest: nothing happened today in {city or 'all'} — not sending")
        return 0

    recipients = await digest_recipients(city) or list(config.ADMIN_IDS)
    # 29.09: отметки «не доходит» берутся ДО рассылки — по итогам прошлых неудач; сбой -> {}.
    marks = await staff_reach.unreachable_marks()
    lost = [uid for uid in recipients if uid in marks]
    if lost:
        try:
            lost_names = await get_display_names(lost)
        except Exception as e:
            logger.warning(f"daily_digest: unreachable names lookup failed: {e}")
            lost_names = {}
        text += "\n" + unreachable_line([manager_name(uid, lost_names) for uid in lost])
    sent = 0
    for uid in recipients:
        try:
            await bot.send_message(uid, text, parse_mode="HTML")
            sent += 1
            await staff_reach.note_delivered(uid)
        except Exception as e:
            if staff_reach.is_unreachable_error(e):
                await staff_reach.note_undeliverable(uid, e)
            # Тот же fail-soft, что у notify_by_capability: один сломанный чат не блокирует
            # доставку остальным.
            logger.warning(f"daily_digest: failed to send to {uid}: {e}")
    return sent


async def daily_digest_job() -> int:
    """Cron-job target: аргументов нет (persisted job — только picklable примитивы), Bot
    берётся из `services.scheduler._bot`, как у остальных джоб этого проекта. Выключенный
    тумблер — ранний выход без единого запроса к БД (та же форма, что у
    `allowlist_refresh_job`/`chat_membership_refresh_job`)."""
    from services import scheduler as _sched
    try:
        if not await digest_enabled():
            logger.debug("daily_digest: daily_digest_enabled is off")
            return 0
        total = 0
        for city in await digest_cities():
            total += await send_city_digest(_sched._bot, city)
        return total
    except Exception as e:
        logger.error(f"daily_digest_job failed: {e}")
        return 0
