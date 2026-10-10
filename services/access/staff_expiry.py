"""Права со сроком действия — общая механика (`.planning/IDEAS-CHECKIN-BACKLOG-260924.md`
идея №6), плюс срок прав волонтёра из ссылки-приглашения (идея №5, `handlers/
admin_volunteer_invite.py`). Единая точка:

- разбора даты «ДД.ММ.ГГГГ» -> ISO «YYYY-MM-DD» (то, что хранится в `staff.expires_at`/
  `volunteer_invites.link_expires_at`/`volunteer_invites.rights_expires_at`) — тот же реальный
  `strptime`, что `domain/settings/validation.py` использует для типа `date_only` (31.02 отбрасывается
  так же честно, как «abc»), не переиспользован напрямую: та функция завязана на ключ реестра
  (`SETTINGS_SCHEMA[key]`), а эти значения в реестре не лежат;
- «до конца форума города» — `forum_date` + `sos_active_days` (per_city, `domain/settings/schema.py`)
  дают последний день форума (та же формула, что `services.sos.is_sos_active_for_city`:
  `[start, start + days - 1]`), плюс ОДИН день (D-6: «последний день форума + 1, если даты
  заданы») — чтобы разбор после закрытия последней сессии не упёрся в уже истёкшее право;
- проверки «действует ли срок ещё сегодня» — используется бот-слоем (`database/db.py`) и
  веб-слоем (`dashboard/access.py`, у него СВОЯ копия сравнения строк ISO-дат без импорта этого
  модуля — `dashboard/*.py` не имеет права тянуть `services.*`, см. докстринг
  `dashboard/access.py`).

aiogram-free (голая stdlib + `cities`/`services.infra.timeutil`), тот же класс модуля, что
`services/sos.py`/`services/reject_rules.py`."""
from __future__ import annotations

from datetime import datetime, timedelta

from domain.cities import get_setting_typed_for_city
from services.infra.timeutil import msk_now

DEFAULT_ACTIVE_DAYS = 2  # тот же дефолт, что services.sos.DEFAULT_ACTIVE_DAYS


def today_iso() -> str:
    return msk_now().date().isoformat()


def is_expiry_active(expires_at: str | None, today: str | None = None) -> bool:
    """NULL/пусто -> бессрочно. Иначе право действует ПО этот день включительно."""
    if not expires_at:
        return True
    return expires_at >= (today or today_iso())


def parse_ddmmyyyy(raw: str) -> str | None:
    """«04.10.2026» -> «2026-10-04»; невалидная строка -> None."""
    try:
        return datetime.strptime((raw or "").strip(), "%d.%m.%Y").date().isoformat()
    except ValueError:
        return None


def format_ddmmyyyy(iso_date: str | None) -> str | None:
    if not iso_date:
        return None
    try:
        return datetime.strptime(iso_date, "%Y-%m-%d").strftime("%d.%m.%Y")
    except ValueError:
        return None


async def forum_end_date_iso(city: str | None) -> str | None:
    """«Последний день форума этого города + 1» — ISO. Даты не заданы/не парсятся -> None
    (fail-soft: вызывающий экран прячет кнопку — CLAUDE.md, не предлагать то, что заведомо
    откажет)."""
    from services.reject_rules import forum_date_for  # ленивый импорт, разрыв цикла (тот же
    # приём, что уже задокументирован в services/sos.py::is_sos_active_for_city).

    raw = await forum_date_for(city)
    if not raw:
        return None
    try:
        start = datetime.strptime(raw.strip(), "%d.%m.%Y").date()
    except ValueError:
        return None
    raw_days = await get_setting_typed_for_city("sos_active_days", city)
    try:
        days = int(raw_days)
    except (TypeError, ValueError):
        days = DEFAULT_ACTIVE_DAYS
    if days < 1:
        days = 1
    last_day = start + timedelta(days=days - 1)
    return (last_day + timedelta(days=1)).isoformat()


def relative_days_iso(days: int) -> str:
    """«Сегодня + N дней» — ISO. Используется пресетами ссылки-приглашения (3/7 дней)."""
    return (msk_now().date() + timedelta(days=days)).isoformat()
