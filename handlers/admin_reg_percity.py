"""Phase 13 (13-05, REFAC-01) / Phase 25 split — реестр вопросов и текстов анкеты, per-city.

Вынесено из `handlers/admin_reg_config.py` (module-size convention, tests/test_module_size_
convention_260816.py): экраны «📋 Вопросы регистрации» (toggle_reg_question/toggle_party_
question/toggle_short_question), «✏️ Тексты вопросов» (reg_prompt_*), переключатель
резюме-режима (reg_resume_mode_toggle) и их общие «↩️ Как везде» confirm-флоу
(reg_q_reset_city*/reg_prompt_rst*) — вместе с хелперами, которые использует только эта
пара экранов. Регистрируется на ТОТ ЖЕ общий `admin.router`, что и остальные швы
(13-02..13-05 shared-router seam-import), сразу после `admin_reg_config` в порядке импорта
`handlers/admin.py` — порядок регистрации хендлеров не меняется.

Пресеты события (`_apply_event_preset`/`admin_event_preset`/`preset_apply`/`preset_confirm`),
экран «🔘 Кнопки главного меню» и все три `_refresh_*_sheet_header` остаются в
`admin_reg_config.py` — `preset_confirm` дёргает `render_questions_text`/
`build_questions_keyboard` отсюда ленивым импортом (тот же приём, что и у соседних швов,
см. `admin_reg_config.py::reg_questions_back`/`admin_settings.py::toggle_nudge_enabled`),
а хендлеры здесь дёргают `_refresh_*_sheet_header` обратным импортом из `admin_reg_config`
(этот модуль загружается раньше — `from handlers import admin_reg_config` стоит первым
в `handlers/admin.py`).
"""
import html as html_module
import logging

from aiogram import F, types
from aiogram.fsm.context import FSMContext
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from settings_schema import get_setting_typed
from database.db import get_setting, set_setting, delete_setting
from handlers.states import EditSetting
from handlers.reg_schema import REG_FLOW, REG_LABELS, REG_CATEGORIES
from cities import (
    ALL_CITIES,
    admin_selected_city,
    cities_module_on,
    city_codes,
    city_label,
    get_setting_typed_for_city,
    per_city_key,
)
from handlers.admin import router
from handlers.admin_consent import remind_consent_purposes_if_widened
from handlers.admin_settings import _per_city_visible_codes  # Phase 13 (13-06): settings moved out of admin.py
from handlers.admin_reg_config import (
    _refresh_sheet_header,
    _refresh_party_sheet_header,
    _refresh_short_sheet_header,
)
import reg_engine  # квик 260906-7zv: help_default/has_help — швов циклов нет, reg_engine handlers не импортирует

logger = logging.getLogger(__name__)

# INVARIANT (13-01 cap-test, extended by 13-04 to scan every handlers/admin*.py file): every
# `@router.*` decorator below MUST fit on ONE line.

# Phase 25 (CITYQ-04): a question/resume-mode toggle changes the sheet's column set — old
# rows keep their positions until the manager reruns «♻️ Пересобрать таблицу» (CONTEXT.md's
# Google-таблица decision). Appended to every toggle alert below, one place to keep the
# wording consistent.
_REBUILD_HINT = "\n\nСтроки до этого момента не сдвинутся — нажми ♻️ Пересобрать таблицу."


async def _is_question_on(setting_key: str) -> bool:
    # REG-02 (06-04): delegates entirely to the registry's typed toggle accessor — byte-
    # identical to the prior manual (val == "on") if val is not None else REG_DEFAULTS.get(...)
    # idiom (get_setting_typed's toggle branch reproduces it exactly), single get_setting call.
    return await get_setting_typed(setting_key)


# Phase 5 (D-04): party question tri-state helpers. These are PURE — they operate on the
# raw get_setting(f"{key}__party") return value (None | "on" | "off") and must never route
# through _is_question_on, which collapses None into a resolved boolean and would make
# "inherit" indistinguishable from "off".
def _party_tri_state_label(raw: str | None) -> str:
    if raw is None:
        return "➕ Наследует"
    return "✅ Вкл" if raw == "on" else "❌ Выкл"


def _party_tri_state_advance(raw: str | None) -> str | None:
    """Cycle: None (inherit) -> "on" -> "off" -> None (inherit). The None return value
    signals the caller to delete_setting (key-absence IS the inherit state, D-04)."""
    if raw is None:
        return "on"
    if raw == "on":
        return "off"
    return None


def _track_switcher_row(active: str) -> list[InlineKeyboardButton]:
    """D-06: first row of the questions keyboard — switches the whole screen between the
    full-track view (existing 2-state toggles) and the party-track view (tri-state).

    Phase 7 (SHORT-03): third button for the short (promo) track. Its own screen is
    2-state (✅/❌), not tri-state like party — see render_questions_text/
    build_questions_keyboard for the rationale."""
    return [
        InlineKeyboardButton(text=("• " if active == "full" else "") + "Полный", callback_data="reg_q_track:full"),
        InlineKeyboardButton(text=("• " if active == "party" else "") + "Party", callback_data="reg_q_track:party"),
        InlineKeyboardButton(text=("• " if active == "short" else "") + "⚡ Краткая", callback_data="reg_q_track:short"),
    ]


def _categorized_question_keys() -> list[tuple[str, str]]:
    """(header, setting_key) rows in category order. Any REG_FLOW key not placed in a
    REG_CATEGORIES bucket lands in a trailing «Прочие» group so nothing is ever hidden."""
    seen = set()
    rows: list[tuple[str, str]] = []
    for header, keys in REG_CATEGORIES:
        for k in keys:
            rows.append((header, k))
            seen.add(k)
    leftover = [sk for _, sk, *_ in REG_FLOW if sk not in seen]
    for k in leftover:
        rows.append(("📦 Прочие", k))
    return rows


def _question_override_key(track: str, setting_key: str, code: str) -> str | None:
    """Composite per-city key for ONE question at ONE track — the only place deciding which
    track suffix (if any) sits between the base `reg_q_*` key and `cities.PER_CITY_SEP`.
    Returns `None` when `code` is not a real city code (per_city_key's own contract)."""
    if track == "party":
        return per_city_key(f"{setting_key}__party", code)
    if track == "short":
        return per_city_key(f"{setting_key}__short", code)
    return per_city_key(setting_key, code)


