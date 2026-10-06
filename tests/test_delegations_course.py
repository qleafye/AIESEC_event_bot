"""Делегации вузов — разбор свободного текста курса и правило ЦА с отсечкой по дате.

Таблицы «вход → выход» на вымышленных строках тех же форм, что встречаются в выгрузке формы
(опечатки уровня, цифра без уровня, уровень без цифры, номер группы вместо курса, не студенты).
Всё, что парсер не может решить, обязано давать «check» — молча в ЦА или не-ЦА не относим.
"""
import re
from datetime import date, datetime
from pathlib import Path

import pytest

from reg_options import COURSE_OPTIONS
from services.delegations_course import ParsedCourse, evaluate_ta, parse_course

CUTOFF = datetime(2026, 9, 23)
NOT_TA = ["1", "2"]
BEFORE = "2026-09-20 10:00:00"
AFTER = "2026-10-01 12:00:00"
MASTER = "Магистратура/Аспирантура"


# ---------------------------------------------------------------- parse_course: уровень

@pytest.mark.parametrize("text, level", [
    ("бакалавриат", "bachelor"),
    ("Балакавриат", "bachelor"),
    ("бакаоавриат", "bachelor"),
    ("Балавриат", "bachelor"),
    ("Бакалавар", "bachelor"),
    ("Балаквариат", "bachelor"),
    ("Бакалавр", "bachelor"),
    ("БАКАЛАВРИАТ 3", "bachelor"),
    ("базовое высшее", "bachelor"),
    ("1 базовое высшее", "bachelor"),
    ("бакалавриат/базовое высшее", "bachelor"),
    ("магистратура", "master"),
    ("Магистратура 1-ый курс", "master"),
    ("маг", "master"),
    ("1 курс магистратуры МГУ", "master"),
    ("аспирантура", "phd"),
    ("ординатура", "phd"),
    ("специалитет, 1 курс", "specialist"),
    ("Специалист", "specialist"),
    ("2", None),
    ("первый курс", None),
    ("-", None),
    ("", None),
    (None, None),
    # Два разных уровня в одной строке — не угадываем.
    ("магистратура после бакалавриата, 1 курс", None),
])
def test_level_detection(text, level):
    assert parse_course(text).level == level


# ---------------------------------------------------------------- parse_course: курс

@pytest.mark.parametrize("text, year", [
    ("1 бакалавриат", 1),
    ("Бакалавриат,4", 4),
    ("Магистратура, 2курс", 2),
    ("Магистратура 1-ый курс", 1),
    ("2 курс", 2),
    ("1 курс (бакалавриат)", 1),
    ("4 курс, Бакалавриат", 4),
    ("Бакалавр 4", 4),
    ("БАКАЛАВРИАТ 3", 3),
    ("Специалитет, 5 курс", 5),
    ("6", 6),
    ("первый курс", 1),
    ("второй", 2),
    ("третий курс бакалавриата", 3),
    ("четвёртый курс", 4),
    ("четвертый", 4),
    ("пятый курс", 5),
    ("шестой курс", 6),
    ("второкурсник", 2),
    # Трёхзначные и склеенные числа — не курс.
    ("Бакалавриат, 101", None),
    ("Бакалавриат Р/БО25-2", None),
    ("группа 2024", None),
    ("7", None),
    ("0", None),
    # Два кандидата: со словом «курс» — побеждает ближний, без него — не угадываем.
    ("3 курс, поток 2", 3),
    ("поток 2, 3 курс", 3),
    ("1 или 2", None),
    ("Бакалавриат", None),
    ("-", None),
    ("", None),
    (None, None),
])
def test_year_detection(text, year):
    assert parse_course(text).year == year


# ---------------------------------------------------------------- parse_course: канон

