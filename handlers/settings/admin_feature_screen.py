"""Экран одной фичи: подключено, включено, пустые тексты и три кучи.

Редкое открывается отдельно. Строки «наполовину» этот модуль не рисует.
Городское переопределение поведения сюда не входит — шапка города только
показывает уже сохранённое значение, как соседние экраны настроек.
"""
import html as html_module

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from database.db import get_setting
from domain.cities import (
    ALL_CITIES,
    admin_selected_city,
    cities_module_on,
    city_label,
    city_override_codes,
    is_per_city,
    per_city_key,
)
from domain.settings.schema import (
    FEATURE_SWITCH,
    PILE_LABELS,
    PILE_SERVICE,
    PILES,
    SETTINGS_SCHEMA,
    feature_status_lines,
    is_wiring_key,
    setting_pile,
    setting_rare,
    switch_phrase,
)
from services.forum.program import own_program_photo

# Кнопка выключателя на экране фичи. Оплата из бота уехала в приложение — там только строка.
_SWITCH_BUTTON = {
    "reg": "settings_toggle_reg",
    "party": "toggle_party_enabled",
    "consent": "toggle_consent_enabled",
}


def _settings():
    from handlers.settings import admin_settings
    return admin_settings


def _label(st, key: str) -> str:
    for field_key, label, _prompt in st.SETTINGS_FIELDS:
        if field_key == key:
            return label
    meta = SETTINGS_SCHEMA.get(key) or {}
    return meta.get("label") or "Настройка"


async def _header(admin_id):
    header_code = await admin_selected_city(admin_id) if admin_id is not None else None
    per_city_ctx = bool(header_code and header_code != ALL_CITIES)
    return header_code, per_city_ctx


async def _stored(key: str, header_code, per_city_ctx):
    if per_city_ctx and is_per_city(key):
        own_key = per_city_key(key, header_code)
        own_value = await get_setting(own_key) if own_key else None
        if own_value:
            return own_value, True
    return await get_setting(key), False


async def _flag_line(st, key: str, label: str, header_code, per_city_ctx, city_module_on: bool) -> str:
    if per_city_ctx and is_per_city(key):
        _value, own = await _stored(key, header_code, True)
        if own:
            return f"{label}: ✏️ своё"
        value = await get_setting(key)
        if value:
            common = "задано"
        elif key in st._SETTINGS_DISPLAY_DEFAULTS:
            common = "по умолчанию"
        else:
            common = "не задано"
        return f"{label}: как везде · {common}"
    value, _own = await _stored(key, header_code, False)
    if value:
        flag = "✏️ задано"
    elif key in st._SETTINGS_DISPLAY_DEFAULTS:
        flag = "<i>по умолчанию</i>"
    else:
        flag = "<i>— не задано</i>"
    city_suffix = ""
    if city_module_on and is_per_city(key):
        codes = await city_override_codes(key)
        if codes:
            city_suffix = f" · 🏙 {len(codes)}"
    return f"{label}: {flag}{city_suffix}"


async def _effectively_set(st, key: str, header_code, per_city_ctx) -> bool:
    value, own = await _stored(key, header_code, per_city_ctx)
    if own or value:
        return True
    return key in st._SETTINGS_DISPLAY_DEFAULTS


async def _status(st, token: str, keys: list[str], header_code, per_city_ctx) -> list[str]:
    wiring = [key for key in keys if is_wiring_key(key)]
    connected = True
    if wiring:
        connected = False
        for key in wiring:
            if await _effectively_set(st, key, header_code, per_city_ctx):
                connected = True
                break
    switch = FEATURE_SWITCH.get(token)
    raw = None
    if switch:
        raw, _own = await _stored(switch, header_code, per_city_ctx)
    missing = []
    for key in keys:
        if setting_pile(key) != "delegate":
            continue
        meta = SETTINGS_SCHEMA.get(key) or {}
        if meta.get("type") != "text":
            continue
        if await _effectively_set(st, key, header_code, per_city_ctx):
            continue
        missing.append(_label(st, key))
    return feature_status_lines(connected, switch_phrase(switch, raw), missing)


async def _photo_lines(st, header_code, per_city_ctx) -> list[str]:
    lines = []
    for prefix, label, _prompt in st.PHOTO_FIELDS:
        own = prefix == "program" and per_city_ctx
        photo = await (own_program_photo(header_code) if own else get_setting(f"{prefix}_photo_file_id"))
        lines.append(f"{label}: {'✅ загружена' if photo else '<i>— не задано</i>'}")
    for prefix, label, _prompt in st.FILE_FIELDS:
        photo = await get_setting(f"{prefix}_photo_file_id")
        doc = await get_setting(f"{prefix}_doc_file_id")
        lines.append(f"{label}: {'✅ загружен' if (photo or doc) else '<i>— не задано</i>'}")
    return lines


def _photo_buttons(st, filled: dict[str, bool]) -> list:
    buttons = []
    for prefix, label, _prompt in st.PHOTO_FIELDS:
        buttons.append(InlineKeyboardButton(
            text=f"📷 {label}", callback_data=f"settings_photo:{prefix}",
        ))
    for prefix, label, _prompt in st.FILE_FIELDS:
        buttons.append(InlineKeyboardButton(
            text=f"📎 {label}", callback_data=f"settings_file:{prefix}",
        ))
    return buttons