async def _question_effective_and_own(track: str, setting_key: str, code: str) -> tuple[str, bool]:
    """Effective status label + whether the CITY has its own override, for one question row
    at one real city header, in one track (Phase 25, CITYQ-04).

    full: effective via `get_setting_typed_for_city` (module-off/no-city fallback baked in);
    own = the composite key is present (always an explicit "on"/"off", never deleted).
    party/short: effective is read RAW (never through `_is_question_on`, which would collapse
    `None`) — city raw wins if present, else the day's global raw; own = the CITY key is
    present at all (both "on" and "off" count as "own" — absence alone means "inherit")."""
    override_key = _question_override_key(track, setting_key, code)
    if track == "party":
        track_key = f"{setting_key}__party"
        city_raw = await get_setting(override_key) if override_key else None
        raw = city_raw if city_raw is not None else await get_setting(track_key)
        return _party_tri_state_label(raw), city_raw is not None
    if track == "short":
        track_key = f"{setting_key}__short"
        city_raw = await get_setting(override_key) if override_key else None
        raw = city_raw if city_raw is not None else await get_setting(track_key)
        return ("✅ Вкл" if raw == "on" else "❌ Выкл"), city_raw is not None
    # reg_q_* keys are SETTINGS_SCHEMA type "toggle" -> get_setting_typed_for_city already
    # resolves to a bool (settings_schema._parse_setting's toggle branch), never "on"/"off" —
    # unlike the "enum"-typed menu_*/reg_resume_mode keys the sibling screens compare as strings.
    is_on = await get_setting_typed_for_city(setting_key, code)
    own = bool(override_key and await get_setting(override_key))
    return ("✅" if is_on else "❌"), own


async def _questions_city_has_override(code: str, track: str) -> bool:
    """True if the city has at least one REG_FLOW question overridden for the GIVEN track —
    mirrors `_menu_city_has_override`'s truthy-key-presence check, scoped to one track. Feeds
    the merged keyboard's «↩️ Как везде» row (same shape as the menu-buttons screen's
    reset-visibility gate)."""
    for _header, setting_key in _categorized_question_keys():
        override_key = _question_override_key(track, setting_key, code)
        if override_key and await get_setting(override_key):
            return True
    return False


async def render_questions_text(track: str = "full", admin_id: int | None = None) -> str:
    """Phase 25 (CITYQ-04, WR-05): resolves the header ITSELF, once — header = real city ->
    title names the city, every row shows the city's EFFECTIVE value plus a
    «(своё)»/«(как везде)» mark (same wording as the menu-buttons screen). Header = None
    (module off / no admin_id passed) / ALL_CITIES («все города») -> today's global screen,
    byte-identical (untouched branch below)."""
    header_code = await admin_selected_city(admin_id) if admin_id is not None else None
    per_city_ctx = bool(header_code and header_code != ALL_CITIES)

    if per_city_ctx:
        city_txt = await city_label(header_code)
        lines = [f"📋 <b>Вопросы регистрации — {html_module.escape(city_txt)}</b>", ""]
    else:
        lines = ["📋 <b>Вопросы регистрации</b>", ""]
    if track == "party":
        lines.append(
            "<i>Действуют в режиме «🎉 Party». ➕ Наследует — берётся общая настройка, "
            "✅/❌ — переопределено для этого трека.</i>"
        )
    elif track == "short":
        # Phase 7 (SHORT-03): 2-state, not tri-state. Absent __short key means "не спрашивается"
        # (07-01's is-not-None gate resolves absence to False, no fallback to the global toggle) —
        # visually there is nothing to "inherit", so a third inherit-labelled button would look
        # identical to "off" and just confuse the manager ("нажимаю, ничего не меняется").
        lines.append(
            "<i>Действуют в режиме «⚡ Краткая». По умолчанию вопрос не задаётся — включай "
            "только нужные.</i>"
        )
    else:
        lines.append("<i>Действуют в режиме «📋 Полная регистрация». Сгруппированы по типу события.</i>")
    lines.append("")
    current = None
    for header, setting_key in _categorized_question_keys():
        if header != current:
            lines.append(f"\n<b>{header}</b>")
            current = header
        label = REG_LABELS.get(setting_key, setting_key)
        if per_city_ctx:
            status, own = await _question_effective_and_own(track, setting_key, header_code)
            mark = " <i>(своё)</i>" if own else " <i>(как везде)</i>"
            lines.append(f"{status} {label}{mark}")
            continue
        if track == "party":
            raw = await get_setting(f"{setting_key}__party")
            status = _party_tri_state_label(raw)
        elif track == "short":
            raw = await get_setting(f"{setting_key}__short")
            status = "✅ Вкл" if raw == "on" else "❌ Выкл"
        else:
            status = "✅" if await _is_question_on(setting_key) else "❌"
        lines.append(f"{status} {label}")
    return "\n".join(lines)