@pytest.mark.parametrize("text, canonical", [
    ("1 бакалавриат", "1"),
    ("Балакавриат 3", "3"),
    ("Бакалавриат,4", "4"),
    ("Специалитет, 5 курс", "5+"),
    ("шестой курс", "5+"),
    ("3", "3"),
    ("Магистратура, 2курс", MASTER),
    ("магистратура", MASTER),
    ("аспирантура", MASTER),
    ("Бакалавриат", None),
    ("Бакалавриат, 101", None),
    ("-", None),
    (None, None),
])
def test_canonical_value(text, canonical):
    assert parse_course(text).canonical == canonical


def test_canonical_is_always_a_course_option_or_none():
    samples = ["1 бакалавриат", "магистратура", "6", "Бакалавриат", "-", "x" * 500, "Специалитет, 5 курс"]
    for text in samples:
        canonical = parse_course(text).canonical
        assert canonical is None or canonical in COURSE_OPTIONS


# ---------------------------------------------------------------- parse_course: не студенты

@pytest.mark.parametrize("text", [
    "Выпускник магистратуры",
    "Магистратура, выпускник",
    "выпускница бакалавриата 2025",
    "Сопровождающий преподаватель",
    "преподаватель кафедры, 1 курс",
])
def test_non_student_shapes(text):
    parsed = parse_course(text)
    assert parsed.non_student is True
    assert parsed.year is None
    assert parsed.canonical is None


def test_parsed_course_is_frozen_dataclass():
    parsed = parse_course("2 курс")
    assert isinstance(parsed, ParsedCourse)
    with pytest.raises(Exception):
        parsed.year = 5  # type: ignore[misc]


def test_hostile_input_does_not_crash():
    hostile = "(((бак[1]*+?\\" * 50 + "1 курс" + ")" * 300
    parsed = parse_course(hostile)
    assert isinstance(parsed, ParsedCourse)
    assert parse_course("бакалавриат " * 100).level == "bachelor"


# ---------------------------------------------------------------- evaluate_ta: после отсечки

@pytest.mark.parametrize("text, expected", [
    ("Магистратура, 1 курс", "ok"),
    ("Магистратура 2 курс", "ok"),
    ("аспирантура", "ok"),
    ("1 бакалавриат", "no"),
    ("2 курс бакалавриата", "no"),
    ("Балакавриат 2", "no"),
    ("1 курс (бакалавриат)", "no"),
    ("1 базовое высшее", "no"),
    ("специалитет, 1 курс", "no"),
    ("Бакалавриат, 3 курс", "ok"),
    ("Бакалавриат,4", "ok"),
    ("Специалитет, 5 курс", "ok"),
    ("Бакалавриат", "check"),
    ("специалитет", "check"),
    ("1", "check"),
    ("2", "check"),
    ("3", "ok"),
    ("4 курс", "ok"),
    ("первый курс", "check"),
    ("третий курс", "ok"),
    ("-", "check"),
    ("", "check"),
    (None, "check"),
    ("Бакалавриат, 101", "check"),
    ("Бакалавриат Р/БО25-2", "check"),
    ("1 или 2", "check"),
    ("Выпускник магистратуры", "check"),
    ("Сопровождающий преподаватель", "check"),
    ("Специалист по внешним коммуникациям", "check"),
    ("магистратура после бакалавриата, 1 курс", "check"),
])
def test_rule_after_cutoff(text, expected):
    assert evaluate_ta(parse_course(text), AFTER, CUTOFF, NOT_TA) == expected


# ---------------------------------------------------------------- evaluate_ta: до отсечки

@pytest.mark.parametrize("text", ["Бакалавриат", "2", "1 бакалавриат", "-", "", None, "Бакалавриат, 101"])
def test_before_cutoff_everyone_is_ok(text):
    assert evaluate_ta(parse_course(text), BEFORE, CUTOFF, NOT_TA) == "ok"


@pytest.mark.parametrize("text", ["Выпускник магистратуры", "Сопровождающий преподаватель"])
def test_non_student_is_check_even_before_cutoff(text):
    assert evaluate_ta(parse_course(text), BEFORE, CUTOFF, NOT_TA) == "check"


