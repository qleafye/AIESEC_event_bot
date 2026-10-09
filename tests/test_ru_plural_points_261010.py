"""Склонение после числа: дробные числа и согласование «N баллов» в готовом тексте."""
from services.ru_plural import agree_points, ru_plural

F = ("балл", "балла", "баллов")


def test_integers():
    assert [ru_plural(n, *F) for n in (1, 2, 5, 11, 21, 22, 112, 0)] == [
        "балл", "балла", "баллов", "баллов", "балл", "балла", "баллов", "баллов"]


def test_fractions_and_strings():
    assert ru_plural("0,5", *F) == "балла"
    assert ru_plural(1.5, *F) == "балла"
    assert ru_plural("+10", *F) == "баллов"
    assert ru_plural("\u22123", *F) == "балла"
    assert ru_plural("—", *F) == "баллов"


def test_agree_points_ru():
    assert agree_points("Дедлайн: «Сторис» (1 баллов)") == "Дедлайн: «Сторис» (1 балл)"
    assert agree_points("22 баллов, 5 балл, 101 монет, 3 коинов") == "22 балла, 5 баллов, 101 монета, 3 коина"
    assert agree_points("Баланс изменён: +1 баллов.") == "Баланс изменён: +1 балл."
    assert agree_points("0,5 баллов") == "0,5 баллов"  # дробь не трогаем
    assert agree_points("5 баллами, баллов нет") == "5 баллами, баллов нет"


def test_agree_points_en():
    assert agree_points("(1 points) and 2 point, 21 points") == "(1 point) and 2 points, 21 points"
    assert agree_points(None) is None
