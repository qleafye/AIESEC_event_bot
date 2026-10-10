"""Приёмка 16.09: Mini App шлёт дату нативного поля в ISO («2007-03-15»), сервер принимал
только «ДД.ММ.ГГГГ» — в приложении не проходил ни один шаг-дата."""
from tests._paths import REPO_ROOT
from pathlib import Path

import reg_engine


def test_iso_birth_date_is_accepted_and_stored_like_chat():
    assert reg_engine.validate_answer("birth_date", "2007-03-15") == ("15.03.2007", None)


def test_chat_format_unchanged():
    assert reg_engine.validate_answer("birth_date", "15.03.2007") == ("15.03.2007", None)


def test_iso_still_goes_through_range_check():
    value, err = reg_engine.validate_answer("birth_date", "2026-03-15")
    assert value is None and err


def test_garbage_keeps_format_error():
    assert reg_engine.validate_answer("birth_date", "15/03/2007") == (
        None, "Формат даты: ДД.ММ.ГГГГ. Попробуй ещё раз.",
    )


def test_date_control_shows_legacy_iso_value_like_chat():
    """Приёмка 09.10: поле даты в приложении — текст «ДД.ММ.ГГГГ», как в чате (было нативное
    поле с переводом в ISO). Старое ISO-значение показывается в формате чата."""
    js = (REPO_ROOT / "miniapp/static/js/form.js").read_text(encoding="utf-8")
    body = js.split("function dateControl", 1)[1].split("\n}\n", 1)[0]
    assert "iso[3]" in body and "iso[1]" in body
    assert 'type: "date"' not in body
