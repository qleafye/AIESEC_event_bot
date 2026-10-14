"""Общий debounce «последний вызов побеждает» для фоновых пересборок (вкладки Google-таблицы).

Форма — как у `services/game/game_sync.py`: каждый `request()` отменяет ещё ждущий таймер и
заводит новый, поэтому пачка событий даёт одну пересборку через `delay` секунд после последнего.
Задачу регистрирует тот слой, который знает, что пересобирать (`set_job`) — этот модуль ничего
не знает о таблицах и не импортирует handlers.

Сбой (исключение или кортеж результатов с отрицательным числом — `sync_named_worksheet` вернул -1)
шлёт админам ОДНО предупреждение на серию сбоев; успех сбрасывает флаг.
"""
from __future__ import annotations

import asyncio
import logging

from services.infra.background import spawn

logger = logging.getLogger(__name__)


class DebouncedJob:
    def __init__(self, name: str, alert_text: str, delay: float = 30):
        self.name = name
        self.alert_text = alert_text
        self.delay = delay
        self._job = None
        self.pending: "asyncio.Task | None" = None
        self._failure_warned = False

    def set_job(self, fn) -> None:
        """Зарегистрировать корутинную функцию, которую `request()` в итоге вызовет."""
        self._job = fn

    def request(self, delay: float | None = None) -> None:
        """Поставить (или переставить) отложенный запуск. Синхронный, ничего не ждёт."""
        if self.pending is not None and not self.pending.done():
            self.pending.cancel()
        self.pending = spawn(self._run_after(self.delay if delay is None else delay))

    async def _alert(self) -> None:
        from services.sheets.sheets import _send_admin_alert  # sheets тянет gspread — лениво
        await _send_admin_alert(self.alert_text)

    async def _run_after(self, delay: float) -> None:
        try:
            await asyncio.sleep(delay)
        except asyncio.CancelledError:
            return  # перекрыт более поздним request() — свою работу не делает
        if self._job is None:
            # Процесс, где пересборка не зарегистрирована (веб Mini App), — не ошибка.
            logger.debug("%s: пересборка не зарегистрирована в этом процессе — пропуск", self.name)
            return
        try:
            result = await self._job()
        except Exception:
            logger.exception("%s: фоновая пересборка упала", self.name)
            if not self._failure_warned:
                self._failure_warned = True
                await self._alert()
            return
        ok = True
        if isinstance(result, tuple) and result:
            ok = all(n >= 0 for n in result)
        if ok:
            self._failure_warned = False
        elif not self._failure_warned:
            self._failure_warned = True
            await self._alert()

    def reset_for_tests(self) -> None:
        self.pending = None
        self._failure_warned = False
        self._job = None
