"""Phase 13 (13-04, REFAC-01): shared aggregator-core helpers.

Genuinely cross-domain private helpers pulled out of `handlers/admin.py` so every seam module
(admin_gamification.py, admin_roles.py, and later admin plans) can import them without reaching
back into the god-file or recreating a circular import. No `Router` here, no `@router.*`
handler — this module is plumbing only, mirroring the `handlers/reg_schema.py` pattern from
13-02.

Two clusters live here (.planning CONCERNS.md "Internal coupling inside handlers/admin.py"):
- `admin_keyboard_for` + its dependency chain (`build_admin_keyboard`, `_visible_menu_rows`,
  `_ADMIN_MENU_ROWS`) — called from nearly every domain (every handler that re-renders the main
  admin panel after an action).
- The city-scope cluster (`_admin_city_view`/`_admin_city_scope`/`_admin_city_label`/
  `_card_out_of_scope`/`_submission_out_of_scope` + their alert strings) — moderation
  (appr_*/rcpt_*, still in admin.py) and gamification review (grev_*, admin_gamification.py)
  both gate per-card/per-submission actions through it.
"""
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton

from database.db import get_user
from cities import (
    admin_selected_city,
    city_label,
    city_scope,
    normalize_city,
    ALL_CITIES,
    ALL_CITIES_LABEL,
)
from handlers.admin_caps import required_capability, resolve_capabilities, ANY_CAPABILITY


# ROLE-01 (D-15): the ONE list of (text, callback_data) menu rows — same order the plain
# 14-button panel has always rendered in (13 original rows + Phase 07.2's «Города
# мероприятия» row, appended last). This is the single source `_visible_menu_rows` filters
# and `build_admin_keyboard` renders from; there is no second "button -> required right" map
# anywhere in this file (D-01/D-15 invariant) — the required capability for each row is
# looked up straight from `ADMIN_CAPS` (via `required_capability`), the exact map
# `CapabilityMiddleware` enforces on the backend.
_ADMIN_MENU_ROWS: list[tuple[str, str]] = [
    ("📊 Статистика регистраций", "admin_stats"),
    ("🗓 Регистрации по месяцам", "admin_monthly_stats"),
    ("📈 Источники", "admin_source_stats"),
    ("📄 Экспорт CSV", "admin_export_csv"),
    ("📝 Незавершённые → таблица", "admin_export_incomplete"),
    ("📋 Заявки", "admin_applications"),
    ("🧾 Чеки", "admin_receipts"),
    ("🔒 Залипшие вопросы", "admin_stuck_questions"),
    ("📢 Рассылка", "admin_broadcast"),
    ("🔄 Синхронизация таблицы", "admin_sync_sheet"),
    ("♻️ Пересобрать таблицу", "admin_rebuild_sheet"),
    ("🧹 Убрать дубли из таблицы", "admin_dedupe_sheet"),
    ("⚙️ Настройки форума", "admin_settings"),
    ("📖 Справка по настройкам", "admin_settings_guide"),
    ("🏙 Города мероприятия", "admin_cities"),
    ("🎯 Задания", "admin_game_tasks"),  # Phase 16 (16-03): same label as the delegate's section
    ("🎮 Проверка заданий", "admin_game_review"),
    ("🪙 Монеты вручную", "admin_coins_manual"),
    ("📜 Журнал монет", "admin_coins_journal"),
    ("🔄 Таблица геймы", "admin_game_sync_sheet"),
    ("📊 Статистика геймы", "admin_game_stats"),
]


def _visible_menu_rows(caps: set) -> list[tuple[str, str]]:
    """Pure, synchronous, no I/O (CONVENTIONS.md `_private`-helper idiom) — the unit-testable
    half of D-15's "menu built from this person's rights". `caps` must already be resolved
    (the SQLite read happens once, in `build_admin_keyboard`, not per-row here). Hiding a row
    the caller can't reach is convenience only — `CapabilityMiddleware` (handlers/admin_caps.py)
    is what actually enforces access on every callback, independently of what this function
    returns (D-15 requires "AND", never "OR")."""
    rows = []
    for text, callback_data in _ADMIN_MENU_ROWS:
        cap = required_capability(callback_data=callback_data)
        if cap is None:
            continue  # deny-by-default (D-02): an unmapped row is never shown either
        if cap == ANY_CAPABILITY:
            if caps:
                rows.append((text, callback_data))
        elif cap in caps:
            rows.append((text, callback_data))
    return rows


async def build_admin_keyboard(user_id: int) -> InlineKeyboardMarkup:
    """ROLE-01 (D-15): the panel built for THIS person — a fresh capability read
    (`resolve_capabilities`, D-05: no cache) followed by the pure row filter above. Every
    caller MUST await this now; there is deliberately no synchronous fallback left, so a
    forgotten `await` fails loudly (a coroutine object is not a valid `reply_markup`) instead
    of silently showing an unfiltered panel to everyone."""
    caps = await resolve_capabilities(user_id)
    rows = _visible_menu_rows(caps)
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=text, callback_data=callback_data)]
        for text, callback_data in rows
    ])


