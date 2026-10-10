"""Команды в синей кнопке «Меню» Telegram (`setMyCommands`) — из настроек, без BotFather.

Делегаты видят только /start (`BotCommandScopeDefault`). Организаторы — суперадмины из
`config.ADMIN_IDS` и все с действующей ролью в `staff` — ещё и /admin: у каждого свой список
(`BotCommandScopeChat`), делегату чужой не показывается. Английская версия — тот же список с
`language_code="en"`: Telegram сам покажет её тем, у кого приложение на английском.

Не задано или пусто — список не трогаем (ловушка описания бота: пустая настройка стирала то,
что задано в BotFather). Значение читается из базы как есть, без дефолта реестра: первый старт
после выката ничего не меняет, пока менеджер сам не впишет подписи. Пустое /start — бот не
ставит общий список; пустое /admin — не ставит и не снимает личные списки организаторов;
пустая английская версия — не ставит английские.

Кому личный список поставлен, бот запоминает (`STAFF_IDS_KEY`, служебная строка настроек) и при
следующем синке снимает его у тех, кто больше не организатор, — /admin пропадает из их меню.

Применяется при старте бота (фоном: личных списков может быть сотня) и после правки любой из
четырёх настроек — из бота и из приложения (`settings_audit.run_setting_hooks`). Правки
склеиваются: сохранение четырёх подписей разом из приложения даёт один синк, а не четыре.
Ответ Telegram «подождите» (429) выдерживается и запрос повторяется один раз. Роль, выданная
или снятая между синками, попадёт в меню при следующем старте или правке подписей; сама /admin
работает и без строки в меню.
"""
import asyncio
import json
import logging

from config import config
from database import db

logger = logging.getLogger(__name__)

# Предел Bot API на описание команды.
DESCRIPTION_LIMIT = 256
# Пауза между личными списками (лимит Telegram ~30 запросов в секунду) и окно склейки правок.
CALL_PAUSE_SECONDS = 0.05
DEBOUNCE_SECONDS = 2.0
# Служебная строка настроек (не ключ реестра): кому поставлен личный список с /admin.
STAFF_IDS_KEY = "bot_commands_staff_ids"

# (язык Telegram, ключ описания /start, ключ описания /admin); None — список по умолчанию.
LANG_KEYS = (
    (None, "bot_command_start_text", "bot_command_admin_text"),
    ("en", "bot_command_start_text_en", "bot_command_admin_text_en"),
)
COMMAND_KEYS = frozenset(k for _, start, admin in LANG_KEYS for k in (start, admin))

_lock = asyncio.Lock()
_pending: asyncio.Task | None = None


async def _description(key: str) -> str:
    """Сырое значение из базы: нет строки или пусто — пустая строка (не трогать)."""
    return (await db.get_setting(key) or "").strip()[:DESCRIPTION_LIMIT]


async def organizer_ids() -> list[int]:
    """Суперадмины и держатели действующей роли — те, кому открывается /admin."""
    from services.staff_expiry import is_expiry_active, today_iso

    today = today_iso()
    ids = list(dict.fromkeys(config.ADMIN_IDS or ()))
    for row in await db.list_staff():
        if is_expiry_active(row.get("expires_at"), today) and row["telegram_id"] not in ids:
            ids.append(row["telegram_id"])
    return ids


async def _stored_staff_ids() -> list[int]:
    try:
        return [int(x) for x in json.loads(await db.get_setting(STAFF_IDS_KEY) or "[]")]
    except (TypeError, ValueError):
        return []


async def _call(what: str, make_call) -> bool:
    """Один запрос к Telegram: 429 — выждать `retry_after` и повторить один раз; любая другая
    ошибка — в лог, остальные запросы синка идут дальше."""
    from aiogram.exceptions import TelegramRetryAfter

    for attempt in (1, 2):
        try:
            await make_call()
            return True
        except TelegramRetryAfter as exc:
            if attempt == 2:
                logger.warning("bot_commands: %s — Telegram снова просит подождать", what)
                return False
            await asyncio.sleep(exc.retry_after + 1)
        except Exception as exc:  # noqa: BLE001 — один отказ не срывает остальные списки
            logger.info("bot_commands: %s не применено: %s", what, exc)
            return False
    return False


async def sync_bot_commands(bot) -> None:
    """Ставит списки команд по настройкам и снимает /admin у бывших организаторов."""
    from aiogram.types import BotCommand, BotCommandScopeChat, BotCommandScopeDefault

    organizers: list[int] | None = None
    for lang, start_key, admin_key in LANG_KEYS:
        start = await _description(start_key)
        if not start:
            continue
        public = [BotCommand(command="start", description=start)]
        await _call(f"общий список ({lang or 'ru'})", lambda p=public, lg=lang: bot.set_my_commands(
            p, scope=BotCommandScopeDefault(), language_code=lg))
        admin = await _description(admin_key)
        if not admin:
            continue
        if organizers is None:
            organizers = await organizer_ids()
        staff = public + [BotCommand(command="admin", description=admin)]
        for chat_id in organizers:
            await _call(f"меню организатора {chat_id} ({lang or 'ru'})",
                        lambda c=chat_id, s=staff, lg=lang: bot.set_my_commands(
                            s, scope=BotCommandScopeChat(chat_id=c), language_code=lg))
            await asyncio.sleep(CALL_PAUSE_SECONDS)
    if organizers is None:
        return  # /admin не задан ни на одном языке — личные списки не наши, не трогаем
    current = set(organizers)
    for chat_id in [c for c in await _stored_staff_ids() if c not in current]:
        for lang, _start, _admin in LANG_KEYS:
            await _call(f"снятие /admin у {chat_id} ({lang or 'ru'})",
                        lambda c=chat_id, lg=lang: bot.delete_my_commands(
                            scope=BotCommandScopeChat(chat_id=c), language_code=lg))
            await asyncio.sleep(CALL_PAUSE_SECONDS)
    await db.set_setting(STAFF_IDS_KEY, json.dumps(sorted(current)))


async def sync_bot_commands_safe(bot) -> None:
    async with _lock:  # старт и правка не идут параллельно
        try:
            await sync_bot_commands(bot)
        except Exception as exc:  # noqa: BLE001 — недоступный Telegram не должен ронять бота/экран
            logger.error("bot_commands: не удалось поставить команды меню: %s", exc)


async def _delayed_sync(bot) -> None:
    global _pending
    await asyncio.sleep(DEBOUNCE_SECONDS)
    _pending = None  # правка во время синка запланирует ещё один — она не потеряется
    await sync_bot_commands_safe(bot)


def schedule_sync(bot) -> asyncio.Task:
    """Синк через `DEBOUNCE_SECONDS`; правки внутри окна склеиваются в один."""
    global _pending
    if _pending is None or _pending.done():
        from services.background import spawn

        _pending = spawn(_delayed_sync(bot))
    return _pending


async def on_setting_written(key: str) -> None:
    if key not in COMMAND_KEYS:
        return
    from services.bot_profile import _running_bot  # вне процесса бота — поставит старт бота

    bot = _running_bot()
    if bot is not None:
        schedule_sync(bot)