def test_cutoff_boundary_is_exclusive():
    parsed = parse_course("1 бакалавриат")
    assert evaluate_ta(parsed, "2026-09-22 23:59:59", CUTOFF, NOT_TA) == "ok"
    assert evaluate_ta(parsed, "2026-09-23 00:00:00", CUTOFF, NOT_TA) == "no"


# ---------------------------------------------------------------- evaluate_ta: список и дата

def test_not_ta_list_is_honoured_not_hardcoded():
    parsed = parse_course("Бакалавриат, 3 курс")
    assert evaluate_ta(parsed, AFTER, CUTOFF, ["1", "2", "3"]) == "no"
    assert evaluate_ta(parsed, AFTER, CUTOFF, ["1"]) == "ok"
    assert evaluate_ta(parse_course("2 курс бакалавриата"), AFTER, CUTOFF, ["1"]) == "ok"
    # Голая цифра без уровня: если курс в списке «не ЦА» — проверить, иначе ЦА.
    assert evaluate_ta(parse_course("3"), AFTER, CUTOFF, ["1", "2", "3"]) == "check"
    assert evaluate_ta(parse_course("3"), AFTER, CUTOFF, ["1", "2"]) == "ok"
    # Магистратура в списке не бывает, но даже если — магистры ЦА.
    assert evaluate_ta(parse_course("магистратура, 1 курс"), AFTER, CUTOFF, COURSE_OPTIONS) == "ok"


@pytest.mark.parametrize("answered_at", [None, "", "вчера", "2026-09-20", "20.09.2026 10:00:00"])
def test_unknown_date_counts_as_after_cutoff(answered_at):
    assert evaluate_ta(parse_course("1 бакалавриат"), answered_at, CUTOFF, NOT_TA) == "no"
    assert evaluate_ta(parse_course("Бакалавриат"), answered_at, CUTOFF, NOT_TA) == "check"


def test_no_cutoff_means_rule_applies_to_everyone():
    assert evaluate_ta(parse_course("1 бакалавриат"), BEFORE, None, NOT_TA) == "no"
    assert evaluate_ta(parse_course("магистратура"), BEFORE, None, NOT_TA) == "ok"


@pytest.mark.parametrize("cutoff", [
    "23.09.2026",            # дефолт реестра, когда ключ не сохраняли (строка, не datetime)
    date(2026, 9, 23),
    datetime(2026, 9, 23, 0, 0),
])
def test_cutoff_accepts_registry_default_string_and_date(cutoff):
    parsed = parse_course("1 бакалавриат")
    assert evaluate_ta(parsed, BEFORE, cutoff, NOT_TA) == "ok"
    assert evaluate_ta(parsed, AFTER, cutoff, NOT_TA) == "no"


def test_garbage_cutoff_string_is_treated_as_no_cutoff():
    assert evaluate_ta(parse_course("1 бакалавриат"), BEFORE, "когда-нибудь", NOT_TA) == "no"


def test_empty_not_ta_list_makes_bachelors_ok():
    assert evaluate_ta(parse_course("1 бакалавриат"), AFTER, CUTOFF, []) == "ok"
    assert evaluate_ta(parse_course("1 бакалавриат"), AFTER, CUTOFF, None) == "ok"


# ---------------------------------------------------------------- чистота модуля

def test_module_is_pure_and_references_course_options():
    src = Path("services/delegations_course.py").read_text(encoding="utf-8")
    assert "aiosqlite" not in src
    assert "aiogram" not in src
    assert not re.search(r"^\s*(from|import)\s+database", src, re.M)
    assert "COURSE_OPTIONS" in src
    # Строки в таблицах вымышленные: ни одного адреса почты ни в модуле, ни в этом файле.
    for path in ("services/delegations_course.py", __file__):
        assert not re.search(r"[\w.+-]+@[\w-]+\.[a-z]{2,}", Path(path).read_text(encoding="utf-8"))
