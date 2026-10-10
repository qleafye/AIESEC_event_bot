"""Склонение после числа: дробные числа, и слово валюты согласуется только в позиции
подстановки («{coins} баллов»), а не по всему готовому тексту."""
from domain.game.labels import fill_template
from services.infra.ru_plural import agree_placeholder, points_word, ru_plural

F = ("балл", "балла", "баллов")


def test_integers():
    assert [ru_plural(n, *F) for n in (1, 2, 5, 11, 21, 22, 112, 0)] == [
        "балл", "балла", "баллов", "баллов", "балл", "балла", "баллов", "баллов"]


def test_fractions_and_strings():
    assert ru_plural("0,5", *F) == "балла"
    assert ru_plural(1.5, *F) == "балла"
    assert ru_plural("+10", *F) == "баллов"
    assert ru_plural("−3", *F) == "балла"
    assert ru_plural("—", *F) == "баллов"


def test_nan_and_inf_are_many():
    for n in (float("nan"), float("inf"), float("-inf"), "nan", "inf"):
        assert ru_plural(n, *F) == "баллов"


def test_placeholder_word_agrees_with_value():
    assert fill_template("«{task}» ({coins} баллов)", task="Сторис", coins=1) == "«Сторис» (1 балл)"
    assert fill_template("{penalized} баллов вместо {coins}", penalized=22, coins=30) == "22 балла вместо 30"
    assert fill_template("({coins} points)", coins=1) == "(1 point)"
    assert fill_template("{delta} монет", delta="+2") == "+2 монеты"
    assert fill_template("{gap} баллов", gap="0,5") == "0,5 балла"


def test_only_word_right_after_placeholder_changes():
    """Случаи из ревью: всё, что не «{число} слово», остаётся дословно."""
    cases = {
        "Всего 1 021 баллов, у тебя {coins} баллов": "Всего 1 021 баллов, у тебя 1 балл",
        "от 2 баллов до 2 баллов; {coins} баллов": "от 2 баллов до 2 баллов; 1 балл",
        "за 1–3 баллов ({coins} баллов)": "за 1–3 баллов (1 балл)",
        "2026-01-21 баллов, 23:21 баллов, ID-21 монет, {coins} баллов":
            "2026-01-21 баллов, 23:21 баллов, ID-21 монет, 1 балл",
    }
    for template, expected in cases.items():
        assert fill_template(template, coins=1) == expected


def test_substituted_values_are_not_touched():
    out = fill_template("«{task}» ({coins} баллов)", task="Задание 21 баллов", coins=5)
    assert out == "«Задание 21 баллов» (5 баллов)"
    out = fill_template("{name}: {coins} баллов", name="Иван 1 баллов", coins=2)
    assert out == "Иван 1 баллов: 2 балла"


def test_placeholder_needs_numeric_value_and_adjacent_word():
    assert agree_placeholder("{rank} баллов", "rank", "—") == "{rank} баллов"
    assert agree_placeholder("{coins}, баллов", "coins", 1) == "{coins}, баллов"
    assert agree_placeholder("{coins} балловый", "coins", 1) == "{coins} балловый"
    assert agree_placeholder(None, "coins", 1) is None


def test_old_coins_menu_caption_still_opens_balance():
    """Дефолт кнопки стал «🪙 Мои баллы»; старые клавиатуры с «🪙 Мои монеты» работают как раньше."""
    from services.bot.menu_labels import STATIC_TEXT_TO_KEY, default_caption

    assert default_caption("menu_coins") == "🪙 Мои баллы"
    for text in ("🪙 Мои баллы", "🪙 My points", "🪙 Мои монеты", "🪙 My coins"):
        assert STATIC_TEXT_TO_KEY[text] == "menu_coins"


def test_points_word():
    assert [points_word(n) for n in (1, 3, 5, -2, 11)] == ["балл", "балла", "баллов", "балла", "баллов"]
