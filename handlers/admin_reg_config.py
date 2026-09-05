"""Phase 13 (13-05, REFAC-01) / Phase 25 split — event-preset + main-menu-button config seam.

`admin.py:2362-3061` moved byte-for-byte, contiguous slice (13-05) onto the shared `admin.router`.
Module-size convention (tests/test_module_size_convention_260816.py) later split this file
further: the per-city question/prompt screens (`toggle_reg_question`/`toggle_party_question`/
`toggle_short_question`/`reg_prompt_*`/`reg_q_reset_city*`/`reg_resume_mode_toggle`) moved to
`handlers/admin_reg_percity.py`, imported right after this module in `handlers/admin.py` (same
registration order, same shared router — golden snapshot in test_refac_snapshot_260816.py stays
byte-for-byte, it only tracks handler name/filter/order, never the defining file).

What's left here: three Sheets-header refreshers used by BOTH seams
(`_refresh_sheet_header`/`_refresh_party_sheet_header`/`_refresh_short_sheet_header`) — this
module loads FIRST (seam-import order in `handlers/admin.py`), so `admin_reg_percity.py` imports
them back at module level; event-type presets (`_apply_event_preset`/`admin_event_preset`/
`preset_apply`/`preset_confirm` — `preset_confirm` reaches into `admin_reg_percity.py` for
`render_questions_text`/`build_questions_keyboard` via a lazy import, same idiom this file
already used for `admin_sections.settings_return_screen`); and the header-aware
«🔘 Кнопки главного меню» screen (`menu_*`).
"""
import html as html_module
import logging

from aiogram import F, types
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from settings_schema import get_setting_typed
from database.db import get_setting, set_setting, delete_setting
from services.sheets import ensure_sheet_header
from services.background import spawn as _spawn
from keyboards.builders import MENU_BUTTONS
from handlers.reg_schema import (
    REG_DEFAULTS,
    REG_LABELS,
    REG_PRESETS,
    active_sheet_headers,
    _apply_party_preset,
    _apply_short_preset,
)
from cities import (
    ALL_CITIES,
    admin_selected_city,
    cities_module_on,
    city_codes,
    city_label,
    get_setting_typed_for_city,
    per_city_key,
    enabled_cities,
    is_default_city,
)
from handlers.admin import router
from handlers.admin_consent import remind_consent_purposes_after_preset
from handlers.admin_settings import _per_city_visible_codes  # Phase 13 (13-06): settings moved out of admin.py

logger = logging.getLogger(__name__)

# INVARIANT (13-01 cap-test, extended by 13-04 to scan every handlers/admin*.py file): every
# `@router.*` decorator below MUST fit on ONE line.

