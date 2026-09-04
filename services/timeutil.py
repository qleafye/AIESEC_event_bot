"""Quick 260904-kk6 (Q1): leaf-модуль часового пояса — единственный (кроме `miniapp/timeutil.py`,
см. её докстринг) файл `services/*.py`/`handlers/*.py`, где назван часовой пояс Europe/Moscow.

Переезд из `services/scheduler.py` (TZFIX-260816): `services/questions.py::format_stamp`
обязан переводить UTC-метки в МСК на отображении, но импортировать `services.scheduler` не
может — тот модуль тянет aiogram + APScheduler, а `services/questions.py` импортируется из
`miniapp/routers/questions.py`, где ребра `miniapp -> aiogram` нет и не будет (D-01,
докстринг `miniapp/timeutil.py`). Свой второй литерал в `questions.py` тоже нельзя: сторож
`tests/test_timezone_fix_260816.py::test_moscow_literal_declared_exactly_once` требует ровно
одно вхождение строкового литерала этого пояса во всём `services/*.py` + `handlers/*.py`.
Поэтому литерал переезжает сюда — в leaf-модуль на голой stdlib (ни одного импорта проекта), а
`services/scheduler.py` реэкспортирует `MOSCOW_TZ` отсюда: все существующие
`from services.scheduler import MOSCOW_TZ` (game_digest.py, tests/test_scheduler_restart_260816.py,
tests/test_timezone_fix_260816.py, tests/test_polls_260822.py) продолжают работать без правок.

`miniapp/timeutil.py` СОХРАНЯЕТ свою собственную копию `MOSCOW_TZ` — её докстринг уже
объясняет, почему это третий (и с `miniapp/routers/admin_tasks.py` — четвёртый) осознанный
литерал, а не второй случайно разошедшийся. Объединение leaf-модулей `services/timeutil.py`
и `miniapp/timeutil.py` в один — отдельный тикет, не этот квик.
"""
from __future__ import annotations

from datetime import datetime, timezone
from zoneinfo import ZoneInfo

MOSCOW_TZ = ZoneInfo("Europe/Moscow")


def utc_naive_to_msk(dt: datetime) -> datetime:
    """Naive UTC datetime -> naive московский datetime.

    Трактует `dt` как UTC (`.replace(tzinfo=timezone.utc)`, не `.astimezone` — `dt` не несёт
    своей зоны), переводит в `MOSCOW_TZ` и снова снимает tzinfo: весь проект оперирует
    naive-временем (naive сравнивается с naive, `datetime.utcnow()`/`datetime.now()` тоже
    naive) — возвращать aware-объект значило бы завести второй, несовместимый со всем
    остальным кодом, тип метки времени.
    """
    return dt.replace(tzinfo=timezone.utc).astimezone(MOSCOW_TZ).replace(tzinfo=None)
