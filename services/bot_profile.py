"""Описание бота в Telegram (`setMyDescription` / `setMyShortDescription`) из настроек.

Запрос РилТолка 04.10: бот находят раньше канала, а в пустом чате с ботом ни слова о том,
кто это и где соцсети. Описание раньше ставилось только через BotFather — менеджер сам
этого сделать не мог. Теперь это две обычные текстовые настройки (`bot_description`,
`bot_short_description`); применяются при старте бота и сразу после правки в админке.
"""
import logging

from settings_schema import get_setting_typed

logger = logging.getLogger(__name__)

# Пределы Bot API: длиннее Telegram отвергает запрос целиком — режем, а не теряем правку.
DESCRIPTION_LIMIT = 512
SHORT_DESCRIPTION_LIMIT = 120

PROFILE_KEYS = ("bot_description", "bot_short_description")


async def sync_bot_profile(bot) -> None:
    """Пустая настройка — пустое описание (Telegram снимает прежнее). Fail-soft — на вызывающем."""
    description = (await get_setting_typed("bot_description") or "").strip()
    short = (await get_setting_typed("bot_short_description") or "").strip()
    await bot.set_my_description(description=description[:DESCRIPTION_LIMIT])
    await bot.set_my_short_description(short_description=short[:SHORT_DESCRIPTION_LIMIT])


async def on_setting_written(key: str) -> None:
    if key not in PROFILE_KEYS:
        return
    from services.scheduler import get_bot

    try:
        await sync_bot_profile(get_bot())
    except Exception as exc:  # noqa: BLE001 — недоступный Telegram не должен ронять экран настроек
        logger.error("bot_profile: не удалось применить описание бота: %s", exc)