async def _refresh_sheet_header(city_code: str | None = None, setting_key: str | None = None) -> None:
    """Regenerate the Google-sheet header after a question toggle so newly enabled
    questions show up as columns right away. The header is otherwise built only at
    startup (main.py), so mid-session toggles left the sheet missing enabled columns —
    the reported bug. Fail-soft (ensure_sheet_header swallows API/credential errors) and
    backgrounded so the admin UI stays snappy.

    NOTE: a column inserted mid-list only aligns rows appended AFTER the toggle; rows
    already in the sheet keep their original positions. Set the event type before
    delegates start registering to avoid mid-event drift.

    Phase 25 (CITYQ-03): two new optional args, both `None` by default — every EXISTING call
    site in this plan keeps calling with zero arguments and gets the exact behavior above
    (main tab only). A future per-city toggle screen (plan 25-04) will pass them:
    - `city_code` given (a manager toggled a question FOR one city) — touch ONLY that city's
      tab, never the main tab or any other city;
    - `city_code is None` and `setting_key` given (a GLOBAL question toggle) — main tab AS
      ABOVE, plus every ENABLED non-default city's tab that has NO OWN override for this
      setting key (`per_city_key(setting_key, code)` empty) — CONTEXT.md's Google-таблица
      decision: a global toggle must reach every tab that doesn't have its own answer already.
      A city with its own override is deliberately left untouched — the global flip did not
      change what that city sees.
    - both `None` (presets, and any other bulk writer that doesn't know a single setting key)
      — main tab only, exactly as before this phase.
    Each city tab is its own try/except (mirrors the party/short siblings below) so one city's
    Sheets failure never cancels the rest."""
    from handlers.reg_schema import city_row_tab
    from services.sheets import ensure_named_sheet_header

    if city_code is not None:
        try:
            headers = await active_sheet_headers(city_code)
            tab = await city_row_tab(city_code, None)
        except Exception as e:
            logger.warning(f"_refresh_sheet_header: could not compute headers for city={city_code!r}: {e}")
            return
        if tab is None:
            return
        _spawn(ensure_named_sheet_header(tab, headers))
        return

    try:
        headers = await active_sheet_headers()
    except Exception as e:
        logger.warning(f"_refresh_sheet_header: could not compute headers: {e}")
        return
    _spawn(ensure_sheet_header(headers))

    if setting_key is None or not await cities_module_on():
        return
    for city in await enabled_cities():
        code = city["code"]
        if is_default_city(code):
            continue
        own_key = per_city_key(setting_key, code)
        if own_key is not None and await get_setting(own_key):
            continue  # у города своё значение — глобальный тумблер его не касается
        try:
            city_headers = await active_sheet_headers(code)
            city_tab = await city_row_tab(code, None)
        except Exception as e:
            logger.warning(f"_refresh_sheet_header: could not compute headers for city={code!r}: {e}")
            continue
        if city_tab is None:
            continue
        _spawn(ensure_named_sheet_header(city_tab, city_headers))


async def _refresh_party_sheet_header(city_code: str | None = None, setting_key: str | None = None) -> None:
    """MEDIUM-01: resync the party tab's physical header after a party-question toggle/preset so
    party rows appended afterwards align to the same columns as row 1. party_sheet_row recomputes
    party_sheet_headers() live per append, so a mid-event __party override otherwise shifts every
    subsequent row against the once-written startup header — a silent column misalignment.
    Mirrors _refresh_sheet_header for the main tab. GATED on party_enabled='on' (like the startup
    _maybe_ensure_party_sheet_header) so toggling a party override while the track is OFF never
    materializes the tab (D-15). Fail-soft + backgrounded.

    Phase 25 (CITYQ-03): same `city_code`/`setting_key` contract as `_refresh_sheet_header`
    (see its docstring), with ONE difference: the "does this city already have its own
    override" check reads the TRACK-SPECIFIC key `{setting_key}__party`, not the base key —
    a city can override the party question independently of its full-track override."""
    from handlers.registration import party_sheet_headers, PARTY_SHEET_TAB_DEFAULT
    from handlers.reg_schema import city_row_tab
    from services.sheets import ensure_named_sheet_header
    try:
        # REG-02 (06-05): gate read migrated to the registry; behavior unchanged.
        if (await get_setting_typed("party_enabled")) != "on":
            return
    except Exception as e:
        logger.warning(f"_refresh_party_sheet_header: gate check failed: {e}")
        return

    if city_code is not None:
        try:
            headers = await party_sheet_headers(city_code)
            tab = await city_row_tab(city_code, "party_overnight")
        except Exception as e:
            logger.warning(f"_refresh_party_sheet_header: could not compute headers for city={city_code!r}: {e}")
            return
        if tab is None:
            return
        _spawn(ensure_named_sheet_header(tab, headers))
        return

    try:
        tab = await get_setting("party_sheet_tab") or PARTY_SHEET_TAB_DEFAULT
        headers = await party_sheet_headers()
    except Exception as e:
        logger.warning(f"_refresh_party_sheet_header: could not compute headers: {e}")
        return
    _spawn(ensure_named_sheet_header(tab, headers))

    if setting_key is None or not await cities_module_on():
        return
    track_key = f"{setting_key}__party"
    for city in await enabled_cities():
        code = city["code"]
        if is_default_city(code):
            continue
        own_key = per_city_key(track_key, code)
        if own_key is not None and await get_setting(own_key):
            continue
        try:
            city_headers = await party_sheet_headers(code)
            city_tab = await city_row_tab(code, "party_overnight")
        except Exception as e:
            logger.warning(f"_refresh_party_sheet_header: could not compute headers for city={code!r}: {e}")
            continue
        if city_tab is None:
            continue
        _spawn(ensure_named_sheet_header(city_tab, city_headers))


