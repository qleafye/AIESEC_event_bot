"""Phase 19.1 Plan 07 (D-02/D-03/D-04/D-08/D-15/D-16/D-20): второй шов «🎨 Оформление» Mini
App — пресеты (BlueBook/YouLead/Своя) и ручки кастома.

Регистрирует хендлеры на общий `router` владельца (`handlers.admin`, техника 13-02) и
импортируется из ХВОСТА `handlers/admin_settings.py`, СРАЗУ ПОСЛЕ `admin_miniapp` (та же
техника шва, что и остальные модули Phase 13/15/19). Вынесен в отдельный файл от
`handlers/admin_miniapp.py` из-за потолка размера модуля (CONVENTIONS.md) — вход сюда идёт
кнопкой «🎭 Пресеты и ручки оформления» с первого экрана «🎨 Оформление».

Правило D-03 (пресет — стартовая точка, не жёсткий шаблон): применение пресета пишет ВСЕ его
ручки разом (`web_theme.PRESETS[name]`) плюс сам `miniapp_theme_preset`; «Своя (на базе X)» не
хранится отдельным флагом, а вычисляется сравнением текущих значений ручек с дефолтами базового
пресета через `web_theme.resolve_theme` — тот же приём, что и в `/app/theme.css`/`/theme.css`.

Правило «бот для людей» (CLAUDE.md): код ключа реестра, имя пресета (`bluebook`/`youlead`) и
код шрифта (`raleway_italic` и т.п.) нигде не показываются человеку напрямую — только через
человеческие подписи (`_PRESET_LABELS`/`_FONT_LABELS`/`_COLOR_LABELS`/`_ASSET_SLOT_BY_NAME`).
"""
import html as html_module
import logging
import re
from pathlib import Path

from aiogram import F, types
from aiogram.filters import StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State
from aiogram.types import FSInputFile, InlineKeyboardButton, InlineKeyboardMarkup

import web_theme
from database.db import set_setting, delete_setting
from settings_schema import get_setting_typed
from handlers.admin import router
from handlers.states import MiniAppTheme

logger = logging.getLogger(__name__)

# `^#[0-9A-Fa-f]{6}$` — та же серверная валидация, что в `web_theme` (T-19.1-26): один регекс
# для входного текста и для того, что реально попадает в CSS.
_HEX_RE = re.compile(r"^#[0-9A-Fa-f]{6}$")

# tokens.css `:root --text` (light-тема, BRAND чёрный) — базовая краска текста, против которой
# проверяется контраст ручки «Цвет фона» (текст сидит НА фоне). Ручки «Акцент»/«Вторичный»
# проверяются против текущего `bg` — они, как правило, сами служат текстом/иконкой на фоне.
_TEXT_INK = "#1C1C1C"

PREVIEW_DIR = Path(__file__).resolve().parent.parent / "assets" / "theme-preview"

# D-02: только два реальных пресета (RealTalk не заводим — нет бренд-материалов).
_PRESET_LABELS: dict[str, str] = {
    "bluebook": "AIESEC BlueBook",
    "youlead": "YouLead",
}
_PRESET_BLURBS: dict[str, str] = {
    "bluebook": "строгий AIESEC: синий акцент, воздух, крупная типографика Raleway.",
    "youlead": "игривый, на «ты», с мемами: оранжево-красный акцент, курсивные заголовки.",
}

_COLOR_LABELS: dict[str, str] = {
    "accent": "Акцент",
    "secondary": "Вторичный цвет",
    "bg": "Цвет фона",
}

# RESEARCH Q3 (закрытый список): физически в проекте вшиты только эти три начертания.
_FONT_LABELS: dict[str, str] = {
    "raleway": "Raleway — строгий",
    "raleway_italic": "Raleway курсив — игривый",
    "lato": "Lato — нейтральный",
}

