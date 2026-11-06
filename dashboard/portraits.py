"""Сводки страницы «ФОРУМ: ИТОГИ».

Считаются на каждый запрос из открытой read-only базы, без кэша. В ответ попадают
только счётчики и подписи кнопок. Имён, контактов и свободных текстов анкет нет.
"""
from __future__ import annotations

import json
import re
from collections import Counter
from datetime import datetime

from dashboard.queries import Scope, _scope_sql, _table_exists

_GOALS = (
    "Пообщаться с людьми из моей сферы, нетворкинг",
    "Найти возможность трудоустройства",
    "Прокачать свои hard и soft skills",
    "Получить карьерную консультацию от HR",
    "Узнать о деятельности компаний",
)
_FORMATS = (
    "Панельные дискуссии",
    "Мастер-классы",
    "Сессии со спикерами",
    "Нетворкинг-сессии",
    "Ярмарка открытых вакансий",
)
_FIELDS = (
    "Бизнес и управление",
    "IT и технологии",
    "Социальные и гуманитарные науки",
    "Математические и естественные науки",
)
_UNI = {
    "ниу вшэ": "НИУ ВШЭ",
    "вшэ": "НИУ ВШЭ",
    "высшая школа экономики": "НИУ ВШЭ",
    "финансовый университет при правительстве российской федерации": "Финансовый университет",
    "финансовый университет при правительстве рф": "Финансовый университет",
    "финансовый университет": "Финансовый университет",
    "финуниверситет": "Финансовый университет",
    "рудн": "РУДН",
    "рудн им. п. лумумбы": "РУДН",
    "российский университет дружбы народов": "РУДН",
    "мгу": "МГУ",
    "мгу им. ломоносова": "МГУ",
    "мгу имени м.в. ломоносова": "МГУ",
    "мгу им. м.в. ломоносова": "МГУ",
    "тюмгу": "ТюмГУ",
    "тюменский государственный университет": "ТюмГУ",
    "ранхигс": "РАНХиГС",
    "мгимо": "МГИМО",
    "маи": "МАИ",
    "мпгу": "МПГУ",
    "рту мирэа": "РТУ МИРЭА",
    "мирэа": "РТУ МИРЭА",
    "рггу": "РГГУ",
}
_EMPTY = {"", "-", "пропустить", "нет", "не указано", "skip", "none"}
_COLUMNS = (
    "telegram_id", "course", "study_field", "university", "goal", "formats",
    "source", "source_from_tag", "education_status", "age", "participant_type",
)
_FIELD_MIN = 25
_BOT_WORD = re.compile(r"(?<![а-я])бот(?:а|у|ом|е|ы|ов|ик|е)?(?![а-я])")


def _columns(conn, table: str) -> set[str]:
    if not _table_exists(conn, table):
        return set()
    return {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}


def _norm(value) -> str:
    return " ".join(str(value or "").replace("\n", " ").split())


def _empty(value) -> bool:
    return _norm(value).casefold() in _EMPTY


def _pct(num: int, den: int) -> str:
    if not den:
        return "—"
    return f"{num / den * 100:.1f}%".replace(".", ",")


def _avg(total: float, count: int, digits: int = 2) -> str:
    if not count:
        return "—"
    return f"{total / count:.{digits}f}".replace(".", ",")


def _words(n: int, one: str, few: str, many: str) -> str:
    n = abs(int(n))
    if n % 10 == 1 and n % 100 != 11:
        return one
    if 2 <= n % 10 <= 4 and not 12 <= n % 100 <= 14:
        return few
    return many


def _ru_day(day: str) -> str:
    try:
        return datetime.strptime(day, "%Y-%m-%d").strftime("%d.%m.%Y")
    except ValueError:
        return day


def _list_setting(conn, key: str, default: tuple[str, ...]) -> list[str]:
    if not _table_exists(conn, "bot_settings"):
        return list(default)
    row = conn.execute("SELECT value FROM bot_settings WHERE key = ?", (key,)).fetchone()
    raw = "" if row is None else (row["value"] or "")
    text = str(raw).strip()
    if not text:
        return list(default)
    if text.startswith("["):
        try:
            loaded = json.loads(text)
        except json.JSONDecodeError:
            loaded = None
        if isinstance(loaded, list):
            items = [_norm(item) for item in loaded if _norm(item)]
            return items or list(default)
    lines = [_norm(line) for line in text.splitlines() if _norm(line)]
    return lines or list(default)


