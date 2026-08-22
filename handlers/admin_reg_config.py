"""Phase 13 (13-05, REFAC-01): registration-question config seam.

`admin.py:2362-3061` moved byte-for-byte, contiguous slice — the reg_q_*/reg_prompt_*/preset_*
registration-question toggle screens plus the header-aware «🔘 Кнопки главного меню» screen
(menu_toggle/menu_reset_city*) — onto the SAME shared `admin.router` (13-02/13-03/13-04/13-05
shared-router seam-import technique).
"""
import html as html_module
import logging

from aiogram import F, types
from aiogram.fsm.context import FSMContext
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from settings_schema import get_setting_typed
from database.db import get_setting, set_setting, delete_setting
from services.sheets import ensure_sheet_header
from services.background import spawn as _spawn
from keyboards.builders import MENU_BUTTONS
from handlers.states import EditSetting
from handlers.reg_schema import (
    REG_FLOW,
    REG_DEFAULTS,
    REG_LABELS,
    REG_PRESETS,
    REG_CATEGORIES,
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
)
from handlers.admin import router
from handlers.admin_consent import remind_consent_purposes_if_widened, remind_consent_purposes_after_preset
from handlers.admin_settings import render_settings_text, build_settings_keyboard, _per_city_visible_codes  # Phase 13 (13-06): settings moved out of admin.py

logger = logging.getLogger(__name__)

# INVARIANT (13-01 cap-test, extended by 13-04 to scan every handlers/admin*.py file): every
# `@router.*` decorator below MUST fit on ONE line.


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


async def render_questions_text(track: str = "full") -> str:
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