async def _refresh_short_sheet_header(city_code: str | None = None, setting_key: str | None = None) -> None:
    """Phase 7 (SHORT-03): resync the short (promo) tab's physical header after a __short
    question toggle/preset — mirrors _refresh_party_sheet_header exactly. GATED on
    registration_mode == 'short' (gate #5) so a tap on the short-track questions screen while
    the manager is still on «Полная» never materializes an empty promo tab. Fail-soft +
    backgrounded, local import to avoid a circular import (same idiom as the party sibling).

    Phase 25 (CITYQ-03): same `city_code`/`setting_key` contract as `_refresh_sheet_header`,
    override check against the TRACK-SPECIFIC `{setting_key}__short` key — same reasoning as
    the party sibling above."""
    from handlers.registration import short_sheet_headers, SHORT_SHEET_TAB_DEFAULT
    from handlers.reg_schema import city_row_tab
    from services.sheets import ensure_named_sheet_header
    try:
        if (await get_setting_typed("registration_mode")) != "short":
            return
    except Exception as e:
        logger.warning(f"_refresh_short_sheet_header: gate check failed: {e}")
        return

    if city_code is not None:
        try:
            headers = await short_sheet_headers(city_code)
            tab = await city_row_tab(city_code, "short")
        except Exception as e:
            logger.warning(f"_refresh_short_sheet_header: could not compute headers for city={city_code!r}: {e}")
            return
        if tab is None:
            return
        _spawn(ensure_named_sheet_header(tab, headers))
        return

    try:
        tab = await get_setting("short_sheet_tab") or SHORT_SHEET_TAB_DEFAULT
        headers = await short_sheet_headers()
    except Exception as e:
        logger.warning(f"_refresh_short_sheet_header: could not compute headers: {e}")
        return
    _spawn(ensure_named_sheet_header(tab, headers))

    if setting_key is None or not await cities_module_on():
        return
    track_key = f"{setting_key}__short"
    for city in await enabled_cities():
        code = city["code"]
        if is_default_city(code):
            continue
        own_key = per_city_key(track_key, code)
        if own_key is not None and await get_setting(own_key):
            continue
        try:
            city_headers = await short_sheet_headers(code)
            city_tab = await city_row_tab(code, "short")
        except Exception as e:
            logger.warning(f"_refresh_short_sheet_header: could not compute headers for city={code!r}: {e}")
            continue
        if city_tab is None:
            continue
        _spawn(ensure_named_sheet_header(city_tab, city_headers))


async def _apply_event_preset(preset_key: str) -> None:
    """Bulk-write reg_q_* + payment_enabled for the chosen preset. Every REG_DEFAULTS
    key is set explicitly (on if in the preset's list, else off) so the result is
    deterministic regardless of prior per-question overrides."""
    preset = REG_PRESETS[preset_key]
    on_set = set(preset["on"])
    for key in REG_DEFAULTS:
        await set_setting(key, "on" if key in on_set else "off")
    await set_setting("payment_enabled", preset["payment_enabled"])