async def build_questions_keyboard(track: str = "full", admin_id: int | None = None):
    """Same header-aware branch as `render_questions_text` (WR-05: this function resolves the
    header itself, ONCE). Callback data never carries the city code (T-25-14) — every toggle
    button keeps its EXISTING callback_data regardless of header, the write handler re-reads
    the header itself."""
    header_code = await admin_selected_city(admin_id) if admin_id is not None else None
    per_city_ctx = bool(header_code and header_code != ALL_CITIES)

    buttons = [_track_switcher_row(track)]
    current = None
    for header, setting_key in _categorized_question_keys():
        if header != current:
            # Non-actionable section header (noop callback).
            buttons.append([InlineKeyboardButton(text=f"── {header} ──", callback_data="reg_q_noop")])
            current = header
        label = REG_LABELS.get(setting_key, setting_key)
        if per_city_ctx:
            status, own = await _question_effective_and_own(track, setting_key, header_code)
            marker = "• " if own else ""
            toggle_label = f"{status} {marker}{label}"
            if track == "party":
                buttons.append([InlineKeyboardButton(text=toggle_label, callback_data=f"reg_q_ptoggle:{setting_key}")])
            elif track == "short":
                buttons.append([InlineKeyboardButton(text=toggle_label, callback_data=f"reg_q_stoggle:{setting_key}")])
            else:
                buttons.append([InlineKeyboardButton(text=toggle_label, callback_data=f"reg_q_toggle:{setting_key}")])
                if setting_key == "reg_q_resume":
                    buttons.append([await _resume_mode_toggle_button(header_code)])
            continue
        if track == "party":
            raw = await get_setting(f"{setting_key}__party")
            toggle_text = f"{_party_tri_state_label(raw)} {label}"
            buttons.append([InlineKeyboardButton(text=toggle_text, callback_data=f"reg_q_ptoggle:{setting_key}")])
        elif track == "short":
            # Phase 7 (SHORT-03): read the RAW __short value (never _is_question_on/
            # get_setting_typed) — only the literal "on" counts as enabled; absence and any
            # other value both render as ❌ Выкл, matching the 2-state model above.
            raw = await get_setting(f"{setting_key}__short")
            toggle_text = f"{'✅ Вкл' if raw == 'on' else '❌ Выкл'} {label}"
            buttons.append([InlineKeyboardButton(text=toggle_text, callback_data=f"reg_q_stoggle:{setting_key}")])
        else:
            toggle_text = f"{'✅' if await _is_question_on(setting_key) else '❌'} {label}"
            buttons.append([InlineKeyboardButton(text=toggle_text, callback_data=f"reg_q_toggle:{setting_key}")])
            if setting_key == "reg_q_resume":
                buttons.append([await _resume_mode_toggle_button(None)])

    if per_city_ctx and await _questions_city_has_override(header_code, track):
        buttons.append([InlineKeyboardButton(text="↩️ Как везде", callback_data="reg_q_reset_city")])
    buttons.append([InlineKeyboardButton(text="← Назад", callback_data="reg_q_back")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


@router.callback_query(F.data == "admin_reg_questions")
async def show_reg_questions(callback: types.CallbackQuery):
    admin_id = callback.from_user.id
    text = await render_questions_text(admin_id=admin_id)
    await callback.message.edit_text(text, parse_mode="HTML", reply_markup=await build_questions_keyboard(admin_id=admin_id))
    await callback.answer()


@router.callback_query(F.data.startswith("reg_q_toggle:"))
async def toggle_reg_question(callback: types.CallbackQuery):
    setting_key = callback.data.split(":", 1)[1]
    admin_id = callback.from_user.id
    header_code = await admin_selected_city(admin_id)
    per_city_ctx = bool(header_code and header_code != ALL_CITIES)

    if per_city_ctx:
        # T-25-12: RIGHT re-checked here, not just via a hidden button.
        visible = await _per_city_visible_codes(admin_id)
        if header_code not in visible:
            await callback.answer("Этот город правит суперадмин", show_alert=True)
            return
        valid_keys = {sk for _, sk, *_ in REG_FLOW}
        if setting_key not in valid_keys:
            await callback.answer("Неизвестный вопрос.", show_alert=True)
            return
        # T-093-25 idiom: composed key comes ONLY from cities.per_city_key.
        composed = per_city_key(setting_key, header_code)
        if composed is None:
            await callback.answer("Неизвестный город", show_alert=True)
            return
        # reg_q_* is SETTINGS_SCHEMA type "toggle" -> already a bool, see _question_effective_and_own.
        current_on = await get_setting_typed_for_city(setting_key, header_code)
        new_val = "off" if current_on else "on"
        await set_setting(composed, new_val)
        label = REG_LABELS.get(setting_key, setting_key)
        status = "✅ Вкл" if new_val == "on" else "❌ Выкл"
        city_txt = await city_label(header_code)
        await callback.answer(f"{label} — {city_txt}: {status}{_REBUILD_HINT}", show_alert=True)
        text = await render_questions_text("full", admin_id)
        await callback.message.edit_text(text, parse_mode="HTML", reply_markup=await build_questions_keyboard("full", admin_id))
        await _refresh_sheet_header(header_code, setting_key)  # only THIS city's tab
        await remind_consent_purposes_if_widened(callback.message, setting_key, new_val)
        return

    # Global branch — REG-02 (06-04): registry-driven resolution, byte-identical to the prior
    # manual (val == "on") if val is not None else REG_DEFAULTS.get(setting_key, "on") == "on"
    # idiom.
    current_on = await get_setting_typed(setting_key)

    new_val = "off" if current_on else "on"
    await set_setting(setting_key, new_val)

    label = REG_LABELS.get(setting_key, setting_key)
    status = "✅ Вкл" if new_val == "on" else "❌ Выкл"
    await callback.answer(f"{label}: {status}{_REBUILD_HINT}", show_alert=True)

    text = await render_questions_text("full", admin_id)
    await callback.message.edit_text(text, parse_mode="HTML", reply_markup=await build_questions_keyboard("full", admin_id))
    await _refresh_sheet_header(setting_key=setting_key)  # main tab + cities without their own override
    # Quick 260822: включение резюме = передача файлов в Nextcloud — напоминание о целях.
    await remind_consent_purposes_if_widened(callback.message, setting_key, new_val)


@router.callback_query(F.data.startswith("reg_q_track:"))
async def reg_q_track_switch(callback: types.CallbackQuery):
    """D-06: track switcher row — re-renders the SAME «📋 Вопросы регистрации» message in
    the requested track context. No FSM state — the requested track lives entirely in the
    callback_data of the tapped button."""
    track = callback.data.split(":", 1)[1]
    if track not in ("full", "party", "short"):
        track = "full"
    admin_id = callback.from_user.id
    text = await render_questions_text(track, admin_id)
    await callback.message.edit_text(text, parse_mode="HTML", reply_markup=await build_questions_keyboard(track, admin_id))
    await callback.answer()


@router.callback_query(F.data.startswith("reg_q_ptoggle:"))
async def toggle_party_question(callback: types.CallbackQuery):
    """D-04: tri-state cycle inherit(absent) -> on -> off -> inherit for the party-track
    override of one question. Reads/writes the RAW f"{setting_key}__party" value — never
    routes through _is_question_on, which would collapse None and make "inherit"
    indistinguishable from "off". delete_setting is the "back to inherit" primitive."""
    setting_key = callback.data.split(":", 1)[1]
    # T-05-03-02: validate setting_key against REG_FLOW before ever suffixing/writing it —
    # an unknown key from a crafted callback is rejected, never turned into a bot_settings write.
    valid_keys = {sk for _, sk, *_ in REG_FLOW}
    if setting_key not in valid_keys:
        await callback.answer("Неизвестный вопрос.", show_alert=True)
        return

    admin_id = callback.from_user.id
    header_code = await admin_selected_city(admin_id)
    per_city_ctx = bool(header_code and header_code != ALL_CITIES)
    party_key = f"{setting_key}__party"

    if per_city_ctx:
        visible = await _per_city_visible_codes(admin_id)
        if header_code not in visible:
            await callback.answer("Этот город правит суперадмин", show_alert=True)
            return
        composed = per_city_key(party_key, header_code)
        if composed is None:
            await callback.answer("Неизвестный город", show_alert=True)
            return
        current = await get_setting(composed)  # None | "on" | "off" — do NOT collapse
        new_val = _party_tri_state_advance(current)
        if new_val is None:
            await delete_setting(composed)  # back to inherit — key ABSENCE is the inherit state
        else:
            await set_setting(composed, new_val)
        label = _party_tri_state_label(new_val)
        city_txt = await city_label(header_code)

        await _refresh_party_sheet_header(header_code, setting_key)  # only THIS city's tab
        await callback.answer(
            f"{REG_LABELS.get(setting_key, setting_key)} (party) — {city_txt}: {label}{_REBUILD_HINT}",
            show_alert=True,
        )
        text = await render_questions_text("party", admin_id)
        await callback.message.edit_text(text, parse_mode="HTML", reply_markup=await build_questions_keyboard("party", admin_id))
        return

    current = await get_setting(party_key)  # None | "on" | "off" — do NOT collapse
    new_val = _party_tri_state_advance(current)
    if new_val is None:
        await delete_setting(party_key)  # back to inherit — key ABSENCE is the inherit state
    else:
        await set_setting(party_key, new_val)
    label = _party_tri_state_label(new_val)

    await _refresh_party_sheet_header(setting_key=setting_key)  # MEDIUM-01: keep the party tab aligned
    await callback.answer(f"{REG_LABELS.get(setting_key, setting_key)} (party): {label}{_REBUILD_HINT}", show_alert=True)
    text = await render_questions_text("party", admin_id)
    await callback.message.edit_text(text, parse_mode="HTML", reply_markup=await build_questions_keyboard("party", admin_id))


@router.callback_query(F.data.startswith("reg_q_stoggle:"))
async def toggle_short_question(callback: types.CallbackQuery):
    """Phase 7 (SHORT-03): 2-state toggle (on/off) for the short-track __short override of
    one question. Deliberately 2-state, not tri-state like toggle_party_question — see
    render_questions_text's short branch for the rationale (absent __short key already means
    "off" per 07-01, so a separate "inherit" state would be indistinguishable from "off" and
    only confuse the manager). delete_setting is never used here — every tap writes an
    explicit "on"/"off", unlike the party cycle's "back to inherit" step."""
    setting_key = callback.data.split(":", 1)[1]
    # T-07-09: validate setting_key against REG_FLOW before ever suffixing/writing it — a
    # crafted "reg_q_stoggle:party_enabled" (or any non-REG_FLOW key) is rejected, never
    # turned into a bot_settings write.
    valid_keys = {sk for _, sk, *_ in REG_FLOW}
    if setting_key not in valid_keys:
        await callback.answer("Неизвестный вопрос.", show_alert=True)
        return

    admin_id = callback.from_user.id
    header_code = await admin_selected_city(admin_id)
    per_city_ctx = bool(header_code and header_code != ALL_CITIES)
    short_key = f"{setting_key}__short"

    if per_city_ctx:
        visible = await _per_city_visible_codes(admin_id)
        if header_code not in visible:
            await callback.answer("Этот город правит суперадмин", show_alert=True)
            return
        composed = per_city_key(short_key, header_code)
        if composed is None:
            await callback.answer("Неизвестный город", show_alert=True)
            return
        current = await get_setting(composed)  # None | "on" | "off"
        new_val = "off" if current == "on" else "on"
        await set_setting(composed, new_val)  # always an explicit write, never delete_setting
        label = "✅ Вкл" if new_val == "on" else "❌ Выкл"
        city_txt = await city_label(header_code)

        await _refresh_short_sheet_header(header_code, setting_key)  # only THIS city's tab
        await callback.answer(
            f"{REG_LABELS.get(setting_key, setting_key)} (краткая) — {city_txt}: {label}{_REBUILD_HINT}",
            show_alert=True,
        )
        text = await render_questions_text("short", admin_id)
        await callback.message.edit_text(text, parse_mode="HTML", reply_markup=await build_questions_keyboard("short", admin_id))
        return

    current = await get_setting(short_key)  # None | "on" | "off"
    new_val = "off" if current == "on" else "on"
    await set_setting(short_key, new_val)  # always an explicit write, never delete_setting
    label = "✅ Вкл" if new_val == "on" else "❌ Выкл"

    await _refresh_short_sheet_header(setting_key=setting_key)  # keep the short tab header aligned
    await callback.answer(f"{REG_LABELS.get(setting_key, setting_key)} (краткая): {label}{_REBUILD_HINT}", show_alert=True)
    text = await render_questions_text("short", admin_id)
    await callback.message.edit_text(text, parse_mode="HTML", reply_markup=await build_questions_keyboard("short", admin_id))


# --- Resume mode (file-or-text vs text-only), Phase 25 (CITYQ-04) ---
#
# Human labels only — the enum's own on-the-wire values (file_or_text/text_only) never reach
# a manager, same "коды не показываем" rule as everywhere else in the admin surface.
_RESUME_MODE_HUMAN = {"file_or_text": "файл или текст", "text_only": "только текст"}


def _resume_mode_toggle_label(current: str) -> str:
    """«Текущее → Новое» direction idiom — same shape as every other direction-toggle button
    in the admin surface (handlers/admin_settings.py: registration_mode/bonus_enabled/...)."""
    target = "file_or_text" if current == "text_only" else "text_only"
    return f"📄 Резюме: {_RESUME_MODE_HUMAN.get(current, current)} → {_RESUME_MODE_HUMAN.get(target, target)}"


async def _resume_mode_toggle_button(header_code: str | None) -> InlineKeyboardButton:
    """The keyboard row placed right under «📄 Резюме» on the full-track screen. `header_code`
    given (a real city) -> effective value + «(своё)» bullet marker for that city; `None` ->
    global value, no marker."""
    if header_code:
        current = await get_setting_typed_for_city("reg_resume_mode", header_code)
        override_key = per_city_key("reg_resume_mode", header_code)
        own = bool(override_key and await get_setting(override_key))
        marker = "• " if own else ""
    else:
        current = await get_setting_typed("reg_resume_mode")
        marker = ""
    return InlineKeyboardButton(text=f"{marker}{_resume_mode_toggle_label(current)}", callback_data="reg_resume_mode_toggle")


@router.callback_query(F.data == "reg_resume_mode_toggle")
async def reg_resume_mode_toggle(callback: types.CallbackQuery):
    """Human toggle for the resume-collection mode — same right-check order as the three
    question toggles above; the composite key is the BASE reg_resume_mode key (no track
    suffix, CITYQ-01: it already resolves through cities.get_setting_typed_for_city directly)."""
    admin_id = callback.from_user.id
    header_code = await admin_selected_city(admin_id)
    per_city_ctx = bool(header_code and header_code != ALL_CITIES)

    if per_city_ctx:
        visible = await _per_city_visible_codes(admin_id)
        if header_code not in visible:
            await callback.answer("Этот город правит суперадмин", show_alert=True)
            return
        composed = per_city_key("reg_resume_mode", header_code)
        if composed is None:
            await callback.answer("Неизвестный город", show_alert=True)
            return
        current = await get_setting_typed_for_city("reg_resume_mode", header_code)
        new_val = "file_or_text" if current == "text_only" else "text_only"
        await set_setting(composed, new_val)
    else:
        current = await get_setting_typed("reg_resume_mode")
        new_val = "file_or_text" if current == "text_only" else "text_only"
        await set_setting("reg_resume_mode", new_val)

    label = _RESUME_MODE_HUMAN.get(new_val, new_val)
    await callback.answer(f"📄 Резюме: {label}{_REBUILD_HINT}", show_alert=True)
    text = await render_questions_text("full", admin_id)
    await callback.message.edit_text(text, parse_mode="HTML", reply_markup=await build_questions_keyboard("full", admin_id))


# --- «↩️ Как везде» for the questions screen, Phase 25 (CITYQ-04) ---
#
# The tapped button carries no track (ADMIN_CAPS keeps ONE exact-match entry for
# "reg_q_reset_city" — see admin_caps.py's ordering comment). The keyboard CURRENTLY shown
# already encodes which track it belongs to, via its own toggle-button callback prefixes
# (reg_q_ptoggle:/reg_q_stoggle:/reg_q_toggle:) — reading that avoids a second state channel
# (FSM or a module-level dict) just for routing this one confirm screen.
def _track_from_keyboard(markup: InlineKeyboardMarkup | None) -> str:
    if markup is not None:
        for row in markup.inline_keyboard:
            for button in row:
                cd = button.callback_data or ""
                if cd.startswith("reg_q_ptoggle:"):
                    return "party"
                if cd.startswith("reg_q_stoggle:"):
                    return "short"
    return "full"


@router.callback_query(F.data == "reg_q_reset_city")
async def reg_q_reset_city(callback: types.CallbackQuery):
    """Confirm screen for «↩️ Как везде» — same two-step confirm gate idiom as
    `menu_reset_city`: names the city and the number of questions about to lose their own
    value for the CURRENT track before deleting anything."""
    admin_id = callback.from_user.id
    if not await cities_module_on():
        await callback.answer("Города выключены", show_alert=True)
        return
    header_code = await admin_selected_city(admin_id)
    if not header_code or header_code == ALL_CITIES:
        await callback.answer("Нет своих настроек для сброса", show_alert=True)
        return
    visible = await _per_city_visible_codes(admin_id)
    if header_code not in visible:
        await callback.answer("Этот город правит суперадмин", show_alert=True)
        return

    track = _track_from_keyboard(callback.message.reply_markup)
    override_count = 0
    for _header, setting_key in _categorized_question_keys():
        override_key = _question_override_key(track, setting_key, header_code)
        if override_key and await get_setting(override_key):
            override_count += 1
    if override_count == 0:
        await callback.answer("Нет своих настроек для сброса", show_alert=True)
        return

    city_txt = await city_label(header_code)
    track_txt = {"party": " (трек Party)", "short": " (трек «Краткая»)"}.get(track, "")
    text = (
        f"Город {html_module.escape(city_txt)} снова будет показывать общий набор вопросов{track_txt};\n"
        f"свои значения {override_count} вопросов пропадут."
    )
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Да, как везде", callback_data=f"reg_q_reset_city_go:{header_code}:{track}")],
        [InlineKeyboardButton(text="← Отмена", callback_data=f"reg_q_track:{track}")],
    ])
    await callback.message.edit_text(text, parse_mode="HTML", reply_markup=kb)
    await callback.answer()


