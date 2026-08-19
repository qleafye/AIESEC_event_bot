"""Quick GAME-CITY-TABS: per-city gamification sheet tabs.

With the cities module ON, the manager gets — besides the two whole-event tabs («Гейма» matrix
+ «История сдач») — a pair of tabs PER ENABLED CITY holding only that city's rows:
«СПб Гейма» / «СПб История сдач». Naming follows the registration tabs byte-for-byte:
`cities.city_tab_base(code) + cities.tab_suffix(kind)` with the two new kinds `game` /
`game_history` (registry keys `city_tab_suffix__game` / `city_tab_suffix__game_history`).

A city whose tab base is empty (the default city, Moscow, whose registration rows live on
the main tab) gets NO per-city gamification tabs — `"" + " Гейма"` would collide with the
whole-event «Гейма» tab. Same rule as `handlers/reg_schema.py::city_row_tab` (no base -> the
shared tab). The manager can give such a city its own tabs by naming its tab base on the
«🏙 Города» screen (`city_tab__{code}`).

Import discipline: this module imports `cities` (which imports `database.db` +
`settings_schema` only) and nothing from `handlers.*` — the tab builders themselves live in
`handlers/admin_gamification.py` and are handed the filtered rows this module produces.
"""

import html
import logging

from cities import (
    cities_module_on,
    city_label,
    city_tab_base,
    enabled_cities,
    normalize_city,
    tab_suffix,
)
from settings_schema import get_setting_typed

logger = logging.getLogger(__name__)

# `kind` vocabulary shared by game_tab_plan() consumers.
KIND_MATRIX = "matrix"
KIND_HISTORY = "history"


def filter_tasks_for_city(tasks: list[dict], code: str) -> list[dict]:
    """Pure: tasks visible to delegates of `code` — a task with NULL event_city means "all
    cities" (same rule as db.list_all_tasks(city_scope=..., include_null=True)); a task with a
    city resolves through `normalize_city` so unknown/stale codes fall into the default city,
    exactly like `cities.city_scope`'s exclusion shape for SQL."""
    return [
        t for t in tasks
        if t.get("event_city") is None or normalize_city(t.get("event_city")) == code
    ]


def filter_submissions_for_city(submissions: list[dict], code: str) -> list[dict]:
    """Pure: submissions whose DELEGATE belongs to `code` (`user_event_city`, not the task's
    city — same choice as the live moderation queue, db.get_pending_submissions). NULL /
    unknown resolve to the default city via `normalize_city`, so pre-cities rows are never
    dropped from every city tab at once."""
    return [s for s in submissions if normalize_city(s.get("user_event_city")) == code]


async def game_tab_plan() -> list[dict]:
    """Ordered list of every gamification tab a rebuild writes, whole-event first:
        {"kind": "matrix"|"history", "city": None|code, "city_label": str|None, "tab": name}
    Module OFF -> exactly the two whole-event tabs (behaviour unchanged). Module ON -> plus a
    matrix+history pair for every ENABLED city that has a non-empty tab base (see module
    docstring for why an empty base is skipped)."""
    matrix_tab = await get_setting_typed("game_matrix_tab")
    history_tab = await get_setting_typed("game_history_tab")
    plan: list[dict] = [
        {"kind": KIND_MATRIX, "city": None, "city_label": None, "tab": matrix_tab},
        {"kind": KIND_HISTORY, "city": None, "city_label": None, "tab": history_tab},
    ]
    if not await cities_module_on():
        return plan
    matrix_suffix = await tab_suffix("game")
    history_suffix = await tab_suffix("game_history")
    for city in await enabled_cities():
        code = city["code"]
        base = await city_tab_base(code)
        if not base:
            continue
        label = await city_label(code)
        plan.append({"kind": KIND_MATRIX, "city": code, "city_label": label,
                     "tab": f"{base}{matrix_suffix}"})
        plan.append({"kind": KIND_HISTORY, "city": code, "city_label": label,
                     "tab": f"{base}{history_suffix}"})
    return plan


def rows_for_entry(entry: dict, tasks: list[dict], submissions: list[dict]) -> tuple[list[dict], list[dict]]:
    """(tasks, submissions) an entry of game_tab_plan() should be built from — unfiltered
    for a whole-event entry, city-filtered otherwise."""
    code = entry.get("city")
    if code is None:
        return tasks, submissions
    return filter_tasks_for_city(tasks, code), filter_submissions_for_city(submissions, code)


def describe_plan(plan: list[dict], *, only_city: str | None = None) -> list[str]:
    """Human lines for the confirm/report screens — one line per whole-event tab, one line
    per city (both of its tabs). `only_city` narrows the per-city lines to that city (a
    manager bound to one city sees «their» tabs); whole-event lines always stay. Tab names and
    city labels are HTML-escaped here (the screens render with parse_mode=HTML)."""
    lines: list[str] = []
    for e in plan:
        if e["city"] is None:
            what = "матрица участники × задания" if e["kind"] == KIND_MATRIX else "все сдачи"
            scope = ", все города" if any(x["city"] for x in plan) else ""
            lines.append(f"«{html.escape(e['tab'])}» — {what}{scope}")
    seen: set[str] = set()
    for e in plan:
        code = e["city"]
        if code is None or code in seen:
            continue
        if only_city is not None and code != only_city:
            continue
        seen.add(code)
        tabs = [x["tab"] for x in plan if x["city"] == code]
        lines.append(
            ", ".join(f"«{html.escape(t)}»" for t in tabs)
            + f" — только {html.escape(str(e['city_label']))}"
        )
    return lines
