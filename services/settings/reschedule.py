"""Перепланировка джоб после правки настроек — вызывается из `settings_audit.run_setting_hooks`.

Раньше жила в `handlers/settings/admin_settings.py` и срабатывала только на правку из бота: дата форума,
тумблеры и время отчёта дня / опроса неявившихся / шпаргалки волонтёрам, QR-рассылка и отзывы о
сессиях, изменённые из Mini App, оставались на старой дате до рестарта. Теперь это хук воронки
записи, которую зовут и бот, и разборщик очереди `settings_changed` — в процессе бота, где есть
планировщик. Каждая перепланировка в своём try: сбой не роняет запись настройки."""
from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

# (имя для лога, ключи-триггеры, модуль сервиса, функция полной сверки, функция одного города)
_SPECS = (
    ("checkin_qr", frozenset({"forum_date"}), "services.checkin_broadcast",
     "reconcile_broadcasts", "schedule_city_jobs"),
    ("volunteer_guide", frozenset({
        "forum_date", "checkin_volunteer_guide_broadcast_enabled",
        "checkin_volunteer_guide_broadcast_time", "checkin_volunteer_guide_text",
    }), "services.checkin_volunteer_broadcast", "reconcile", "schedule_city_job"),
    ("forum_day_report", frozenset({
        "forum_date", "forum_day_report_enabled", "forum_day_report_time",
    }), "services.forum_day_report", "reconcile", "schedule_city_job"),
    ("forum_noshow_poll", frozenset({
        "forum_date", "forum_noshow_poll_enabled", "forum_noshow_poll_time",
    }), "services.forum_noshow_poll", "reconcile", "schedule_city_job"),
    # Предложение «в Москву» зависит и от своих двух ключей, и от опроса неявившихся
    # (уходит не раньше чем через 3 часа после него), и от даты форума города.
    ("regional_noshow_move", frozenset({
        "forum_date", "regional_noshow_offer_enabled", "regional_noshow_offer_time",
        "forum_noshow_poll_enabled", "forum_noshow_poll_time",
    }), "services.regional_noshow_move", "reconcile", "schedule_city_job"),
)


def _plan(keys) -> list[tuple[str, str, str | None]]:
    """Что переставить для пачки ключей: [(имя джобы, модуль/«feedback», город или None)].
    Одна сверка на модуль: «все города» (голый ключ) покрывает городские сверки того же
    модуля, а повторы одного города схлопываются."""
    from domain.cities import PER_CITY_SEP, split_per_city_key
    from domain.settings.ops import base_setting_key

    whole: set[str] = set()
    per_city: dict[str, set[str | None]] = {}
    order: list[str] = []
    for key in dict.fromkeys(keys):
        base = base_setting_key(key)
        targets = [name for name, trig, *_ in _SPECS if base in trig]
        if base == "session_feedback_delay_minutes":
            targets.append("session_feedback")
        for name in targets:
            if name not in order:
                order.append(name)
            if PER_CITY_SEP in key:
                parsed = split_per_city_key(key)
                per_city.setdefault(name, set()).add(parsed[1] if parsed is not None else None)
            else:
                whole.add(name)
    plan: list[tuple[str, str | None, bool]] = []
    for name in order:
        if name in whole:
            plan.append((name, None, True))
        else:
            plan.extend((name, city, False) for city in sorted(per_city.get(name, ()), key=lambda c: c or ""))
    return plan


async def reschedule_for_settings(keys) -> None:
    """Переставляет джобы, зависящие от пачки ключей, — по одному разу на модуль и город,
    а не по ключу. Композитный ключ трогает только свой город, голый — сверка по всем."""
    import importlib

    specs = {name: (module, reconcile_all, schedule_city) for name, _keys, module, reconcile_all, schedule_city in _SPECS}
    for name, city, whole in _plan(keys):
        try:
            if name == "session_feedback":
                from services import session_feedback as sf

                if whole:
                    await sf.reconcile_all()
                elif city is not None:
                    await sf.reconcile_city(city)
                continue
            module, reconcile_all, schedule_city = specs[name]
            mod = importlib.import_module(module)
            if whole:
                await getattr(mod, reconcile_all)()
            else:
                await getattr(mod, schedule_city)(city)
        except Exception as exc:  # noqa: BLE001 — перепланировка не роняет запись настройки
            logger.error("settings_reschedule: %s (город %r) сорвалась: %s", name, city, exc)


async def reschedule_for_setting(key: str) -> None:
    """Переставляет джобы, зависящие от ключа `key` (глобального или `{base}__city__{code}`)."""
    await reschedule_for_settings([key])
