"""Координатор 25.09 (учёт доставки решения) — общий 429-ретрай для ОДИНОЧНОЙ отправки, тот же
паттерн, что уже даёт результат в `services/broadcast_run.py::run_broadcast`/`run_revoke`
(один повтор после `TelegramRetryAfter`, `sleep(retry_after + 1)`), но без пакетного цикла —
здесь ровно одна логическая отправка на вызов.

Нужен отдельным модулем, а не дублируется в `services/applications/application_effects.py`/
`handlers/reg/reg_schema.py`, потому что ОБА места — одобрение (send_completion_and_bonus) и отказ
(apply_decision_effects) — обязаны ретраить одинаково: «📨 Переотправить решения»
(services/applications/decision_delivery.py) зовёт ТОТ ЖЕ код формирования письма, что и обычное решение
(задача координатора, п.4: «не дублируй тексты») — если бы ретрай жил в двух местах, поведение
неизбежно разъехалось бы при следующей правке одного из них."""
from __future__ import annotations

import asyncio
from typing import Awaitable, Callable

from aiogram.exceptions import TelegramRetryAfter


async def send_with_retry(attempt: Callable[[], Awaitable[None]]) -> Exception | None:
    """`attempt` — асинхронная фабрика без аргументов, вызывающая ровно одну логическую
    отправку (может быть `bot.send_message(...)` напрямую или обёртка вроде
    `quiet_hours.send_or_queue_text(...)` — не важно, лишь бы сетевой вызов внутри поднимал
    исключение при сбое, а не глотал его сам).

    Возврат: `None` — успех (с ретраем или без), иначе — итоговое исключение (после ОДНОГО
    429-ретрая, если он случился, тот же лимит, что у broadcast_run). Никогда не поднимает
    исключение сама — вызывающий сам решает, что сделать с ошибкой (залогировать/записать учёт
    доставки/и то и другое)."""
    try:
        await attempt()
        return None
    except TelegramRetryAfter as e:
        await asyncio.sleep(e.retry_after + 1)
        try:
            await attempt()
            return None
        except Exception as e2:
            return e2
    except Exception as e:
        return e
