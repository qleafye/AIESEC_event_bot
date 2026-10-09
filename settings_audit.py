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


async def run_setting_hooks(key: str) -> None:
    """Реакции бота на правку ключа. Зовётся и после записи из бота, и разборщиком очереди
    приложения (`settings_changed`): правка в приложении должна действовать так же сразу.
    Каждая реакция в своём try — сбой одной не отменяет остальные и не роняет запись."""
    from services import bot_profile, daily_digest, reject_rules_notify, scheduler

    for hook in (reject_rules_notify.on_setting_written, bot_profile.on_setting_written,
                 daily_digest.on_setting_written, scheduler.on_setting_written):
        try:
            await hook(key)
        except Exception as exc:  # noqa: BLE001 — реакция на правку не имеет права уронить запись
            logger.error("settings_audit: реакция на %r сорвалась: %s", key, exc)


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
