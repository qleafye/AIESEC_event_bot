"""Форум-ночь п.4 (расписание форума в боте, FORUM-CHECKIN.md D-18..D-20/D-24) — бизнес-правила
поверх сырого CRUD `database.db.program_halls`/`program_sessions`: гибкий разбор ввода времени
и дня, человекочитаемое предупреждение о занятости зала, слоты параллельных сессий и
копирование программы одного дня между городами.

aiogram-free (тот же инвариант, что `services/reject_rules.py`/`services/checkin.py`) —
импортирует только `database.db` и стандартную библиотеку; `services.timeutil.msk_now`
подтягивается лениво внутри функции (не на уровне модуля), чтобы не завести цикл с модулями,
которые сами читают время форума на импорте.

`point_for_session` — единственное, что этот план готовит для БУДУЩЕЙ отметки на сессиях
(D-18: обязательная отметка, D-20: «последний скан слота засчитывается» — тот же слот, что
строит `group_parallel` ниже). Саму отметку эта задача не делает.
"""
from __future__ import annotations

import re
from datetime import date, datetime, timedelta

from database.db import (
    create_program_hall,
    create_program_session,
    get_program_hall,
    get_program_session,
    list_program_halls,
    list_program_sessions_for_city_day,
    sessions_overlapping_hall,
)

_TIME_TOKEN_RE = re.compile(r"\d{1,2}(?:[:.]\d{2})?")


def _parse_one_time(token: str) -> str | None:
    token = token.replace(".", ":")
    hh, _sep, mm = token.partition(":")
    if not mm:
        mm = "00"
    if not hh.isdigit() or not mm.isdigit():
        return None
    hh_i, mm_i = int(hh), int(mm)
    if not (0 <= hh_i <= 23) or not (0 <= mm_i <= 59):
        return None
    return f"{hh_i:02d}:{mm_i:02d}"


def parse_time_range(raw: str) -> tuple[str, str] | None:
    """«10:00-11:30» / «10.00 11.30» / «10–11:30» -> `("10:00", "11:30")`. Разбор ищет РОВНО
    два «времени-подобных» куска (`\\d{1,2}` или `\\d{1,2}[:.]\\d{2}`) в строке, независимо от
    разделителя между и внутри них — все три формата подсказки проходят одним путём, без трёх
    отдельных веток парсинга. `None` — не разобралось ИЛИ конец не позже начала (валидация
    CLAUDE.md «конец позже начала»; текст ошибки — забота вызывающего хендлера)."""
    if not raw:
        return None
    tokens = _TIME_TOKEN_RE.findall(raw.strip())
    if len(tokens) != 2:
        return None
    start = _parse_one_time(tokens[0])
    end = _parse_one_time(tokens[1])
    if start is None or end is None:
        return None
    if end <= start:
        return None
    return start, end


def format_time_range(start_time: str, end_time: str) -> str:
    return f"{start_time}–{end_time}"


def _today() -> date:
    from services.timeutil import msk_now  # ленивый импорт — см. докстринг модуля
    return msk_now().date()


def parse_day_input(raw: str, *, reference: date | None = None) -> str | None:
    """«31.10» / «31.10.2026» / «2026-10-31» -> `"2026-10-31"`. Без года — год берётся у
    `reference` (обычно дата форума этого города), иначе у текущего момента."""
    if not raw:
        return None
    raw = raw.strip()
    for fmt in ("%d.%m.%Y", "%Y-%m-%d"):
        try:
            return datetime.strptime(raw, fmt).strftime("%Y-%m-%d")
        except ValueError:
            pass
    try:
        parsed = datetime.strptime(raw, "%d.%m")
    except ValueError:
        return None
    year = (reference or _today()).year
    try:
        return date(year, parsed.month, parsed.day).isoformat()
    except ValueError:
        return None


def day_label(day_iso: str) -> str:
    """`"2026-10-31"` -> `"31.10.2026"` — человеческий формат для кнопок/заголовков; вход, не
    прошедший разбор (не должен случаться — значение уже наше собственное ISO), возвращается
    как есть, а не роняет экран."""
    try:
        return datetime.strptime(day_iso, "%Y-%m-%d").strftime("%d.%m.%Y")
    except (TypeError, ValueError):
        return day_iso


def suggested_days(forum_date: datetime | None) -> list[str]:
    """Дата форума города + 1 + 2 дня (регион — один день, Москва — два подряд) — подсказка
    кнопок дня на экране программы, ДО того как менеджер завёл хоть одну сессию."""
    if forum_date is None:
        return []
    base = forum_date.date()
    return [(base + timedelta(days=n)).isoformat() for n in (0, 1, 2)]


async def hall_conflict_warning(
    city: str, day: str, hall_id: int | None, start_time: str, end_time: str, *,
    exclude_id: int | None = None,
) -> str | None:
    """Человеческое предупреждение («В зале «Большой» в это время уже «X» 10:00–11:00») —
    `None`, если зал не выбран вовсе («без зала» не может конфликтовать) или конфликта нет."""
    if hall_id is None:
        return None
    overlapping = await sessions_overlapping_hall(
        city, day, hall_id, start_time, end_time, exclude_id=exclude_id,
    )
    if not overlapping:
        return None
    hall = await get_program_hall(hall_id)
    hall_name = hall["name"] if hall else "?"
    other = overlapping[0]
    return (
        f"В зале «{hall_name}» в это время уже «{other['title']}» "
        f"{format_time_range(other['start_time'], other['end_time'])}."
    )


