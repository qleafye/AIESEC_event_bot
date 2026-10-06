"""Делегации вузов — разбор свободного текста курса и правило целевой аудитории.

Чистый модуль (stdlib + `reg_options.COURSE_OPTIONS`): без БД, без бот-фреймворка, синхронный
— тот же класс функции, что `reg_engine.evaluate_reject_rules`. Вход — ответ формы «Бакалавриат
или магистратура, номер курса» как его пишут люди: «1 бакалавриат», «Балакавриат 3»,
«Магистратура, 2курс», «первый курс», «Бакалавриат, 101». Выход `evaluate_ta` — одно из трёх:

- `ok`    — целевая аудитория: подал до отсечки (курс не важен), магистратура/аспирантура,
            либо бакалавриат/специалитет с курсом вне списка «не ЦА»;
- `no`    — не ЦА: бакалавриат/специалитет, курс из списка «не ЦА» (по умолчанию 1–2), подан
            после отсечки;
- `check` — бот не берётся решать, и менеджер нажимает «ЦА / не ЦА» сам.

Почему голая цифра 1–2 и уровень без цифры — «проверить», а не «нет»: «2» без слова может быть
вторым курсом магистратуры (ЦА), а «Бакалавриат» без номера — третьим курсом. Молча в одну из
сторон не относим; голая 3+ — ЦА при любом уровне (магистратура и специалитет 3+ тоже ЦА).
Выпускники, преподаватели и сопровождающие — всегда «проверить», даже до отсечки.
"""
import re
from dataclasses import dataclass
from datetime import date, datetime

from reg_options import COURSE_OPTIONS

_MAX_LEN = 200                       # защита регулярок от километровых ответов
_FMT = "%Y-%m-%d %H:%M:%S"           # так пишет answered_at services/ext_forms_parse._to_msk
_ANCHOR_RADIUS = 8                   # ближайшая к слову «курс» цифра не дальше стольких символов

_NON_STUDENT = re.compile(r"выпускн|преподават|сопровожда")
# Порядок не важен: два разных уровня в одной строке = уровень неизвестен (не угадываем).
_LEVELS = (
    ("phd", re.compile(r"аспирант|ординат")),
    ("master", re.compile(r"магистр|(?<!\w)маг(?!\w)")),
    ("specialist", re.compile(r"специалитет|специалист")),
    # Опечатки «Балакавриат/Балавриат/бакаоавриат» и новое название «базовое высшее».
    ("bachelor", re.compile(r"бак|бал\w*в|базов\w*\s+высш")),
)
# Одиночная цифра 1–6: не часть длинного числа и не приклеена к букве/«-»/«/» слева
# («101», «БО25-2», «2024» — не курс; «2курс», «1-ый», «,4» — курс).
_DIGIT = re.compile(r"(?<![\w/\-])([1-6])(?!\d)")
_ORDINALS = (("перв", 1), ("втор", 2), ("трет", 3), ("четв", 4), ("пят", 5), ("шест", 6))
_ORDINAL = re.compile(
    r"(?<!\w)(перв|втор|трет|четв[её]рт|пят|шест)"
    r"(?:ый|ой|ий|ого|его|ом|ем|ая|ые|ых|ую|-?го|[оь]е?курсни\w*)?(?!\w)"
)
_COURSE_WORD = re.compile(r"курс")

_MASTER_OPTION = next(o for o in COURSE_OPTIONS if "магистр" in o.lower())
_PLUS_OPTION = next(o for o in COURSE_OPTIONS if o.endswith("+"))
_PLUS_FROM = int(_PLUS_OPTION.rstrip("+"))


@dataclass(frozen=True)
class ParsedCourse:
    level: str | None        # "bachelor" | "master" | "specialist" | "phd" | None
    year: int | None         # 1..6 или None
    canonical: str | None    # значение из reg_options.COURSE_OPTIONS или None
    non_student: bool        # выпускник / преподаватель / сопровождающий — всегда «проверить»


