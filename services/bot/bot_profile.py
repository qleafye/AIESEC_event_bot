"""Профиль бота в Telegram из настроек: имя (`setMyName`), описание (`setMyDescription` /
`setMyShortDescription`).

Запрос РилТолка 04.10: бот находят раньше канала, а в пустом чате с ботом ни слова о том,
кто это и где соцсети. Описание раньше ставилось только через BotFather — менеджер сам
этого сделать не мог. Теперь это две обычные текстовые настройки (`bot_description`,
`bot_short_description`); применяются при старте бота и сразу после правки в админке.

09.10: имя бота (`bot_name`) — тоже настройка. В отличие от описания, имя Telegram даёт
менять редко (ответ 429 с ожиданием до суток), поэтому:
  * до записи проверяются только длина и пустота (`settings_ops.cross_setting_error`) —
    Telegram там не зовём: запись ещё может не состояться (подтверждение, отмена), и у бота
    осталось бы имя, которого нет в настройках;
  * после записи из бота (`settings_audit.set_setting_by_admin`) и после разбора очереди
    приложения (`settings_changed`) имя ставится в Telegram; отказ — в настройке
    возвращается прежнее значение, автору правки уходит понятная ошибка;
  * старт бота ставит имя, только если в Telegram сейчас другое (`getMyName` дешёвый) —
    перезапуски не тратят лимит;
  * пустое значение имя в Telegram не трогает: снять имя бот не может, только сменить.
"""
import logging
import sys

from domain.settings.schema import get_setting_typed

logger = logging.getLogger(__name__)

# Пределы Bot API: длиннее Telegram отвергает запрос целиком — режем, а не теряем правку.
DESCRIPTION_LIMIT = 512
SHORT_DESCRIPTION_LIMIT = 120
NAME_LIMIT = 64

PROFILE_KEYS = ("bot_description", "bot_short_description")
NAME_KEY = "bot_name"


async def sync_bot_profile(bot) -> None:
    """Пустая настройка — пустое описание (Telegram снимает прежнее). Fail-soft — на вызывающем."""
    description = (await get_setting_typed("bot_description") or "").strip()
    short = (await get_setting_typed("bot_short_description") or "").strip()
    await bot.set_my_description(description=description[:DESCRIPTION_LIMIT])
    await bot.set_my_short_description(short_description=short[:SHORT_DESCRIPTION_LIMIT])


async def on_setting_written(key: str) -> None:
    # `bot_name` здесь НЕ применяется: из бота его ставит проверка до записи, из приложения —
    # разбор очереди с ответом автору (см. докстринг модуля). Второй вызов `setMyName` на ту же
    # правку только тратил бы редкий лимит Telegram.
    if key not in PROFILE_KEYS:
        return
    from services.scheduler import get_bot

    try:
        await sync_bot_profile(get_bot())
    except Exception as exc:  # noqa: BLE001 — недоступный Telegram не должен ронять экран настроек
        logger.error("bot_profile: не удалось применить описание бота: %s", exc)


# ── Имя бота ──────────────────────────────────────────────────────────────────

def name_length_error(value: str | None) -> str | None:
    if value and len(value.strip()) > NAME_LIMIT:
        return (f"Имя бота длиннее {NAME_LIMIT} символов ({len(value.strip())}) — Telegram такое "
                "не примет. Сократите и пришлите ещё раз.")
    return None