@router.callback_query(F.data == "admin_event_preset")
async def admin_event_preset(callback: types.CallbackQuery):
    buttons = [
        [InlineKeyboardButton(text=p["label"], callback_data=f"preset_apply:{key}")]
        for key, p in REG_PRESETS.items()
    ]
    buttons.append([InlineKeyboardButton(text="← Назад", callback_data="reg_q_back")])
    await callback.message.edit_text(
        "🎛 <b>Тип события</b>\n\n"
        "Выбери пресет — он одним махом включит нужные вопросы и выключит остальные "
        "(+ настроит модуль оплаты). Экстра-вопросы потом докинешь вручную в «📋 Вопросы регистрации».\n\n"
        "⚠️ Перезатрёт текущие тумблеры вопросов.",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons),
    )
    await callback.answer()


@router.callback_query(F.data.startswith("preset_apply:"))
async def preset_apply(callback: types.CallbackQuery):
    key = callback.data.split(":", 1)[1]
    preset = REG_PRESETS.get(key)
    if not preset:
        await callback.answer("Неизвестный пресет.", show_alert=True)
        return
    on_labels = ", ".join(REG_LABELS.get(k, k) for k in preset["on"])
    # D-07: the party preset carries no "payment_enabled" key (party pricing is plan 05-05's
    # concern, not this preset's). preset.get(...) avoids a KeyError that the global
    # @dp.errors() handler would otherwise swallow silently (the admin sees nothing happen).
    payment_enabled = preset.get("payment_enabled")
    pay_line = ""
    if payment_enabled is not None:
        pay = "включится" if payment_enabled == "on" else "выключится"
        pay_line = f" Модуль оплаты <b>{pay}</b>."
    # D-07: __party/__short keys never overlap the globals a live full-form admin is looking
    # at, so neither the party nor the short (Phase 7) preset needs the "перезатрёт текущие
    # настройки" warning the forum/conf presets carry — nothing existing gets overwritten.
    warn = "" if key in ("party", "short") else "\n\n⚠️ Текущие настройки вопросов будут перезаписаны."
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Применить", callback_data=f"preset_confirm:{key}")],
        [InlineKeyboardButton(text="← Отмена", callback_data="admin_event_preset")],
    ])
    await callback.message.edit_text(
        f"Применить пресет <b>{preset['label']}</b>?\n\n"
        f"<b>Включатся:</b> {on_labels}\n"
        f"Остальные вопросы выключатся.{pay_line}"
        f"{warn}",
        parse_mode="HTML",
        reply_markup=kb,
    )
    await callback.answer()