async def sessions_for_city_day(city: str, day: str) -> list[dict]:
    """Сессии дня с подмешанным `hall_name` (`None` — зал не выбран или уже удалён) —
    вызывающему (админский/делегатский экран) не нужно самому джойнить `program_halls`."""
    rows = await list_program_sessions_for_city_day(city, day)
    halls_cache: dict[int, dict | None] = {}
    result = []
    for row in rows:
        hall_id = row.get("hall_id")
        hall_name = None
        if hall_id is not None:
            if hall_id not in halls_cache:
                halls_cache[hall_id] = await get_program_hall(hall_id)
            hall = halls_cache[hall_id]
            hall_name = hall["name"] if hall else None
        result.append({**row, "hall_name": hall_name})
    return result


async def sessions_now(city: str, at: datetime) -> list[dict]:
    """Сессии, идущие ПРЯМО СЕЙЧАС (`start_time <= HH:MM < end_time`) — строковое сравнение
    совпадает с временны́м, оба конца в формате `HH:MM`."""
    day = at.strftime("%Y-%m-%d")
    hhmm = at.strftime("%H:%M")
    sessions = await sessions_for_city_day(city, day)
    return [s for s in sessions if s["start_time"] <= hhmm < s["end_time"]]


def group_parallel(sessions: list[dict]) -> list[list[dict]]:
    """Слоты дня (D-20 «последний скан слота засчитывается» — будущему чек-ину нужна ровно эта
    группировка): сессии, пересекающиеся по времени ТРАНЗИТИВНО (A∩B и B∩C -> один слот, даже
    если A и C напрямую не пересекаются), схлопываются в одну группу. `sessions` — сессии
    ОДНОГО дня (как отдаёт `sessions_for_city_day`), порядок входа не важен — сортируются
    заново."""
    ordered = sorted(sessions, key=lambda s: (s["start_time"], s["end_time"], s.get("id") or 0))
    groups: list[list[dict]] = []
    current: list[dict] = []
    current_end: str | None = None
    for s in ordered:
        if current and s["start_time"] < current_end:
            current.append(s)
            current_end = max(current_end, s["end_time"])
        else:
            if current:
                groups.append(current)
            current = [s]
            current_end = s["end_time"]
    if current:
        groups.append(current)
    return groups


def parallel_group(session: dict, day_sessions: list[dict]) -> list[dict]:
    """Слот, содержащий `session` (включая её саму), отсортированный по времени/id. `session`
    не найдена среди `day_sessions` (гонка правки/удаления между чтением списка и вызовом) ->
    `[session]` — сама по себе, без соседей, вместо падения."""
    for group in group_parallel(day_sessions):
        if any(s["id"] == session["id"] for s in group):
            return group
    return [session]


def point_for_session(session_id: int) -> str:
    """Точка отметки будущего чек-ина на сессиях (FORUM-CHECKIN.md D-18) — API готово заранее,
    саму отметку этот план не делает."""
    return f"session:{session_id}"


async def copy_program_day(from_city: str, to_city: str, day: str) -> dict:
    """Копирует залы (по имени — существующий зал с тем же именем в `to_city` переиспользуется,
    иначе заводится новый) и сессии ОДНОГО дня `day` из `from_city` в `to_city`, СОХРАНЯЯ саму
    строку дня как есть (форумы регионов идут в один и тот же день — «СПб→Тюмень, один формат
    форума», плана не о переносе даты). Существующие сессии `to_city`/`day` не трогаются и не
    удаляются — копия ДОБАВЛЯЕТ, а не заменяет (экран подтверждения называет это явно, права
    менеджера на оба города проверяет ВЫЗЫВАЮЩИЙ, до этого вызова — эта функция сама с
    `settings_ops.per_city_visible_codes` не сверяется, тем же разделением, что db.py/services
    в остальном проекте)."""
    source_sessions = await list_program_sessions_for_city_day(from_city, day)
    dest_halls = {h["name"]: h["id"] for h in await list_program_halls(to_city)}
    hall_map: dict[int, int | None] = {}
    halls_created = 0
    sessions_created = 0
    for session in source_sessions:
        hall_id = session.get("hall_id")
        new_hall_id = None
        if hall_id is not None:
            if hall_id not in hall_map:
                src_hall = await get_program_hall(hall_id)
                name = src_hall["name"] if src_hall else None
                if name is None:
                    hall_map[hall_id] = None
                elif name in dest_halls:
                    hall_map[hall_id] = dest_halls[name]
                else:
                    created_id = await create_program_hall(
                        to_city, name, src_hall.get("capacity") if src_hall else None,
                    )
                    dest_halls[name] = created_id
                    hall_map[hall_id] = created_id
                    halls_created += 1
            new_hall_id = hall_map[hall_id]
        await create_program_session(
            to_city, day, session["start_time"], session["end_time"], session["title"],
            speaker=session.get("speaker"), hall_id=new_hall_id,
            description=session.get("description"),
        )
        sessions_created += 1
    return {"halls_created": halls_created, "sessions_created": sessions_created}
