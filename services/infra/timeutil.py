"""Quick 260904-kk6 (Q1): leaf-модуль часового пояса — единственный (кроме `miniapp/timeutil.py`,
см. её докстринг) файл `services/*.py`/`handlers/*.py`, где назван часовой пояс Europe/Moscow.

Переезд из `services/scheduler.py` (TZFIX-260816): `services/questions.py::format_stamp`
обязан переводить UTC-метки в МСК на отображении, но импортировать `services.scheduler` не
может — тот модуль тянет aiogram + APScheduler, а `services/questions.py` импортируется из
`miniapp/routers/questions.py`, где ребра `miniapp -> aiogram` нет и не будет (D-01,
докстринг `miniapp/timeutil.py`). Свой второй литерал в `questions.py` тоже нельзя: сторож
`tests/test_timezone_fix_260816.py::test_moscow_literal_declared_exactly_once` требует ровно
одно вхождение строкового литерала этого пояса во всём `services/*.py` + `handlers/*.py`.
Поэтому литерал переезжает сюда — в leaf-модуль на голой stdlib (ни одного импорта проекта), а
`services/scheduler.py` реэкспортирует `MOSCOW_TZ` отсюда: все существующие
`from services.scheduler import MOSCOW_TZ` (game_digest.py, tests/test_scheduler_restart_260816.py,
tests/test_timezone_fix_260816.py, tests/test_polls_260822.py) продолжают работать без правок.

`miniapp/timeutil.py` СОХРАНЯЕТ свою собственную копию `MOSCOW_TZ` — её докстринг уже
объясняет, почему это третий (и с `miniapp/routers/admin_tasks.py` — четвёртый) осознанный
литерал, а не второй случайно разошедшийся. Объединение leaf-модулей `services/infra/timeutil.py`
и `miniapp/timeutil.py` в один — отдельный тикет, не этот квик.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

MOSCOW_TZ = ZoneInfo("Europe/Moscow")


def utc_naive_to_msk(dt: datetime) -> datetime:
    """Naive UTC datetime -> naive московский datetime.

    Трактует `dt` как UTC (`.replace(tzinfo=timezone.utc)`, не `.astimezone` — `dt` не несёт
    своей зоны), переводит в `MOSCOW_TZ` и снова снимает tzinfo: весь проект оперирует
    naive-временем (naive сравнивается с naive, `datetime.utcnow()`/`datetime.now()` тоже
    naive) — возвращать aware-объект значило бы завести второй, несовместимый со всем
    остальным кодом, тип метки времени.
    """
    return dt.replace(tzinfo=timezone.utc).astimezone(MOSCOW_TZ).replace(tzinfo=None)


def msk_from_timestamp(ts: float) -> datetime:
    """Unix-эпоха (секунды) -> naive московский datetime, НЕЗАВИСИМО от часового пояса процесса.

    Голый `datetime.fromtimestamp(ts)` берёт зону процесса: в UTC-контейнере бота метка
    выходит на 3 часа раньше московской (выгрузка офлайн-сканера, `services/forum/checkin.py`).
    """
    return datetime.fromtimestamp(ts, tz=MOSCOW_TZ).replace(tzinfo=None)


def aware_to_msk(dt: datetime) -> datetime:
    """Aware datetime (ISO с `Z`/смещением) -> naive московский datetime. Naive `dt`
    возвращается как есть — его зона неизвестна, трактовать её молча как UTC нельзя."""
    if dt.tzinfo is None:
        return dt
    return dt.astimezone(MOSCOW_TZ).replace(tzinfo=None)


def msk_now() -> datetime:
    """Naive московское «сейчас» — ЕДИНСТВЕННЫЙ источник для всей семьи меток времени,

    которые бот пишет и по которым сам же бьёт сутки (карточка заявки, профиль Mini App,
    строка листа, дашборд, фильтр рассылки по дате). Naive — весь проект сравнивает naive
    с naive, как и `utc_naive_to_msk` выше. Контейнер бота живёт в UTC (`TZ` в
    `docker-compose.yml` намеренно не задан — TZFIX-260816), поэтому голый `datetime.now()`
    в местах, которые печатают время человеку, запрещён: он отстаёт от Москвы на 3 часа
    (квик 260912-mcj).
    """
    return datetime.now(MOSCOW_TZ).replace(tzinfo=None)


def process_clock_is_utc() -> bool:
    """True, если часы процесса (`datetime.now()`) совпадают с UTC (`datetime.utcnow()`)

    с точностью до минуты. Используется РОВНО один раз — гейтом одноразовой миграции старых
    строк семьи (`database/db.py::_migrate_local_timestamps_to_msk`, квик 260912-mcj):
    «часы процесса = UTC» означает «все прежние строки этой семьи писал UTC-контейнер
    (прод/стенд), их нужно сдвинуть на +3 часа». Вынесена в отдельную функцию, а не инлайн-
    выражение внутри миграции, ровно чтобы тесты могли её замокать независимо от реальных
    часов машины, на которой запускаются.
    """
    return abs((datetime.now() - datetime.utcnow()).total_seconds()) < 60


# ── Часовой пояс города (смещение от Москвы, настройка `city_tz_offset`) ─────────────────────
# Метки в БД остаются московскими. Смещение нужно в двух случаях: сравнить «сейчас» со временем,
# которое менеджер города ввёл по-местному (`city_now`), и показать человеку московскую метку из
# БД в местном времени (`to_city_time`). Чистые функции ниже без БД; чтение настройки — в
# `city_offset_hours` (импорт `cities` внутри функции: файл остаётся листом для всех, кто
# импортирует его на верхнем уровне, в том числе из миграций и miniapp).

TZ_OFFSET_MIN = -1
TZ_OFFSET_MAX = 9


def clamp_offset(value) -> int:
    """Сырое значение настройки -> целое смещение в [-1; 9]; мусор/пусто -> 0 (МСК)."""
    try:
        n = int(str(value).strip())
    except (TypeError, ValueError):
        return 0
    return n if TZ_OFFSET_MIN <= n <= TZ_OFFSET_MAX else 0


def shift_hours(dt: datetime, hours: int) -> datetime:
    return dt + timedelta(hours=hours) if hours else dt


def offset_label(hours: int) -> str:
    """0 -> «МСК», 2 -> «МСК+2», -1 -> «МСК−1» (та же подпись, что у кнопок выбора)."""
    if hours == 0:
        return "МСК"
    return f"МСК+{hours}" if hours > 0 else f"МСК−{-hours}"


async def city_offset_hours(city: str | None) -> int:
    """Смещение города от Москвы в часах. Город неизвестен/настройка не задана/сбой чтения -> 0,
    то есть поведение «как раньше» (fail-soft: время не должно ронять экран и джобу)."""
    if not city:
        return 0
    try:
        from domain.cities import get_setting_typed_for_city
        return clamp_offset(await get_setting_typed_for_city("city_tz_offset", city))
    except Exception:
        return 0


async def city_now(city: str | None) -> datetime:
    """Naive местное «сейчас» города = `msk_now()` + смещение. Сравнивать с временем, которое
    менеджер города ввёл по-местному (сессии программы, окна, расписания)."""
    return shift_hours(msk_now(), await city_offset_hours(city))


async def to_city_time(dt_msk: datetime, city: str | None) -> datetime:
    """Московскую метку из БД -> местное время города (для показа человеку)."""
    return shift_hours(dt_msk, await city_offset_hours(city))
