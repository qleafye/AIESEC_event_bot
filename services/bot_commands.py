"""Команды в синей кнопке «Меню» Telegram (`setMyCommands`) — из настроек, без BotFather.

Делегаты видят только /start (`BotCommandScopeDefault`). Организаторы — суперадмины из
`config.ADMIN_IDS` и все с действующей ролью в `staff` — ещё и /admin: у каждого свой список
(`BotCommandScopeChat`), делегату чужой не показывается. Английская версия — тот же список с
`language_code="en"`: Telegram сам покажет её тем, у кого приложение на английском.

Пустое описание — список не трогаем (ловушка описания бота: пустая настройка стирала то, что
задано в BotFather). Пустое /start — бот не ставит общий список вовсе; пустое /admin — не
ставит списки организаторам; пустая английская версия — не ставит английские списки.

Применяется при старте бота (фоном: личных списков может быть сотня) и после правки любой из
четырёх настроек — из бота и из приложения (`settings_audit.run_setting_hooks`). Роль, выданная
или снятая между ними, попадёт в меню при следующем старте или правке описаний; сама /admin
работает и без строки в меню.
"""
import logging

from config import config
from settings_schema import get_setting_typed

logger = logging.getLogger(__name__)

# Предел Bot API на описание команды.
DESCRIPTION_LIMIT = 256

# (язык Telegram, ключ описания /start, ключ описания /admin); None — список по умолчанию.
LANG_KEYS = (
    (None, "bot_command_start_text", "bot_command_admin_text"),
    ("en", "bot_command_start_text_en", "bot_command_admin_text_en"),
)
COMMAND_KEYS = frozenset(k for _, start, admin in LANG_KEYS for k in (start, admin))


async def _description(key: str) -> str:
    return (await get_setting_typed(key) or "").strip()[:DESCRIPTION_LIMIT]


async def organizer_ids() -> list[int]:
    """Суперадмины и держатели действующей роли — те, кому открывается /admin."""
    from database.db import list_staff
    from services.staff_expiry import is_expiry_active, today_iso

    today = today_iso()
    ids = list(dict.fromkeys(config.ADMIN_IDS or ()))
    for row in await list_staff():
        if is_expiry_active(row.get("expires_at"), today) and row["telegram_id"] not in ids:
            ids.append(row["telegram_id"])
    return ids


async def sync_bot_commands(bot) -> None:
    """Ставит списки команд по настройкам. Fail-soft по личным спискам: человек, не нажавший
    /start, Telegram'у неизвестен — его пропускаем, остальных ставим."""
    from aiogram.types import BotCommand, BotCommandScopeChat, BotCommandScopeDefault

    organizers: list[int] | None = None
    for lang, start_key, admin_key in LANG_KEYS:
        start = await _description(start_key)
        if not start:
            continue
        public = [BotCommand(command="start", description=start)]
        await bot.set_my_commands(public, scope=BotCommandScopeDefault(), language_code=lang)
        admin = await _description(admin_key)
        if not admin:
            continue
        if organizers is None:
            organizers = await organizer_ids()
        staff = public + [BotCommand(command="admin", description=admin)]
        for chat_id in organizers:
            try:
                await bot.set_my_commands(staff, scope=BotCommandScopeChat(chat_id=chat_id),
                                          language_code=lang)
            except Exception as exc:  # noqa: BLE001 — один недоступный чат не срывает остальных
                logger.info("bot_commands: меню организатора %s не поставлено: %s", chat_id, exc)


async def sync_bot_commands_safe(bot) -> None:
    try:
        await sync_bot_commands(bot)
    except Exception as exc:  # noqa: BLE001 — недоступный Telegram не должен ронять бота/экран
        logger.error("bot_commands: не удалось поставить команды меню: %s", exc)


async def on_setting_written(key: str) -> None:
    if key not in COMMAND_KEYS:
        return
    from services.bot_profile import _running_bot  # вне процесса бота — поставит старт бота

    bot = _running_bot()
    if bot is not None:
        await sync_bot_commands_safe(bot)
