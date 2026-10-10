"""Перепланировка джоб после правки настроек — вызывается из `settings_audit.run_setting_hooks`.

Раньше жила в `handlers/admin_settings.py` и срабатывала только на правку из бота: дата форума,
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


async def reschedule_for_setting(key: str) -> None:
    """Переставляет джобы, зависящие от ключа `key` (глобального или `{base}__city__{code}`).
    Композитный ключ трогает только свой город, голый — сверка по всем городам."""
    from cities import PER_CITY_SEP, split_per_city_key
    from settings_ops import base_setting_key

    base = base_setting_key(key)
    for name, keys, module, reconcile_all, schedule_city in _SPECS:
        if base not in keys:
            continue
        try:
            import importlib

            mod = importlib.import_module(module)
            if PER_CITY_SEP in key:
                parsed = split_per_city_key(key)
                await getattr(mod, schedule_city)(parsed[1] if parsed is not None else None)
            else:
                await getattr(mod, reconcile_all)()
        except Exception as exc:  # noqa: BLE001 — перепланировка не роняет запись настройки
            logger.error("settings_reschedule: %s по ключу %r сорвалась: %s", name, key, exc)

    if base != "session_feedback_delay_minutes":
        return
    try:
        from services import session_feedback as sf

        if PER_CITY_SEP in key:
            parsed = split_per_city_key(key)
            if parsed is not None:
                await sf.reconcile_city(parsed[1])
        else:
            await sf.reconcile_all()
    except Exception as exc:  # noqa: BLE001
        logger.error("settings_reschedule: отзывы о сессиях по ключу %r сорвалась: %s", key, exc)
