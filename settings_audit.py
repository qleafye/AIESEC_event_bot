"""Квик 260913-16o: воронка записи настроек с автором в логе.

Инцидент прода 06.09: в 05:06 UTC кто-то переключил `full_approval` в «авто», 38 заявок
одобрились молча, и установить автора было нечем — `database.db.set_setting` логирует только
ключ и значение, без того, КТО нажал.

Этот модуль — единственная воронка для 73 из 75 вызовов `set_setting`/`delete_setting` из
`handlers/*.py`: перед настоящей записью в лог уходит строка с `admin=<id>`, отличимая от
строки самой БД префиксом. Сигнатуры `database.db.set_setting`/`delete_setting` НЕ меняются —
вторая строка лога из БД остаётся, двойная запись — цена того, что воронка не единственная
точка входа в `bot_settings`.

Корневой модуль (не `handlers/`): без aiogram-импортов, не тянет цикл `handlers.admin` ↔ шов
и не попадает под потолок размера `handlers/*.py` (`tests/test_module_size_convention_260816.py`).

Вызов настоящей записи — ЧЕРЕЗ АТРИБУТ МОДУЛЯ (`from database import db`, внутри
`db.set_setting(...)`), а не `from database.db import set_setting`: тесты монкейпатчат
`database.db.set_setting`, и импорт-по-имени такой подмены не увидит.

`admin_id=None` допустим (вызов не из-под пользователя — например, технический штамп) и
логируется как есть.

Второе назначение воронки (план 31-12, D-14): ПОСЛЕ настоящей записи — реакция на правку
анкеты. Экранов, где менеджер трогает вопросы анкеты и списки вариантов ответа, несколько
(общие настройки, per-city переопределения, списки вариантов), а воронка записи одна — второго
места, которое пришлось бы синхронно поддерживать при появлении нового экрана, не заводим.
Импорт `services.reject_rules_notify` — ЛЕНИВЫЙ, внутри каждой функции (корневой модуль не
должен тянуть `services/*` на уровне модуля и рисковать циклом), и в собственном
`try/except` — сохранение настройки менеджера важнее реакции на неё и не имеет права упасть
из-за её сбоя. Поведение самой записи (сигнатуры, `admin_id=None`, строка лога) не меняется.
"""
from __future__ import annotations

import logging

from database import db

logger = logging.getLogger(__name__)


async def run_setting_hooks(key: str, *, reject_rules: bool = True, reschedule: bool = True) -> None:
    """Реакции бота на правку ключа. Зовётся и после записи из бота, и разборщиком очереди
    приложения (`settings_changed`): правка в приложении должна действовать так же сразу.
    Каждая реакция в своём try — сбой одной не отменяет остальные и не роняет запись."""
    from services import bot_commands, bot_profile, daily_digest, menu_labels, reject_rules_notify, scheduler
    from settings_reschedule import reschedule_for_setting

    hooks = [bot_profile.on_setting_written, bot_commands.on_setting_written,
             daily_digest.on_setting_written,
             scheduler.on_setting_written, menu_labels.on_setting_written]
    if reschedule:
        hooks.append(reschedule_for_setting)
    if reject_rules:
        hooks.insert(0, reject_rules_notify.on_setting_written)
    for hook in hooks:
        try:
            await hook(key)
        except Exception as exc:  # noqa: BLE001 — реакция на правку не имеет права уронить запись
            logger.error("settings_audit: реакция на %r сорвалась: %s", key, exc)
    if key in MENU_BUTTON_KEYS:
        _start_menu_button_resync()


# Кнопку меню чата (иконка приложения у поля ввода) Telegram держит до новой установки: общую —
# для всех, и свою — у каждого, кто выбрал язык (`handlers/reg_lang.py`), своя главнее общей.
# После правки подписи или включения/выключения приложения переставляются обе, в фоне: на пару
# тысяч делегатов это пара минут, сохранение настройки ждать их не должно.
#
# Один проход на процесс: новое сохранение отменяет идущий проход и начинает заново. Проход
# читает настройки в момент установки кнопки, поэтому побеждает последнее сохранение, а не тот
# из двух параллельных проходов, что закончил позже.
MENU_BUTTON_KEYS = frozenset({"miniapp_open_button", "miniapp_enabled"})
_menu_resync_task = None

_RESYNC_RUNNING_NOTE = (
    "\n\n📱 Кнопка приложения у поля ввода обновится у всех делегатов в ближайшие минуты — "
    "у каждого на его языке."
)
_RESYNC_NOT_STARTED_NOTE = (
    "\n\n⚠️ Кнопку приложения у поля ввода сейчас переставить не удалось — у делегатов пока "
    "старая. Сохраните текст ещё раз через пару минут."
)