@router.callback_query(F.data.startswith("reg_q_reset_city_go:"))
async def reg_q_reset_city_go(callback: types.CallbackQuery):
    parts = callback.data.split(":")
    code = parts[1] if len(parts) > 1 else ""
    track = parts[2] if len(parts) > 2 and parts[2] in ("full", "party", "short") else "full"
    admin_id = callback.from_user.id
    if not await cities_module_on():
        await callback.answer("Города выключены", show_alert=True)
        return
    if code not in city_codes():
        await callback.answer("Неизвестный город", show_alert=True)
        return
    # T-25-11: RIGHT checked against the code carried in callback_data (not just the current
    # header) — same ordering as `menu_reset_city_go` (right check before freshness).
    visible = await _per_city_visible_codes(admin_id)
    if code not in visible:
        await callback.answer("Этот город правит суперадмин", show_alert=True)
        return
    # Freshness — the confirm screen named the header's city; if the header moved on since,
    # refuse and re-render for the NEW header instead of deleting.
    current = await admin_selected_city(admin_id)
    if code != current:
        await callback.answer("Город админки изменился — подтвердите заново.", show_alert=True)
        text = await render_questions_text(track, admin_id)
        await callback.message.edit_text(text, parse_mode="HTML", reply_markup=await build_questions_keyboard(track, admin_id))
        return

    # Idempotent -- deleting an already-absent key is a no-op, safe to repeat.
    for _header, setting_key in _categorized_question_keys():
        override_key = _question_override_key(track, setting_key, code)
        if override_key:
            await delete_setting(override_key)

    if track == "party":
        await _refresh_party_sheet_header(code)
    elif track == "short":
        await _refresh_short_sheet_header(code)
    else:
        await _refresh_sheet_header(code)

    city_txt = await city_label(code)
    await callback.answer(f"Готово: {city_txt} — как везде", show_alert=True)
    text = await render_questions_text(track, admin_id)
    await callback.message.edit_text(text, parse_mode="HTML", reply_markup=await build_questions_keyboard(track, admin_id))


