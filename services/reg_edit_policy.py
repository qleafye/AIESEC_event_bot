"""Квик 260911-w2m: единственное место, где живёт правило «можно ли делегату сейчас
редактировать уже ПОДАННУЮ анкету». Три врезки гейта (профиль/PATCH/submit приложения,
вход в правку из чата) зовут только `edit_gate` — второй копии этого правила в проекте
нет.

Модуль aiogram-free и на уровне импорта, И на уровне вызова (тот же контракт, что
`services/quiet_hours.py`): его импортирует и вызывает и бот (`handlers/registration.py`,
`handlers/reg_resume.py`), и веб-процесс Mini App (`miniapp/routers/profile.py`,
`miniapp/routers/form.py`) — сторож aiogram-free `miniapp/deps.py` подпроцессом проверяет
именно это.

Р-1 (план, декларации): отклонённый делегат («status == rejected») под гейт не попадает
вовсе, а не «кроме rejected» отдельной веткой — `reg_engine.has_submitted_anketa` уже
ложна для rejected (правка отклонённой заявки технически неотличима от первичной подачи,
D-10 повторной подачи). Второй трактовки статуса здесь не заводим.

Р-2: пустой статус (`user_row.get("status")` -> None/"") читается как «одобрена» — тем же
выражением `(status or "approved")`, что `miniapp/routers/profile.py`/`handlers/
registration.py` уже применяют в других местах. Импортированная строка без статуса ведёт
себя как approved только когда положение реально смотрит на статус («until_decision») —
при дефолте "always" эта ветка вообще не читается.
"""
from __future__ import annotations

import logging

import reg_engine
from settings_schema import SETTINGS_SCHEMA, get_setting_typed

logger = logging.getLogger(__name__)

ALWAYS = "always"
UNTIL_DECISION = "until_decision"
NEVER = "never"


def edit_allowed_for(policy: str, *, submitted: bool, status: str | None) -> bool:
    """Чистая функция правила — без единого чтения БД/реестра, тестируется параметрически.

    Первичная подача (`submitted` ложно) не гейтится НИ ПРИ КАКОМ положении — это главный
    сторож задачи, тумблер про правку технически не может ничего запретить тому, кто анкету
    ещё не подавал. Неизвестное/будущее значение `policy` (например опечатка миграции)
    fail-soft уходит в «разрешено» — тот же принцип, что у `_next_resume_mode` в
    `handlers/admin_reg_percity.py`."""
    if not submitted:
        return True
    if policy == NEVER:
        return False
    if policy == UNTIL_DECISION:
        return (status or "approved") != "approved"
    return True


async def edit_gate(user_row: dict | None) -> tuple[bool, str | None]:
    """Единая асинхронная точка входа для всех трёх поверхностей гейта.

    `user_row` может быть пустым словарём/None (делегат без анкеты вовсе) —
    `reg_engine.has_submitted_anketa` переваривает это сама (пустая строка -> `submitted`
    ложно -> разрешено).

    Возвращает `(True, None)` при разрешённой правке — БЕЗ чтения текста отказа (лишнее
    чтение реестра на каждое открытие профиля/анкеты). При запрете — `(False, text)`, где
    `text` никогда не пуст: пустое значение в БД подменяется дефолтом из `SETTINGS_SCHEMA`
    (делегат не должен получить пустое сообщение).

    Любой сбой чтения реестра — fail-soft `(True, None)` с `logger.error`, тем же приёмом,
    что и прочие резолвы настроек в проекте (никогда не блокировать делегата из-за сбоя
    инфраструктуры)."""
    try:
        policy = await get_setting_typed("reg_edit_policy")
        season = await get_setting_typed("event_season") or None
        submitted = reg_engine.has_submitted_anketa(user_row, season)
        status = (user_row or {}).get("status")
        if edit_allowed_for(policy, submitted=submitted, status=status):
            return True, None
        text = await get_setting_typed("reg_edit_closed_text")
        if not text:
            text = SETTINGS_SCHEMA["reg_edit_closed_text"]["default"]
        return False, text
    except Exception:
        logger.error("reg_edit_policy.edit_gate: сбой чтения реестра, fail-soft к «разрешено»", exc_info=True)
        return True, None
