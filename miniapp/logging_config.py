"""Квик 260919-u7e (P5, находка 02-miniapp «Mini App слеп: ошибок не видно»): без единой
точки настройки логирования у процесса Mini App логгеры модулей (`miniapp.routers.form`,
`miniapp.telegram_api`, ...) не имели ни одного хендлера. `logger.warning`/`.error` уходили
в `logging.lastResort` (только `WARNING+`, без формата — ни времени, ни имени логгера) либо
терялись целиком (`INFO`) — за 45,5 часов прод-логов `docker compose logs miniapp` нёс 8745
строк access-лога uvicorn и НОЛЬ строк приложения, хотя код логирует (form.py, telegram_api.py).

`configure_logging()` — единственная точка вызова (`miniapp.main.create_app`, один раз на
процесс/тест): формат — как у бота (`main.py::_configure_logging`, `%(asctime)s - %(name)s -
%(levelname)s - %(message)s`), вывод — stdout (том `./logs` у сервиса `miniapp` смонтирован,
но не читается мониторингом прода — `docker compose logs` смотрит stdout контейнера, туда и
пишем, в отличие от бота, который весь детальный лог держит в файле). Время — как у контейнера
(UTC, `docker-compose.yml`: `TZ` сервису намеренно не задаётся, T-19-64) — процесс не трогаем.

Уровень root — INFO. Без исключения для `httpx`/`httpcore` это протекло бы токеном бота в
stdout: `telegram_api._method_url` строит URL Bot API С ТОКЕНОМ В ПУТИ
(`https://api.telegram.org/bot<TOKEN>/<method>`), а httpx на `INFO` логирует "HTTP Request:
<method> <url> ..." ЦЕЛИКОМ, включая query/path — то самое, что `telegram_api.py` (докстринг
модуля, T-19-19) явно обещает никогда не класть в лог. Поднятие root на INFO без этой строки
было бы Rule 2 дырой, а не просто шумом.

Не дублирует access-лог uvicorn: `uvicorn`/`uvicorn.access` в `uvicorn.config.LOGGING_CONFIG`
несут `propagate: False` — их строки не долетают до root независимо от того, что сюда добавлено.
"""
from __future__ import annotations

import logging
import sys

LOG_FORMAT = "%(asctime)s - %(name)s - %(levelname)s - %(message)s"

# Библиотеки, которые на INFO говорят больше, чем нужно (или опасное) — тот же приём, что
# `main.py::_configure_logging` (`aiogram.event`/`apscheduler`/`urllib3`/`gspread`/`asyncio`),
# но список свой: процесс Mini App не тянет aiogram/apscheduler, зато держит httpx (T-19-19).
_QUIET_LOGGERS = ("httpx", "httpcore")

# Маркер идемпотентности на самом root-логгере: `configure_logging()` зовётся из
# `miniapp.main.create_app()`, а тесты создают приложение много раз в одном процессе
# (`TestClient(create_app(cfg=...))` на каждый тест) — без маркера каждый вызов добавлял бы
# ещё один StreamHandler, и prod-строка печаталась бы N раз.
_CONFIGURED_ATTR = "_miniapp_logging_configured"


def configure_logging(level: int = logging.INFO) -> None:
    root = logging.getLogger()
    if not getattr(root, _CONFIGURED_ATTR, False):
        handler = logging.StreamHandler(sys.stdout)
        handler.setFormatter(logging.Formatter(LOG_FORMAT))
        root.addHandler(handler)
        root.setLevel(level)
        setattr(root, _CONFIGURED_ATTR, True)
    # Вне гварда идемпотентности и переустанавливается на КАЖДЫЙ вызов: защита от утечки
    # токена (T-19-19) не должна зависеть от того, был ли этот вызов первым в процессе --
    # что-то ещё (тест, библиотека) могло поднять httpx обратно на INFO/DEBUG между вызовами.
    for name in _QUIET_LOGGERS:
        logging.getLogger(name).setLevel(logging.WARNING)


__all__ = ["LOG_FORMAT", "configure_logging"]