@router.callback_query(F.data == "reg_q_noop")
async def reg_q_noop(callback: types.CallbackQuery):
    # Section-header button in the categorized question view — not actionable.
    await callback.answer()


@router.callback_query(F.data == "reg_q_back")
async def reg_questions_back(callback: types.CallbackQuery):
    # Phase 20 (20-04): выход с экрана «📋 Вопросы регистрации» ведёт в его раздел —
    # «📝 Анкета». Подсказка резолверу — callback_data СВОЕГО экрана (`admin_reg_questions`),
    # а не собственный `reg_q_back`: строки `reg_q_back` в SECTIONS нет и быть не должно.
    from handlers.admin_sections import settings_return_screen  # ленивый шов
    text, kb = await settings_return_screen(callback.from_user.id, callback_data="admin_reg_questions")
    await callback.message.edit_text(text, parse_mode="HTML", reply_markup=kb)
    await callback.answer()


# --- Event-type presets (one-tap bulk toggle) ---

# --- Editable question prompts (YL'26: per-event wording, 0 хардкода) ---

def _prompt_steps() -> list[tuple[str, str]]:
    """(step_key, human label) for every question whose wording can be overridden."""
    steps = [("full_name", "🪪 Фамилия и Имя")]
    for step_key, setting_key, *_ in REG_FLOW:
        steps.append((step_key, REG_LABELS.get(setting_key, step_key)))
    return steps


def _prompt_track_switcher_row(active: str) -> list[InlineKeyboardButton]:
    """Quick 260724-cfn (WR-02b): mirrors _track_switcher_row for the «Тексты вопросов»
    screen — switches between editing the global (full) prompt overrides and the
    party-track (__party) prompt overrides. Own callback namespace (reg_prompt_track:)
    so it never collides with the questions-toggle screen's reg_q_track: switcher."""
    return [
        InlineKeyboardButton(text=("• " if active == "full" else "") + "Полный", callback_data="reg_prompt_track:full"),
        InlineKeyboardButton(text=("• " if active == "party" else "") + "Party", callback_data="reg_prompt_track:party"),
    ]


def _prompt_base_key(track: str, step_key: str) -> str:
    """The plain (non-composite) `reg_prompt_*` key for one step at one track — the single
    place deciding whether the `__party` track suffix sits between `reg_prompt_{step}` and
    nothing else (Phase 25, CITYQ-05, mirrors `_question_override_key`'s track-suffix seam)."""
    return f"reg_prompt_{step_key}__party" if track == "party" else f"reg_prompt_{step_key}"


