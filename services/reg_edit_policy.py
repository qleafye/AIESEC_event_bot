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

Квик 260922-wrg (задача 1): второе, независимое правило — «можно ли ОТКЛОНЁННОМУ делегату
подать анкету ЗАНОВО» (`resubmit_allowed_for`/`resubmit_gate`), живёт в этом же модуле рядом
с `edit_gate`, но НЕ смешивается с ним: `edit_gate` отклонённого вообще не гейтит (Р-1 выше),
а `resubmit_gate` гейтит ТОЛЬКО отклонённого. `open_gate` — общая точка входа для трёх
поверхностей Mini App (профиль/форма), которым нужны ОБА правила разом: сначала `edit_gate`
(правка уже поданной), затем, если он разрешил, `resubmit_gate` (повторная подача после
отказа). Гейт не касается ТЕКУЩЕГО сезона делегата, если строка вообще из ПРОШЛОГО сезона —
`reg_engine.is_past_season_row` отличает «отклонён в этом сезоне» (гейтится) от «возвращенец
из прошлого сезона» (не гейтится, он и так проходит первичную подачу).

Правка 260922-wrg (владелец, «настройки должны работать по городам»): `reg_edit_policy` и
`reg_resubmit_after_reject`/`reg_resubmit_closed_text` — `SETTINGS_SCHEMA[key]["per_city"] is
True`. Оба гейта резолвят их через `cities.get_setting_typed_for_city(key,
user_row.get("event_city"))` — та же лестница, что у любого другого per-city ключа (модуль
городов выключен ИЛИ у делегата нет `event_city` ИЛИ у города нет своего значения -> общее
значение байт-в-байт, `cities.py` уже это гарантирует). `event_season`, наоборот, НЕ per-city
(сезон — свойство события целиком, не города) — читается как раньше, простым
`get_setting_typed`."""
from __future__ import annotations

import logging

import reg_engine
from settings_schema import SETTINGS_SCHEMA, get_setting_typed
from cities import get_setting_typed_for_city

logger = logging.getLogger(__name__)

ALWAYS = "always"
UNTIL_DECISION = "until_decision"
NEVER = "never"

# Квик 260922-wrg: положения тумблера «🔁 Повторная подача после отказа».
ALLOW = "allow"
DENY = "deny"


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
    инфраструктуры).

    Правка 260922-wrg: `reg_edit_policy` — per_city, резолвится по `event_city` СТРОКИ
    (`user_row.get("event_city")`), не по городу вызывающего админа/делегата откуда-то ещё —
    единственный источник города здесь та же строка, что несёт остальные поля гейта."""
    try:
        city = (user_row or {}).get("event_city")
        policy = await get_setting_typed_for_city("reg_edit_policy", city)
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


def resubmit_allowed_for(policy: str, *, status: str | None, current_season: bool) -> bool:
    """Чистая функция правила «можно ли подать анкету ЗАНОВО после отказа» — без единого
    чтения БД/реестра, тестируется параметрически (тот же приём, что `edit_allowed_for`).

    Гейт применяется ТОЛЬКО к отклонённому делегату ТЕКУЩЕГО сезона — любой другой статус
    (`approved`/`pending`/пусто) не наш гейт вовсе, а отклонённый ПРОШЛОГО сезона — обычный
    возвращенец (`current_season=False`), ему всегда можно, у него нет «повторной подачи»,
    для него это первичная подача нового сезона. Неизвестное/будущее значение `policy` —
    fail-soft в «разрешено», тот же принцип, что `edit_allowed_for`."""
    if status != "rejected":
        return True
    if not current_season:
        return True
    if policy == DENY:
        return False
    return True


async def resubmit_gate(user_row: dict | None) -> tuple[bool, str | None]:
    """Асинхронная точка входа правила повторной подачи — тот же контракт, что `edit_gate`:
    `(True, None)` при разрешении, `(False, text)` при запрете (текст никогда не пуст —
    пустое значение в БД подменяется дефолтом `SETTINGS_SCHEMA["reg_resubmit_closed_text"]`),
    любой сбой чтения реестра — fail-soft `(True, None)` с `logger.error`.

    Правка 260922-wrg: `reg_resubmit_after_reject`/`reg_resubmit_closed_text` — оба per_city,
    резолвятся по тому же `user_row.get("event_city")`, что и `edit_gate` выше."""
    try:
        city = (user_row or {}).get("event_city")
        policy = await get_setting_typed_for_city("reg_resubmit_after_reject", city)
        season = await get_setting_typed("event_season") or None
        status = (user_row or {}).get("status")
        current_season = not reg_engine.is_past_season_row(user_row, season)
        if resubmit_allowed_for(policy, status=status, current_season=current_season):
            return True, None
        text = await get_setting_typed_for_city("reg_resubmit_closed_text", city)
        if not text:
            text = SETTINGS_SCHEMA["reg_resubmit_closed_text"]["default"]
        return False, text
    except Exception:
        logger.error("reg_edit_policy.resubmit_gate: сбой чтения реестра, fail-soft к «разрешено»", exc_info=True)
        return True, None


async def open_gate(user_row: dict | None) -> tuple[bool, str | None]:
    """Общая точка входа для трёх поверхностей Mini App (профиль/PATCH/submit): сначала
    `edit_gate` (правка уже поданной анкеты), при разрешении — `resubmit_gate` (повторная
    подача после отказа). Порядок важен только для текста алерта — оба правила fail-soft и
    независимы друг от друга."""
    can_edit, text = await edit_gate(user_row)
    if not can_edit:
        return can_edit, text
    return await resubmit_gate(user_row)