async def render_group_text(token: str, admin_id: int | None, *, rare_only: bool = False) -> str:
    st = _settings()
    header_code, per_city_ctx = await _header(admin_id)
    city_module_on = await cities_module_on()
    uni_hidden = await st.uni_controls_hidden_by_v2()
    all_keys = [
        key for key in st._settings_group_keys(token)
        if not (uni_hidden and key == st.UNI_LIST_KEY)
    ]
    group_label = st._settings_group_label(token)
    lines = []
    if per_city_ctx:
        lines.append(f"🏙 {html_module.escape(await city_label(header_code))}")
    if rare_only:
        lines += [f"⚙️ <b>Редкое → {group_label}</b>", ""]
        rare_keys = [key for key in all_keys if setting_rare(key)]
        if not rare_keys:
            lines.append("Редких настроек нет.")
        for key in rare_keys:
            lines.append(await _flag_line(
                st, key, _label(st, key), header_code, per_city_ctx, city_module_on,
            ))
        return "\n".join(lines)

    lines += [f"⚙️ <b>Настройки → {group_label}</b>", ""]
    lines += await _status(st, token, all_keys, header_code, per_city_ctx)
    lines.append("")
    shown = [key for key in all_keys if not setting_rare(key)]
    for pile in PILES:
        lines.append(f"<b>{PILE_LABELS[pile]}</b>")
        pile_keys = [key for key in shown if setting_pile(key) == pile]
        if not pile_keys and not (pile == PILE_SERVICE and token == st.PHOTO_FILE_GROUP):
            lines.append("Пока пусто.")
        for key in pile_keys:
            lines.append(await _flag_line(
                st, key, _label(st, key), header_code, per_city_ctx, city_module_on,
            ))
        if pile == PILE_SERVICE and token == st.PHOTO_FILE_GROUP:
            lines += await _photo_lines(st, header_code, per_city_ctx)
    if token == "consent":
        lines += await st.consent_group_extra_lines()
    if token == "system":
        lines.append(await st._chat_status_line())
    return "\n".join(lines)


async def _switch_button(st, token: str, header_code, per_city_ctx):
    callback = _SWITCH_BUTTON.get(token)
    switch = FEATURE_SWITCH.get(token)
    if not callback or not switch:
        return None
    raw, _own = await _stored(switch, header_code, per_city_ctx)
    phrase = switch_phrase(switch, raw)
    label = (SETTINGS_SCHEMA.get(switch) or {}).get("label") or "Включено"
    return InlineKeyboardButton(text=f"{label}: {phrase}", callback_data=callback)


async def build_group_keyboard(token: str, admin_id: int | None, *, rare_only: bool = False):
    st = _settings()
    header_code, per_city_ctx = await _header(admin_id)
    uni_hidden = await st.uni_controls_hidden_by_v2()
    all_keys = list(st._settings_group_keys(token))
    buttons: list[list[InlineKeyboardButton]] = []

    if rare_only:
        for key in all_keys:
            if uni_hidden and key == st.UNI_LIST_KEY:
                continue
            if not setting_rare(key):
                continue
            buttons.append([InlineKeyboardButton(
                text=f"✏️ {_label(st, key)}", callback_data=f"settings_edit:{key}",
            )])
        buttons.append([InlineKeyboardButton(
            text="← К экрану фичи", callback_data=f"settings_group:{token}",
        )])
        return InlineKeyboardMarkup(inline_keyboard=buttons)

    shown = [
        key for key in all_keys
        if not setting_rare(key) and not (uni_hidden and key == st.UNI_LIST_KEY)
    ]
    rare = [key for key in all_keys if setting_rare(key) and not (uni_hidden and key == st.UNI_LIST_KEY)]
    switch = await _switch_button(st, token, header_code, per_city_ctx)
    for pile in PILES:
        buttons.append([InlineKeyboardButton(
            text=PILE_LABELS[pile], callback_data="settings_group_noop",
        )])
        if pile == "work" and switch is not None:
            buttons.append([switch])
        for key in shown:
            if setting_pile(key) != pile:
                continue
            buttons.append([InlineKeyboardButton(
                text=f"✏️ {_label(st, key)}", callback_data=f"settings_edit:{key}",
            )])
        if pile == PILE_SERVICE and token == st.PHOTO_FILE_GROUP:
            for button in _photo_buttons(st, {}):
                buttons.append([button])
    if uni_hidden and st.UNI_LIST_KEY in all_keys:
        buttons.append([InlineKeyboardButton(text=st.UNI_V2_NOTE, callback_data="settings_group_noop")])
    if rare:
        buttons.append([InlineKeyboardButton(
            text="Редкое", callback_data=f"settings_group:{token}:rare",
        )])
    if token == "game":
        buttons.append([InlineKeyboardButton(
            text=await st.game_submit_notify_button_text(),
            callback_data="toggle_game_submit_notify",
        )])
        from handlers.amb.admin_amb_section import selection_toggle_button
        buttons.append([await selection_toggle_button()])
    if token == "amb":
        buttons.append([InlineKeyboardButton(
            text=await st._wave_rating_show_names_button_text(),
            callback_data="toggle_wave_rating_show_names",
        )])
    if token == "consent":
        buttons += st.consent_group_extra_buttons()
    if token == "sheets":
        from handlers.sheets.admin_sheet_tabs import sheet_tabs_group_extra_buttons
        buttons += sheet_tabs_group_extra_buttons()
    from handlers.settings.admin_sections import owner_back_button
    buttons.append([await owner_back_button(f"settings_group:{token}")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)
