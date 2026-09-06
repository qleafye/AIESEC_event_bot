"""UAT 260906 (стендовый прогон 3, фаза 27) — сторож двух находок в машинном переводе,
пропущенных методикой замера 27-01/27-PROBE-REPORT.md:

1. **Изолированный сентинел ("Z"-артефакт).** Замер 27-01 подставлял сентинел ВНУТРИ
   реального русского предложения (9/9 кейсов «Сентинелы DNT»); ни один не проверял сентинел
   как ЕДИНСТВЕННОЕ содержимое строки — а ровно так выглядит любой DNT-город в списке опций
   анкеты. На изолированном сентинеле движок дублирует/утаскивает финальный символ суффикса
   («Москва и МО» → «Moscow and MOZ», «Казань» → «KazanZ»). Реальный движок здесь НЕ
   участвует (Pitfall замера 27-01, `tests/test_i18n_glossary_fio_260906.py`) — тесты кормят
   `apply()` СИНТЕТИЧЕСКИМ MT-выводом, имитирующим найденный артефакт и соседние с ним
   косметические варианты (регистр, пробел между буквами маркера, точка-абревиатура).
2. **Замыкающий эмодзи и гендерная приписка.** «...в меню ниже 👇» → «...below e» (движок не
   переживает голый эмодзи в конце строки, `split_leading_symbols` снимал только ведущий) и
   «зарегистрирован(а)» → «...registered (a)» (у английского нет грамматического рода).
   Фикс — `split_trailing_symbols`/`strip_gender_suffix` (`services/i18n_glossary.py`),
   врезанные в конвейер `services/i18n_worker.py::drain` ДО вызова драйвера.

Стаб-драйвер для end-to-end тестов конвейера — тот же контракт `TranslationDriver`, что и
`tests/test_i18n_worker_27.py::_StubDriver` (см. докстринг того файла)."""
import asyncio

from config import config
from database import db
from services import i18n_worker
from services.i18n_glossary import (
    DNT,
    apply,
    protect,
    split_leading_symbols,
    split_trailing_symbols,
    strip_gender_suffix,
)


# ── apply(): изолированный сентинел переживает косметический дрейф движка ──────────────────

def test_apply_strips_stray_marker_char_after_isolated_city_sentinel():
    """Точное воспроизведение находки UAT: «Москва и МО» — ВСЯ строка после `protect()`
    превращается в один голый сентинел. Синтетический MT-вывод — сентинел + один посторонний
    маркерный символ («Z») сразу после, без пробела (найденный артефакт)."""
    text = "Москва и МО"
    protected, mapping = protect(text)
    assert protected == list(mapping.keys())[0]  # вся строка — один токен, без остатка

    token = next(iter(mapping))
    fake_mt_output = token + "Z"  # артефакт: лишний маркерный символ приклеился к токену

    result = apply(text, fake_mt_output, mapping)

    assert result == "Moscow and MO"
    assert "Z" not in result  # ключевая проверка находки — не "Moscow and MOZ"


def test_apply_strips_stray_marker_char_for_every_dnt_city():
    """Та же находка на всех восьми городах DNT — ни один не должен оставлять маркерный
    символ рядом с восстановленным термином."""
    cities = {
        "Москва и МО": "Moscow and MO",
        "Санкт-Петербург": "St. Petersburg",
        "Новосибирск": "Novosibirsk",
        "Екатеринбург": "Yekaterinburg",
        "Казань": "Kazan",
        "Нижний Новгород": "Nizhny Novgorod",
        "Красноярск": "Krasnoyarsk",
        "Уфа": "Ufa",
    }
    for ru_city, en_city in cities.items():
        protected, mapping = protect(ru_city)
        token = next(iter(mapping))
        fake_mt_output = token + "z"  # регистр вперемешку — артефакт не всегда капсом

        result = apply(ru_city, fake_mt_output, mapping)

        assert result == en_city, f"{ru_city}: получили {result!r}"