async def build_questions_keyboard(track: str = "full"):
    buttons = [_track_switcher_row(track)]
    current = None
    for header, setting_key in _categorized_question_keys():
        if header != current:
            # Non-actionable section header (noop callback).
            buttons.append([InlineKeyboardButton(text=f"── {header} ──", callback_data="reg_q_noop")])
            current = header
        label = REG_LABELS.get(setting_key, setting_key)
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
    buttons.append([InlineKeyboardButton(text="← Назад к настройкам", callback_data="reg_q_back")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


@router.callback_query(F.data == "admin_reg_questions")
async def show_reg_questions(callback: types.CallbackQuery):
    text = await render_questions_text()
    await callback.message.edit_text(text, parse_mode="HTML", reply_markup=await build_questions_keyboard())
    await callback.answer()


async def _refresh_sheet_header() -> None:
    """Regenerate the Google-sheet header after a question toggle so newly enabled
    questions show up as columns right away. The header is otherwise built only at
    startup (main.py), so mid-session toggles left the sheet missing enabled columns —
    the reported bug. Fail-soft (ensure_sheet_header swallows API/credential errors) and
    backgrounded so the admin UI stays snappy.

    NOTE: a column inserted mid-list only aligns rows appended AFTER the toggle; rows
    already in the sheet keep their original positions. Set the event type before
    delegates start registering to avoid mid-event drift."""
    try:
        headers = await active_sheet_headers()
    except Exception as e:
        logger.warning(f"_refresh_sheet_header: could not compute headers: {e}")
        return
    _spawn(ensure_sheet_header(headers))


async def _refresh_party_sheet_header() -> None:
    """MEDIUM-01: resync the party tab's physical header after a party-question toggle/preset so
    party rows appended afterwards align to the same columns as row 1. party_sheet_row recomputes
    party_sheet_headers() live per append, so a mid-event __party override otherwise shifts every
    subsequent row against the once-written startup header — a silent column misalignment.
    Mirrors _refresh_sheet_header for the main tab. GATED on party_enabled='on' (like the startup
    _maybe_ensure_party_sheet_header) so toggling a party override while the track is OFF never
    materializes the tab (D-15). Fail-soft + backgrounded."""
    from handlers.registration import party_sheet_headers, PARTY_SHEET_TAB_DEFAULT
    from services.sheets import ensure_named_sheet_header
    try:
        # REG-02 (06-05): gate read migrated to the registry; behavior unchanged.
        if (await get_setting_typed("party_enabled")) != "on":
            return
        tab = await get_setting("party_sheet_tab") or PARTY_SHEET_TAB_DEFAULT
        headers = await party_sheet_headers()
    except Exception as e:
        logger.warning(f"_refresh_party_sheet_header: could not compute headers: {e}")
        return
    _spawn(ensure_named_sheet_header(tab, headers))


async def _refresh_short_sheet_header() -> None:
    """Phase 7 (SHORT-03): resync the short (promo) tab's physical header after a __short
    question toggle/preset — mirrors _refresh_party_sheet_header exactly. GATED on
    registration_mode == 'short' (gate #5) so a tap on the short-track questions screen while
    the manager is still on «Полная» never materializes an empty promo tab. Fail-soft +
    backgrounded, local import to avoid a circular import (same idiom as the party sibling)."""
    from handlers.registration import short_sheet_headers, SHORT_SHEET_TAB_DEFAULT
    from services.sheets import ensure_named_sheet_header
    try:
        if (await get_setting_typed("registration_mode")) != "short":
            return
        tab = await get_setting("short_sheet_tab") or SHORT_SHEET_TAB_DEFAULT
        headers = await short_sheet_headers()
    except Exception as e:
        logger.warning(f"_refresh_short_sheet_header: could not compute headers: {e}")
        return
    _spawn(ensure_named_sheet_header(tab, headers))


@router.callback_query(F.data.startswith("reg_q_toggle:"))
async def toggle_reg_question(callback: types.CallbackQuery):
    setting_key = callback.data.split(":", 1)[1]

    # REG-02 (06-04): registry-driven resolution, byte-identical to the prior manual
    # (val == "on") if val is not None else REG_DEFAULTS.get(setting_key, "on") == "on" idiom.
    current_on = await get_setting_typed(setting_key)

    new_val = "off" if current_on else "on"
    await set_setting(setting_key, new_val)

    label = REG_LABELS.get(setting_key, setting_key)
    status = "✅ Вкл" if new_val == "on" else "❌ Выкл"
    await callback.answer(f"{label}: {status}", show_alert=True)

    text = await render_questions_text()
    await callback.message.edit_text(text, parse_mode="HTML", reply_markup=await build_questions_keyboard())
    await _refresh_sheet_header()  # keep the sheet header in sync with enabled questions
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
    text = await render_questions_text(track)
    await callback.message.edit_text(text, parse_mode="HTML", reply_markup=await build_questions_keyboard(track))
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

    party_key = f"{setting_key}__party"
    current = await get_setting(party_key)  # None | "on" | "off" — do NOT collapse
    new_val = _party_tri_state_advance(current)
    if new_val is None:
        await delete_setting(party_key)  # back to inherit — key ABSENCE is the inherit state
    else:
        await set_setting(party_key, new_val)
    label = _party_tri_state_label(new_val)

    await _refresh_party_sheet_header()  # MEDIUM-01: keep the party tab header aligned with the toggle
    await callback.answer(f"{REG_LABELS.get(setting_key, setting_key)} (party): {label}", show_alert=True)
    text = await render_questions_text("party")
    await callback.message.edit_text(text, parse_mode="HTML", reply_markup=await build_questions_keyboard("party"))


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

    short_key = f"{setting_key}__short"
    current = await get_setting(short_key)  # None | "on" | "off"
    new_val = "off" if current == "on" else "on"
    await set_setting(short_key, new_val)  # always an explicit write, never delete_setting
    label = "✅ Вкл" if new_val == "on" else "❌ Выкл"

    await _refresh_short_sheet_header()  # keep the short tab header aligned with the toggle
    await callback.answer(f"{REG_LABELS.get(setting_key, setting_key)} (краткая): {label}", show_alert=True)
    text = await render_questions_text("short")
    await callback.message.edit_text(text, parse_mode="HTML", reply_markup=await build_questions_keyboard("short"))


@router.callback_query(F.data == "reg_q_noop")
async def reg_q_noop(callback: types.CallbackQuery):
    # Section-header button in the categorized question view — not actionable.
    await callback.answer()


@router.callback_query(F.data == "reg_q_back")
async def reg_questions_back(callback: types.CallbackQuery):
    text = await render_settings_text(callback.from_user.id)
    await callback.message.edit_text(text, parse_mode="HTML", reply_markup=await build_settings_keyboard(callback.from_user.id))
    await callback.answer()


# --- Event-type presets (one-tap bulk toggle) ---

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
    buttons.append([InlineKeyboardButton(text="← Назад к настройкам", callback_data="reg_q_back")])
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
    key = callback.data.split(":", 1)[1]
    preset = REG_PRESETS.get(key)
    if not preset:
        await callback.answer("Неизвестный пресет.", show_alert=True)
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


async def render_prompts_text(track: str = "full") -> str:
    text = (
        "✏️ <b>Тексты вопросов</b>\n\nВыбери вопрос и пришли свой текст. ✅ — текст переопределён, "
        "✏️ — стандартный. Чтобы вернуть стандартный, отправь «-»."
    )
    if track == "party":
        text += (
            "\n\n<i>Действуют в режиме 🎉 Party. ✏️ — берётся общий текст вопроса, "
            "✅ — переопределено для party. «-» — сброс к общему.</i>"
        )
    return text


async def build_prompts_keyboard(track: str = "full"):
    buttons = [_prompt_track_switcher_row(track)]
    for step_key, label in _prompt_steps():
        if track == "party":
            key = f"reg_prompt_{step_key}__party"
            callback_data = f"reg_prompt_edit:{step_key}:party"
        else:
            key = f"reg_prompt_{step_key}"
            callback_data = f"reg_prompt_edit:{step_key}"
        custom = await get_setting(key)
        mark = "✅" if custom else "✏️"
        buttons.append([InlineKeyboardButton(text=f"{mark} {label}", callback_data=callback_data)])
    buttons.append([InlineKeyboardButton(text="← Назад к настройкам", callback_data="admin_settings")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


@router.callback_query(F.data == "admin_reg_prompts")
async def admin_reg_prompts(callback: types.CallbackQuery):
    text = await render_prompts_text("full")
    await callback.message.edit_text(text, parse_mode="HTML", reply_markup=await build_prompts_keyboard("full"))
    await callback.answer()


@router.callback_query(F.data.startswith("reg_prompt_track:"))
async def reg_prompt_track_switch(callback: types.CallbackQuery):
    """Quick 260724-cfn (WR-02b): re-renders the SAME «✏️ Тексты вопросов» message in the
    requested track context. No FSM state — mirrors reg_q_track_switch."""
    track = callback.data.split(":", 1)[1]
    if track not in ("full", "party"):
        track = "full"
    text = await render_prompts_text(track)
    await callback.message.edit_text(text, parse_mode="HTML", reply_markup=await build_prompts_keyboard(track))
    await callback.answer()


@router.callback_query(F.data.startswith("reg_prompt_edit:"))
async def reg_prompt_edit(callback: types.CallbackQuery, state: FSMContext):
    # Quick 260724-cfn (WR-02b): optional trailing ":party" track suffix. step_keys (full_name
    # + REG_FLOW) never contain ":", so this split is safe. Any suffix other than the literal
    # "party" falls back to "full" (closed whitelist, mirrors reg_q_track_switch).
    parts = callback.data.split(":")
    step_key = parts[1]
    track = "party" if len(parts) > 2 and parts[2] == "party" else "full"
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
    await state.update_data(setting_key=key)
    await callback.answer()


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

    buttons.append([InlineKeyboardButton(text="← Назад к настройкам", callback_data="menu_back")])
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
    text = await render_settings_text(callback.from_user.id)
    await callback.message.edit_text(text, parse_mode="HTML", reply_markup=await build_settings_keyboard(callback.from_user.id))
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
