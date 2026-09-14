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
import logging
from datetime import timedelta

from config import config
from cities import ALL_CITIES, cities_module_on, city_scope, enabled_cities, per_city_key
from database.db import (
    count_and_list_filtered,
    stale_chat_member_candidates,
    upsert_chat_member,
    CHAT_PRESENT_STATUSES,
)
from services.timeutil import msk_now
from settings_audit import delete_setting_by_admin, set_setting_by_admin
from settings_schema import get_setting_typed

logger = logging.getLogger(__name__)

CHAT_ID_KEY = "delegate_chat_id"
CHAT_TITLE_KEY = "delegate_chat_title"

# Задача 2 (сверка): батч + пауза держат нагрузку на Telegram API под лимитом (~20 вызовов/с),
# потолок на один прогон — 1500 делегатов расходятся за 2-3 прогона джобы, а не блокируют её
# на часы, если чат огромный.
REFRESH_BATCH = 20
REFRESH_PAUSE_SECONDS = 1.0
REFRESH_MAX_CALLS = 500


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

    checked = present = absent = errors = 0
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

    return {"checked": checked, "present": present, "absent": absent, "errors": errors,
            "truncated": truncated}


async def refresh_all_chats(bot) -> list[dict]:
    """Сверяет ВСЕ привязанные чаты. Тумблер выключен -> пустой список, ни одного вызова
    `get_chat_member` (ни у одного чата)."""
    if not await tracking_on():
        return []
    reports = []
    for entry in await bound_chats():
        report = await refresh_chat(bot, entry["chat_id"], entry["city"])
        reports.append({**report, "chat_id": entry["chat_id"], "city": entry["city"]})
    return reports