def after_save_note(key: str) -> str:
    """Строка менеджеру под «сохранено» для правки, которая доходит до делегатов не сразу:
    идёт ли перестановка кнопки меню чата или её не удалось начать."""
    if key not in MENU_BUTTON_KEYS:
        return ""
    task = _menu_resync_task
    return _RESYNC_RUNNING_NOTE if task is not None and not task.done() else _RESYNC_NOT_STARTED_NOTE


def _start_menu_button_resync() -> None:
    import asyncio

    global _menu_resync_task
    from services.scheduler import get_bot

    try:
        bot = get_bot()
    except RuntimeError:  # процесс без бота (тест, скрипт) — переставлять нечем
        logger.info("settings_audit: бота в процессе нет, кнопку меню чата не переставляю")
        return
    from handlers.admin_miniapp import sync_all_chat_menu_buttons

    if _menu_resync_task is not None and not _menu_resync_task.done():
        _menu_resync_task.cancel()  # устаревший проход — новое сохранение начнёт заново
    _menu_resync_task = asyncio.get_running_loop().create_task(sync_all_chat_menu_buttons(bot))


async def run_setting_hooks_batch(keys: list[str], *, reject_rules: bool = True) -> None:
    """Хуки для пачки ключей, записанных одним действием (пресет типа события). Каждая реакция
    узкая — срабатывает только на «свои» ключи, повторные сообщения людям не рассылает.
    Автоотказ — исключение: он пересчитывает и пишет держателям права, поэтому зовётся ОДИН
    раз на пачку (`on_settings_written_batch`), а не по ключу; `reject_rules=False` — писатель
    уже сделал это сам (`reg_presets.apply_reg_preset`)."""
    unique = list(dict.fromkeys(keys))
    for key in unique:
        await run_setting_hooks(key, reject_rules=False, reschedule=False)
    # Перепланировка — одна на модуль и город за всю пачку, а не по ключу.
    from settings_reschedule import reschedule_for_settings

    try:
        await reschedule_for_settings(unique)
    except Exception as exc:  # noqa: BLE001 — реакция на правку не имеет права уронить запись
        logger.error("settings_audit: перепланировка пачки сорвалась: %s", exc)
    if reject_rules:
        from services import reject_rules_notify

        await reject_rules_notify.on_settings_written_batch(list(keys))


async def set_setting_by_admin(admin_id: int | None, key: str, value: str) -> None:
    logger.info(f"admin={admin_id} setting {key} <- {value!r}")
    previous = await db.get_setting(key) if key == "bot_name" else None
    await db.set_setting(key, value)
    await run_setting_hooks(key)
    if key == "bot_name":
        # Имя бота ставится в Telegram ПОСЛЕ записи; отказ Telegram возвращает прежнее
        # значение (`revert_setting` ниже) и сообщает менеджеру — services/bot_profile.py.
        from services.bot_profile import after_name_saved_by_admin

        await after_name_saved_by_admin(admin_id, previous)


async def write_setting_logged(admin_id: int | None, key: str, value: str | None) -> None:
    """Запись с автором в логе, но БЕЗ реакций бота: для веб-процесса Mini App, где бота и токена
    нет. Реакции (имя/описание бота, перепланировка джоб, кнопка меню) разбирает бот сам —
    вызывающий ставит в очередь `settings_changed` (`miniapp.outbox.enqueue`). `value=None` —
    сброс значения."""
    if value is None:
        logger.info(f"admin={admin_id} setting {key} <- (сброшено)")
        await db.delete_setting(key)
        return
    logger.info(f"admin={admin_id} setting {key} <- {value!r}")
    await db.set_setting(key, value)


async def revert_setting(admin_id: int | None, key: str, previous: str | None) -> None:
    """Откат значения после отказа внешней стороны (Telegram не принял имя бота). Без
    реакций на правку: возвращается то, что уже действовало."""
    logger.info(f"admin={admin_id} setting {key} <- {previous!r} (откат: Telegram не принял)")
    if previous is None:
        await db.delete_setting(key)
    else:
        await db.set_setting(key, previous)


async def delete_setting_by_admin(admin_id: int | None, key: str) -> None:
    logger.info(f"admin={admin_id} setting {key} <- (сброшено)")
    await db.delete_setting(key)
    await run_setting_hooks(key)
