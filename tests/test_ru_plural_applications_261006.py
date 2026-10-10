import pytest

from services.ru_plural import ru_plural
from handlers.applications.admin_moderation import _applications_word


@pytest.mark.parametrize("n,word", [
    (1, "заявку"), (2, "заявки"), (3, "заявки"), (4, "заявки"), (5, "заявок"),
    (11, "заявок"), (12, "заявок"), (21, "заявку"), (22, "заявки"), (100, "заявок"), (101, "заявку"),
])
def test_approve_all_word_agrees_with_number(n, word):
    assert _applications_word(n) == word


def test_ru_plural_generic():
    assert ru_plural(3, "день", "дня", "дней") == "дня"
