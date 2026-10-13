"""Кому открыто приложение — одно правило для бота и для веб-процесса.

Тумблер «📱 Приложение включено» выключает приложение ДЕЛЕГАТАМ. Персонал (хоть одно право
приложения — те же права, что в админке бота) пользуется им всегда: сканер на входе, мастер
«🚀 Первая настройка» и поиск настроек есть только там. Веб-гейт — `miniapp/main.py::
_passes_when_off`, точки входа в боте — меню «📱 Приложение» и его хендлер.

Модуль без aiogram и без fastapi: его импортируют и бот, и `miniapp/deps.py`."""
from __future__ import annotations

from domain.settings.schema import get_setting_typed

# Права, у которых в приложении нет ни одного экрана: «🔗 Ссылки с метками» живут только в
# боте. Держатель одной такой роли персоналом приложения не считается.
BOT_ONLY_CAPS = frozenset({"source_links"})


def is_app_staff(caps) -> bool:
    return bool(set(caps or ()) - BOT_ONLY_CAPS)


async def miniapp_open_to_delegates() -> bool:
    """Приложение открыто ДЕЛЕГАТАМ: включено и не стоит «🔒 Только менеджерам». Одно правило
    для всех точек входа делегата (меню, кнопка анкеты, кнопка меню чата) — делегат не видит
    кнопку, которая ответила бы ему «только для менеджеров»."""
    if await get_setting_typed("miniapp_enabled") != "on":
        return False
    return await get_setting_typed("miniapp_staff_only") != "on"


async def miniapp_open_for(telegram_id: int | None) -> bool:
    """Включено — открыто всем; выключено — только персоналу. Сбой чтения прав — «закрыто»."""
    if await get_setting_typed("miniapp_enabled") == "on":
        return True
    if telegram_id is None:
        return False
    from handlers.access.admin_caps import resolve_capabilities  # ленивый: тянет aiogram, нужен только боту

    return is_app_staff(await resolve_capabilities(telegram_id))
