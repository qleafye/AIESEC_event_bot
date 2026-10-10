"""Квик 260914-rgr (RGR-01..07), правка 15.09 («привязка через личку админа»): доменная
логика учёта чата делегатов — без единого aiogram-хендлера (те живут в
`handlers/group_chat.py`, который и импортирует этот модуль; отдельного экрана «💬 Чат» с
15.09 больше нет — тумблер учёта переехал строкой в `handlers/admin_settings.py`).

Привязка «город -> chat_id» физически хранится в `bot_settings` теми же композитными
ключами, что и любой другой per-city override (`cities.per_city_key`), но читается НЕ через
`cities.get_setting_for_city` (D-7): та функция при выключенном модуле городов молча падает
на глобальный ключ, а нам как раз нужно противоположное — при ВКЛЮЧЁННОМ модуле городов чат
одного города не должен протекать делегатам другого ни при каких обстоятельствах. Поэтому
`bound_chats()` сама решает, глобальный ключ читать или per-city, и никогда не смешивает эти
два источника в одном ответе.

`database/db.py` не может импортировать `cities.py` (цикл) — а этот модуль может (он не
`database/`), поэтому вся резолюция «какие города включены» и сборка per-city ключа живёт
здесь, а не в `db.py`.
"""
from __future__ import annotations

import asyncio
import html
import logging
from datetime import timedelta

from config import config
from cities import (
    ALL_CITIES, cities_module_on, city_scope, enabled_cities, normalize_city, per_city_key,
)
from database.db import (
    chat_member_statuses,
    count_and_list_filtered,
    enqueue_sheet_chat_cells,
    replace_chat_admins,
    set_chat_bot_state,
    stale_chat_member_candidates,
    upsert_chat_member,
    users_status_city,
    CHAT_PRESENT_STATUSES,
)
from services.timeutil import msk_now
from services.settings.audit import delete_setting_by_admin, set_setting_by_admin
from domain.settings.schema import get_setting_typed

logger = logging.getLogger(__name__)
# Итог сверки состава чата: отдельный именованный логгер, который `main._configure_logging`
# выводит на INFO даже там, где корень держит только WARNING (прод).
RECON_LOGGER = logging.getLogger("chat_recon")

CHAT_ID_KEY = "delegate_chat_id"
CHAT_TITLE_KEY = "delegate_chat_title"

# Задача 2 (сверка): батч + пауза держат нагрузку на Telegram API под лимитом (~20 вызовов/с),
# потолок на один прогон — 1500 делегатов расходятся за 2-3 прогона джобы, а не блокируют её
# на часы, если чат огромный.
REFRESH_BATCH = 20
REFRESH_PAUSE_SECONDS = 1.0
REFRESH_MAX_CALLS = 500

# Квик 260915-twr (D2): ключи реестра для личных сообщений админу вокруг разовой сверки
# после привязки чата — group "system" в domain/settings/schema.py, НЕ в _SYSTEM_FIELD_ORDER
# (тот же прецедент, что у CHAT_ID_KEY/CHAT_TITLE_KEY: это служебные сообщения о фоновом
# прогоне, не делегатская копирайтинг-копия, экран настроек их не показывает).
CHAT_BIND_RECONCILE_START_KEY = "chat_bind_reconcile_start_text"
CHAT_BIND_RECONCILE_DONE_KEY = "chat_bind_reconcile_done_text"

# Через сколько после привязки ставится разовая сверка — не мгновенно (перепривязка/сетевая
# пауза после ответа Telegram на chatbind ещё не улеглась), но и не позже "быстрого" ощущения
# для админа, который только что тапнул кнопку города.
_BIND_RECONCILE_DELAY = timedelta(seconds=5)


