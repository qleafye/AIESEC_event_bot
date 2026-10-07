"""Форум-ночь п.4 (расписание форума в боте, FORUM-CHECKIN.md D-18..D-20/D-24) — бизнес-правила
поверх сырого CRUD `database.db.program_halls`/`program_sessions`: гибкий разбор ввода времени
и дня, человекочитаемое предупреждение о занятости зала, слоты параллельных сессий и
копирование программы одного дня между городами.

aiogram-free (тот же инвариант, что `services/reject_rules.py`/`services/checkin.py`) —
импортирует только `database.db`/`cities`/стандартную библиотеку (тот же набор, что уже тянет
`services/reject_rules.py` — прецедент, что этот класс модулей вправе импортировать `cities`,
не только `database.db`); `services.timeutil.msk_now` подтягивается лениво внутри функции (не
на уровне модуля), чтобы не завести цикл с модулями, которые сами читают время форума на
импорте.

`point_for_session` — единственное, что этот план готовит для БУДУЩЕЙ отметки на сессиях
(D-18: обязательная отметка, D-20: «последний скан слота засчитывается» — тот же слот, что
строит `group_parallel` ниже). Саму отметку эта задача не делает.

D-29 (FORUM-CHECKIN.md, «Решения владельца 24.09»): `resolve_program_photo`/`resolve_program_view`/
`has_program_content`/`build_delegate_program` — общая точка правды для ТРЁХ поверхностей
(чат-кнопка `handlers/user_actions.py::show_program`, гейт кнопки меню
`keyboards/builders.py::get_main_menu_kb`, Mini App `miniapp/routers/program.py`), чтобы «что
показываем» не разъехалось между ними. Фото программы — per_city СОСТАВНОЙ ключ через
`cities.per_city_key`, НЕ через реестровый `per_city: True`/`get_setting_for_city`
(`tests/test_settings_percity_resolver.py::test_no_per_city_key_is_photo_or_file_type`
запрещает per_city-флаг на photo/file записях реестра, D-10: медиа-ключи вне обычного
резолвера) — читается сырым `get_setting` в обход реестра, byte-в-byte идиома, что у
`checkinvol_toggle_go`/`_vol_cfg_text_kb` (per_city_key + прямое чтение/запись)."""
from __future__ import annotations

import re
from datetime import date, datetime, timedelta

