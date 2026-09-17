"""Квик 260917-en (живая проверка 17.09, доп. находка «а») — машинный перевод «Отлично,
начинаем регистрацию.» дал «All right, start registering..» (двойная точка, артефакт движка,
неестественная фраза). Строка короткая, детерминированная, отправляется сразу после согласий
(`handlers/registration.py::_start_registration_flow` -> `_safe_answer`, шов перевода уже есть)
— тот же класс, что и остальной ярус A: рукописный перевод надёжнее машинного.

Литералы сверены байт-в-байт с `handlers/registration.py::_start_registration_flow` и с
`services/i18n_sources.py::code_literals()` (`lit:registration._start_registration_flow`) —
несовпадение хотя бы на символ означает, что перевод здесь никогда не сработает (см. докстринг
`i18n_ui_en.py`)."""
from handlers import reg_i18n
from services import i18n_sources

_PLAIN = "Отлично, начинаем регистрацию."
_REFERRED = "Отлично, ты пришёл по приглашению друга. Начинаем регистрацию."


def test_plain_start_literal_translated_via_layer_a_no_double_dot():
    out = reg_i18n.tr_text(_PLAIN, "en", {})
    assert out == "Great, let's start your application."
    assert ".." not in out


def test_referred_start_literal_translated_via_layer_a():
    out = reg_i18n.tr_text(_REFERRED, "en", {})
    assert out == "Great, you're here through a friend's invite. Let's start your application."


def test_ru_identity_unchanged():
    assert reg_i18n.tr_text(_PLAIN, "ru", {}) is _PLAIN
    assert reg_i18n.tr_text(_REFERRED, "ru", {}) is _REFERRED


def test_literals_still_match_code_literals_corpus():
    """Сторож дрейфа: если текст в registration.py когда-нибудь изменится без зеркальной
    правки здесь и в i18n_sources.py, перевод тихо перестанет находиться (fail-soft, D-04) —
    эта проверка ловит расхождение раньше стендового UAT."""
    tier_b_texts = {text.strip() for _origin, text in i18n_sources.code_literals()}
    assert _PLAIN in tier_b_texts
    assert _REFERRED in tier_b_texts