# Phase 07.2 (CITY-02): per-admin city switcher, rendered as a header row over the panel.
# `build_admin_keyboard(user_id)` already does the D-15 capability filtering (ROLE-01/08-05);
# this wrapper only adds the city-switcher header row on top, unchanged from Phase 07.2.
async def admin_keyboard_for(admin_id: int) -> InlineKeyboardMarkup:
    code = await admin_selected_city(admin_id)
    base = await build_admin_keyboard(admin_id)
    if code is None:  # module off — byte-identical to today, no switcher row
        return base
    # Phase 09.3 (CITY-08): «Все города» mode gets its own label (no "🏙 Город: " prefix) —
    # the button text ITSELF is ALL_CITIES_LABEL, not the label glued after the usual prefix.
    label_text = ALL_CITIES_LABEL if code == ALL_CITIES else f"🏙 Город: {await city_label(code)}"
    header = [InlineKeyboardButton(text=label_text, callback_data="admin_city_switch")]
    return InlineKeyboardMarkup(inline_keyboard=[header] + base.inline_keyboard)


# Phase 07.2 (CITY-02): the ONE resolver both moderation queues (applications + receipts)
# read the selected city through — no handler calls admin_selected_city/city_scope directly.
# If a third moderation queue is ever added (gamification, Phase 9), it plugs in here too.
#
# WR-05: a screen must resolve the city ONCE and derive both the scope and the label from that
# single read. `_admin_city_scope` + `_admin_city_label` called back-to-back are two independent
# coroutines, each re-reading `admin_city__{id}` (cities_module_on() + get_setting = two more
# SQLite connections). aiogram processes updates concurrently, so the setting can change between
# the two awaits — the queue would then be FILTERED by one city and HEADED by another, exactly
# the confusion the header exists to prevent. Every render-time call site uses this helper.
async def _admin_city_view(admin_id: int) -> tuple[tuple[str, tuple[str, ...]] | None, str | None]:
    """(city_scope, label) for one admin, resolved from a SINGLE `admin_selected_city` read.
    Three states now (Phase 09.3, CITY-08):
    - `(None, None)` = no scope (module off) — the unfiltered, pre-CITY-02 behaviour.
    - `(None, ALL_CITIES_LABEL)` = module on, admin explicitly chose «Все города» — the SAME
      unfiltered SQL scope as module-off, but the label is NOT None. Every existing call site
      that branches on `label is None` to mean "module off, no city text" now gets a real
      (non-None) label for this mode instead — it will NOT crash or raise, but it WILL print
      the wrong copy (or the raw "*" literal, pre-this-fix) unless that branch is re-read and,
      where the text differs by mode, given a third branch. Re-check every `label is None`
      ternary you touch downstream of this function.
    - a real city code returns its own scoped tuple + label, unchanged."""
    code = await admin_selected_city(admin_id)
    if code is None:
        return None, None
    if code == ALL_CITIES:
        return None, await city_label(code)
    return city_scope(code), await city_label(code)


# Thin single-value views over _admin_city_view — kept because "what city is this admin scoped
# to" is the seam Phase 8 (staff/capabilities) plugs into, and the module-off parity contract
# asserts on them directly.
async def _admin_city_scope(admin_id: int) -> tuple[str, tuple[str, ...]] | None:
    return (await _admin_city_view(admin_id))[0]


async def _admin_city_label(admin_id: int) -> str | None:
    return (await _admin_city_view(admin_id))[1]


# WR-03: the QUEUE is scoped, but the per-card actions (appr_approve / appr_reject /
# rcpt_confirm / rcpt_reject) address a row by telegram_id from the callback data and knew
# nothing about the city. Cards stay in the chat history and inline buttons never expire, so a
# card rendered for city A and tapped after switching to city B acted OUTSIDE the current
# scope — breaking the "the panel shows and changes only the selected city" guarantee the
# ADMIN_GUIDE makes. These are single-record actions (the card shows the name), so one check is
# enough; unlike CR-02's mass approval, nothing here is redesigned.
async def _card_out_of_scope(admin_id: int, tid: int | None) -> bool:
    """True when `tid` does not belong to the admin's currently selected city. Always False
    with the module off (scope None = «ничего не фильтруется», the pre-CITY-02 path)."""
    if tid is None:
        return False
    scope = await _admin_city_scope(admin_id)
    if scope is None:
        return False
    user = await get_user(tid)
    # normalize_city collapses NULL/unknown into the default city — the SAME resolver the
    # queue's SQL scope uses, so «заявка без города» lands in Moscow here too, not «nowhere».
    return normalize_city((user or {}).get("event_city")) != scope[0]


_OUT_OF_SCOPE_ALERT = "Эта заявка из другого города — переключите город."


# CR-03 (09.1-REVIEW.md): the submission-level twin of `_card_out_of_scope` above. The QUEUE
# (`get_pending_submissions(city_scope=...)`) was already scoped by the DELEGATE's city — this
# checks the same thing for the per-card DECISION handlers (grev_approve /
# grev_approve_custom_start / grev_reject_start), which used to act on `submission_id` from
# callback_data with zero city check and credit coins / notify the delegate regardless. Checks
# the SUBMITTER's city, not the task's city (a task can be city-scoped or "all cities" — WR-06
# covers that at submit time; here we only care whether the DELEGATE is inside the admin's
# current scope, exactly the resolver `get_pending_submissions` itself uses).
async def _submission_out_of_scope(admin_id: int, submission: dict | None) -> bool:
    """True when the submission's delegate is outside the admin's currently selected city.
    Always False with the module off (scope None = «ничего не фильтруется», same parity
    contract as `_card_out_of_scope`)."""
    if submission is None:
        return False
    scope = await _admin_city_scope(admin_id)
    if scope is None:
        return False
    user = await get_user(submission["user_id"])
    return normalize_city((user or {}).get("event_city")) != scope[0]


_SUBMISSION_OUT_OF_SCOPE_ALERT = "Эта сдача из другого города — переключите город."