def _user_scope(conn, scope: Scope) -> tuple[list[str], tuple]:
    parts, params = _scope_sql(conn, scope)
    qualified = [
        part.replace("event_city", "users.event_city").replace("season", "users.season")
        for part in parts
    ]
    return qualified, params


def _load(conn, scope: Scope) -> tuple[list[dict], list[dict], int, dict[int, set[str]]]:
    """Одобренные среза, из них с отметкой входа, чужие отметки, дни по человеку."""
    cols = _columns(conn, "users")
    if "telegram_id" not in cols or "status" not in cols:
        return [], [], 0, {}
    picked = [name for name in _COLUMNS if name in cols]
    parts, params = _user_scope(conn, scope)
    where = ["users.status = 'approved'", *parts]
    rows = [
        {name: row[name] for name in picked}
        for row in conn.execute(
            f"SELECT {', '.join(picked)} FROM users WHERE {' AND '.join(where)}",
            params,
        )
    ]
    approved_ids = {row["telegram_id"] for row in rows}
    days: dict[int, set[str]] = {}
    entry_ids: set[int] = set()
    if _table_exists(conn, "checkins"):
        for row in conn.execute(
            "SELECT telegram_id, day FROM checkins WHERE point = 'entry'"
        ):
            entry_ids.add(row["telegram_id"])
            if row["telegram_id"] in approved_ids:
                days.setdefault(row["telegram_id"], set()).add(row["day"] or "")
    arrived = [row for row in rows if row["telegram_id"] in entry_ids]
    outside = len(entry_ids - approved_ids)
    return rows, arrived, outside, days


def _course(value) -> str:
    raw = _norm(value)
    low = raw.casefold()
    if _empty(raw):
        return "не указан"
    if low in {"1", "2", "3", "4", "5"}:
        return f"{low} курс"
    if low in {"5+", "5 +"}:
        return "5+ курс"
    if low in {"магистратура/аспирантура", "магистратура", "магистратура 2"}:
        return "магистратура / аспирантура"
    if low == "graduated":
        return "уже выпустился"
    if low == "college":
        return "колледж"
    if low == "подготовительные курсы":
        return "подготовительные курсы"
    return "другая формулировка"


def _uni(value) -> str:
    if _empty(value):
        return "не указан"
    key = _norm(value).casefold().replace("ё", "е")
    return _UNI.get(key, _norm(value))


def _field(value) -> str:
    if _empty(value):
        return "не указано"
    return _norm(value)


def _source(row: dict) -> str:
    raw = _norm(row.get("source"))
    if _empty(raw):
        return "не указан"
    if row.get("source_from_tag"):
        return f"по ссылке: {raw}"
    return raw


def _edu(value) -> str:
    raw = _norm(value)
    low = raw.casefold().replace("ё", "е")
    if _empty(raw):
        return "не указано"
    if low in {"да, в вузе или колледже", "да, в вузе", "да", "yes"}:
        return "учится в вузе или колледже"
    if low == "нет, завершил(а) обучение":
        return "обучение уже завершено"
    if low in {"нет, не получал(а) образование", "нет"}:
        return "не учится"
    return "другая формулировка"


def _age_band(value) -> str:
    if value is None or _norm(value) == "":
        return "не указан"
    try:
        age = int(value)
    except (TypeError, ValueError):
        return "не указан"
    if age < 16 or age > 45:
        return "не указан"
    if age <= 17:
        return "16-17"
    if age <= 19:
        return "18-19"
    if age <= 21:
        return "20-21"
    if age <= 24:
        return "22-24"
    return "25-45"


def _track(value) -> str:
    raw = _norm(value)
    if raw == "full":
        return "полная регистрация"
    if raw == "short":
        return "краткая форма"
    if not raw:
        return "не указан"
    folded = raw.casefold()
    if any("а" <= char <= "я" or char == "ё" for char in folded):
        return raw
    return "другая анкета"


def _split_known(value, options: list[str]) -> tuple[list[str], bool]:
    if _empty(value):
        return [], False
    rest = _norm(value)
    found = []
    for option in sorted(options, key=len, reverse=True):
        if option and option in rest:
            found.append(option)
            rest = rest.replace(option, " ")
    return found, bool(rest.strip(" ,;"))


def _row(label: str, key: str, left: int, right: int, left_den: int, right_den: int) -> dict:
    return {
        "key": key,
        "label": label,
        "reg": left,
        "reg_pct": _pct(left, left_den),
        "arr": right,
        "arr_pct": _pct(right, right_den),
    }


