"""Heartbeat-файл для Docker HEALTHCHECK — «жив ли long polling», а не «жив ли процесс».

Зачем отдельный файл. PID 1 контейнера — сам бот, поэтому «процесс запущен» Docker видит
и без healthcheck. Интересен другой вопрос: доходит ли бот до Telegram. Бот может висеть
с живым event loop, но мёртвым поллингом (прокси умер, getUpdates падает в бесконечном
backoff, сессия зависла) — и снаружи это не видно. Heartbeat делает это видимым через
``docker inspect --format '{{.State.Health.Status}}'`` / `docker ps`.

Семантика (три части, все в этом модуле):

1. ``PollingHeartbeatMiddleware`` — session-middleware aiogram (``bot.session.middleware``).
   Срабатывает на КАЖДЫЙ вызов Bot API, но отмечает живость только по успешному ответу
   ``getUpdates`` — это и есть тик поллинга. В простое aiogram делает getUpdates каждые
   ~10 с (long-poll timeout), под нагрузкой — чаще, так что сигнал регулярный. Пока
   поллинг падает (сеть/прокси), отметка не обновляется — heartbeat стареет.
2. ``heartbeat_loop`` — фоновая задача (через ``services.background.spawn``, чтобы
   ``cancel_all`` её гасила на shutdown). Раз в ``HEARTBEAT_INTERVAL`` секунд ПИШЕТ файл,
   но только если поллинг отмечался живым не позже ``POLLING_STALE_SECONDS`` назад.
   Таймер сам по себе файл не трогает — иначе он отражал бы лишь живость цикла.
3. ``clear_heartbeat`` — ``main.py`` вызывает в ``finally``: файл удаляется при любом
   выходе из ``start_polling``, чтобы остановленный бот не выглядел здоровым до
   истечения возраста.

Проверка: ``python -m services.heartbeat --check`` — exit 0, если файл есть и возраст
меньше ``--max-age`` (по умолчанию ``HEALTHCHECK_MAX_AGE`` = 120 с), иначе 1. Это ~4 пропуска
getUpdates-тика подряд / 4 тика таймера — кратковременная ротация прокси healthcheck не
заденет, а реальный простой поллинга проявится за 2–3 минуты.

Важно: Docker сам по себе unhealthy-контейнер НЕ перезапускает (``restart: always``
реагирует на выход процесса, не на health). Статус — диагностика; авто-рестарт по health
потребует autoheal-сайдкара или orchestration — сознательно не включаем.

Файл по умолчанию — в системном temp (``/tmp/aiesec-bot-heartbeat`` в контейнере): не
нужен volume, не засоряет ``data/``, а ``/tmp`` доступен на запись appuser (UID 1000).
Путь переопределяется ``HEARTBEAT_PATH`` в ``.env`` (bootstrap-значение, не настройка бота).
Запись атомарная (tmp-файл рядом + ``os.replace``), чтобы healthcheck никогда не прочитал
полузаписанный файл.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys
import tempfile
import time

logger = logging.getLogger(__name__)

HEARTBEAT_INTERVAL = 30          # как часто фоновая задача пишет файл
POLLING_STALE_SECONDS = 90       # после скольких секунд без удачного getUpdates НЕ писать
HEALTHCHECK_MAX_AGE = 120        # порог для --check

DEFAULT_PATH = os.path.join(tempfile.gettempdir(), "aiesec-bot-heartbeat")

# monotonic-время последнего успешного getUpdates (None = поллинг ещё ни разу не отвечал)
_last_poll_ok: float | None = None


def default_path() -> str:
    """Путь heartbeat-файла: ``config.HEARTBEAT_PATH``, если config импортируется, иначе temp.

    Ленивый импорт: ``--check`` должен работать и там, где config не собирается
    (например, нет ``.env`` при ручном запуске на хосте)."""
    try:
        from config import config  # noqa: WPS433 — lazy on purpose

        return config.HEARTBEAT_PATH or DEFAULT_PATH
    except Exception:  # noqa: BLE001 — любой сбой конфига = дефолт
        return DEFAULT_PATH


# ---------------------------------------------------------------- file primitives

def touch_heartbeat(path: str | None = None, now: float | None = None) -> None:
    """Атомарно записать текущий unix-time в *path* (tmp рядом + ``os.replace``)."""
    path = path or default_path()
    ts = time.time() if now is None else now
    directory = os.path.dirname(path) or "."
    os.makedirs(directory, exist_ok=True)
    tmp = f"{path}.{os.getpid()}.tmp"
    with open(tmp, "w", encoding="ascii") as fh:
        fh.write(f"{ts:.3f}\n")
        fh.flush()
    os.replace(tmp, path)


def heartbeat_age_seconds(path: str | None = None, now: float | None = None) -> float | None:
    """Возраст heartbeat в секундах; ``None`` — файла нет или он нечитаем/испорчен."""
    path = path or default_path()
    try:
        with open(path, "r", encoding="ascii") as fh:
            raw = fh.read().strip()
        written = float(raw)
    except (OSError, ValueError):
        return None
    current = time.time() if now is None else now
    return max(0.0, current - written)


def clear_heartbeat(path: str | None = None) -> None:
    """Удалить heartbeat-файл (shutdown). Отсутствие файла — не ошибка."""
    path = path or default_path()
    try:
        os.remove(path)
    except FileNotFoundError:
        pass
    except OSError:
        logger.warning("heartbeat: could not remove %s", path, exc_info=True)


# ---------------------------------------------------------------- polling liveness

def mark_polling_alive(now: float | None = None) -> None:
    """Отметить успешный тик поллинга (вызывается middleware; публично — для тестов)."""
    global _last_poll_ok
    _last_poll_ok = time.monotonic() if now is None else now


def polling_alive(stale_after: float = POLLING_STALE_SECONDS, now: float | None = None) -> bool:
    """Был ли успешный getUpdates не позже *stale_after* секунд назад."""
    if _last_poll_ok is None:
        return False
    current = time.monotonic() if now is None else now
    return (current - _last_poll_ok) <= stale_after


def reset_polling_state() -> None:
    """Сброс in-memory отметки (тесты)."""
    global _last_poll_ok
    _last_poll_ok = None


try:  # aiogram есть всегда в рантайме бота; защита — чтобы --check не тянул его зря
    from aiogram.client.session.middlewares.base import BaseRequestMiddleware
    from aiogram.methods import GetUpdates
except Exception:  # noqa: BLE001 — pragma: no cover
    BaseRequestMiddleware = object  # type: ignore[assignment,misc]
    GetUpdates = None  # type: ignore[assignment]


class PollingHeartbeatMiddleware(BaseRequestMiddleware):  # type: ignore[misc]
    """Session-middleware: отмечает живость поллинга по успешному ответу getUpdates.

    Подключение: ``bot.session.middleware(PollingHeartbeatMiddleware())``. Любые другие
    методы API и любые исключения проходят насквозь без побочных эффектов."""

    async def __call__(self, make_request, bot, method):
        response = await make_request(bot, method)
        if GetUpdates is not None and isinstance(method, GetUpdates):
            mark_polling_alive()
        return response


async def heartbeat_loop(
    path: str | None = None,
    interval: float = HEARTBEAT_INTERVAL,
    stale_after: float = POLLING_STALE_SECONDS,
) -> None:
    """Фоновая задача: каждые *interval* с писать heartbeat, если поллинг жив.

    Запускать через ``services.background.spawn`` — тогда ``cancel_all`` остановит её на
    shutdown. Ошибки записи логируются и не убивают цикл (fail-soft)."""
    path = path or default_path()
    warned_stale = False
    while True:
        try:
            if polling_alive(stale_after):
                touch_heartbeat(path)
                warned_stale = False
            elif not warned_stale:
                logger.warning(
                    "heartbeat: polling not confirmed alive for >%ss — not touching %s",
                    stale_after, path,
                )
                warned_stale = True
        except Exception:  # noqa: BLE001 — fail-soft
            logger.warning("heartbeat: touch failed", exc_info=True)
        await asyncio.sleep(interval)


# ---------------------------------------------------------------- CLI (HEALTHCHECK)

def check(path: str | None = None, max_age: float = HEALTHCHECK_MAX_AGE) -> int:
    """Код возврата для HEALTHCHECK: 0 — свежий heartbeat, 1 — нет/старый."""
    age = heartbeat_age_seconds(path)
    if age is None:
        print("heartbeat: missing", file=sys.stderr)
        return 1
    if age > max_age:
        print(f"heartbeat: stale ({age:.0f}s > {max_age:.0f}s)", file=sys.stderr)
        return 1
    print(f"heartbeat: ok ({age:.0f}s)")
    return 0


def _main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Bot heartbeat (Docker HEALTHCHECK)")
    parser.add_argument("--check", action="store_true", help="exit 0 if heartbeat fresh, else 1")
    parser.add_argument("--path", default=None, help=f"heartbeat file (default: {DEFAULT_PATH})")
    parser.add_argument("--max-age", type=float, default=HEALTHCHECK_MAX_AGE,
                        help=f"max age in seconds (default {HEALTHCHECK_MAX_AGE})")
    args = parser.parse_args(argv)
    if not args.check:
        parser.print_help()
        return 2
    return check(args.path, args.max_age)


if __name__ == "__main__":
    sys.exit(_main())
