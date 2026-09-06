"""Phase 23.1-05 (UI-REDESIGN-06): общее московское «сегодня» для miniapp-роутеров.

Вынесено из `miniapp/routers/hub.py` (план 23.1-03) — `tasks.py` (план 23.1-05, строка
«сколько дней осталось» до дедлайна задания) на плане самого плана 23.1-05 попросил не
копировать локальный помощник хаба, а вынести общее место. `services/scheduler.py` держит
СВОЙ `MOSCOW_TZ` (TZFIX-260816) — это не второй, случайно разошедшийся литерал часового пояса,
а второй, ОСОЗНАННЫЙ: `services.scheduler` тянет aiogram + APScheduler, а `miniapp/` обязан
оставаться aiogram-free (D-01, `miniapp/deps.py`) — ребра `miniapp -> services.scheduler` нет
и не будет. Квик 260906-52m свёл бывший четвёртый литерал (`miniapp/routers/admin_tasks.py`,
дедлайны задач менеджера, план 16) сюда — тот роутер теперь импортирует `now_msk_naive`
отсюда вместо собственной копии `MOSCOW_TZ`/`now_moscow_naive`. Осознанных литералов
Europe/Moscow под всем проектом два: `services/timeutil.py` и этот файл.
"""
from __future__ import annotations

from datetime import date, datetime
from zoneinfo import ZoneInfo

MOSCOW_TZ = ZoneInfo("Europe/Moscow")


def today_msk() -> date:
    """Московская календарная дата «сейчас» — единая точка входа для всех miniapp-роутеров,
    которым нужно посчитать «сколько дней осталось» без aiogram-зависимостей."""
    return datetime.now(MOSCOW_TZ).date()


def now_msk_naive() -> datetime:
    """Quick 260904-dq1: naive московское «сейчас» для веб-процесса — тот же контракт, что
    `services.scheduler._now_moscow_naive()` у бота (TZFIX-260816: naive, сравнимо с naive
    временем из настроек), но СВОЙ `MOSCOW_TZ` этого файла (докстринг модуля: ребра
    `miniapp -> services.scheduler` нет и не будет)."""
    return datetime.now(MOSCOW_TZ).replace(tzinfo=None)