# «Имя слота» (в callback_data и в FSM-состоянии) -> (State, ключ реестра, подпись, где видно).
# Единственное место, где перечислены девять фото-ручек — клавиатура/загрузка/удаление читают
# ровно этот список, ничего не дублируется руками во второй раз.
_ASSET_SLOTS: list[tuple[str, State, str, str, str]] = [
    ("logo", MiniAppTheme.logo, "miniapp_logo", "Лого", "в шапке приложения"),
    ("logo_dark", MiniAppTheme.logo_dark, "miniapp_logo_dark", "Лого для тёмной темы",
     "в шапке приложения в тёмной теме — необязательно, без него используется обычное лого"),
    ("cover", MiniAppTheme.cover, "miniapp_cover", "Обложка",
     "на привет-экране делегата и тонкой полосой в шапке дашборда"),
    ("cover_dark", MiniAppTheme.cover_dark, "miniapp_cover_dark", "Обложка для тёмной темы",
     "необязательно, без неё используется обычная обложка"),
    ("sticker_empty", MiniAppTheme.sticker_empty, "miniapp_sticker_empty", "Стикер «пусто»",
     "в пустых списках заданий/монет/рейтинга"),
    ("sticker_success", MiniAppTheme.sticker_success, "miniapp_sticker_success", "Стикер «успех»",
     "на экране принятой сдачи"),
    ("sticker_error", MiniAppTheme.sticker_error, "miniapp_sticker_error", "Стикер «ошибка»",
     "на экране ошибки"),
    ("sticker_top1", MiniAppTheme.sticker_top1, "miniapp_sticker_top1", "Стикер «топ-1»",
     "у делегата на первом месте рейтинга"),
    ("coin_icon", MiniAppTheme.coin_icon, "miniapp_coin_icon", "Своя иконка монеты",
     "вместо вшитой иконки пресета — необязательно"),
]
_ASSET_SLOT_BY_NAME: dict[str, tuple[State, str, str, str]] = {
    name: (state, key, label, hint) for name, state, key, label, hint in _ASSET_SLOTS
}
_ASSET_SLOT_BY_STATE_SUFFIX: dict[str, tuple[str, str, str]] = {
    name: (key, label, hint) for name, _state, key, label, hint in _ASSET_SLOTS
}


def _swatch_emoji(hex_color: str) -> str:
    """Грубое приближение HEX к одному из девяти цветных квадратов Telegram (нет доступа к
    произвольной палитре в тексте сообщения) — «квадратик-эмодзи» из D-04, не точный цвет."""
    r, g, b = (int(hex_color[i:i + 2], 16) for i in (1, 3, 5))
    if r > 235 and g > 235 and b > 235:
        return "⬜"
    if r < 25 and g < 25 and b < 25:
        return "⬛"
    if max(r, g, b) - min(r, g, b) < 20:
        return "⬛" if (r + g + b) / 3 < 128 else "⬜"
    if r >= g and r >= b:
        return "🟧" if g >= b else "🟥"
    if g >= r and g >= b:
        return "🟨" if r >= b else "🟩"
    return "🟪" if r >= g else "🟦"


async def _contrast_note(handle: str, value: str) -> str:
    """D-04: контраст объясняется словами, а не отказом — плохой контраст НЕ блокирует
    сохранение. `bg` проверяется против фиксированной краски текста (`_TEXT_INK`); `accent`/
    `secondary` — против текущего значения `bg` (они сами обычно служат текстом/иконкой)."""
    if handle == "bg":
        ratio = web_theme.contrast_ratio(_TEXT_INK, value)
    else:
        bg = await get_setting_typed(web_theme.THEME_KEYS["bg"])
        if not isinstance(bg, str) or not _HEX_RE.match(bg):
            bg = web_theme.PRESETS[web_theme.DEFAULT_PRESET]["bg"]
        ratio = web_theme.contrast_ratio(value, bg)
    if ratio >= 4.5:
        return "Текст на этом фоне читается."
    return f"Слишком бледно: на этом фоне контраст {ratio:.1f}, нужно от 4.5 — возьмите темнее."