def _wait_text(seconds: int) -> str:
    minutes = max(1, -(-int(seconds) // 60))
    if minutes < 60:
        return f"{minutes} мин"
    hours, rest = divmod(minutes, 60)
    return f"{hours} ч {rest} мин" if rest else f"{hours} ч"


async def apply_bot_name(bot, name: str | None) -> str | None:
    """Ставит имя в Telegram. `None` — получилось (или менять нечего), иначе готовый текст
    ошибки для менеджера. Пустое имя и имя, совпадающее с текущим, Telegram не трогают."""
    from aiogram.exceptions import TelegramBadRequest, TelegramRetryAfter

    name = (name or "").strip()
    if not name:
        return None
    error = name_length_error(name)
    if error:
        return error
    try:
        current = await bot.get_my_name()
        if getattr(current, "name", None) == name:
            return None
        await bot.set_my_name(name=name)
    except TelegramRetryAfter as exc:
        logger.warning("bot_profile: Telegram просит подождать со сменой имени %s с", exc.retry_after)
        return ("Telegram разрешает менять имя бота лишь несколько раз подряд, лимит сейчас "
                f"исчерпан. Попробуйте снова через {_wait_text(exc.retry_after)}. "
                "Имя пока осталось прежним.")
    except TelegramBadRequest as exc:
        logger.warning("bot_profile: Telegram не принял имя %r: %s", name, exc)
        return ("Telegram не принял это имя. Попробуйте другое — покороче, без ссылок и "
                "служебных символов. Имя пока осталось прежним.")
    except Exception as exc:  # noqa: BLE001 — сеть/прокси: человеку — что делать, в лог — причину
        logger.error("bot_profile: не удалось сменить имя бота: %s", exc)
        return ("Не получилось связаться с Telegram, имя бота не изменилось. Попробуйте "
                "сохранить ещё раз через минуту.")
    logger.info("bot_profile: имя бота изменено на %r", name)
    return None


def _running_bot():
    """Bot процесса бота или `None` (процесс приложения, тесты). Через `sys.modules`, а не
    импорт: в процессе приложения планировщика нет и тянуть его туда незачем."""
    sched = sys.modules.get("services.scheduler")
    return getattr(sched, "_bot", None) if sched is not None else None


async def precheck_bot_name(value: str | None) -> str | None:
    """Проверка `bot_name` до записи (`settings_ops.cross_setting_error`): только длина и
    пустота, Telegram не зовём (см. докстринг модуля)."""
    if value is not None and not value.strip():
        return "Имя бота не может быть пустым. Пришлите имя текстом, например «Юлид’26 · регистрация»."
    return name_length_error(value)


async def apply_saved_name(bot, previous: str | None, author_id: int | None) -> str | None:
    """Ставит в Telegram только что сохранённое имя. Отказ Telegram — прежнее значение
    настройки возвращается (если её не успели поменять ещё раз), автору — сообщение.
    Возвращает текст ошибки или `None`."""
    from database import db
    from services.settings.audit import revert_setting  # ленивый: settings_audit лениво зовёт этот модуль

    attempted = await db.get_setting(NAME_KEY)
    error = await apply_bot_name(bot, attempted)
    if not error:
        return None
    if await db.get_setting(NAME_KEY) == attempted:
        await revert_setting(author_id, NAME_KEY, previous)
    if author_id:
        kept = f"\n\nВ настройке осталось прежнее имя «{previous}»." if previous else ""
        try:
            await bot.send_message(author_id, f"⚠️ Имя бота не сменилось.\n\n{error}{kept}")
        except Exception as exc:  # noqa: BLE001 — автор мог заблокировать бота; в логе причина
            logger.warning("bot_profile: не удалось сообщить %s об ошибке имени: %s", author_id, exc)
    return error


async def after_name_saved_by_admin(admin_id: int | None, previous: str | None) -> None:
    """Запись из бота (`settings_audit.set_setting_by_admin`). Вне процесса бота — ничего:
    имя поставит бот (старт или очередь приложения)."""
    bot = _running_bot()
    if bot is not None:
        await apply_saved_name(bot, previous, admin_id)


async def apply_name_from_app(bot, author_id: int | None, previous: str | None = None) -> None:
    """Имя, сохранённое в приложении (разбор очереди `settings_changed`)."""
    await apply_saved_name(bot, previous, author_id)


async def sync_bot_name(bot) -> None:
    """Старт бота: имя из настроек, если задано и в Telegram другое. Fail-soft — в лог."""
    error = await apply_bot_name(bot, await get_setting_typed(NAME_KEY))
    if error:
        logger.warning("bot_profile: имя бота на старте не применено: %s", error)