def _normalize(text) -> str:
    if not isinstance(text, str):
        return ""
    return text[:_MAX_LEN].strip().casefold()


def _detect_level(text: str) -> str | None:
    found = {name for name, rx in _LEVELS if rx.search(text)}
    return found.pop() if len(found) == 1 else None


def _candidates(text: str) -> list[tuple[int, int, int]]:
    cands = [(int(m.group(1)), m.start(), m.end()) for m in _DIGIT.finditer(text)]
    for m in _ORDINAL.finditer(text):
        stem = m.group(1)
        year = next(y for s, y in _ORDINALS if stem.startswith(s))
        cands.append((year, m.start(), m.end()))
    return cands


def _detect_year(text: str) -> int | None:
    cands = _candidates(text)
    if not cands:
        return None
    anchors = [m.span() for m in _COURSE_WORD.finditer(text)]
    if anchors:
        # Есть слово «курс» — побеждает ближайшая к нему цифра («3 курс, поток 2» → 3).
        dist = {c: min(max(0, a0 - c[2], c[1] - a1) for a0, a1 in anchors) for c in cands}
        best = min(dist.values())
        if best <= _ANCHOR_RADIUS:
            cands = [c for c in cands if dist[c] == best]
    values = {c[0] for c in cands}
    return values.pop() if len(values) == 1 else None


def _canonical(level: str | None, year: int | None) -> str | None:
    if level in ("master", "phd"):
        return _MASTER_OPTION
    if year is None:
        return None
    return _PLUS_OPTION if year >= _PLUS_FROM else str(year)


def parse_course(text: str | None) -> ParsedCourse:
    """Разобрать ответ формы на уровень, номер курса и каноническое значение анкеты."""
    norm = _normalize(text)
    if not norm:
        return ParsedCourse(None, None, None, False)
    if _NON_STUDENT.search(norm):
        return ParsedCourse(None, None, None, True)
    level = _detect_level(norm)
    year = _detect_year(norm)
    return ParsedCourse(level, year, _canonical(level, year), False)


def _cutoff_dt(cutoff) -> datetime | None:
    """Отсечка из реестра: datetime (сохранённое значение), date, либо строка «ДД.ММ.ГГГГ»
    (дефолт ключа, когда его ни разу не сохраняли). Непонятное = отсечки нет."""
    if isinstance(cutoff, datetime):
        return cutoff
    if isinstance(cutoff, date):
        return datetime(cutoff.year, cutoff.month, cutoff.day)
    if isinstance(cutoff, str):
        try:
            return datetime.strptime(cutoff.strip(), "%d.%m.%Y")
        except ValueError:
            return None
    return None


def evaluate_ta(parsed: ParsedCourse, answered_at: str | None, cutoff, not_ta_canonical) -> str:
    """Правило ЦА: "ok" | "no" | "check" (смысл — в докстринге модуля).

    `answered_at` — строка «ГГГГ-ММ-ДД ЧЧ:ММ:СС» по Москве; пустая или кривая дата считается
    ПОСЛЕ отсечки (привилегию «подал заранее» без даты не даём). `cutoff` None — правило
    действует для всех. `not_ta_canonical` — курсы «не ЦА» из настроек в значениях анкеты.
    """
    not_ta = set(not_ta_canonical or ())
    cutoff_at = _cutoff_dt(cutoff)
    try:
        answered = datetime.strptime(str(answered_at), _FMT)
    except (TypeError, ValueError):
        answered = None
    if parsed.non_student:
        return "check"
    if cutoff_at is not None and answered is not None and answered < cutoff_at:
        return "ok"
    if parsed.level in ("master", "phd"):
        return "ok"
    if parsed.year is not None:
        if parsed.level in ("bachelor", "specialist"):
            return "no" if parsed.canonical in not_ta else "ok"
        return "check" if parsed.canonical in not_ta else "ok"
    return "check"