async def _current_preset_and_custom() -> tuple[str, bool]:
    """D-03: «Своя» вычисляется сравнением, не хранится отдельным флагом."""
    settings = {key: await get_setting_typed(key) for key in web_theme.THEME_KEYS.values()}
    resolved = web_theme.resolve_theme(settings)
    preset_name = resolved["preset"]
    preset_defaults = web_theme.PRESETS[preset_name]
    is_custom = any(
        resolved[handle] != preset_defaults[handle]
        for handle in ("accent", "secondary", "bg", "heading_font", "playful_tone", "pattern_enabled")
    )
    return preset_name, is_custom


async def _resolved_handles() -> dict:
    settings = {key: await get_setting_typed(key) for key in web_theme.THEME_KEYS.values()}
    return web_theme.resolve_theme(settings)


# ── рендер экрана ────────────────────────────────────────────────────────────────────────

async def render_miniapp_theme_text() -> str:
    preset_name, is_custom = await _current_preset_and_custom()
    label = _PRESET_LABELS.get(preset_name, preset_name)
    header = f"Пресет: Своя (на базе {label})" if is_custom else f"Пресет: {label}"

    resolved = await _resolved_handles()
    font_label = _FONT_LABELS.get(resolved["heading_font"], resolved["heading_font"])
    playful = resolved["playful_tone"] == "on"
    pattern = resolved["pattern_enabled"] == "on"

    lines = ["🎭 <b>Пресеты и ручки оформления</b>", "", f"<b>{header}</b>", ""]
    lines.append(f"🎨 Акцент: <b>{html_module.escape(resolved['accent'])}</b> {_swatch_emoji(resolved['accent'])}")
    lines.append(f"🎨 Вторичный: <b>{html_module.escape(resolved['secondary'])}</b> {_swatch_emoji(resolved['secondary'])}")
    lines.append(f"🎨 Фон: <b>{html_module.escape(resolved['bg'])}</b> {_swatch_emoji(resolved['bg'])}")
    lines.append(f"🔤 Шрифт заголовков: <b>{html_module.escape(font_label)}</b>")
    lines.append(("✅" if playful else "☐") + " Игривый тон текстов")
    lines.append(("✅" if pattern else "☐") + " Бренд-паттерн на фоне")
    lines.append("")
    lines.append("Картинки — лого/обложка/стикеры/иконка монеты — кнопками ниже.")
    return "\n".join(lines)


def _asset_slot_button_rows(slot_name: str, current_value) -> list[list[InlineKeyboardButton]]:
    _state, _key, label, _hint = _ASSET_SLOT_BY_NAME[slot_name]
    mark = "✅ " if current_value else "☐ "
    rows = [[InlineKeyboardButton(
        text=f"{mark}🖼 {label}", callback_data=f"miniapp_theme_photo:{slot_name}",
    )]]
    if current_value:
        rows.append([InlineKeyboardButton(
            text=f"🗑 Убрать: {label}", callback_data=f"miniapp_theme_remove_photo:{slot_name}",
        )])
    return rows