def _paired(cut_id, title, note, left: Counter, right: Counter, left_den, right_den,
            order=None, limit=None, keep_zeros=False, closed=False) -> dict:
    blanks = {"не указан", "не указано"}
    if order is None:
        names = [name for name, _count in left.most_common()]
    else:
        names = list(order)
        if not closed:
            for name in left:
                if name not in names:
                    names.append(name)
    plain = [name for name in names if name not in blanks]
    if limit is None:
        shown = plain
        rest = []
    else:
        shown = plain[:limit]
        rest = plain[limit:]
    rows = []
    for name in shown:
        if not keep_zeros and not left[name] and not right[name]:
            continue
        rows.append(_row(name, f"c|{cut_id}|{name}", left[name], right[name], left_den, right_den))
    if rest:
        lrest = sum(left[name] for name in rest)
        rrest = sum(right[name] for name in rest)
        label = f"остальные ({len(rest)})"
        rows.append(_row(label, f"c|{cut_id}|{label}", lrest, rrest, left_den, right_den))
    for blank in ("не указан", "не указано"):
        if left[blank] or right[blank]:
            rows.append(_row(
                blank, f"c|{cut_id}|{blank}", left[blank], right[blank], left_den, right_den,
            ))
    return {"id": cut_id, "title": title, "note": note, "rows": rows, "footer": []}


def _count(people, fn) -> Counter:
    return Counter(fn(person) for person in people)


