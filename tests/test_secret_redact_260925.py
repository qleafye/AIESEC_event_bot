"""Токен бота не должен попадать ни в алерты админам, ни в логи (инцидент 25.09).

Алерт «Прокси переключился … Причина: TelegramNetworkError: … https://api.telegram.org/bot<ТОКЕН>/
sendPhoto» ушёл в личку всем админам: `str(exception)` aiogram/aiohttp несёт URL Bot API, а
токен лежит прямо в пути. Сторожим санитайзер, алерт прокси и фильтр логов.
"""
from __future__ import annotations

import asyncio
import io
import logging

import pytest
from aiogram.exceptions import TelegramNetworkError

import secret_redact
import services.proxy_session as proxy_session
from config import config
from secret_redact import (
    RedactSecretsFilter,
    install_log_redaction,
    redact_secrets,
    register_secret,
)

TOKEN = "123456:AAH-abc_DEF"
URL = f"https://api.telegram.org/bot{TOKEN}/sendPhoto"
# Настоящий по форме токен (35 символов хвоста) — для маскировки без префикса «bot».
REAL_SHAPE = "7712345678:AAEhBP0av28nQuN-_zT8x9Q1abcdefGHIJK"


# ── санитайзер ───────────────────────────────────────────────────────────────

def test_redact_bot_token_in_api_url():
    out = redact_secrets(f"Cannot connect: {URL}")
    assert TOKEN not in out
    assert "AAH-abc_DEF" not in out
    assert "https://api.telegram.org/bot[скрыт]/sendPhoto" in out


def test_redact_bare_token_without_bot_prefix():
    out = redact_secrets(f"token={REAL_SHAPE} rejected")
    assert REAL_SHAPE not in out
    assert "token=[скрыт] rejected" == out


def test_redact_registered_secret(monkeypatch):
    monkeypatch.setattr(secret_redact, "_registered", set())
    odd = "not-a-telegram-shape-secret-XYZ"
    register_secret(odd)
    assert odd not in redact_secrets(f"leak: {odd}")


def test_register_secret_accepts_secretstr_and_ignores_short(monkeypatch):
    from pydantic import SecretStr

    monkeypatch.setattr(secret_redact, "_registered", set())
    register_secret(SecretStr("some-long-secret-value-123"))
    register_secret("short")
    register_secret(None)
    register_secret("")
    assert secret_redact._registered == {"some-long-secret-value-123"}
    assert redact_secrets("short and plain") == "short and plain"


def test_redact_url_password():
    out = redact_secrets("Cannot connect to socks5://alice:s3cr3t@1.2.3.4:1080 via host")
    assert "s3cr3t" not in out
    assert "socks5://alice:***@1.2.3.4:1080" in out


@pytest.mark.parametrize("plain", [
    "Сбор в 12:30, зал 3",
    "https://api.telegram.org/file/",
    "http://example.com:8080/path",
    "user 123456789 blocked the bot",
    "",
])
def test_redact_leaves_ordinary_text(plain):
    assert redact_secrets(plain) == plain


def test_redact_non_string_and_none():
    assert redact_secrets(None) == ""
    assert TOKEN not in redact_secrets(RuntimeError(URL))


# ── алерт прокси ─────────────────────────────────────────────────────────────

class _FakeBot:
    def __init__(self):
        self.sent = []

    async def send_message(self, chat_id, text, parse_mode="HTML"):
        self.sent.append((chat_id, text))
        self.parse_modes = getattr(self, "parse_modes", []) + [parse_mode]


@pytest.fixture
def alert_bot(monkeypatch):
    bot = _FakeBot()
    monkeypatch.setattr(config, "ADMIN_IDS", [111, 222])
    monkeypatch.setattr(proxy_session, "_blocked_admins", set())
    proxy_session.set_alert_bot(bot)
    try:
        yield bot
    finally:
        proxy_session.set_alert_bot(None)
        proxy_session._alert_bot_warned = False


@pytest.mark.parametrize("make_error", [
    lambda: TelegramNetworkError(method=object(), message=f"HTTP Client says - ClientOSError: {URL}"),
    lambda: Exception(f"Cannot connect to host api.telegram.org:443 ssl:default [{URL}]"),
])
def test_proxy_alert_hides_bot_token(alert_bot, make_error):
    cause = proxy_session._describe_error(make_error())
    assert TOKEN not in cause

    asyncio.run(proxy_session._alert_admins_proxy_storm(
        "direct", "socks5://***@1.2.3.4:1080", 1, None, None, cause=cause,
    ))

    assert [chat for chat, _ in alert_bot.sent] == [111, 222]
    for _chat, text in alert_bot.sent:
        assert "Причина:" in text
        assert TOKEN not in text
        assert "AAH-abc_DEF" not in text
        assert "bot[скрыт]" in text