def _prompt_override_key(track: str, step_key: str, code: str) -> str | None:
    """Composite per-city key for one prompt-text step at one track. Returns `None` when
    `code` is not a real city code (`per_city_key`'s own contract)."""
    return per_city_key(_prompt_base_key(track, step_key), code)


async def render_prompts_text(track: str = "full", admin_id: int | None = None) -> str:
    """Phase 25 (CITYQ-05, WR-05): resolves the header ITSELF, once — header = real city ->
    title names the city, no city code anywhere. Header = None (module off / no admin_id
    passed) / ALL_CITIES («все города») -> today's global screen, byte-identical (untouched
    branch below)."""
    header_code = await admin_selected_city(admin_id) if admin_id is not None else None
    per_city_ctx = bool(header_code and header_code != ALL_CITIES)

    # квик 260906-7zv (HELP-01, D-1): одна строка про 💡 в обеих ветках, ДО party-блока и без
    # кодов городов — она о ключе reg_help_*, который остаётся глобальным всегда.
    _help_hint = (
        "\n\n💡 — подсказка формата под вопросом, её видит делегат в анкете. Подсказка общая: "
        "одна на все города и оба трека."
    )
    if per_city_ctx:
        city_txt = await city_label(header_code)
        text = (
            f"✏️ <b>Тексты вопросов — {html_module.escape(city_txt)}</b>\n\n"
            "Выбери вопрос и пришли свой текст ИМЕННО для этого города. ✅ — у города свой "
            "текст, ✏️ — как везде. Чтобы вернуть общий текст, отправь «-»."
            + _help_hint
        )
    else:
        text = (
            "✏️ <b>Тексты вопросов</b>\n\nВыбери вопрос и пришли свой текст. ✅ — текст переопределён, "
            "✏️ — стандартный. Чтобы вернуть стандартный, отправь «-»."
            + _help_hint
        )
    if track == "party":
        text += (
            "\n\n<i>Действуют в режиме 🎉 Party. ✏️ — берётся общий текст вопроса, "
            "✅ — переопределено для party. «-» — сброс к общему.</i>"
        )
    return text


async def build_prompts_keyboard(track: str = "full", admin_id: int | None = None):
    """Same header-aware branch as `render_prompts_text` (WR-05: this function resolves the
    header itself, ONCE). Callback data never carries the city code (T-25-14 lineage) — every
    button keeps its EXISTING callback_data regardless of header, the edit handler re-reads
    the header itself."""
    header_code = await admin_selected_city(admin_id) if admin_id is not None else None
    per_city_ctx = bool(header_code and header_code != ALL_CITIES)

    buttons = [_prompt_track_switcher_row(track)]
    for step_key, label in _prompt_steps():
        callback_data = f"reg_prompt_edit:{step_key}:party" if track == "party" else f"reg_prompt_edit:{step_key}"
        if per_city_ctx:
            override_key = _prompt_override_key(track, step_key, header_code)
            custom = bool(override_key and await get_setting(override_key))
        else:
            custom = await get_setting(_prompt_base_key(track, step_key))
        mark = "✅" if custom else "✏️"
        row = [InlineKeyboardButton(text=f"{mark} {label}", callback_data=callback_data)]
        # квик 260906-7zv (HELP-01, D-1): вторая кнопка в той же строке — только у шагов с
        # подсказкой формата (`reg_engine.has_help`). Без ✅/✏️ маркеров: они здесь уже значат
        # «у города своё», а ключ reg_help_{step} от города/трека не зависит — переиспользовать
        # их для глобального ключа было бы враньём разметкой. Суффикс `:party` в callback'е —
        # ТОЛЬКО подсказка «куда вернуться после правки», сам ключ reg_help_{step} от трека не
        # зависит (никакого reg_help_{step}__party не заводится).
        if reg_engine.has_help(step_key):
            help_callback = f"reg_help_edit:{step_key}:party" if track == "party" else f"reg_help_edit:{step_key}"
            help_custom = bool(await get_setting(f"reg_help_{step_key}"))
            help_label = "💡 Подсказка: своя" if help_custom else "💡 Подсказка: стандартная"
            row.append(InlineKeyboardButton(text=help_label, callback_data=help_callback))
        buttons.append(row)
    # Phase 20 (20-04): «Назад» ведёт в раздел-владелец этого экрана — «📝 Анкета».
    from handlers.admin_sections import back_button  # ленивый шов (цикл на уровне модуля)
    buttons.append([back_button("admin_reg_prompts")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


@router.callback_query(F.data == "admin_reg_prompts")
async def admin_reg_prompts(callback: types.CallbackQuery):
    admin_id = callback.from_user.id
    text = await render_prompts_text("full", admin_id)
    await callback.message.edit_text(text, parse_mode="HTML", reply_markup=await build_prompts_keyboard("full", admin_id))
    await callback.answer()


@router.callback_query(F.data.startswith("reg_prompt_track:"))
async def reg_prompt_track_switch(callback: types.CallbackQuery):
    """Quick 260724-cfn (WR-02b): re-renders the SAME «✏️ Тексты вопросов» message in the
    requested track context. No FSM state — mirrors reg_q_track_switch."""
    track = callback.data.split(":", 1)[1]
    if track not in ("full", "party"):
        track = "full"
    admin_id = callback.from_user.id
    text = await render_prompts_text(track, admin_id)
    await callback.message.edit_text(text, parse_mode="HTML", reply_markup=await build_prompts_keyboard(track, admin_id))
    await callback.answer()


@router.callback_query(F.data.startswith("reg_prompt_edit:"))
async def reg_prompt_edit(callback: types.CallbackQuery, state: FSMContext):
    # Quick 260724-cfn (WR-02b): optional trailing ":party" track suffix. step_keys (full_name
    # + REG_FLOW) never contain ":", so this split is safe. Any suffix other than the literal
    # "party" falls back to "full" (closed whitelist, mirrors reg_q_track_switch).
    parts = callback.data.split(":")
    step_key = parts[1]
    track = "party" if len(parts) > 2 and parts[2] == "party" else "full"
    admin_id = callback.from_user.id
    header_code = await admin_selected_city(admin_id)
    per_city_ctx = bool(header_code and header_code != ALL_CITIES)

    if per_city_ctx:
        # Phase 25 (CITYQ-05): city branch — header already resolved to a real city (implies
        # the cities module is on, admin_selected_city's own contract) -> RIGHT re-checked
        # here (T-25-15/16 lineage) -> step_key verified against the CLOSED `_prompt_steps()`
        # list BEFORE it is ever folded into a composed key (T-25-17: a crafted step is
        # rejected, never turned into a bot_settings write) -> per_city_key(...) not None.
        visible = await _per_city_visible_codes(admin_id)
        if header_code not in visible:
            await callback.answer("Этот город правит суперадмин", show_alert=True)
            return
        valid_keys = {sk for sk, _ in _prompt_steps()}
        if step_key not in valid_keys:
            await callback.answer("Неизвестный вопрос.", show_alert=True)
            return
        composed = per_city_key(_prompt_base_key(track, step_key), header_code)
        if composed is None:
            await callback.answer("Неизвестный город", show_alert=True)
            return

        city_txt = await city_label(header_code)
        own_current = await get_setting(composed)
        global_current = await get_setting(_prompt_base_key(track, step_key))
        text = f"🏙 {html_module.escape(city_txt)}\n\n"
        if own_current:
            text += f"Сейчас у города:\n<b>{html_module.escape(own_current)}</b>\n\n"
        else:
            text += "Сейчас у города: <i>как везде</i>\n\n"
        if global_current:
            text += f"Общий текст:\n<b>{html_module.escape(global_current)}</b>\n\n"
        else:
            text += "Общий текст: <i>стандартный (по умолчанию)</i>\n\n"
        text += "Пришли новый текст — будет своим ТОЛЬКО для этого города."
        text += "\n\n<i>«-» — вернуть общий текст.</i>"

        suffix = ":party" if track == "party" else ""
        rows: list[list[InlineKeyboardButton]] = []
        if own_current:
            rows.append([InlineKeyboardButton(text="↩️ Как везде", callback_data=f"reg_prompt_rst:{step_key}{suffix}")])
        rows.append([InlineKeyboardButton(text="❌ Отмена", callback_data="settings_cancel")])
        await callback.message.edit_text(text, parse_mode="HTML", reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))
        await state.set_state(EditSetting.waiting_for_value)
        # Phase 25 (CITYQ-05): FSM carries ONLY the composed key, deliberately WITHOUT the
        # sibling "return to the general per-key editor" data field settings_edit_city sets —
        # that field sends settings_edit_value to the GENERAL settings editor screen after
        # saving, and the "reg_prompts" group has no bot screen there. Return stays today's
        # settings_return_screen fallback — the same known "no screen step" limitation the
        # global (non-city) branch below already lives with.
        await state.set_data({"setting_key": composed})
        await callback.answer()
        return

    # Global branch (module off / no city header / «все города») — сегодняшний код байт-в-байт.
    key = f"reg_prompt_{step_key}__party" if track == "party" else f"reg_prompt_{step_key}"
    current = await get_setting(key)
    text = "Пришли новый текст вопроса."
    if current:
        text = f"Текущий текст: <b>{html_module.escape(current)}</b>\n\n{text}"
    text += "\n\n<i>«-» — вернуть стандартный текст.</i>"
    cancel_kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="❌ Отмена", callback_data="settings_cancel")],
    ])
    await callback.message.edit_text(text, parse_mode="HTML", reply_markup=cancel_kb)
    await state.set_state(EditSetting.waiting_for_value)
    await state.set_data({"setting_key": key})  # поток владеет данными один (admin_settings.consent_pdf_set)
    await callback.answer()