async def build_miniapp_theme_keyboard() -> InlineKeyboardMarkup:
    preset_name, is_custom = await _current_preset_and_custom()
    resolved = await _resolved_handles()

    rows: list[list[InlineKeyboardButton]] = []

    # ── пресеты (D-20) ──────────────────────────────────────────────────────────────────
    for name in ("bluebook", "youlead"):
        mark = "✅ " if (preset_name == name and not is_custom) else ""
        rows.append([InlineKeyboardButton(
            text=mark + _PRESET_LABELS[name], callback_data=f"miniapp_preset:{name}",
        )])
    rows.append([InlineKeyboardButton(
        text=("✅ " if is_custom else "") + "Своя", callback_data="miniapp_theme_noop",
    )])
    if is_custom:
        rows.append([InlineKeyboardButton(
            text="↩️ Сбросить к пресету", callback_data="miniapp_theme_reset",
        )])

    # ── D-04, 1: три цвета ──────────────────────────────────────────────────────────────
    for handle in ("accent", "secondary", "bg"):
        rows.append([InlineKeyboardButton(
            text=f"🎨 {_COLOR_LABELS[handle]}: {resolved[handle]}",
            callback_data=f"miniapp_theme_color:{handle}",
        )])

    # ── D-04, 2: шрифт заголовков ───────────────────────────────────────────────────────
    for font_key, font_label in _FONT_LABELS.items():
        mark = "✅ " if font_key == resolved["heading_font"] else "☐ "
        rows.append([InlineKeyboardButton(
            text=mark + font_label, callback_data=f"miniapp_theme_font:{font_key}",
        )])

    # ── D-04, 3: тон текстов ────────────────────────────────────────────────────────────
    playful_mark = "✅ " if resolved["playful_tone"] == "on" else "☐ "
    rows.append([InlineKeyboardButton(
        text=playful_mark + "Игривый тон текстов", callback_data="miniapp_theme_toggle_playful",
    )])

    # ── D-04, 4: лого (светлое + тёмное) ────────────────────────────────────────────────
    for slot_name in ("logo", "logo_dark"):
        _state, key, _label, _hint = _ASSET_SLOT_BY_NAME[slot_name]
        rows.extend(_asset_slot_button_rows(slot_name, await get_setting_typed(key)))

    # ── D-04, 5: обложка (светлая + тёмная) ─────────────────────────────────────────────
    for slot_name in ("cover", "cover_dark"):
        _state, key, _label, _hint = _ASSET_SLOT_BY_NAME[slot_name]
        rows.extend(_asset_slot_button_rows(slot_name, await get_setting_typed(key)))

    # ── D-04, 6: бренд-паттерн ──────────────────────────────────────────────────────────
    pattern_mark = "✅ " if resolved["pattern_enabled"] == "on" else "☐ "
    rows.append([InlineKeyboardButton(
        text=pattern_mark + "Бренд-паттерн на фоне", callback_data="miniapp_theme_toggle_pattern",
    )])

    # ── D-04, 7: четыре стикера ──────────────────────────────────────────────────────────
    for slot_name in ("sticker_empty", "sticker_success", "sticker_error", "sticker_top1"):
        _state, key, _label, _hint = _ASSET_SLOT_BY_NAME[slot_name]
        rows.extend(_asset_slot_button_rows(slot_name, await get_setting_typed(key)))

    # ── D-04, 8: иконка монеты ───────────────────────────────────────────────────────────
    _state, coin_key, _label, _hint = _ASSET_SLOT_BY_NAME["coin_icon"]
    rows.extend(_asset_slot_button_rows("coin_icon", await get_setting_typed(coin_key)))

    rows.append([InlineKeyboardButton(text="← К оформлению", callback_data="admin_miniapp_settings")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def _rerender_theme(callback: types.CallbackQuery) -> None:
    await callback.message.edit_text(
        await render_miniapp_theme_text(),
        parse_mode="HTML",
        reply_markup=await build_miniapp_theme_keyboard(),
    )


def _cancel_edit_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="❌ Отмена", callback_data="miniapp_theme_cancel_edit")],
    ])


# ── вход на экран ────────────────────────────────────────────────────────────────────────

@router.callback_query(F.data == "miniapp_theme_open")
async def open_miniapp_theme(callback: types.CallbackQuery, state: FSMContext):
    # Defensive clear (тот же приём, что `open_miniapp_settings`): заход не должен оставлять
    # зависшую FSM правки с прошлого визита.
    await state.clear()
    await callback.message.edit_text(
        await render_miniapp_theme_text(),
        parse_mode="HTML",
        reply_markup=await build_miniapp_theme_keyboard(),
    )
    await callback.answer()


@router.callback_query(F.data == "miniapp_theme_noop")
async def miniapp_theme_noop(callback: types.CallbackQuery):
    await callback.answer()


@router.callback_query(F.data == "miniapp_theme_cancel_edit")
async def miniapp_theme_cancel_edit(callback: types.CallbackQuery, state: FSMContext):
    await state.set_state(None)
    await callback.answer()
    await _rerender_theme(callback)