def test_apply_tolerates_lowercased_and_spaced_sentinel():
    """Косметический дрейф: движок иногда токенизирует сентинел посимвольно (пробел между
    буквами маркера) и/или отдаёт его в нижнем регистре — восстановление должно пройти без
    потери перевода."""
    text = "АЙСЕК"
    protected, mapping = protect(text)
    token = next(iter(mapping))
    assert token.startswith("ZQ") and token.endswith("QZ")

    # "ZQ1QZ" -> "z q 1 q z" (нижний регистр + пробелы между буквами маркера, число цело)
    number = token[len("ZQ"):-len("QZ")]
    mangled = f"z q {number} q z"

    result = apply(text, mangled, mapping)

    assert result == "AIESEC"


def test_apply_tolerates_trailing_period_after_sentinel():
    """Тот же класс артефакта, что дал «Fio.» вместо «ФИО» до DNT-фикса (докстринг
    `services/i18n_glossary.py`): движок иногда завершает одинокий ASCII-токен точкой, как
    аббревиатуру. Точка должна поглощаться, а не оставаться в переводе."""
    text = "Юлид"
    protected, mapping = protect(text)
    token = next(iter(mapping))
    fake_mt_output = token + "."

    result = apply(text, fake_mt_output, mapping)

    assert result == "YouLead"
    assert "." not in result


def test_apply_discards_translation_when_sentinel_dropped_entirely():
    """Реальная потеря (не косметика) — движок вообще не вернул сентинел. `apply()` обязан
    отбросить перевод целиком (fail-soft, `""`), терпимость не маскирует настоящую потерю."""
    text = "АЙСЕК"
    protected, mapping = protect(text)

    result = apply(text, "hosts a delegate forum", mapping)

    assert result == ""


def test_apply_discards_translation_when_sentinel_duplicated():
    """Задвоение сентинела — тоже реальная потеря (движок размножил токен), не косметика."""
    text = "АЙСЕК"
    protected, mapping = protect(text)
    token = next(iter(mapping))

    result = apply(text, f"{token} {token}", mapping)

    assert result == ""


def test_apply_dnt_city_inside_sentence_with_trailing_date_tail_still_exact():
    """Регрессия предыдущего поведения (9/9 замера 27-01): сентинел ВНУТРИ строки, с хвостом
    после — «, 30-31 октября» — переживает перевод и восстанавливается БЕЗ маркерных
    ошмётков, ровно как раньше, новый терпимый регэксп не должен ничего портить в этом,
    уже проверенном замером, сценарии."""
    text = "Казань, 30-31 октября"
    protected, mapping = protect(text)
    token = next(iter(mapping))
    assert protected == f"{token}, 30-31 октября"

    # Эхо-имитация: движок копирует сентинел дословно (доказано замером 27-01 внутри
    # предложения), окружающий текст оставляет как есть.
    result = apply(text, protected, mapping)

    assert result == "Kazan, 30-31 октября"


def test_dnt_still_maps_all_cities_after_sentinel_format_change():
    """Сторож формата: словарь DNT не тронут фиксом сентинела (только механика поиска в
    `apply`/`restore`, не сам глоссарий)."""
    assert DNT["Казань"] == "Kazan"
    assert DNT["Москва и МО"] == "Moscow and MO"
    assert DNT["АЙСЕК"] == "AIESEC"


# ── split_trailing_symbols(): замыкающий эмодзи отделяется симметрично ведущему ────────────

def test_split_trailing_symbols_separates_emoji_with_preceding_space():
    text = "С возвращением! Ты уже зарегистрирован — всё нужное в меню ниже \U0001f447"

    rest, suffix = split_trailing_symbols(text)

    assert rest == "С возвращением! Ты уже зарегистрирован — всё нужное в меню ниже"
    assert suffix == " \U0001f447"
    assert rest + suffix == text  # склейка без потерь


def test_split_trailing_symbols_no_suffix_when_text_ends_in_letter():
    text = "Введи ФИО"

    rest, suffix = split_trailing_symbols(text)

    assert rest == text
    assert suffix == ""


def test_split_trailing_symbols_all_symbols_returns_no_split():
    text = "👇👇👇"

    rest, suffix = split_trailing_symbols(text)

    assert rest == text
    assert suffix == ""


def test_split_leading_and_trailing_together_round_trip():
    """Обе функции применяются последовательно (docstring `split_trailing_symbols`) — их
    комбинация должна восстанавливать исходную строку без потерь символов."""
    text = "🎖 Позиция в АЙСЕК 👇"

    prefix, rest = split_leading_symbols(text)
    core, suffix = split_trailing_symbols(rest)

    assert prefix + core + suffix == text
    assert core == "Позиция в АЙСЕК"