def _is_absent_error(exc: Exception) -> bool:
    """`PARTICIPANT_ID_INVALID` / `USER_ID_INVALID` от `getChatMember` — Telegram отвечает
    ровно одно: такого участника в этом чате нет (аккаунт удалён, id не виден боту). Это
    полноценный ответ «нет в чате», а не сбой запроса.

    Сверка по ТЕКСТУ исключения, не по классу `aiogram.exceptions.TelegramBadRequest`: модуль
    намеренно aiogram-free (докстринг :1-17), а фейковые боты в тестах бросают обычные
    `Exception` — текстовая сверка работает для обоих и не тянет aiogram в `services/`."""
    text = str(exc).upper()
    return "PARTICIPANT_ID_INVALID" in text or "USER_ID_INVALID" in text


async def tracking_on() -> bool:
    return await get_setting_typed("chat_tracking_enabled") == "on"


async def bound_chats() -> list[dict]:
    """Список привязанных чатов: `{"city": код|None, "chat_id": int, "title": str}`.

    Модуль городов ВЫКЛЮЧЕН -> максимум одна запись из глобальных ключей (`city: None`).
    Модуль городов ВКЛЮЧЁН -> по одной записи на КАЖДЫЙ включённый город, читая ТОЛЬКО
    per-city ключ (`per_city_key(CHAT_ID_KEY, code)`) — БЕЗ фолбэка на глобальный (D-7):
    иначе чат Москвы протёк бы делегатам любого другого города, у которого своей привязки
    ещё нет. Мусорное/непарсящееся значение (не целое число) пропускается с
    `logger.warning` по ключу — сверке и экрану нужен целый chat_id, а не что попало."""
    if not await cities_module_on():
        raw_id = await get_setting_typed(CHAT_ID_KEY)
        chat_id = _parse_chat_id(CHAT_ID_KEY, raw_id)
        if chat_id is None:
            return []
        title = await get_setting_typed(CHAT_TITLE_KEY) or ""
        return [{"city": None, "chat_id": chat_id, "title": title}]

    out: list[dict] = []
    for city in await enabled_cities():
        code = city["code"]
        id_key = per_city_key(CHAT_ID_KEY, code)
        if id_key is None:
            continue
        raw_id = await get_setting_typed(id_key)
        chat_id = _parse_chat_id(id_key, raw_id)
        if chat_id is None:
            continue
        title_key = per_city_key(CHAT_TITLE_KEY, code)
        title = (await get_setting_typed(title_key)) if title_key else ""
        out.append({"city": code, "chat_id": chat_id, "title": title or ""})
    return out


def _parse_chat_id(key: str, raw: str | None) -> int | None:
    if not raw:
        return None
    try:
        return int(str(raw).strip())
    except (TypeError, ValueError):
        logger.warning("chat_tracking: неразбираемый chat_id в ключе %s: %r", key, raw)
        return None


async def bind_chat(admin_id: int | None, chat_id: int, title: str, city: str | None) -> None:
    """Записывает привязку. `city is None` -> глобальные ключи; иначе — per-city пара.
    Неизвестный код города (`per_city_key` вернул `None`) — тихий no-op, вызывающий
    (`handlers/group_chat.py`) уже проверил код по `city_codes()`/`enabled_cities()` до
    вызова, повторная проверка здесь — страховка, не основной путь."""
    if city is None or city == ALL_CITIES:
        await set_setting_by_admin(admin_id, CHAT_ID_KEY, str(chat_id))
        await set_setting_by_admin(admin_id, CHAT_TITLE_KEY, title or "")
        return
    id_key = per_city_key(CHAT_ID_KEY, city)
    title_key = per_city_key(CHAT_TITLE_KEY, city)
    if id_key is None or title_key is None:
        logger.warning("chat_tracking.bind_chat: неизвестный код города %r — привязка отклонена", city)
        return
    await set_setting_by_admin(admin_id, id_key, str(chat_id))
    await set_setting_by_admin(admin_id, title_key, title or "")