# ── пресеты: выбор кнопкой, превью картинкой, применение с подтверждением (D-20) ──────────

@router.callback_query(F.data.startswith("miniapp_preset:"))
async def miniapp_preset_pick(callback: types.CallbackQuery):
    name = callback.data.split(":", 1)[1]
    if name not in web_theme.PRESETS:
        await callback.answer("Неизвестная кнопка", show_alert=True)
        return

    caption = (
        f"🎭 <b>{html_module.escape(_PRESET_LABELS[name])}</b>\n\n"
        f"{html_module.escape(_PRESET_BLURBS[name])}\n\n"
        "Применить этот пресет? Цвета, шрифт заголовков, тон и бренд-паттерн встанут по нему; "
        "загруженные лого, обложка и стикеры не изменятся."
    )
    kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="✅ Применить", callback_data=f"miniapp_preset_apply:{name}"),
        InlineKeyboardButton(text="❌ Отмена", callback_data="miniapp_preset_cancel"),
    ]])

    # T-19.1-28 (fail-soft): нет файла превью — не роняем экран, задаём тот же вопрос текстом.
    preview_path = PREVIEW_DIR / f"{name}.png"
    sent_photo = False
    if preview_path.is_file():
        try:
            await callback.message.answer_photo(
                FSInputFile(str(preview_path)), caption=caption, parse_mode="HTML", reply_markup=kb,
            )
            sent_photo = True
        except Exception:
            logger.warning("miniapp theme preview send failed for preset=%s", name, exc_info=True)
    if not sent_photo:
        await callback.message.answer(caption, parse_mode="HTML", reply_markup=kb)
    await callback.answer()


@router.callback_query(F.data.startswith("miniapp_preset_apply:"))
async def miniapp_preset_apply(callback: types.CallbackQuery):
    name = callback.data.split(":", 1)[1]
    if name not in web_theme.PRESETS:
        await callback.answer("Неизвестная кнопка", show_alert=True)
        return
    # D-03: применение пишет ВСЕ ручки пресета разом, одним проходом, плюс сам ключ пресета —
    # частичная запись оставила бы reзе рассинхрон, задокументированный в 19.1-02-SUMMARY.
    for handle, value in web_theme.PRESETS[name].items():
        await set_setting(web_theme.THEME_KEYS[handle], value)
    await set_setting(web_theme.THEME_KEYS["preset"], name)
    await callback.answer(f"Применено: {_PRESET_LABELS[name]}")
    await callback.message.answer(
        await render_miniapp_theme_text(),
        parse_mode="HTML",
        reply_markup=await build_miniapp_theme_keyboard(),
    )


@router.callback_query(F.data == "miniapp_preset_cancel")
async def miniapp_preset_cancel(callback: types.CallbackQuery):
    await callback.answer("Отменено")


# ── сброс к пресету (D-03), с подтверждением и перечислением последствий ──────────────────

@router.callback_query(F.data == "miniapp_theme_reset")
async def miniapp_theme_reset_start(callback: types.CallbackQuery):
    preset_name, is_custom = await _current_preset_and_custom()
    if not is_custom:
        await callback.answer("Уже на пресете — сбрасывать нечего", show_alert=True)
        return
    label = _PRESET_LABELS.get(preset_name, preset_name)
    text = (
        f"↩️ <b>Сбросить к пресету «{html_module.escape(label)}»?</b>\n\n"
        "Вернутся: цвета (акцент/вторичный/фон), шрифт заголовков, тон текстов, бренд-паттерн.\n"
        "НЕ тронется: загруженные лого, обложка, стикеры и иконка монеты — они не часть пресета."
    )
    kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="✅ Да, сбросить", callback_data="miniapp_theme_reset_go"),
        InlineKeyboardButton(text="❌ Отмена", callback_data="miniapp_theme_cancel_edit"),
    ]])
    await callback.message.edit_text(text, parse_mode="HTML", reply_markup=kb)
    await callback.answer()