# ── strip_gender_suffix(): русская гендерная приписка не протаскивается в перевод ──────────

def test_strip_gender_suffix_removes_parenthetical_a():
    assert strip_gender_suffix("Ты уже зарегистрирован(а) — всё нужное") == (
        "Ты уже зарегистрирован — всё нужное"
    )


def test_strip_gender_suffix_multiple_occurrences_in_one_string():
    text = "Ты уже был(а) с нами. Расскажи, что бы ты хотел(а) увидеть."
    assert strip_gender_suffix(text) == (
        "Ты уже был с нами. Расскажи, что бы ты хотел увидеть."
    )


def test_strip_gender_suffix_standalone_word():
    assert strip_gender_suffix("Сам(а)") == "Сам"


def test_strip_gender_suffix_noop_when_absent():
    text = "Обычный текст без приписки"
    assert strip_gender_suffix(text) == text


# ── end-to-end через i18n_worker.drain(): оба бага UAT воспроизведены и починены разом ─────

def _db_ready(tmp_path, name="test_i18n_glossary_sentinels_260906.db"):
    config.DB_PATH = str(tmp_path / name)
    asyncio.run(db.init_db())


class _EchoDriver:
    """Эхо-стаб: возвращает вход как есть (проверяет СТЫКОВКУ конвейера — что дошло/не дошло
    до драйвера и что приклеилось обратно — не качество перевода)."""

    def __init__(self):
        self.batches: list[list[str]] = []

    def translate_batch(self, texts):
        self.batches.append(list(texts))
        return list(texts)

    def unload(self):
        pass


def _patch_driver(monkeypatch, stub):
    async def _fake_get_driver():
        return stub

    monkeypatch.setattr(i18n_worker, "get_driver", _fake_get_driver)


def test_drain_reattaches_trailing_emoji_untouched_and_never_sends_it_to_driver(
    tmp_path, monkeypatch,
):
    """Точное воспроизведение второго бага UAT: замыкающий эмодзи никогда не должен доходить
    до драйвера (значит не может превратиться в постороннюю букву) и должен вернуться в
    финальный текст ровно тем же символом."""
    _db_ready(tmp_path)
    stub = _EchoDriver()
    _patch_driver(monkeypatch, stub)

    source = "С возвращением! Ты уже зарегистрирован(а) — всё нужное в меню ниже \U0001f447"
    asyncio.run(db.enqueue_translation("en", "hashGreet", source))

    done = asyncio.run(i18n_worker.drain())
    assert done == 1

    # Эмодзи не должен присутствовать ни в одном тексте, ушедшем в driver.translate_batch.
    for batch in stub.batches:
        for sent in batch:
            assert "\U0001f447" not in sent

    row = asyncio.run(db.get_translation("en", "hashGreet"))
    assert row["text"].endswith("\U0001f447")
    assert "(а)" not in row["text"]
    assert "(a)" not in row["text"]


def test_drain_isolated_city_sentinel_produces_clean_translation(tmp_path, monkeypatch):
    """Точное воспроизведение первого бага UAT через полный конвейер `drain()`: driver
    имитирует найденный "Z"-артефакт (лишний маркерный символ после сентинела), итоговый
    перевод в БД обязан быть чистым, без огрызка маркера."""
    _db_ready(tmp_path)

    def _append_stray_marker_char(texts):
        # Имитация находки UAT: движок приклеивает лишний символ маркера сразу после сентинела
        # ("XQ1QZ" -> "XQ1QZZ"), но только когда сентинел — единственное содержимое строки
        # (изолированный сентинел, находка «пробел в методике замера»).
        out = []
        for t in texts:
            if t.startswith("ZQ") and t.endswith("QZ"):
                out.append(t + "Z")
            else:
                out.append(t)
        return out

    stub = _EchoDriver()
    stub.translate_batch = _append_stray_marker_char
    _patch_driver(monkeypatch, stub)

    asyncio.run(db.enqueue_translation("en", "hashCity", "Казань"))

    done = asyncio.run(i18n_worker.drain())
    assert done == 1

    row = asyncio.run(db.get_translation("en", "hashCity"))
    assert row["text"] == "Kazan"