@router.callback_query(F.data.startswith("preset_confirm:"))
async def preset_confirm(callback: types.CallbackQuery):
    # Phase 25 (module-size split): render_questions_text/build_questions_keyboard now live in
    # admin_reg_percity.py -- lazy import avoids a load-time cycle (that module imports the
    # _refresh_*_sheet_header trio back from THIS module, which must finish loading first).
    from handlers.admin_reg_percity import render_questions_text, build_questions_keyboard
    key = callback.data.split(":", 1)[1]
    preset = REG_PRESETS.get(key)
    if not preset:
        await callback.answer("Неизвестный пресет.", show_alert=True)
        return
    # T-25-13: a preset is a GLOBAL bulk-writer (_apply_event_preset/_apply_party_preset/
    # _apply_short_preset all write the base/track key, never a per-city composite) — applying
    # it while the header is scoped to one city would silently rewrite EVERY city's effective
    # question set without the manager ever seeing that. Early exit, no state changed.
    admin_id = callback.from_user.id
    header_code = await admin_selected_city(admin_id)
    if header_code and header_code != ALL_CITIES:
        await callback.answer(
            "Пресет меняет набор вопросов для всех городов. Переключи шапку на «🌍 Все города», "
            "если правда этого хочешь.",
            show_alert=True,
        )
        return
    if key == "party":
        # D-07: route to the isolated __party-only bulk writer. _apply_event_preset writes
        # GLOBAL reg_q_* keys for every REG_DEFAULTS entry — routing the party key there
        # would erase the live full-delegate question set, exactly what D-07 exists to prevent.
        await _apply_party_preset()
        await callback.answer(f"Пресет применён: {preset['label']}", show_alert=True)
        text = await render_questions_text("party")
        await callback.message.edit_text(
            text, parse_mode="HTML", reply_markup=await build_questions_keyboard("party")
        )
        # No _refresh_sheet_header(): the party preset changes no global setting, so the main
        # sheet header cannot have drifted. MEDIUM-01: but the party preset DOES change the
        # __party question set, so resync the PARTY tab's own header (plan 05-06).
        await _refresh_party_sheet_header()
        return
    if key == "short":
        # Phase 7 (SHORT-03): route to the isolated __short-only bulk writer, same reasoning
        # as the party branch above — _apply_event_preset writes GLOBAL reg_q_* keys, which
        # would erase the live full-delegate question set. The promo preset changes no global
        # setting, so the MAIN sheet header cannot have drifted — only the SHORT tab's own
        # header needs a resync.
        await _apply_short_preset()
        await callback.answer(f"Пресет применён: {preset['label']}", show_alert=True)
        text = await render_questions_text("short")
        await callback.message.edit_text(
            text, parse_mode="HTML", reply_markup=await build_questions_keyboard("short")
        )
        await _refresh_short_sheet_header()
        return
    await _apply_event_preset(key)
    await callback.answer(f"Пресет применён: {preset['label']}", show_alert=True)
    text = await render_questions_text()
    await callback.message.edit_text(text, parse_mode="HTML", reply_markup=await build_questions_keyboard())
    await _refresh_sheet_header()  # preset flips many questions → resync the sheet header
    # Quick 260822: пресет меняет состав обработки (оплата, резюме) — менеджеру напоминание
    # сверить текст согласия с новыми целями.
    await remind_consent_purposes_after_preset(callback.message, preset["label"])



# --- Menu Button Toggles ---
#
# Phase 09.3 (07, CITY-09): header-aware, single screen. Folds in 09.2-06's separate
# per-city entry point and its whole picker sub-flow (list screen, per-city text/keyboard
# builders, pick/toggle/clear/clear-go handlers — all deleted) — mirrors the exact merge
# shape 09.3-06 already applied to the per-key settings editor (_settings_edit_screen): one
# render helper branching on an ALREADY-RESOLVED header (WR-05), no separate picker screen.
# The has-override helper below is kept (still needed by the merged keyboard's «↩️ Все как
# везде» row).

async def render_menu_text(admin_id: int | None = None) -> str:
    """Header = real city -> title names the city, every row shows the city's EFFECTIVE
    value (`get_setting_typed_for_city`) plus a «(своё)»/«(как везде)» mark. Header = None
    (module off, or no admin_id passed) / ALL_CITIES («все города») -> today's global
    screen, byte-identical (09.2-06: menu_* is a registry `enum` key, options ["on","off"],
    default "on" — the enum branch of `_parse_setting` is `raw if raw else default`, so
    None/"" -> "on", any other value (including "off"/garbage) -> not "on", matching the
    pre-registry `val is None or val == "on"` idiom)."""
    header_code = await admin_selected_city(admin_id) if admin_id is not None else None
    if header_code and header_code != ALL_CITIES:
        city_txt = await city_label(header_code)
        lines = [f"🔘 <b>Кнопки главного меню — {html_module.escape(city_txt)}</b>", ""]
        for key, text in MENU_BUTTONS:
            is_on = await get_setting_typed_for_city(key, header_code) == "on"
            override_key = per_city_key(key, header_code)
            own = bool(override_key and await get_setting(override_key))
            status = "✅" if is_on else "❌"
            mark = " <i>(своё)</i>" if own else " <i>(как везде)</i>"
            lines.append(f"{status} {text}{mark}")
        return "\n".join(lines)

    lines = ["🔘 <b>Кнопки главного меню</b>", ""]
    for key, text in MENU_BUTTONS:
        is_on = await get_setting_typed(key) == "on"
        status = "✅" if is_on else "❌"
        lines.append(f"{status} {text}")
    return "\n".join(lines)