@router.callback_query(F.data == "miniapp_theme_reset_go")
async def miniapp_theme_reset_go(callback: types.CallbackQuery):
    preset_name, _is_custom = await _current_preset_and_custom()
    preset = web_theme.PRESETS[preset_name]
    for handle in ("accent", "secondary", "bg", "heading_font", "playful_tone", "pattern_enabled"):
        await set_setting(web_theme.THEME_KEYS[handle], preset[handle])
    await callback.answer("Сброшено к пресету")
    await _rerender_theme(callback)


# ── D-04, 1: три цвета — HEX текстом, контраст словами ────────────────────────────────────

@router.callback_query(F.data.startswith("miniapp_theme_color:"))
async def miniapp_theme_color_start(callback: types.CallbackQuery, state: FSMContext):
    handle = callback.data.split(":", 1)[1]
    if handle not in _COLOR_LABELS:
        await callback.answer("Неизвестная кнопка", show_alert=True)
        return
    key = web_theme.THEME_KEYS[handle]
    current = await get_setting_typed(key)
    label = _COLOR_LABELS[handle]
    text = (
        f"🎨 <b>{html_module.escape(label)}</b>\n\n"
        f"Сейчас: <b>{html_module.escape(current or '—')}</b>\n\n"
        "Пришлите новый цвет в формате HEX: решётка и шесть символов после неё, "
        "например <code>#037EF3</code>."
    )
    await callback.message.edit_text(text, parse_mode="HTML", reply_markup=_cancel_edit_keyboard())
    await state.update_data(miniapp_theme_color_handle=handle)
    await state.set_state(MiniAppTheme.color)
    await callback.answer()


@router.message(MiniAppTheme.color)
async def miniapp_theme_color_step(message: types.Message, state: FSMContext):
    value = (message.text or "").strip()
    data = await state.get_data()
    handle = data.get("miniapp_theme_color_handle")
    label = _COLOR_LABELS.get(handle, "Цвет")
    if handle not in _COLOR_LABELS or not _HEX_RE.match(value):
        await message.answer(
            "Нужно шесть символов после решётки, например #037EF3. Пришлите цвет ещё раз."
        )
        return
    await set_setting(web_theme.THEME_KEYS[handle], value)
    await state.set_state(None)
    note = await _contrast_note(handle, value)
    await message.answer(f"Готово: {label} — {value} {_swatch_emoji(value)}. {note}")
    await message.answer(
        await render_miniapp_theme_text(),
        parse_mode="HTML",
        reply_markup=await build_miniapp_theme_keyboard(),
    )


# ── D-04, 2: шрифт заголовков — закрытый список из трёх начертаний ────────────────────────

@router.callback_query(F.data.startswith("miniapp_theme_font:"))
async def miniapp_theme_font_pick(callback: types.CallbackQuery):
    font_key = callback.data.split(":", 1)[1]
    if font_key not in web_theme.FONT_STACKS:
        await callback.answer("Неизвестная кнопка", show_alert=True)
        return
    await set_setting(web_theme.THEME_KEYS["heading_font"], font_key)
    await callback.answer(f"Шрифт: {_FONT_LABELS[font_key]}")
    await _rerender_theme(callback)


# ── D-04, 3/6: тумблеры тона и бренд-паттерна ──────────────────────────────────────────────

@router.callback_query(F.data == "miniapp_theme_toggle_playful")
async def miniapp_theme_toggle_playful(callback: types.CallbackQuery):
    key = web_theme.THEME_KEYS["playful_tone"]
    current = await get_setting_typed(key)
    new_val = "off" if current == "on" else "on"
    await set_setting(key, new_val)
    await callback.answer("Игривый тон: " + ("да" if new_val == "on" else "нет"))
    await _rerender_theme(callback)


@router.callback_query(F.data == "miniapp_theme_toggle_pattern")
async def miniapp_theme_toggle_pattern(callback: types.CallbackQuery):
    key = web_theme.THEME_KEYS["pattern_enabled"]
    current = await get_setting_typed(key)
    new_val = "off" if current == "on" else "on"
    await set_setting(key, new_val)
    await callback.answer("Бренд-паттерн: " + ("включён" if new_val == "on" else "выключен"))
    await _rerender_theme(callback)