async def unbind_chat(admin_id: int | None, city: str | None) -> None:
    """Снимает привязку (удаляет пару ключей). Данные `chat_members`/`chat_activity`/
    `chat_events` НЕ трогает — это дело `database.db.purge_chat_data`, отдельный явный шаг
    только из экрана отвязки в админке (задача 2), а не побочный эффект каждого `unbind_chat`
    (например, авто-снятие привязки при выходе бота из чата, задача 1, данные оставляет)."""
    if city is None or city == ALL_CITIES:
        await delete_setting_by_admin(admin_id, CHAT_ID_KEY)
        await delete_setting_by_admin(admin_id, CHAT_TITLE_KEY)
        return
    id_key = per_city_key(CHAT_ID_KEY, city)
    title_key = per_city_key(CHAT_TITLE_KEY, city)
    if id_key is None or title_key is None:
        return
    await delete_setting_by_admin(admin_id, id_key)
    await delete_setting_by_admin(admin_id, title_key)


async def chat_for_city(city: str | None) -> dict | None:
    """Обратный резолв: привязка (если есть) для конкретного города (или глобальная,
    если `city is None`/модуль городов выключен)."""
    for entry in await bound_chats():
        if entry["city"] == city:
            return entry
    return None


async def city_for_chat(chat_id: int) -> str | None:
    """Обратный резолв: код города (или `None` для глобальной привязки) по chat_id —
    `sentinel` `ALL_CITIES` не возвращается, привязка всегда живёт под конкретным кодом."""
    for entry in await bound_chats():
        if entry["chat_id"] == chat_id:
            return entry["city"]
    return None


async def is_bot_admin_user(telegram_id: int) -> bool:
    """Смеет привязывать/отвязывать чат тот же круг людей, что держит право `settings` —
    привязка чата это настройка интеграции (D-1), не действие над конкретной заявкой.
    Ленивый импорт `handlers.admin_caps` — `services` не тянет `handlers` на уровне модуля
    (тот же приём, что у остальных `services/*`, импортирующих `handlers.*` лениво)."""
    if telegram_id in config.ADMIN_IDS:
        return True
    from handlers.admin_caps import resolve_capabilities

    return "settings" in await resolve_capabilities(telegram_id)


# ── 29.09: колонка «В чате» в листе делегатов ────────────────────────────────────────────

CHAT_CELL_YES = "да"
CHAT_CELL_NO = "нет"
CHAT_CELL_UNKNOWN = "не проверено"
CHAT_CELL_NA = "-"


async def chat_cell_values(telegram_ids: list[int]) -> dict[int, str]:
    """ЕДИНСТВЕННЫЙ источник значения ячейки «В чате» (строка листа, пересборка, очередь).

    Одобренный делегат -> «да» (статус в чате СВОЕГО города из `CHAT_PRESENT_STATUSES`),
    «нет» (запись есть, но не присутствие), «не проверено» (записи нет — сверка ещё не
    спрашивала). Не одобрен, не зарегистрирован или у его города чат не привязан -> «-».
    Чат города выбирается по `bound_chats()` с той же семантикой, что `city_scope`
    (`normalize_city`: NULL/мусор -> город по умолчанию), без фолбэка на чужой чат (D-7).
    Запросов к базе: один по users + по одному на задействованный чат (чанками)."""
    ids = list(dict.fromkeys(int(t) for t in telegram_ids))
    out = {tid: CHAT_CELL_NA for tid in ids}
    if not ids:
        return out
    chats = await bound_chats()
    if not chats:
        return out
    users = await users_status_city(ids)
    module_on = await cities_module_on()
    chat_by_city = {entry["city"]: entry["chat_id"] for entry in chats}
    by_chat: dict[int, list[int]] = {}
    for tid in ids:
        status, event_city = users.get(tid, (None, None))
        if status != "approved":
            continue
        chat_id = chat_by_city.get(normalize_city(event_city) if module_on else None)
        if chat_id is None:
            continue
        by_chat.setdefault(chat_id, []).append(tid)
    for chat_id, members in by_chat.items():
        statuses = await chat_member_statuses(chat_id, members)
        for tid in members:
            if tid not in statuses:
                out[tid] = CHAT_CELL_UNKNOWN
            elif statuses[tid] in CHAT_PRESENT_STATUSES:
                out[tid] = CHAT_CELL_YES
            else:
                out[tid] = CHAT_CELL_NO
    return out