async def build_menu_keyboard(admin_id: int | None = None):
    """Same header-aware branch as `render_menu_text` (WR-05: this function resolves the
    header itself, ONCE — every caller below passes its own already-known `admin_id`, never
    a pre-resolved code, matching the settings-group-screen precedent)."""
    header_code = await admin_selected_city(admin_id) if admin_id is not None else None
    per_city_ctx = bool(header_code and header_code != ALL_CITIES)

    buttons = []
    for key, text in MENU_BUTTONS:
        if per_city_ctx:
            is_on = await get_setting_typed_for_city(key, header_code) == "on"
        else:
            is_on = await get_setting_typed(key) == "on"
        toggle_text = f"{'✅' if is_on else '❌'} {text}"
        buttons.append([InlineKeyboardButton(text=toggle_text, callback_data=f"menu_toggle:{key}")])

    if per_city_ctx and await _menu_city_has_override(header_code):
        buttons.append([InlineKeyboardButton(text="↩️ Все как везде", callback_data="menu_reset_city")])

    buttons.append([InlineKeyboardButton(text="← Назад", callback_data="menu_back")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


@router.callback_query(F.data == "admin_menu_buttons")
async def show_menu_buttons(callback: types.CallbackQuery):
    admin_id = callback.from_user.id
    text = await render_menu_text(admin_id)
    await callback.message.edit_text(text, parse_mode="HTML", reply_markup=await build_menu_keyboard(admin_id))
    await callback.answer()


@router.callback_query(F.data.startswith("menu_toggle:"))
async def toggle_menu_button(callback: types.CallbackQuery):
    key = callback.data.split(":", 1)[1]
    admin_id = callback.from_user.id
    # WR-05: single header read for this handler; both the write branch and the redraw
    # below reuse `admin_id`, never a second `admin_selected_city` call.
    header_code = await admin_selected_city(admin_id)
    per_city_ctx = bool(header_code and header_code != ALL_CITIES)

    if per_city_ctx:
        # T-093-24: RIGHT re-checked here, not just via a hidden button.
        visible = await _per_city_visible_codes(admin_id)
        if header_code not in visible:
            await callback.answer("Этот город правит суперадмин", show_alert=True)
            return
        if key not in {k for k, _ in MENU_BUTTONS}:
            await callback.answer("Неизвестная кнопка", show_alert=True)
            return
        # T-093-25: composed key comes ONLY from cities.per_city_key.
        composed = per_city_key(key, header_code)
        if composed is None:
            await callback.answer("Неизвестный город", show_alert=True)
            return
        current_on = await get_setting_typed_for_city(key, header_code) == "on"
        new_val = "off" if current_on else "on"
        await set_setting(composed, new_val)
        label = dict(MENU_BUTTONS).get(key, key)
        city_txt = await city_label(header_code)
        status = "✅ Вкл" if new_val == "on" else "❌ Выкл"
        await callback.answer(f"{label} — {city_txt}: {status}", show_alert=True)
    else:
        current_on = await get_setting_typed(key) == "on"
        new_val = "off" if current_on else "on"
        await set_setting(key, new_val)
        label = dict(MENU_BUTTONS).get(key, key)
        status = "✅ Вкл" if new_val == "on" else "❌ Выкл"
        await callback.answer(f"{label}: {status}", show_alert=True)

    text = await render_menu_text(admin_id)
    await callback.message.edit_text(text, parse_mode="HTML", reply_markup=await build_menu_keyboard(admin_id))


@router.callback_query(F.data == "menu_back")
async def menu_buttons_back(callback: types.CallbackQuery):
    # Phase 20 (20-04): выход с экрана «🔘 Кнопки меню» ведёт в его раздел — «🎪 Событие»
    # (подсказка — callback_data своего экрана, см. комментарий у reg_questions_back).
    from handlers.admin_sections import settings_return_screen  # ленивый шов
    text, kb = await settings_return_screen(callback.from_user.id, callback_data="admin_menu_buttons")
    await callback.message.edit_text(text, parse_mode="HTML", reply_markup=kb)
    await callback.answer()


async def _menu_city_has_override(code: str) -> bool:
    """True if the city has at least one of the 9 menu_* keys overridden. Still needed by
    the merged keyboard's «↩️ Все как везде» row (09.2-06 lineage, kept verbatim)."""
    for key, _ in MENU_BUTTONS:
        override_key = per_city_key(key, code)
        if override_key and await get_setting(override_key):
            return True
    return False


@router.callback_query(F.data == "menu_reset_city")
async def menu_reset_city(callback: types.CallbackQuery):
    """Confirm screen for «↩️ Все как везде» on the header-scoped menu-buttons screen — same
    two-step confirm gate idiom as `settings_reset_city`/09.2-06's now-deleted per-city
    reset confirm screen: names the number of buttons about to lose their own setting
    before deleting anything."""
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

    override_count = 0
    for key, _ in MENU_BUTTONS:
        override_key = per_city_key(key, header_code)
        if override_key and await get_setting(override_key):
            override_count += 1
    if override_count == 0:
        await callback.answer("Нет своих настроек для сброса", show_alert=True)
        return

    city_txt = await city_label(header_code)
    text = (
        f"Город {html_module.escape(city_txt)} снова будет показывать общий набор кнопок;\n"
        f"свои настройки {override_count} кнопок пропадут."
    )
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Да, как везде", callback_data=f"menu_reset_city_go:{header_code}")],
        [InlineKeyboardButton(text="← Отмена", callback_data="admin_menu_buttons")],
    ])
    await callback.message.edit_text(text, parse_mode="HTML", reply_markup=kb)
    await callback.answer()


