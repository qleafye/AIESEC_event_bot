"""Карточка /find: строка «Email» не показывается, если почты нет."""
import pytest

from handlers.admin import _find_email_line


@pytest.mark.parametrize("email", [None, "", "  ", "-"])
def test_no_email_line_without_email(email):
    assert _find_email_line({"email": email}) == ""


def test_email_line_escaped_when_present():
    assert _find_email_line({"email": "a<b>@x.ru"}) == "Email: a&lt;b&gt;@x.ru\n"
