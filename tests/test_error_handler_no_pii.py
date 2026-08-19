"""Global error handler must log *identifiers* of a failing update, never its content.

The log file sits on a persistent host volume; a registration bot's updates carry PII
(full name, phone, e-mail, answers, file_ids). Before this fix the handler dumped
`update.model_dump_json()` at ERROR on every unhandled exception.
"""
import asyncio
import logging

from aiogram.exceptions import TelegramBadRequest
from aiogram.methods import GetMe
from aiogram.types import CallbackQuery, Chat, ErrorEvent, Message, Update, User

import main

_USER = User(id=424242, is_bot=False, first_name="Иван", last_name="Иванов", username="ivan_pii")
_CHAT = Chat(id=-100777, type="private", first_name="Иван", last_name="Иванов")


def _message_update(text: str) -> Update:
    msg = Message(message_id=7, date=1700000000, chat=_CHAT, from_user=_USER, text=text)
    return Update(update_id=555, message=msg)


def _run(event: ErrorEvent, caplog):
    caplog.set_level(logging.DEBUG, logger="main")
    return asyncio.run(main.on_update_error(event))


def test_unhandled_error_logs_identity_not_payload(caplog):
    secret_text = "Меня зовут Иван Иванов, телефон +79991234567, почта ivan@example.com"
    update = _message_update(secret_text)
    try:
        raise RuntimeError("boom in handler")
    except RuntimeError as exc:
        event = ErrorEvent(update=update, exception=exc)

    assert _run(event, caplog) is True

    records = [r for r in caplog.records if r.levelno == logging.ERROR]
    assert len(records) == 1, "exactly one ERROR line per failing update"
    out = caplog.text
    # identifiers present
    assert "update_id=555" in out
    assert "telegram_user_id=424242" in out
    assert "chat_id=-100777" in out
    assert "event=message" in out
    assert "RuntimeError" in out
    # PII absent
    for pii in ("Иван", "Иванов", "+79991234567", "ivan@example.com", "ivan_pii", secret_text):
        assert pii not in out, f"PII leaked into log: {pii!r}"
    assert "model_dump" not in out


def test_callback_query_identity_uses_message_chat(caplog):
    msg = Message(message_id=9, date=1700000000, chat=_CHAT, from_user=_USER, text="секрет ФИО")
    cq = CallbackQuery(id="cb1", from_user=_USER, chat_instance="x", data="adm:approve:42", message=msg)
    update = Update(update_id=556, callback_query=cq)
    event = ErrorEvent(update=update, exception=ValueError("bad"))

    assert _run(event, caplog) is True
    out = caplog.text
    assert "event=callback_query" in out
    assert "telegram_user_id=424242" in out
    assert "chat_id=-100777" in out
    assert "секрет" not in out
    assert "adm:approve:42" not in out


def test_benign_telegram_errors_are_debug_only(caplog):
    update = _message_update("Иван Иванов")
    exc = TelegramBadRequest(method=GetMe(), message="Bad Request: message is not modified")
    event = ErrorEvent(update=update, exception=exc)

    assert _run(event, caplog) is True
    assert not [r for r in caplog.records if r.levelno >= logging.WARNING]
    assert "Иван" not in caplog.text


def test_update_identity_handles_empty_update():
    ident = main._update_identity(Update(update_id=1))
    assert ident == {"update_id": 1, "event": None, "telegram_user_id": None, "chat_id": None}
