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


async def set_setting_by_admin(admin_id: int | None, key: str, value: str) -> None:
    logger.info(f"admin={admin_id} setting {key} <- {value!r}")
    await db.set_setting(key, value)
    try:
        from services import reject_rules_notify as _rrn
        await _rrn.on_setting_written(key)
    except Exception as exc:  # noqa: BLE001 — реакция на правку не имеет права уронить запись
        logger.error("settings_audit.set_setting_by_admin: реакция на %r сорвалась: %s", key, exc)


async def delete_setting_by_admin(admin_id: int | None, key: str) -> None:
    logger.info(f"admin={admin_id} setting {key} <- (сброшено)")
    await db.delete_setting(key)
    try:
        from services import reject_rules_notify as _rrn
        await _rrn.on_setting_written(key)
    except Exception as exc:  # noqa: BLE001 — реакция на правку не имеет права уронить запись
        logger.error("settings_audit.delete_setting_by_admin: реакция на %r сорвалась: %s", key, exc)
