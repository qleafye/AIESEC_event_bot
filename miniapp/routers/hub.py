"""Phase 23.1 (UI-REDESIGN-02): хаб делегата — тексты и факты первого экрана. Только чтение.
С плана 23.1-06 — официально ручка делегатской обвязки: подписи плиты зовут отсюда не только
хаб, но и три списочных экрана (задания/монеты/рейтинг), четвёртый независимый источник
подписей заводить нельзя.

Клиенту достаточно ОДНОГО запроса, чтобы получить все надписи и посчитанные факты плиты —
подстановка `{done}`/`{total}`/`{days}` делается ЗДЕСЬ, на сервере: `hub.js` ничего не
форматирует (D-06 — тексты хаба это реестр, не литералы JS). `done`/`total` — тот же
источник правды, что список заданий (`miniapp.routers.tasks.tasks_progress`, тот же
`list_active_tasks(city_scope=…)`), `event_dates`/`event_place` — те же ключи
`event_date`/`event_place_name`, что видит бот, с городским скоупом делегата.
`rank_unit` — подпись места в рейтинге с уже подставленным `{total}` (число участников,
`miniapp.routers.coins.count_participants` — тот же источник, что у самого рейтинга и
у баланса монет).

Гейт — `delegate_gate` (тот же приём, что у `miniapp/routers/coins.py`): у хаба нет своего
раздела-чекбокса в `SECTIONS` (он и есть дом приложения), поэтому `require_section` здесь
не нужен — только принадлежность к одобренным делегатам.
"""
from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, Depends

from cities import get_setting_typed_for_city
from database.db import get_user
from settings_schema import get_setting_typed

from miniapp.deps import Principal, delegate_gate
from miniapp.routers.coins import count_participants
from miniapp.routers.tasks import delegate_city_scope, tasks_progress
from miniapp.timeutil import today_msk

router = APIRouter()


def _days_until(raw: str | None) -> int | None:
    """Целое число полных дней от московского «сегодня» (`miniapp.timeutil.today_msk` —
    план 23.1-05 вынес общий помощник отсюда, чтобы `tasks.py` не заводил второй литерал
    часового пояса) до даты `raw` (строго ДД.ММ.ГГГГ). Дата не задана, не разбирается или уже
    прошла (строго меньше сегодняшней) -> `None`."""
    if not raw:
        return None
    try:
        target = datetime.strptime(raw.strip(), "%d.%m.%Y").date()
    except ValueError:
        return None
    delta = (target - today_msk()).days
    return delta if delta >= 0 else None


@router.get("/app/api/hub")
async def hub(p: Principal = Depends(delegate_gate)) -> dict:
    user = await get_user(p.telegram_id)
    event_city = user.get("event_city") if user else None

    done, total = await tasks_progress(p.telegram_id, await delegate_city_scope(p.telegram_id))
    tasks_fact_text = await get_setting_typed("miniapp_hub_tasks_fact_text")
    tasks_fact = tasks_fact_text.format(done=done, total=total) if tasks_fact_text else None

    countdown_date = await get_setting_typed_for_city("miniapp_hub_countdown_date", event_city)
    days = _days_until(countdown_date)
    days_fact_text = await get_setting_typed("miniapp_hub_days_fact_text")
    days_fact = days_fact_text.format(days=days) if (days is not None and days_fact_text) else None

    event_dates = await get_setting_typed_for_city("event_date", event_city) or None
    event_place = await get_setting_typed_for_city("event_place_name", event_city) or None

    # Плита списочного экрана «Рейтинг» (план 23.1-06): «из {total}» подставляется здесь —
    # число участников известно ручке (тот же count_participants, что у /coins/balance и
    # /leaderboard), отдавать шаблон с недоставленной подстановкой нельзя.
    rank_unit_text = await get_setting_typed("miniapp_leaderboard_plate_unit")
    total_participants = await count_participants()
    rank_unit = rank_unit_text.format(total=total_participants) if rank_unit_text else None

    return {
        "balance_eyebrow": await get_setting_typed("miniapp_hub_balance_eyebrow"),
        "balance_unit": await get_setting_typed("miniapp_hub_balance_unit"),
        "next_eyebrow": await get_setting_typed("miniapp_hub_next_eyebrow"),
        "sections_eyebrow": await get_setting_typed("miniapp_hub_sections_eyebrow"),
        "tasks_fact": tasks_fact,
        "days_fact": days_fact,
        "event_dates": event_dates,
        "event_place": event_place,
        "tasks_eyebrow": await get_setting_typed("miniapp_tasks_plate_eyebrow"),
        "rank_eyebrow": await get_setting_typed("miniapp_leaderboard_plate_eyebrow"),
        "rank_unit": rank_unit,
    }