async def chat_cells_map() -> dict[int, str]:
    """{telegram_id: «В чате»} для всех одобренных — массовая пересборка листа одним проходом.
    Кого нет в ответе, тому «-» (не одобрен)."""
    approved = await count_and_list_filtered([{"field": "status", "value": "approved"}])
    return await chat_cell_values(approved)


# ── Задача 2: периодическая сверка состава ───────────────────────────────────────────────

async def _approved_ids_for_city(city: str | None) -> list[int]:
    """Одобренные делегаты города чата, переиспользуя готовый фильтр рассылки
    (`database.db.count_and_list_filtered`/`_build_filter_clause`) — вторая копия SQL-условия
    городского скоупа здесь не заводится. `city is None` -> без городского фрагмента вовсе
    (глобальная привязка или выключенный модуль городов)."""
    filters = [{"field": "status", "value": "approved"}]
    scope = city_scope(city) if city is not None else None
    if scope is not None:
        code, exclude = scope
        filters.append({"field": "event_city", "value": code, "exclude": list(exclude)})
    return await count_and_list_filtered(filters)


async def refresh_chat(bot, chat_id: int, city: str | None, *,
                        max_calls: int = REFRESH_MAX_CALLS) -> dict:
    """Сверяет состав ОДНОГО чата с Telegram: берёт одобренных делегатов его города, спрашивает
    `getChatMember` только у тех, чья запись в `chat_members` отсутствует или устарела
    (`chat_refresh_minutes`), батчами по `REFRESH_BATCH` с паузой `REFRESH_PAUSE_SECONDS` между
    батчами, каждый вызов — в своём `try/except` (fail-soft: одна ошибка не рвёт прогон)."""
    threshold_minutes = await get_setting_typed("chat_refresh_minutes")
    older_than = (msk_now() - timedelta(minutes=threshold_minutes)).strftime("%Y-%m-%d %H:%M:%S")
    approved_ids = await _approved_ids_for_city(city)
    candidates = await stale_chat_member_candidates(chat_id, approved_ids, older_than)

    checked = present = absent = not_found = errors = 0
    truncated = False
    for batch_start in range(0, len(candidates), REFRESH_BATCH):
        batch = candidates[batch_start:batch_start + REFRESH_BATCH]
        for telegram_id in batch:
            if checked >= max_calls:
                truncated = True
                break
            try:
                member = await bot.get_chat_member(chat_id, telegram_id)
                await upsert_chat_member(chat_id, telegram_id, member.status, source="refresh")
                if member.status in CHAT_PRESENT_STATUSES:
                    present += 1
                else:
                    absent += 1
            except Exception as e:
                if _is_absent_error(e):
                    # Квик 260915-twr (D1): 441 такой делегат на проде уходил сюда как «сбой»
                    # и перепроверялся каждые 6 часов впустую — это ответ «нет в чате», не
                    # ошибка, поэтому errors НЕ растёт и warning на каждого из 441 не пишется.
                    await upsert_chat_member(chat_id, telegram_id, "left", source="refresh")
                    not_found += 1
                else:
                    errors += 1
                    logger.warning(
                        "chat_tracking.refresh_chat: getChatMember(%s, id=%s) failed: %s: %s",
                        chat_id, telegram_id, type(e).__name__, e,
                    )
            checked += 1
        if truncated:
            break
        if batch_start + REFRESH_BATCH < len(candidates):
            await asyncio.sleep(REFRESH_PAUSE_SECONDS)

    # Колонка «В чате» в листе: пересчитать ВСЕХ одобренных города (первичное заполнение после
    # выката и «не проверено» -> «да»/«нет»); значение джоба возьмёт из базы. Fail-soft внутри.
    await enqueue_sheet_chat_cells(approved_ids)

    logger.info(
        "chat_tracking.refresh_chat: chat_id=%s city=%s checked=%s present=%s absent=%s "
        "not_found=%s errors=%s truncated=%s",
        chat_id, city, checked, present, absent, not_found, errors, truncated,
    )
    return {"checked": checked, "present": present, "absent": absent, "not_found": not_found,
            "errors": errors, "truncated": truncated}


