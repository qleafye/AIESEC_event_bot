"""Тексты геймы и волн по городу делегата и подстановка `{chat}` — ссылки на чат его города.

Список городских текстов и ключ ссылки — `domain/settings/city_chat.py`. Здесь — чтение:
бот, джобы планировщика и Mini App берут текст отсюда, чтобы один и тот же делегат везде видел
текст своего города.

Порядок для каждого места: шаблон города (фолбэк — общий) → перевод (`tr` места) → замена
`{chat}` → остальные подстановки места. Перевод ищется по исходному шаблону с `{chat}`, поэтому
английский делегат получает перевод, а не русский текст со ссылкой.

Город — у ПОЛУЧАТЕЛЯ (`users.event_city`), а не у волны или менеджера; модуль городов выключен —
None (общий текст, байт-в-байт как раньше). aiogram-free: модуль зовёт и веб-процесс Mini App.
"""
from __future__ import annotations

import logging

from domain.settings.city_chat import CHAT_LINK_KEY

logger = logging.getLogger(__name__)


async def delegate_city(who) -> str | None:
    """Код города делегата: `who` — telegram_id (читается users) или уже прочитанная строка
    users (dict, без БД). Модуль городов выключен, делегата нет или сбой — None."""
    from domain import cities

    if who is None:
        return None
    try:
        if not await cities.cities_module_on():
            return None
        if isinstance(who, dict):
            row = who
        else:
            from database.db import get_user
            row = await get_user(int(who))
        return cities.normalize_city(row.get("event_city") if row else None)
    except Exception as e:  # экран/рассылка не ломаются из-за города — будет общий текст
        logger.warning("city_texts.delegate_city(%r) failed: %s", who if not isinstance(who, dict) else "row", e)
        return None


async def city_chat_link(city: str | None) -> str:
    """Ссылка на чат города. Модуль городов включён — ТОЛЬКО городская настройка, без фолбэка
    на общую: ссылка одного города не уходит делегатам другого. Выключен — общая."""
    from database.db import get_setting
    from domain import cities

    if not await cities.cities_module_on():
        return await get_setting(CHAT_LINK_KEY) or ""
    if not city or city == cities.ALL_CITIES:
        return ""
    key = cities.per_city_key(CHAT_LINK_KEY, city)
    if not key:
        return ""
    return await get_setting(key) or ""


async def fill_chat(text, city: str | None):
    """Заменить `{chat}` ссылкой на чат города; нет `{chat}` — текст как есть, без чтения БД."""
    if not isinstance(text, str) or "{chat}" not in text:
        return text
    return text.replace("{chat}", await city_chat_link(city))


async def city_template(key: str, who) -> tuple[str, str | None]:
    """Сырой шаблон города делегата (фолбэк — общий) и сам город — для мест, где перевод и
    подстановки делает своя функция (tr_fmt, fill_template); `{chat}` — через `fill_chat`."""
    from domain.cities import get_setting_typed_for_city

    city = await delegate_city(who)
    raw = await get_setting_typed_for_city(key, city)
    return (raw or ""), city


async def city_text(key: str, who, *, lang=None, tr_map=None, tr=None) -> str:
    """Готовый текст делегату: шаблон города → перевод (если задан `lang`) → `{chat}`."""
    raw, city = await city_template(key, who)
    text = raw
    if lang is not None and raw:
        if tr is None:
            from services.i18n import i18n as _i18n
            tr = _i18n.tr
        text = tr(raw, lang, tr_map or {})
    return await fill_chat(text, city)