def _cuts(conn, approved, arrived) -> list[dict]:
    left_den, right_den = len(approved), len(arrived)
    cuts = [
        _paired(
            "course", "Курс",
            "Кнопки анкеты. Ответ не из списка сведён в «другая формулировка», сам текст не показан.",
            _count(approved, lambda row: _course(row.get("course"))),
            _count(arrived, lambda row: _course(row.get("course"))),
            left_den, right_den,
            order=["1 курс", "2 курс", "3 курс", "4 курс", "5 курс", "5+ курс",
                   "магистратура / аспирантура", "колледж", "подготовительные курсы",
                   "уже выпустился", "другая формулировка", "не указан"],
            closed=True,
        )
    ]
    canon = _list_setting(conn, "study_field_options", _FIELDS)
    field_left = _count(approved, lambda row: _field(row.get("study_field")))
    field_right = _count(arrived, lambda row: _field(row.get("study_field")))
    extra = [
        name for name, count in field_left.most_common()
        if name not in canon and name != "не указано" and count >= _FIELD_MIN
    ]
    other_names = [
        name for name in field_left
        if name not in canon and name not in extra and name != "не указано"
    ]
    field = _paired(
        "field", "Направление",
        "Первые строки, где они есть, это кнопки анкеты. Свои формулировки показаны от "
        f"{_FIELD_MIN} одобренных. «Экономика» и «Бизнес и управление» не склеены.",
        field_left, field_right, left_den, right_den,
        order=canon + extra,
        closed=True,
    )
    if other_names:
        lrest = sum(field_left[name] for name in other_names)
        rrest = sum(field_right[name] for name in other_names)
        label = f"остальные свои формулировки ({len(other_names)})"
        blank_at = next(
            (i for i, row in enumerate(field["rows"]) if row["label"] in {"не указан", "не указано"}),
            len(field["rows"]),
        )
        field["rows"].insert(blank_at, _row(
            label, f"c|field|{label}", lrest, rrest, left_den, right_den,
        ))
    cuts.append(field)

    uni_left = _count(approved, lambda row: _uni(row.get("university")))
    uni_right = _count(arrived, lambda row: _uni(row.get("university")))
    ranked = sorted(
        (name for name in uni_left if name != "не указан"),
        key=lambda name: (-uni_left[name], name),
    )
    top = ranked[:12]
    rest = ranked[12:]
    rest += [name for name in uni_right if name not in top and name != "не указан" and name not in rest]
    uni_rows = [
        _row(name, f"c|uni|{name}", uni_left[name], uni_right[name], left_den, right_den)
        for name in top
    ]
    if rest:
        label = f"остальные указанные ({len(rest)} написаний)"
        uni_rows.append(_row(
            label, f"c|uni|{label}",
            sum(uni_left[name] for name in rest),
            sum(uni_right[name] for name in rest),
            left_den, right_den,
        ))
    if uni_left["не указан"] or uni_right["не указан"]:
        uni_rows.append(_row(
            "не указан", "c|uni|не указан",
            uni_left["не указан"], uni_right["не указан"], left_den, right_den,
        ))
    cuts.append({
        "id": "uni",
        "title": "Вуз",
        "note": (
            "Склеены только повторы одного названия: ВШЭ и НИУ ВШЭ, написания Финансового "
            "университета, РУДН, МГУ, ТюмГУ, МИРЭА. «Пропустить» посчитан как «не указан». "
            "Остальные написания оставлены как в анкете."
        ),
        "rows": uni_rows,
        "footer": [],
    })

    for cut_id, title, column, setting, default in (
        ("goal", "Зачем пришли", "goal", "goal_options", _GOALS),
        ("formats", "Какие форматы отметили", "formats", "formats_options", _FORMATS),
    ):
        options = _list_setting(conn, setting, default)
        left, right = Counter(), Counter()
        empty = [0, 0]
        own = [0, 0]
        for index, people in enumerate((approved, arrived)):
            bucket = left if index == 0 else right
            for person in people:
                found, has_rest = _split_known(person.get(column), options)
                if not found and not has_rest:
                    empty[index] += 1
                    continue
                for item in found:
                    bucket[item] += 1
                if has_rest:
                    own[index] += 1
        rows = [
            _row(name, f"c|{cut_id}|{name}", left[name], right[name], left_den, right_den)
            for name in options
        ]
        if empty[0] or empty[1]:
            rows.append(_row(
                "не ответили", f"c|{cut_id}|не ответили",
                empty[0], empty[1], left_den, right_den,
            ))
        note = "Можно отметить несколько пунктов, поэтому сумма процентов больше 100."
        if cut_id == "goal":
            note = "Вопрос анкеты «Цель участия». Отдельного вопроса «мотивация» нет. " + note
        footer = []
        if any(own):
            footer.append(
                "Свой текст сверх списка оставили "
                f"{own[0]} {_words(own[0], 'одобренный', 'одобренных', 'одобренных')} и "
                f"{own[1]} {_words(own[1], 'дошедший', 'дошедших', 'дошедших')}. "
                "Тексты здесь не показываем."
            )
        cuts.append({
            "id": cut_id, "title": title, "note": note, "rows": rows, "footer": footer,
        })

    cuts.append(_paired(
        "source", "Откуда узнали",
        "Строка «по ссылке» значит метку в ссылке, не кнопку «откуда узнали».",
        _count(approved, _source), _count(arrived, _source),
        left_den, right_den, limit=12,
    ))
    cuts.append(_paired(
        "edu", "Учёба сейчас",
        "«Да» сведено к «учится». Название вуза, вписанное в это поле вместо кнопки, "
        "попадает в «другая формулировка».",
        _count(approved, lambda row: _edu(row.get("education_status"))),
        _count(arrived, lambda row: _edu(row.get("education_status"))),
        left_den, right_den,
        order=["учится в вузе или колледже", "обучение уже завершено", "не учится",
               "другая формулировка", "не указано"],
        closed=True,
    ))
    age = _paired(
        "age", "Возраст",
        "Пустое поле и значения вне 16-45 посчитаны как «не указан». "
        "Среднее и медиана только по тем, у кого возраст в этом диапазоне.",
        _count(approved, lambda row: _age_band(row.get("age"))),
        _count(arrived, lambda row: _age_band(row.get("age"))),
        left_den, right_den,
        order=["16-17", "18-19", "20-21", "22-24", "25-45", "не указан"],
        keep_zeros=True,
        closed=True,
    )
    age["footer"] = [
        _age_line("одобренных", approved),
        _age_line("дошедших", arrived),
    ]
    cuts.append(age)
    cuts.append(_paired(
        "track", "Трек анкеты",
        "",
        _count(approved, lambda row: _track(row.get("participant_type"))),
        _count(arrived, lambda row: _track(row.get("participant_type"))),
        left_den, right_den,
    ))
    return [cut for cut in cuts if cut["rows"]]