@router.callback_query(F.data.startswith("reg_prompt_rst:"))
async def reg_prompt_rst(callback: types.CallbackQuery):
    """Confirm screen for «↩️ Как везде» on the per-question text editor — same two-step
    confirm gate idiom as `menu_reset_city`/`reg_q_reset_city`: names the city and the
    question about to lose its own text before deleting anything (Phase 25, CITYQ-05)."""
    parts = callback.data.split(":")
    step_key = parts[1]
    track = "party" if len(parts) > 2 and parts[2] == "party" else "full"
    admin_id = callback.from_user.id
    if not await cities_module_on():
        await callback.answer("Города выключены", show_alert=True)
        return
    header_code = await admin_selected_city(admin_id)
    if not header_code or header_code == ALL_CITIES:
        await callback.answer("Нет своего текста для сброса", show_alert=True)
        return
    visible = await _per_city_visible_codes(admin_id)
    if header_code not in visible:
        await callback.answer("Этот город правит суперадмин", show_alert=True)
        return
    valid_keys = {sk for sk, _ in _prompt_steps()}
    if step_key not in valid_keys:
        await callback.answer("Неизвестный вопрос.", show_alert=True)
        return
    composed = per_city_key(_prompt_base_key(track, step_key), header_code)
    if composed is None or not await get_setting(composed):
        await callback.answer("Нет своего текста для сброса", show_alert=True)
        return

    city_txt = await city_label(header_code)
    label = dict(_prompt_steps()).get(step_key, step_key)
    track_txt = " (трек Party)" if track == "party" else ""
    text = (
        f"Город {html_module.escape(city_txt)} снова будет спрашивать «{html_module.escape(label)}»"
        f"{track_txt} общим текстом;\nсвой текст пропадёт."
    )
    suffix = ":party" if track == "party" else ""
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Да, как везде", callback_data=f"reg_prompt_rst_go:{header_code}:{step_key}{suffix}")],
        [InlineKeyboardButton(text="← Отмена", callback_data=f"reg_prompt_edit:{step_key}{suffix}")],
    ])
    await callback.message.edit_text(text, parse_mode="HTML", reply_markup=kb)
    await callback.answer()


@router.callback_query(F.data.startswith("reg_prompt_rst_go:"))
async def reg_prompt_rst_go(callback: types.CallbackQuery):
    parts = callback.data.split(":")
    code = parts[1] if len(parts) > 1 else ""
    step_key = parts[2] if len(parts) > 2 else ""
    track = "party" if len(parts) > 3 and parts[3] == "party" else "full"
    admin_id = callback.from_user.id
    if not await cities_module_on():
        await callback.answer("Города выключены", show_alert=True)
        return
    if code not in city_codes():
        await callback.answer("Неизвестный город", show_alert=True)
        return
    # T-25-16: RIGHT checked against the code carried in callback_data (not just the current
    # header) — same ordering as `menu_reset_city_go`/`reg_q_reset_city_go` (right check
    # before freshness).
    visible = await _per_city_visible_codes(admin_id)
    if code not in visible:
        await callback.answer("Этот город правит суперадмин", show_alert=True)
        return
    valid_keys = {sk for sk, _ in _prompt_steps()}
    if step_key not in valid_keys:
        await callback.answer("Неизвестный вопрос.", show_alert=True)
        return
    # Freshness — the confirm screen named the header's city; if the header moved on since,
    # refuse and re-render for the NEW header instead of deleting.
    current = await admin_selected_city(admin_id)
    if code != current:
        await callback.answer("Город админки изменился — подтвердите заново.", show_alert=True)
        text = await render_prompts_text(track, admin_id)
        await callback.message.edit_text(text, parse_mode="HTML", reply_markup=await build_prompts_keyboard(track, admin_id))
        return

    composed = per_city_key(_prompt_base_key(track, step_key), code)
    if composed:
        await delete_setting(composed)  # idempotent -- deleting an already-absent key is a no-op

    city_txt = await city_label(code)
    await callback.answer(f"Готово: {city_txt} — как везде", show_alert=True)
    text = await render_prompts_text(track, admin_id)
    await callback.message.edit_text(text, parse_mode="HTML", reply_markup=await build_prompts_keyboard(track, admin_id))