def can_delete_from(member) -> bool:
    """Право «Удаление сообщений» по объекту участника (ChatMember* или его заглушке):
    владелец — всё может, администратор — по флагу, остальные — нет."""
    status = getattr(member, "status", None)
    if status == "creator":
        return True
    if status == "administrator":
        return bool(getattr(member, "can_delete_messages", False))
    return False


def moderates(member) -> bool:
    """Админ группы с реальными правами модерации: владелец или право удалять сообщения /
    ограничивать участников. Админ «ради подписи» (кастомный титул без прав) — не команда."""
    status = getattr(member, "status", None)
    if status == "creator":
        return True
    if status != "administrator":
        return False
    return bool(getattr(member, "can_delete_messages", False)
                or getattr(member, "can_restrict_members", False))


async def refresh_chat_admins(bot, chat_id: int) -> bool:
    """Квик 260927: один вызов getChatAdministrators — и админы группы с правами модерации
    (`moderates`; команда, которую дашборд держит вне рейтинга), и состояние САМОГО бота (`chat_bot_state`: статус и право
    «Удаление сообщений») на случай, если апдейт my_chat_member потерялся. Бота нет среди
    админов -> он обычный участник, удалять не может. Fail-soft: сбой — прежние данные
    остаются."""
    try:
        admins = await bot.get_chat_administrators(chat_id)
    except Exception as e:
        logger.info("chat_tracking.refresh_chat_admins: чат id=%s: %s: %s", chat_id, type(e).__name__, e)
        return False
    people = [a.user.id for a in admins if not a.user.is_bot and moderates(a)]
    await replace_chat_admins(chat_id, people)
    own = next((a for a in admins if a.user.id == getattr(bot, "id", None)), None)
    if own is None:
        await set_chat_bot_state(chat_id, "member", False)
    else:
        await set_chat_bot_state(chat_id, getattr(own, "status", None), can_delete_from(own))
    return True


async def _cleanup_enabled() -> bool:
    """Отмечен ли хоть один тип автоочистки служебных уведомлений (ленивый импорт:
    services.chat_cleanup сам импортирует этот модуль)."""
    from services.chat_cleanup import ticked_codes

    return bool(await ticked_codes())


async def refresh_all_chats(bot) -> list[dict]:
    """Сверяет ВСЕ привязанные чаты. Тумблер учёта выключен -> пустой список, ни одного вызова
    `get_chat_member` (ни у одного чата). Админов и права САМОГО бота (одним
    getChatAdministrators на чат) перечитываем и при выключенном учёте, если включена
    автоочистка: иначе однажды снятый флаг «нет прав» не вернулся бы никогда."""
    tracking = await tracking_on()
    if not tracking and not await _cleanup_enabled():
        return []
    reports = []
    for entry in await bound_chats():
        if tracking:
            report = await refresh_chat(bot, entry["chat_id"], entry["city"])
            reports.append({**report, "chat_id": entry["chat_id"], "city": entry["city"]})
        await refresh_chat_admins(bot, entry["chat_id"])
    return reports


# ── 29.09: сверка состава по кнопке менеджера ────────────────────────────────────────────

# Флаг, а не asyncio.Lock: проверка-и-установка без await между ними атомарна в одном цикле,
# и флаг не привязан к event loop (тесты гоняют каждый сценарий своим asyncio.run).
_reconcile_running = False


def claim_reconcile() -> bool:
    """Занять сверку. False — уже идёт (второе нажатие во время прогона)."""
    global _reconcile_running
    if _reconcile_running:
        return False
    _reconcile_running = True
    return True