@router.callback_query(F.data.startswith("menu_reset_city_go:"))
async def menu_reset_city_go(callback: types.CallbackQuery):
    code = callback.data.split(":", 1)[1]
    admin_id = callback.from_user.id
    if not await cities_module_on():
        await callback.answer("Города выключены", show_alert=True)
        return
    if code not in city_codes():
        await callback.answer("Неизвестный город", show_alert=True)
        return
    # T-093-24: RIGHT check against the code carried in callback_data (not just the current
    # header) — catches a bound manager's forged confirmation for another city, same
    # ordering as `settings_reset_city_go` (right check before freshness).
    visible = await _per_city_visible_codes(admin_id)
    if code not in visible:
        await callback.answer("Этот город правит суперадмин", show_alert=True)
        return
    # T-093-26: freshness — the confirm screen named the header's city; if the header moved
    # on since, refuse and re-render the menu screen for the NEW header instead of deleting.
    current = await admin_selected_city(admin_id)
    if code != current:
        await callback.answer("Город админки изменился — подтвердите заново.", show_alert=True)
        text = await render_menu_text(admin_id)
        await callback.message.edit_text(text, parse_mode="HTML", reply_markup=await build_menu_keyboard(admin_id))
        return

    # Idempotent -- deleting an already-absent key is a no-op, safe to repeat.
    for key, _ in MENU_BUTTONS:
        override_key = per_city_key(key, code)
        if override_key:
            await delete_setting(override_key)

    city_txt = await city_label(code)
    await callback.answer(f"Готово: {city_txt} — как везде", show_alert=True)
    text = await render_menu_text(admin_id)
    await callback.message.edit_text(text, parse_mode="HTML", reply_markup=await build_menu_keyboard(admin_id))