# ── D-04, 4/5/7/8: девять фото-ручек (лого×2, обложка×2, стикеры×4, иконка монеты) ─────────
# T-19.1-25: принимается ТОЛЬКО message.photo (Telegram перекодирует в растр); документы и
# произвольный SVG отклоняются с человеческим объяснением — тем же catch-all-хендлером, что и
# любой другой не-фото контент.

@router.callback_query(F.data.startswith("miniapp_theme_photo:"))
async def miniapp_theme_photo_start(callback: types.CallbackQuery, state: FSMContext):
    slot_name = callback.data.split(":", 1)[1]
    entry = _ASSET_SLOT_BY_NAME.get(slot_name)
    if entry is None:
        await callback.answer("Неизвестная кнопка", show_alert=True)
        return
    fsm_state, _key, label, hint = entry
    text = f"🖼 <b>{html_module.escape(label)}</b>\n\nПришлите фото — оно появится {html_module.escape(hint)}."
    await callback.message.edit_text(text, parse_mode="HTML", reply_markup=_cancel_edit_keyboard())
    await state.set_state(fsm_state)
    await callback.answer()


@router.callback_query(F.data.startswith("miniapp_theme_remove_photo:"))
async def miniapp_theme_remove_photo(callback: types.CallbackQuery):
    slot_name = callback.data.split(":", 1)[1]
    entry = _ASSET_SLOT_BY_NAME.get(slot_name)
    if entry is None:
        await callback.answer("Неизвестная кнопка", show_alert=True)
        return
    _state, key, label, _hint = entry
    await delete_setting(key)
    await callback.answer(f"«{label}» убран(а)")
    await _rerender_theme(callback)


# Один физический decorator на одной строке (а не многострочный вызов) -- golden-снапшот
# (`tests/test_refac_snapshot_260816.py`, через `inspect.getsource`) читает только строки,
# начинающиеся с "@router.", и ищет в НИХ токены "MiniAppTheme.xxx" -- перенос аргументов на
# отдельные строки увёл бы derived key в пустоту (проверено вживую при написании этого шва).
@router.message(StateFilter(MiniAppTheme.logo, MiniAppTheme.logo_dark, MiniAppTheme.cover, MiniAppTheme.cover_dark, MiniAppTheme.sticker_empty, MiniAppTheme.sticker_success, MiniAppTheme.sticker_error, MiniAppTheme.sticker_top1, MiniAppTheme.coin_icon), F.photo)
async def miniapp_theme_photo_step(message: types.Message, state: FSMContext):
    raw_state = await state.get_state()
    suffix = raw_state.split(":", 1)[1] if raw_state else None
    entry = _ASSET_SLOT_BY_STATE_SUFFIX.get(suffix)
    if entry is None:  # defensive — недостижимо при живом StateFilter выше
        return
    key, label, _hint = entry
    file_id = message.photo[-1].file_id
    await set_setting(key, file_id)
    await state.set_state(None)
    await message.answer(f"Готово: «{label}» обновлён(а).")
    await message.answer(
        await render_miniapp_theme_text(),
        parse_mode="HTML",
        reply_markup=await build_miniapp_theme_keyboard(),
    )


# См. комментарий у miniapp_theme_photo_step выше -- та же причина держать decorator в одну строку.
@router.message(StateFilter(MiniAppTheme.logo, MiniAppTheme.logo_dark, MiniAppTheme.cover, MiniAppTheme.cover_dark, MiniAppTheme.sticker_empty, MiniAppTheme.sticker_success, MiniAppTheme.sticker_error, MiniAppTheme.sticker_top1, MiniAppTheme.coin_icon))
async def miniapp_theme_photo_step_invalid(message: types.Message):
    await message.answer("Не понял — пришлите фото сообщением (документы и файлы не принимаются).")