def _age_line(label: str, people: list[dict]) -> str:
    ages = []
    for person in people:
        if _age_band(person.get("age")) == "не указан":
            continue
        ages.append(int(person.get("age")))
    if not ages:
        return f"Возраст не указан ни у кого из {label}."
    ages.sort()
    if len(ages) % 2:
        mid = str(ages[len(ages) // 2])
    else:
        mid = f"{(ages[len(ages) // 2 - 1] + ages[len(ages) // 2]) / 2:.1f}".replace(".", ",")
    mean = _avg(sum(ages), len(ages), 1)
    return f"Возраст указан у {len(ages)} из {len(people)} {label}: среднее {mean}, медиана {mid}."


def _json_load(raw, fallback):
    if raw is None or raw == "":
        return fallback
    try:
        return json.loads(raw)
    except (TypeError, json.JSONDecodeError):
        return fallback


def _label(item) -> str:
    if isinstance(item, dict):
        return _norm(item.get("t") or item.get("text") or "")
    return _norm(item)


def _days_text(days: dict[int, set[str]]) -> str:
    if not days:
        return (
            "Отметок входа в этом срезе нет. Дошедшие считаются по скану QR на входе, "
            "не по явке зала."
        )
    found = sorted({day for owned in days.values() for day in owned if day})
    bits = []
    for day in found:
        people = sum(1 for owned in days.values() if day in owned)
        bits.append(f"{_ru_day(day)} — {people} {_words(people, 'человек', 'человека', 'человек')}")
    text = "По дням: " + ", ".join(bits) + "."
    if len(found) == 2:
        only_first = sum(1 for owned in days.values() if owned == {found[0]})
        only_second = sum(1 for owned in days.values() if owned == {found[1]})
        both = sum(1 for owned in days.values() if owned == set(found))
        text += (
            f" Только {_ru_day(found[0])}: {only_first}. "
            f"Только {_ru_day(found[1])}: {only_second}. Оба дня: {both}."
        )
    return text + " Это сканы QR, не перекличка зала."


def _nps(conn, scope: Scope) -> dict:
    sessions = {"available": False, "prompted": 0, "rated": 0, "avg": "—", "rows": []}
    groups: list[dict] = []
    choices: list[dict] = []
    asked = False
    if not _table_exists(conn, "survey_questions"):
        return {"asked": False, "sessions": sessions, "groups": groups, "choices": choices}
    parts, params = _user_scope(conn, scope)
    scope_sql = " AND ".join(["users.status = 'approved'", *parts]) if parts else "users.status = 'approved'"

    if _table_exists(conn, "session_feedback"):
        sessions["available"] = True
        prompted = conn.execute(
            "SELECT COUNT(*) AS n FROM session_feedback "
            "JOIN users ON users.telegram_id = session_feedback.telegram_id "
            f"WHERE {scope_sql}",
            params,
        ).fetchone()["n"]
        rated_rows = list(conn.execute(
            "SELECT rating, COUNT(*) AS n FROM session_feedback "
            "JOIN users ON users.telegram_id = session_feedback.telegram_id "
            f"WHERE rating > 0 AND {scope_sql} GROUP BY rating",
            params,
        ))
        by_rating = {int(row["rating"]): int(row["n"]) for row in rated_rows}
        rated = sum(by_rating.values())
        total = sum(rating * count for rating, count in by_rating.items())
        sessions.update({
            "prompted": int(prompted or 0),
            "rated": rated,
            "avg": _avg(total, rated),
            "rows": [
                {
                    "key": f"s|{rating}",
                    "label": str(rating),
                    "n": by_rating.get(rating, 0),
                    "pct": _pct(by_rating.get(rating, 0), rated),
                }
                for rating in range(1, 6)
            ],
        })

    if not _table_exists(conn, "survey_answers") or not _table_exists(conn, "polls"):
        return {"asked": asked, "sessions": sessions, "groups": groups, "choices": choices}

    questions = list(conn.execute(
        "SELECT q.id, q.poll_id, q.qtype, q.text, q.options_json, q.rows_json, q.scale_hi, "
        "p.question AS poll_title "
        "FROM survey_questions q JOIN polls p ON p.id = q.poll_id "
        "ORDER BY q.poll_id, q.position, q.id"
    ))
    for question in questions:
        hi = _norm(question["scale_hi"])
        if question["qtype"] == "nps" or (hi.isdigit() and int(hi) >= 10):
            asked = True
    progress_cache: dict[int, str] = {}

    def progress(poll_id: int) -> str:
        if poll_id in progress_cache:
            return progress_cache[poll_id]
        text = ""
        if _table_exists(conn, "survey_progress"):
            rows = conn.execute(
                "SELECT state, COUNT(*) AS n FROM survey_progress "
                "JOIN users ON users.telegram_id = survey_progress.user_id "
                f"WHERE survey_progress.poll_id = ? AND {scope_sql} GROUP BY state",
                (poll_id, *params),
            ).fetchall()
            by_state = {row["state"]: int(row["n"]) for row in rows}
            started = sum(by_state.values())
            done = by_state.get("done", 0)
            if started:
                text = f"В этом срезе анкету закончили {done} из {started} начавших."
        progress_cache[poll_id] = text
        return text

    for question in questions:
        text = question["text"] or ""
        title = _norm(text.split("\n", 1)[0])[:140]
        poll_title = _norm(question["poll_title"])
        folded = f"{poll_title}\n{title}".casefold().replace("ё", "е")
        if _BOT_WORD.search(folded):
            continue
        heading = f"{poll_title}. {title}" if poll_title else title
        if question["qtype"] == "table" and ("площадк" in text.casefold() or "перекус" in text.casefold()):
            labels = [_label(item) for item in _json_load(question["rows_json"], [])]
            buckets = {index: [] for index in range(len(labels))}
            answers = conn.execute(
                "SELECT sub, value_json FROM survey_answers "
                "JOIN users ON users.telegram_id = survey_answers.user_id "
                f"WHERE survey_answers.question_id = ? AND {scope_sql}",
                (question["id"], *params),
            )
            for answer in answers:
                sub = str(answer["sub"] or "")
                if not sub.startswith("r"):
                    continue
                try:
                    index = int(sub[1:])
                    score = int(_json_load(answer["value_json"], None))
                except (TypeError, ValueError):
                    continue
                if index in buckets and 1 <= score <= 5:
                    buckets[index].append(score)
            rows = []
            for index, label in enumerate(labels):
                scores = buckets[index]
                if not scores or not label:
                    continue
                rows.append({
                    "key": f"g|{question['id']}|{index}",
                    "label": label,
                    "n": len(scores),
                    "avg": _avg(sum(scores), len(scores)),
                })
            if rows:
                groups.append({
                    "id": question["id"],
                    "title": heading,
                    "note": progress(question["poll_id"]),
                    "rows": rows,
                })
            continue
        if question["qtype"] != "single":
            continue
        options = [_label(item) for item in _json_load(question["options_json"], [])]
        blob = text.casefold()
        mood = (
            "получил" in blob or "как ты сейчас" in blob
            or any("хочу ещё" in opt.casefold() or "хочу еще" in opt.casefold() for opt in options)
        )
        if not mood:
            continue
        counts = Counter()
        for answer in conn.execute(
            "SELECT value_json FROM survey_answers "
            "JOIN users ON users.telegram_id = survey_answers.user_id "
            f"WHERE survey_answers.question_id = ? AND sub = '' AND {scope_sql}",
            (question["id"], *params),
        ):
            try:
                counts[int(_json_load(answer["value_json"], None))] += 1
            except (TypeError, ValueError):
                continue
        total = sum(counts.values())
        if not total:
            continue
        choices.append({
            "id": question["id"],
            "title": heading,
            "note": progress(question["poll_id"]),
            "total": total,
            "rows": [
                {
                    "key": f"q|{question['id']}|{index}",
                    "label": label or f"Вариант {index + 1}",
                    "n": counts[index],
                    "pct": _pct(counts[index], total),
                }
                for index, label in enumerate(options)
            ],
        })
    return {"asked": asked, "sessions": sessions, "groups": groups, "choices": choices}


def _fingerprint(cuts: list[dict], nps: dict) -> str:
    keys = [row["key"] for cut in cuts for row in cut["rows"]]
    keys += [f"f|{cut['id']}|{index}" for cut in cuts for index in range(len(cut["footer"]))]
    keys += [row["key"] for row in nps["sessions"]["rows"]]
    keys += [f"note|{group['id']}|{bool(group['note'])}" for group in nps["groups"]]
    keys += [row["key"] for group in nps["groups"] for row in group["rows"]]
    keys += [f"note|q{choice['id']}|{bool(choice['note'])}" for choice in nps["choices"]]
    keys += [row["key"] for choice in nps["choices"] for row in choice["rows"]]
    return "\n".join(keys)


def forum_portraits(conn, scope: Scope) -> dict:
    """Портрет одобренных и дошедших плюс оценки форума. Без ПД."""
    approved, arrived, outside, days = _load(conn, scope)
    cuts = _cuts(conn, approved, arrived)
    nps = _nps(conn, scope)
    outside_text = ""
    if outside:
        word = _words(outside, "отметка входа", "отметки входа", "отметок входа")
        outside_text = f"Вне этого среза ещё {outside} {word}."
    return {
        "approved": len(approved),
        "arrived": len(arrived),
        "outside": outside,
        "days_text": _days_text(days),
        "outside_text": outside_text,
        "cuts": cuts,
        "nps": nps,
        "fingerprint": _fingerprint(cuts, nps),
    }
