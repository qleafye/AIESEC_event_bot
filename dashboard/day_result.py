"""Страница «Итог дня»: четыре уже посчитанных среза за один выбранный день.

Новой арифметики нет. Приход — `arrival_for_day` (не `arrival_floor`: та подменяет
день). Опросы и оценки — как есть из `feedback_day`. Резерв — `reserve_counts`.
В контекст шаблона не кладём ФИО и telegram id.
"""
from __future__ import annotations

import shared.arrival_stats as arrival_stats
from dashboard import queries


def day_options(conn, scope: queries.Scope) -> list[dict]:
    """Дни программы плюс дни, в которые уже были входы. Один день — одна кнопка.

    Подпись программы (`дд.мм`) важнее: день только с входами подписывается
    так же, как в приходе (`03.10 (сб)`).
    """
    labeled = {row["day"]: row["label"] for row in queries.feedback_program_days(conn, scope)}
    block = queries.arrival_block(conn, scope)
    if block:
        for row in block.get("days") or []:
            day = row.get("day")
            if day and day not in labeled:
                labeled[day] = arrival_stats.day_short(day)
    # Вход резерва (статус «отклонён») в блок прихода не входит, но день с ним
    # должен быть кнопкой: иначе &day= этого дня откатывается на программу.
    for day in queries.entry_days(conn, scope):
        if day and day not in labeled:
            labeled[day] = arrival_stats.day_short(day)
    return [{"day": day, "label": labeled[day]} for day in sorted(labeled)]


def build_day_context(
    conn, cfg, scope: queries.Scope, viewer: dict,
    *, event_code: str | None = None, day: str | None = None,
) -> dict:
    """Контекст шаблона. Один и тот же день уходит во все четыре среза."""
    from dashboard.main import _city_label, _event_logo_url, _favicon_url

    flags = queries.dashboard_flags(conn)
    options = day_options(conn, scope)
    selected = queries.resolve_feedback_day(
        day, options, queries.msk_now().strftime("%Y-%m-%d"),
    )
    feedback = queries.feedback_day(conn, scope, day=selected)
    rules_on = flags.get("reject_rules_enabled") == "on"
    bound_city_code = (viewer or {}).get("bound_city")
    city_options = queries.city_options(conn)
    return {
        "event_name": flags.get("event_name"),
        "event_season": scope.season or flags.get("event_season"),
        "event_logo_url": _event_logo_url(conn),
        "favicon_url": _favicon_url(conn),
        "viewer": {"bound_city": bound_city_code},
        "scope": scope,
        "city_options": city_options,
        "show_city_switcher": bool(city_options) and not bound_city_code,
        "bound_city_label": _city_label(conn, bound_city_code),
        "season_options": queries.season_options(conn),
        "event_code": event_code,
        "day_options": options,
        "day": selected,
        "day_label": feedback["day_label"],
        "arrival": queries.arrival_for_day(conn, scope, selected),
        "forms": feedback["forms"],
        "totals": feedback["totals"],
        "sessions": feedback["sessions"],
        "sessions_totals": feedback["sessions_totals"],
        "reserve": queries.reserve_counts(conn, scope, selected) if rules_on else None,
        "reserve_off": not rules_on,
    }
