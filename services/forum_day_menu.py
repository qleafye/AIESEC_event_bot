"""Идея №1 бэклога чек-ина (`.planning/IDEAS-CHECKIN-BACKLOG-260924.md`): режим «день форума»
главного меню делегата (per_city) — на время форума города `keyboards/builders.py::
get_main_menu_kb` поднимает «🎟 Мой QR»/«📅 Программа»/«❗ Важное»/«🆘 SOS» наверх (каждая — только
если её СОБСТВЕННЫЙ гейт и так её показывает), остальное сдвигается вниз, не прячется.

Окно: с вечера накануне `forum_date` (per_city, `domain/settings/schema.py`) до конца последнего дня
форума. Отдельного ключа «дата окончания форума» в реестре нет — переиспользуем то же
`sos_active_days` (per_city), которым уже считает длительность форума `services.sos.
is_sos_active_for_city` (файл НЕ трогаем, только импортируем публичную константу и читаем тот
же ключ реестра byte-в-byte той же логикой, чтобы окно меню не разошлось с окном SOS).

Мастер-тумблер `forum_day_menu_enabled` (per_city, дефолт "off") — фича не включается сама на
существующих городах без явного решения менеджера. Время начала «вечером накануне» —
`forum_day_menu_start_time` (per_city, формат ЧЧ:ММ, дефолт 18:00).

Fail-soft на каждом шаге (нет даты форума / кривая дата / кривое время / сбой чтения
настройки) -> False — обычное меню, тот же баланс, что у `services.sos.is_sos_active_for_city`
(лучше показать привычное меню, чем сломать его)."""
from __future__ import annotations

import logging
from datetime import date, datetime, timedelta

from cities import get_setting_typed_for_city
from services.timeutil import msk_now

logger = logging.getLogger(__name__)

DEFAULT_START_TIME = "18:00"


async def _forum_window_dates(city: str | None) -> tuple[date, date] | None:
    """`(дата начала, дата окончания)` форума этого города — `None`, если `forum_date` не
    задана или не парсится. Дата окончания = `forum_date + sos_active_days - 1`, тот же расчёт
    длительности, что `services.sos.is_sos_active_for_city` (импорт константы фолбэка оттуда,
    сам ключ реестра — общий `sos_active_days`, отдельного ключа для меню не заводим)."""
    from services.reject_rules import forum_date_for  # ленивый импорт — тот же цикл-разрыв,
    # что уже документирован в services/sos.py/services/checkin_broadcast.py.

    raw = await forum_date_for(city)
    if not raw:
        return None
    try:
        start = datetime.strptime(raw.strip(), "%d.%m.%Y").date()
    except ValueError:
        return None

    from services.sos import DEFAULT_ACTIVE_DAYS  # публичная константа, sos.py не правим

    raw_days = await get_setting_typed_for_city("sos_active_days", city)
    try:
        days = int(raw_days)
    except (TypeError, ValueError):
        days = DEFAULT_ACTIVE_DAYS
    if days < 1:
        days = 1
    return start, start + timedelta(days=days - 1)


def _parse_hhmm(raw: str | None) -> tuple[int, int]:
    """«ЧЧ:ММ» -> `(час, минута)`; любая кривизна (пусто/не то число полей/не int/вне
    диапазона) -> `DEFAULT_START_TIME` (18:00) — та же терпимость, что у остальных
    `format: "time"` полей реестра (domain/settings/validation.py уже не пускает кривое значение
    В БД, это дополнительный фолбэк на случай стороннего значения)."""
    text = (raw or DEFAULT_START_TIME).strip()
    try:
        hh_s, mm_s = text.split(":", 1)
        hh, mm = int(hh_s), int(mm_s)
        if not (0 <= hh <= 23 and 0 <= mm <= 59):
            raise ValueError
        return hh, mm
    except (ValueError, TypeError):
        hh_s, mm_s = DEFAULT_START_TIME.split(":")
        return int(hh_s), int(mm_s)


async def is_forum_day_menu_active_for_city(city: str | None) -> bool:
    """Тумблер `forum_day_menu_enabled` выключен ИЛИ дата форума не задана/не парсится ИЛИ
    сейчас вне окна `[вечер накануне forum_date, конец форума]` -> `False` (обычное меню)."""
    try:
        enabled = await get_setting_typed_for_city("forum_day_menu_enabled", city) == "on"
        if not enabled:
            return False

        window = await _forum_window_dates(city)
        if window is None:
            return False
        start, end = window

        start_time_raw = await get_setting_typed_for_city("forum_day_menu_start_time", city)
        hh, mm = _parse_hhmm(start_time_raw)

        window_start = datetime.combine(start - timedelta(days=1), datetime.min.time()).replace(
            hour=hh, minute=mm
        )
        window_end = datetime.combine(end, datetime.max.time())
        return window_start <= msk_now() <= window_end
    except Exception as e:  # noqa: BLE001 — намеренно широкий fail-soft, см. докстринг модуля
        logger.error("forum_day_menu.is_forum_day_menu_active_for_city(%r): %s", city, e)
        return False
