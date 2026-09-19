"""Квик 260919-u7e (P5, находка 02-miniapp «Mini App слеп: ошибок не видно», пункт 1):
`miniapp.logging_config.configure_logging` -- хендлер на root-логгере процесса Mini App,
формат как у бота (`main.py::_configure_logging`), защита токена бота от утечки через httpx.

До этого квика у `miniapp/` не было ни `basicConfig`, ни `dictConfig` -- `logger.warning`/
`.error` в `routers/form.py`/`telegram_api.py` уходили в `logging.lastResort` (WARNING+, без
формата) либо терялись целиком (INFO). За 45,5 часов прод-логов `docker compose logs miniapp`
нёс 8745 строк access-лога uvicorn и НОЛЬ строк приложения.
"""
from __future__ import annotations

import logging
import re

from miniapp.logging_config import LOG_FORMAT, configure_logging


def test_configure_logging_writes_bot_style_format_to_stdout(capsys):
    """Формат — байт-в-байт как у `main.py::_configure_logging` бота
    (`%(asctime)s - %(name)s - %(levelname)s - %(message)s`), вывод — в stdout (не в
    logging.lastResort, не в stderr, куда попадают тексты httpx/uvicorn 'default'). Хендлер
    конфигурируется заново прямо в тесте (сохранить/очистить/восстановить root), чтобы
    гарантированно поймать РЕАЛЬНЫЙ stdout текущего теста, а не сток, захваченный при первом
    вызове configure_logging() где-то раньше в процессе воркера."""
    root = logging.getLogger()
    saved_handlers = list(root.handlers)
    saved_level = root.level
    had_marker = hasattr(root, "_miniapp_logging_configured")
    root.handlers = []
    if had_marker:
        delattr(root, "_miniapp_logging_configured")
    try:
        configure_logging()
        probe = logging.getLogger("miniapp.selftest_260919")
        probe.warning("проверка формата")
        captured = capsys.readouterr()
    finally:
        root.handlers = saved_handlers
        root.setLevel(saved_level)
        if had_marker:
            setattr(root, "_miniapp_logging_configured", True)

    line = next((l for l in captured.out.splitlines() if "проверка формата" in l), None)
    assert line, f"строка не дошла до stdout: {captured.out!r}"
    assert re.match(
        r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2},\d{3} - miniapp\.selftest_260919 - WARNING - "
        r"проверка формата$",
        line,
    ), line


def test_configure_logging_format_constant_matches_bot_main():
    """`LOG_FORMAT` буквально та же строка, что `main.py::_configure_logging` собирает у бота
    -- сверка константой, а не «похоже выглядит»."""
    assert LOG_FORMAT == "%(asctime)s - %(name)s - %(levelname)s - %(message)s"


def test_configure_logging_is_idempotent_no_duplicate_handlers():
    """`create_app()` зовёт `configure_logging()` на каждый тест миниапп-сюиты (пачками
    создаёт TestClient(create_app(cfg=...))) -- без идемпотентности прод-строка печаталась бы
    N раз на каждое событие."""
    configure_logging()
    root = logging.getLogger()
    before = sum(
        1 for h in root.handlers
        if isinstance(h, logging.StreamHandler) and not isinstance(h, logging.FileHandler)
    )
    configure_logging()
    configure_logging()
    after = sum(
        1 for h in root.handlers
        if isinstance(h, logging.StreamHandler) and not isinstance(h, logging.FileHandler)
    )
    assert after == before
    assert before >= 1


def test_configure_logging_silences_httpx_to_protect_bot_token():
    """T-19-19: `telegram_api._method_url` кладёт токен бота в URL запроса; httpx на INFO
    логирует 'HTTP Request: <method> <url> ...' целиком. Поднятие root-логгера на INFO без
    этой строки утекло бы токеном в stdout на каждый sendMessage/sendPhoto/getFile."""
    configure_logging()
    assert logging.getLogger("httpx").level == logging.WARNING
    assert logging.getLogger("httpcore").level == logging.WARNING
    assert logging.getLogger().level == logging.INFO