def test_proxy_alert_redacts_even_raw_cause(alert_bot):
    """Страховка на самом алерте: даже причина, собранная мимо _describe_error, не утечёт."""
    asyncio.run(proxy_session._alert_admins_proxy_storm(
        "direct", "direct", 1, None, None, cause=f"TelegramNetworkError: {URL}",
    ))
    assert alert_bot.sent
    assert all(TOKEN not in text for _chat, text in alert_bot.sent)


def test_redacted_placeholder_is_not_an_html_tag():
    """Стенд 25.09: плейсхолдер «<скрыт>» при parse_mode=HTML по умолчанию Telegram принимал
    за тег («Unsupported start tag») и отклонял весь алерт — ни один админ его не получал."""
    out = redact_secrets(f"TelegramNetworkError: {URL}")
    assert "<" not in out and ">" not in out


def test_proxy_alert_goes_as_plain_text_even_with_angle_brackets(alert_bot):
    """В str(исключения) бывают свои <...> (repr объектов aiohttp) — алерт уходит простым
    текстом (parse_mode=None), иначе при HTML по умолчанию Telegram его отклонит."""
    asyncio.run(proxy_session._alert_admins_proxy_storm(
        "direct", "direct", 1, None, None,
        cause=f"ClientOSError: <ClientConnectorError host=x> {URL}",
    ))
    assert alert_bot.sent
    assert alert_bot.parse_modes and all(pm is None for pm in alert_bot.parse_modes)
    assert all("<ClientConnectorError" in text for _chat, text in alert_bot.sent)


def test_sheets_alert_goes_as_plain_text(monkeypatch):
    from services import sheets

    bot = _FakeBot()
    monkeypatch.setattr(config, "ADMIN_IDS", [111])
    monkeypatch.setattr(sheets, "_alert_bot", bot)
    asyncio.run(sheets._send_admin_alert(f"Ошибка <GSpreadException> {URL}"))
    assert bot.sent and TOKEN not in bot.sent[0][1]
    assert bot.parse_modes == [None]


# ── логи ─────────────────────────────────────────────────────────────────────

def _capture_logger(name):
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(logging.Formatter("%(levelname)s - %(message)s"))
    handler.addFilter(RedactSecretsFilter())
    log = logging.getLogger(name)
    log.handlers = [handler]
    log.propagate = False
    log.setLevel(logging.DEBUG)
    return log, stream


def test_log_filter_redacts_msg_args_and_traceback():
    log, stream = _capture_logger("test.secret_redact.filter")
    log.warning(f"in msg: {URL}")
    log.warning("in args: %s / %r", URL, URL)
    try:
        raise TelegramNetworkError(method=object(), message=f"boom {URL}")
    except TelegramNetworkError:
        log.exception("send failed")
    try:
        raise RuntimeError(f"socks5://u:hunter2@h:1 {URL}")
    except RuntimeError as e:
        log.error("wrapped: %s", e, exc_info=True)

    out = stream.getvalue()
    assert TOKEN not in out
    assert "AAH-abc_DEF" not in out
    assert "hunter2" not in out
    assert out.count("bot[скрыт]") >= 6
    assert "Traceback" in out


def test_log_filter_keeps_clean_record_untouched():
    record = logging.LogRecord("x", logging.INFO, __file__, 1, "count=%s", (5,), None)
    assert RedactSecretsFilter().filter(record) is True
    assert record.args == (5,)
    assert record.getMessage() == "count=5"


def test_install_log_redaction_covers_child_loggers_via_root_handler():
    """Фильтр на самом root-логгере не видит записи дочерних логгеров — поэтому ставим его
    на хендлеры root; проверяем именно путь через propagate."""
    root = logging.getLogger()
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setLevel(logging.DEBUG)
    root.addHandler(handler)
    child = logging.getLogger("test.secret_redact.child")
    old_level, old_prop = child.level, child.propagate
    child.setLevel(logging.INFO)
    child.propagate = True
    try:
        install_log_redaction()
        install_log_redaction()  # идемпотентна
        assert sum(isinstance(f, RedactSecretsFilter) for f in handler.filters) == 1
        child.error("via child: %s", URL)
    finally:
        root.removeHandler(handler)
        child.setLevel(old_level)
        child.propagate = old_prop
    out = stream.getvalue()
    assert "via child" in out
    assert TOKEN not in out
    assert "bot[скрыт]" in out