from cities import cities_module_on, default_city_code, per_city_key
from database.db import (
    create_program_hall,
    create_program_session,
    get_program_hall,
    get_program_session,
    get_setting,
    has_program_sessions_for_city,
    list_program_days_for_city,
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


async def own_forum_date(city: str | None) -> datetime | None:
    """Дата форума ТОЛЬКО этого города (`services.reject_rules.forum_date_for`, без отката на
    общую): общая дата под шапкой «🌍 Все города» не делает форум у города без своей даты —
    иначе экран программы подсказывал Москве дни чужого регионального форума."""
    from services.reject_rules import forum_date_for

    raw = await forum_date_for(city)
    try:
        return datetime.strptime(raw.strip(), "%d.%m.%Y") if raw else None
    except ValueError:
        return None


def suggested_days(forum_date: datetime | None) -> list[str]:
    """Дата форума города + 1 + 2 дня (регион — один день, Москва — два подряд) — подсказка
    кнопок дня на экране программы, ДО того как менеджер завёл хоть одну сессию."""
    if forum_date is None:
        return []
    base = forum_date.date()
    return [(base + timedelta(days=n)).isoformat() for n in (0, 1, 2)]


async def suggested_days_for_city(city: str | None) -> list[str]:
    """Подсказки дней на экране программы города — только дни его форума (дата города +
    «сколько дней идёт»). У однодневного форума СПб раньше висели ещё 02.10 и 03.10, и сессия на
    чужой день заводилась без предупреждения и показывалась делегатам."""
    from services.sos import sos_active_window

    window = await sos_active_window(city)
    if window is None:
        return []
    start, end = window
    return [(start + timedelta(days=n)).isoformat() for n in range((end - start).days + 1)]


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
    """Сессии дня с подмешанным `hall_name`/`hall_capacity` (`None` — зал не выбран или уже
    удалён, либо вместимость не задана) — вызывающему (админский/делегатский экран, счётчик
    отметок форум-ночи п.5) не нужно самому джойнить `program_halls`."""
    rows = await list_program_sessions_for_city_day(city, day)
    halls_cache: dict[int, dict | None] = {}
    result = []
    for row in rows:
        hall_id = row.get("hall_id")
        hall_name = None
        hall_capacity = None
        if hall_id is not None:
            if hall_id not in halls_cache:
                halls_cache[hall_id] = await get_program_hall(hall_id)
            hall = halls_cache[hall_id]
            if hall:
                hall_name = hall["name"]
                hall_capacity = hall.get("capacity")
        result.append({**row, "hall_name": hall_name, "hall_capacity": hall_capacity})
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
    """Точка отметки чек-ина на сессиях (FORUM-CHECKIN.md D-18) — та же строка, что хранит
    `checkins.point` (`database.db.record_session_checkin`)."""
    return f"session:{session_id}"


def session_point_label(session: dict, *, limit: int = 40) -> str:
    """Подпись кнопки точки отметки сессии: время + (· зал) + название, обрезанное до `limit`
    символов — единая точка форматирования и для сканера Mini App
    (`miniapp/routers/checkin.py`), и для точек в загрузке CSV
    (`handlers/admin_checkin.py`), чтобы подпись не разошлась в двух местах."""
    hall_part = f" · {session['hall_name']}" if session.get("hall_name") else ""
    time_part = format_time_range(session["start_time"], session["end_time"])
    title = (session.get("title") or "").strip()
    if len(title) > limit:
        title = title[: max(0, limit - 1)].rstrip() + "…"
    return f"{time_part}{hall_part} · {title}"


async def checkin_session_points(city: str, at: datetime | None = None) -> list[dict]:
    """Точки отметки на сессиях СЕГОДНЯ (D-18) для сканера/загрузки CSV: «идёт сейчас» —
    первыми, дальше по времени начала. Каждая точка — `{"point", "label", "live", "capacity"}`
    (счётчик уже отмеченных — забота вызывающего: `database.db.count_checkins_by_point`, этот
    модуль отметок не знает вовсе, только программу). Пустой список — на сегодня в городе нет
    ни одной сессии (или программы вообще нет) — вызывающий тогда предлагает только «Вход»."""
    if at is None:
        from services.timeutil import city_now  # ленивый импорт — см. докстринг модуля
        at = await city_now(city)  # время сессий — местное время города
    day = at.strftime("%Y-%m-%d")
    hhmm = at.strftime("%H:%M")
    sessions = await sessions_for_city_day(city, day)
    now_ids = {s["id"] for s in sessions if s["start_time"] <= hhmm < s["end_time"]}
    ordered = sorted(sessions, key=lambda s: (s["id"] not in now_ids, s["start_time"], s["id"]))
    return [
        {
            "point": point_for_session(s["id"]),
            "label": session_point_label(s),
            "live": s["id"] in now_ids,
            "capacity": s.get("hall_capacity"),
        }
        for s in ordered
    ]


def scanned_outside_session_window(session: dict, scanned_at: str | None, *, slack_minutes: int = 30) -> bool:
    """Форум-ночь п.5 (D-18..D-20): время скана из CSV-выгрузки лежит вне интервала сессии
    `[start - slack, end + slack]` того же дня — предупреждение в отчёте («вне времени сессии»,
    `handlers/admin_checkin.py`), НЕ запрет (отметка всё равно ставится — волонтёр мог
    отсканировать чуть раньше входа в зал или чуть позже начала). Пустой/нечитаемый `scanned_at`,
    или другой день — `False` (нечего сравнивать; D-10 уже отдельно помечает approx-время)."""
    if not scanned_at:
        return False
    try:
        dt = datetime.strptime(scanned_at[:19], "%Y-%m-%d %H:%M:%S")
    except ValueError:
        return False
    if dt.strftime("%Y-%m-%d") != session["day"]:
        return False
    try:
        start = datetime.strptime(
            f"{session['day']} {session['start_time']}", "%Y-%m-%d %H:%M",
        ) - timedelta(minutes=slack_minutes)
        end = datetime.strptime(
            f"{session['day']} {session['end_time']}", "%Y-%m-%d %H:%M",
        ) + timedelta(minutes=slack_minutes)
    except ValueError:
        return False
    return not (start <= dt <= end)


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


# ── D-29 (одна кнопка программы у делегата: фото ИЛИ таблица, по выбору админа) ─────────────

PROGRAM_PHOTO_KEY = "program_photo_file_id"
PROGRAM_VIEW_KEY = "program_miniapp_view"


async def resolve_program_photo(city: str | None) -> str | None:
    """`file_id` фото программы для города — своё (per_city составной ключ) ИЛИ общее
    (D-29: чат и Mini App читают один и тот же приоритет). `city=None` (модуль городов
    выключен/город делегата ещё не известен) сразу отдаёт общее значение — тот же контракт
    module-off collapse, что у `cities.get_setting_for_city`."""
    if city and await cities_module_on():
        composed = per_city_key(PROGRAM_PHOTO_KEY, city)
        if composed:
            own = await get_setting(composed)
            if own:
                return own
    return await get_setting(PROGRAM_PHOTO_KEY)


async def resolve_program_view(city: str | None) -> str:
    """`"table"`/`"photo"` — per_city override -> общее -> дефолт по наличию сессий (D-29:
    таблица, если в программе города есть хоть одна сессия, иначе фото). Ключ читается СЫРЫМ
    (`get_setting`, не типизированным резолвером реестра) — иначе «не задано вовсе» неотличимо
    от «явно задано значение по умолчанию», а дефолт здесь зависит от данных, не от статичного
    `SETTINGS_SCHEMA[...]['default']`."""
    raw = None
    if city and await cities_module_on():
        composed = per_city_key(PROGRAM_VIEW_KEY, city)
        if composed:
            raw = await get_setting(composed)
    if raw not in ("table", "photo"):
        raw = await get_setting(PROGRAM_VIEW_KEY)
    if raw in ("table", "photo"):
        return raw
    resolved_city = city or default_city_code()
    return "table" if await has_program_sessions_for_city(resolved_city) else "photo"


# Диск-фоллбэк фото программы — тот же файл, что чат шлёт `FSInputFile` в
# `handlers/user_actions.py::show_program`, когда в настройках фото нет. Mini App отдаёт его
# своей публичной ручкой `GET /app/api/program/photo-default` (без file_id).
PROGRAM_DEFAULT_PHOTO_PATH = "resources/program.jpg"


def default_program_photo_path() -> str | None:
    """Путь к диск-фоллбэку, если файл есть, иначе `None`."""
    import os

    return PROGRAM_DEFAULT_PHOTO_PATH if os.path.isfile(PROGRAM_DEFAULT_PHOTO_PATH) else None


async def own_program_photo(city: str | None) -> str | None:
    """`file_id` фото, загруженного именно для этого города (составной per_city ключ), без
    отката на общее. Модуль городов выключен — городов нет, общее фото и есть «своё»."""
    if not await cities_module_on():
        return await get_setting(PROGRAM_PHOTO_KEY)
    composed = per_city_key(PROGRAM_PHOTO_KEY, city) if city else None
    return (await get_setting(composed) or None) if composed else None


async def program_photo_caption(city: str | None) -> str | None:
    """Подпись к фото программы, которое видит делегат города: у своего фото города — своя
    подпись (загружается вместе с ним, `handlers/admin_program_view.py`), у общего — общая."""
    if await cities_module_on() and city and await own_program_photo(city):
        composed = per_city_key("program_caption", city)
        return (await get_setting(composed)) if composed else None
    return await get_setting("program_caption")


async def resolve_program_photo_source(city: str | None) -> dict | None:
    """Какое фото программы видит делегат города: своё фото города (`{"file_id": …}`); общее
    фото из настроек или файл на диске (`{"path": …}`) — ТОЛЬКО если у города нет ни своего фото,
    ни сессий. Иначе загруженная одним городом общая картинка перекрывала бы программу сессиями
    других городов (раньше так и было: фото СПб видели Тюмень и Москва). `None` — фото нет."""
    own = await own_program_photo(city)
    if own:
        return {"file_id": own}
    if await has_program_sessions_for_city(city or default_city_code()):
        return None
    shared = await get_setting(PROGRAM_PHOTO_KEY)
    if shared:
        return {"file_id": shared}
    path = default_program_photo_path()
    if path:
        return {"path": path}
    return None


async def resolve_program_content(city: str | None) -> tuple[str | None, dict | None]:
    """ЕДИНСТВЕННЫЙ резолвер «что показать делегату»: `("photo", источник)`, `("table", None)`
    или `(None, None)` — показать нечего. Видимость кнопки/плитки (`has_program_content`) и
    содержимое ручки Mini App читают именно его, поэтому «кнопка есть, а внутри пусто» не
    бывает. Выбор менеджера (`resolve_program_view`) — предпочтение: если выбранного вида нет
    (выбрано фото, а заведены только сессии, или наоборот), показываем то, что есть, — как чат,
    который тоже переходит от фото к тексту сессий. Чат (`handlers/user_actions.py::show_program`)
    показывает ровно это же — одно правило для обеих поверхностей."""
    preferred = await resolve_program_view(city)
    photo = await resolve_program_photo_source(city)
    has_sessions = await has_program_sessions_for_city(city or default_city_code())
    for view in (preferred, "table" if preferred == "photo" else "photo"):
        if view == "photo" and photo:
            return "photo", photo
        if view == "table" and has_sessions:
            return "table", None
    return None, None


async def has_program_content(city: str | None) -> bool:
    """Гейт видимости кнопки/плитки «Программа» — есть ли что показать по
    `resolve_program_content` (фото из настроек, файл на диске или сессии)."""
    view, _source = await resolve_program_content(city)
    return view is not None


async def program_menu_visible(city: str | None) -> bool:
    """Видна ли делегату кнопка программы — тумблер `menu_program` (per_city) И есть что
    показать (`has_program_content`). Ровно та пара проверок, что делает
    `keyboards.builders.get_main_menu_kb` для `menu_program`; Mini App (раздел «📅 Программа»
    в `/app/api/me` и гейт `GET /app/api/program`) читает её отсюда, а не собирает заново."""
    from cities import get_setting_typed_for_city

    if await get_setting_typed_for_city("menu_program", city) != "on":
        return False
    return await has_program_content(city)


async def build_delegate_program(city: str | None, at: datetime | None = None) -> list[dict]:
    """Табличный вид программы (D-29 Mini App «красивая таблица»): по дню — слоты
    (`group_parallel`, транзитивное пересечение времени), в каждом слоте — сессии с полем
    `"now"` (сейчас идёт хотя бы одна сессия слота). У каждой сессии свои `now`/`next`: в
    параллельном слоте метку рисуют по сессии, не по слоту. Используется и API Mini App
    (`miniapp/routers/program.py`), и любым будущим текстовым видом в чате — вторая копия
    группировки/сортировки не заводится нигде.

    `city=None` резолвится в `default_city_code()` (та же однocity-фоллбэк идиома, что у
    `keyboards.builders.get_main_menu_kb`/`handlers.admin_program._resolve_city_for_screen`) —
    у сессий программы «нет города» не бывает, только конкретный код."""
    if at is None:
        from services.timeutil import city_now  # ленивый импорт — см. докстринг модуля
        at = await city_now(city or default_city_code())  # время сессий — местное время города

    resolved_city = city or default_city_code()
    today = at.strftime("%Y-%m-%d")
    now_hhmm = at.strftime("%H:%M")

    days = []
    for day in await list_program_days_for_city(resolved_city):
        sessions = await sessions_for_city_day(resolved_city, day)
        slots = []
        for group in group_parallel(sessions):
            is_now = day == today and any(s["start_time"] <= now_hhmm < s["end_time"] for s in group)
            slots.append({
                "start_time": group[0]["start_time"],
                "end_time": max(s["end_time"] for s in group),
                "now": is_now,
                "next": False,
                "sessions": [
                    {
                        "id": s["id"],
                        "title": s["title"],
                        "speaker": s.get("speaker"),
                        "hall_name": s.get("hall_name"),
                        "start_time": s["start_time"],
                        "end_time": s["end_time"],
                        # Приёмка 01.10: у параллельной сессии своя метка — слот 03:00–06:00
                        # «идёт» с 03:00, а его мастер-класс 04:30 ещё нет.
                        "now": day == today and s["start_time"] <= now_hhmm < s["end_time"],
                        "next": False,
                    }
                    for s in group
                ],
            })
        days.append({"day": day, "label": day_label(day), "slots": slots})

    # D-29 «идёт сейчас / следующая»: ровно один слот во всей программе помечен «next» —
    # ближайший будущий слот сегодня или в один из следующих дней, и только пока ни один слот
    # не «now» (сейчас идёт хоть что-то — «следующая» не нужна, слот и так виден как текущий).
    if not any(slot["now"] for day_entry in days for slot in day_entry["slots"]):
        next_slot = next(
            (
                slot
                for day_entry in days if day_entry["day"] >= today
                for slot in day_entry["slots"]
                if day_entry["day"] > today or slot["start_time"] > now_hhmm
            ),
            None,
        )
        if next_slot is not None:
            next_slot["next"] = True
            for session in next_slot["sessions"]:
                session["next"] = session["start_time"] == next_slot["start_time"]
        return days

    # Внутри идущего параллельного слота «следующая» — ближайшая ещё не начавшаяся сессия
    # (слот целиком помечен «now», но отдельная сессия в нём может стартовать позже).
    for day_entry in days:
        for slot in day_entry["slots"]:
            if not slot["now"]:
                continue
            upcoming = [x["start_time"] for x in slot["sessions"] if x["start_time"] > now_hhmm]
            if upcoming:
                first = min(upcoming)
                for session in slot["sessions"]:
                    session["next"] = session["start_time"] == first
    return days