def release_reconcile() -> None:
    global _reconcile_running
    _reconcile_running = False


async def reconcile_all_now(bot, *, claimed: bool = False) -> list[dict] | None:
    """Сверка ВСЕХ привязанных чатов по кнопке «🔄 Сверить состав чата». Тумблер учёта здесь
    НЕ гейтит — по той же причине, что в `bind_reconcile_job`: это явное действие менеджера, а
    не фоновая активность. Сама сверка — `refresh_chat` (батчи, пауза, потолок, fail-soft) и
    `refresh_chat_admins`; второй копии логики нет. Кто свежепроверен (`chat_refresh_minutes`),
    повторно не спрашивается — повторное нажатие дозапрашивает остальных, а не тех же 500.

    Возвращает по чату `{chat_id, city, city_label, title, in_chat, not_in_chat, unknown,
    not_found, errors, truncated, checked}` (in/not_in/unknown — по базе ПОСЛЕ сверки, по
    всем одобренным города) или `None`, если сверка уже идёт. `claimed=True` — флаг уже занят
    вызывающим (`claim_reconcile`), здесь только снимается."""
    if not claimed and not claim_reconcile():
        return None
    try:
        from cities import city_label  # ленивый: чистая подпись города для отчёта

        reports = []
        for entry in await bound_chats():
            chat_id, city = entry["chat_id"], entry["city"]
            report = await refresh_chat(bot, chat_id, city)
            admins_ok = await refresh_chat_admins(bot, chat_id)
            approved = await _approved_ids_for_city(city)
            statuses = await chat_member_statuses(chat_id, approved)
            in_chat = sum(1 for s in statuses.values() if s in CHAT_PRESENT_STATUSES)
            reports.append({
                **report, "chat_id": chat_id, "city": city, "title": entry["title"],
                "city_label": (await city_label(city)) if city else None,
                "in_chat": in_chat, "not_in_chat": len(statuses) - in_chat,
                "unknown": len(approved) - len(statuses),
                "approved": len(approved), "admins_ok": admins_ok,
            })
            log_reconcile_summary(reports[-1])
        if not reports:
            RECON_LOGGER.warning("chat_recon: сверка пустая — ни один чат делегатов не привязан")
        return reports
    finally:
        release_reconcile()


def reconcile_problem(rep: dict) -> str | None:
    """Почему итог сверки пустой или ошибочный, одной фразой без PII; `None` — всё в порядке."""
    if not rep.get("approved"):
        return "нет одобренных делегатов города — сверять некого"
    if rep.get("errors"):
        return (f"ошибки getChatMember: {rep['errors']} — проверьте, что бот админ чата "
                "и видит участников")
    if rep.get("admins_ok") is False:
        return "не удалось прочитать админов чата — у бота нет прав или его убрали из чата"
    if not rep.get("in_chat") and not rep.get("checked"):
        return "никто не проверен и в чате не найдено ни одного делегата"
    if not rep.get("in_chat"):
        return "в чате не найдено ни одного одобренного делегата"
    return None


def log_reconcile_summary(rep: dict) -> None:
    """Одна строка на чат: WARNING с причиной, если сверка пустая/ошибочная, иначе INFO с
    итогом. Только счётчики и код города — никаких имён, юзернеймов и телефонов."""
    base = (
        f"chat_recon: chat_id={rep['chat_id']} city={rep.get('city') or '-'} "
        f"approved={rep.get('approved', 0)} checked={rep.get('checked', 0)} "
        f"in_chat={rep.get('in_chat', 0)} not_in_chat={rep.get('not_in_chat', 0)} "
        f"unknown={rep.get('unknown', 0)} not_found={rep.get('not_found', 0)} "
        f"errors={rep.get('errors', 0)} sheet_queued={rep.get('approved', 0)}"
    )
    problem = reconcile_problem(rep)
    if problem:
        RECON_LOGGER.warning("%s — %s", base, problem)
    else:
        RECON_LOGGER.info("%s — ok", base)