# --- Editable question format hints (квик 260906-7zv, HELP-01/02/03: reg_help_<step>) ------
#
# Подсказка формата под вопросом (`reg_engine.STEP_HELP`/`reg_engine.help_text`) остаётся
# ГЛОБАЛЬНОЙ — без осей города и трека (D-1): валидатор ответа (`_validate_answer_core`) один
# на все города и оба трека, подсказка обязана описывать то, что он принимает, а не отличаться
# по городу. Экран правки поэтому не повторяет per-city лестницу reg_prompt_*, а суффикс
# `:party` в callback'е ниже — только подсказка «куда вернуться после правки», не часть ключа.

async def _reg_help_guard(step_key: str, admin_id: int) -> str | None:
    """Общая преамбула reg_help_edit/reg_help_rst/reg_help_rst_go. Два условия ДО сборки ключа
    `reg_help_{step}` (T-7zv-01, линия T-25-17): шаг сверяется с закрытым `_prompt_steps()` И с
    `reg_engine.has_help` — крафченый/несуществующий шаг не превращается в запись в
    `bot_settings`. Затем право (D-3, T-7zv-02): ключ глобальный, привязанный к городу менеджер
    (`per_city_visible_codes(admin_id) != city_codes()`) его не правит — иначе он переписывает
    подсказку всем городам. Возвращает текст алерта при отказе, `None` — «можно продолжать»."""
    valid_keys = {sk for sk, _ in _prompt_steps()}
    if step_key not in valid_keys or not reg_engine.has_help(step_key):
        return "У этого вопроса нет подсказки формата."
    if await cities_module_on():
        visible = set(await _per_city_visible_codes(admin_id))
        if visible != set(city_codes()):
            return (
                "Подсказка формата общая для всех городов — её меняет суперадмин. "
                "Напишите ему, если пример в подсказке неверный."
            )
    return None


@router.callback_query(F.data.startswith("reg_help_edit:"))
async def reg_help_edit(callback: types.CallbackQuery, state: FSMContext):
    parts = callback.data.split(":")
    step_key = parts[1]
    track = "party" if len(parts) > 2 and parts[2] == "party" else "full"
    admin_id = callback.from_user.id
    denial = await _reg_help_guard(step_key, admin_id)
    if denial:
        await callback.answer(denial, show_alert=True)
        return

    label = dict(_prompt_steps()).get(step_key, step_key)
    header_code = await admin_selected_city(admin_id)
    own_current = await get_setting(f"reg_help_{step_key}")
    default = await reg_engine.help_default(step_key, header_code) or ""
    text = f"💡 <b>Подсказка формата — «{html_module.escape(label)}»</b>\n\n"
    if own_current:
        text += f"Сейчас: <b>{html_module.escape(own_current)}</b>\n\n"
    else:
        text += "Сейчас: <i>стандартная</i>\n\n"
    text += f"Стандартная: <i>{html_module.escape(default)}</i>\n\n"
    text += "Подсказка общая: одна на все города и оба трека — проверка ответа тоже одна.\n\n"
    text += (
        "Подсказка должна описывать то, что бот ПРИНИМАЕТ: если написать в ней формат, "
        "который бот не примет, делегат будет получать ошибку, делая всё по подсказке.\n\n"
    )
    text += (
        f"Пришли новый текст одним сообщением, например:\n<code>{html_module.escape(default)}</code>"
        "\n\n<i>«-» — вернуть стандартную.</i>"
    )

    suffix = ":party" if track == "party" else ""
    rows: list[list[InlineKeyboardButton]] = []
    if own_current:
        rows.append([InlineKeyboardButton(
            text="♻️ Вернуть стандартную", callback_data=f"reg_help_rst:{step_key}{suffix}",
        )])
    rows.append([InlineKeyboardButton(text="❌ Отмена", callback_data="settings_cancel")])
    await callback.message.edit_text(text, parse_mode="HTML", reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))
    await state.set_state(EditSetting.waiting_for_value)
    # Ключ ГЛОБАЛЬНЫЙ (D-1) — трек в FSM не попадает, писать/читать умеет общий
    # settings_edit_value, «-» там уже значит удаление.
    await state.set_data({"setting_key": f"reg_help_{step_key}"})
    await callback.answer()


@router.callback_query(F.data.startswith("reg_help_rst:"))
async def reg_help_rst(callback: types.CallbackQuery):
    """Confirm screen for «♻️ Вернуть стандартную» — та же двухшаговая идиома, что у
    reg_prompt_rst/menu_reset_city: называет вопрос и показывает, ЧТО встанет вместо своего,
    до удаления чего-либо."""
    parts = callback.data.split(":")
    step_key = parts[1]
    track = "party" if len(parts) > 2 and parts[2] == "party" else "full"
    admin_id = callback.from_user.id
    denial = await _reg_help_guard(step_key, admin_id)
    if denial:
        await callback.answer(denial, show_alert=True)
        return
    current = await get_setting(f"reg_help_{step_key}")
    if not current:
        await callback.answer("Подсказка и так стандартная", show_alert=True)
        return

    label = dict(_prompt_steps()).get(step_key, step_key)
    header_code = await admin_selected_city(admin_id)
    default = await reg_engine.help_default(step_key, header_code) or ""
    text = (
        f"Вопрос «{html_module.escape(label)}» снова будет показывать стандартную подсказку: "
        f"<i>{html_module.escape(default)}</i>\nСвоя подсказка пропадёт."
    )
    suffix = ":party" if track == "party" else ""
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Да, вернуть стандартную", callback_data=f"reg_help_rst_go:{step_key}{suffix}")],
        [InlineKeyboardButton(text="← Отмена", callback_data=f"reg_help_edit:{step_key}{suffix}")],
    ])
    await callback.message.edit_text(text, parse_mode="HTML", reply_markup=kb)
    await callback.answer()


@router.callback_query(F.data.startswith("reg_help_rst_go:"))
async def reg_help_rst_go(callback: types.CallbackQuery):
    parts = callback.data.split(":")
    step_key = parts[1]
    track = "party" if len(parts) > 2 and parts[2] == "party" else "full"
    admin_id = callback.from_user.id
    denial = await _reg_help_guard(step_key, admin_id)
    if denial:
        await callback.answer(denial, show_alert=True)
        return
    await delete_setting(f"reg_help_{step_key}")  # idempotent -- deleting an already-absent key is a no-op
    await callback.answer("Готово: стандартная подсказка", show_alert=True)
    text = await render_prompts_text(track, admin_id)
    await callback.message.edit_text(text, parse_mode="HTML", reply_markup=await build_prompts_keyboard(track, admin_id))

