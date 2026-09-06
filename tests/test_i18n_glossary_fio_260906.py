"""Quick 260906 (UAT-фикс 27-05) — регресс на находку стендового UAT: движок машинного
перевода читал «ФИО» как аббревиатуру-код и отдавал «Fio.» вместо «full name» (тот же класс
артефакта, что и `TL`→`TLL`/`BD`→`BDB` в докстринге `services/i18n_glossary.py`). Фикс —
две новые пары в `DNT` («ФИО», «Фамилия Имя Отчество»), защищаемые сентинелами ДО перевода и
восстанавливаемые фиксированным английским эквивалентом ПОСЛЕ (`protect`/`apply`).

Прогоняет реальный `protect`/`apply`, а не мокает driver — так же, как и остальные проверки
глоссария не нуждаются в живом MT-движке (сентинел переживает перевод по контракту 27-01,
здесь просто эхо-imitация «движок вернул текст с сентинелами как есть»)."""
from services.i18n_glossary import DNT, apply, protect


def test_fio_glossary_prevents_enter_fio_bug():
    text = "Введи ФИО"
    protected, mapping = protect(text)
    assert "ФИО" not in protected  # сентинел подставлен ДО перевода

    # Имитация MT-движка: сентинелы переживают перевод дословно (27-01 probe, docstring
    # модуля) — здесь просто эхо, без реального движка.
    fake_mt_output = protected
    result = apply(text, fake_mt_output, mapping)

    assert result == "Введи full name"
    assert "Fio" not in result


def test_full_fio_expansion_glossary_mapping():
    text = "Напиши свои ФИО (Фамилия Имя Отчество):"
    protected, mapping = protect(text)
    result = apply(text, protected, mapping)

    assert result == "Напиши свои full name (last name, first name, patronymic):"
    assert "Fio" not in result


def test_dnt_has_fio_entries():
    assert DNT["ФИО"] == "full name"
    assert DNT["Фамилия Имя Отчество"] == "last name, first name, patronymic"