def reconcile_report_text(reports: list[dict]) -> str:
    """Итог сверки для лички менеджера — по строке на чат, по-человечески."""
    if not reports:
        return "Чат делегатов не подключён — добавьте бота в группу администратором."
    lines = ["🔄 <b>Сверка состава чата завершена</b>", ""]
    retry = False
    for rep in reports:
        title = html.escape(rep.get("title") or "чат")
        where = f"Чат «{title}»" + (f" ({rep['city_label']})" if rep.get("city_label") else "")
        lines.append(
            f"{where}: в чате — {rep['in_chat']}, не в чате — {rep['not_in_chat']}, "
            f"аккаунт не найден — {rep['not_found']}, не удалось проверить — {rep['errors']}."
        )
        if rep.get("unknown"):
            lines.append(f"Ещё не проверены: {rep['unknown']}.")
        retry = retry or bool(rep.get("errors")) or bool(rep.get("truncated")) or bool(rep.get("unknown"))
    lines += ["", "Колонка «В чате» в таблице обновится в течение пары минут."]
    if retry:
        lines.append("Нажмите кнопку ещё раз позже, чтобы дозапросить остальных.")
    return "\n".join(lines)


# ── Квик 260915-twr (D2): разовая сверка сразу после привязки чата ───────────────────────

async def bind_reconcile_job(chat_id: int, city: str | None, admin_id: int) -> None:
    """Цель date-джобы APScheduler, поставленной `schedule_bind_reconcile` сразу после
    привязки чата. Модульного уровня, аргументы — только picklable-скаляры (int / str|None /
    int): APScheduler пиклит цель по ссылке `модуль:имя`, замыкание или объект здесь не
    переживёт сериализацию.

    `tracking_on()` здесь НЕ гейтит (в отличие от `refresh_all_chats`): менеджер только что
    привязал чат явным тапом по кнопке города, эта сверка — прямое следствие ЕГО действия, а
    не фоновая активность, подчинённая общему тумблеру учёта.

    Весь вызов — в `try/except`: упавшая джоба не должна ронять планировщик, а без личного
    отчёта админ просто узнает состав чата на общей плановой сверке."""
    try:
        import services.scheduler as scheduler_module  # ленивый импорт (aiogram-free модуль)

        bot = scheduler_module.get_bot()
        report = await refresh_chat(bot, chat_id, city)
        await refresh_chat_admins(bot, chat_id)
        text = (await get_setting_typed(CHAT_BIND_RECONCILE_DONE_KEY)).format(
            present=report["present"], absent=report["absent"], not_found=report["not_found"],
        )
        await bot.send_message(admin_id, text)
    except Exception as e:
        logger.error(
            "chat_tracking.bind_reconcile_job: сверка чата id=%s после привязки упала: %s: %s",
            chat_id, type(e).__name__, e,
        )


async def schedule_bind_reconcile(chat_id: int, city: str | None, admin_id: int) -> None:
    """Ставит разовую сверку этого чата через `_BIND_RECONCILE_DELAY` после привязки.
    `replace_existing=True` — перепривязка того же чата не плодит вторую джобу.

    Fail-soft ВОКРУГ ВСЕГО вызова (`logger.warning`, не `error`): привязка чата обязана
    состояться и без планировщика — в существующих тестах `test_chat_binding_260914.py`
    планировщик не инициализирован вовсе, и это критерий приёмки, не побочная деталь."""
    try:
        from services.scheduler import get_scheduler

        get_scheduler().add_job(
            bind_reconcile_job, "date", run_date=msk_now() + _BIND_RECONCILE_DELAY,
            args=[chat_id, city, admin_id], id=f"chatbind_reconcile_{chat_id}",
            replace_existing=True,
        )
    except Exception as e:
        logger.warning(
            "chat_tracking.schedule_bind_reconcile: не удалось поставить сверку чата id=%s: %s: %s",
            chat_id, type(e).__name__, e,
        )
