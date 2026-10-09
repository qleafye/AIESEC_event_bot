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


def test_game_template_agrees_points_after_fill():
    """Тексты волн и штрафа: «{coins} баллов» → «1 балл» после подстановки числа."""
    from game_labels import fill_template

    assert fill_template("«{task}» ({coins} баллов)", task="Сторис", coins=1) == "«Сторис» (1 балл)"
    assert fill_template("{penalized} баллов вместо {coins}", penalized=22, coins=30) == "22 балла вместо 30"
    assert fill_template("({coins} points)", coins=1) == "(1 point)"


def test_old_coins_menu_caption_still_opens_balance():
    """Дефолт кнопки стал «🪙 Мои баллы»; старые клавиатуры с «🪙 Мои монеты» работают как раньше."""
    from services.menu_labels import STATIC_TEXT_TO_KEY, default_caption

    assert default_caption("menu_coins") == "🪙 Мои баллы"
    for text in ("🪙 Мои баллы", "🪙 My points", "🪙 Мои монеты", "🪙 My coins"):
        assert STATIC_TEXT_TO_KEY[text] == "menu_coins"



def test_points_word():
    from services.ru_plural import points_word

    assert [points_word(n) for n in (1, 3, 5, -2, 11)] == ["балл", "балла", "баллов", "балла", "баллов"]
