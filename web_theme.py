"""Phase 19.1 Plan 02 (D-03/D-04/D-07): пресеты оформления Mini App и дашборда.

Единственное место в проекте, где живёт знание о пресетах — модуль ЧИСТЫЙ (только stdlib),
без импортов из `handlers/`, `database/`, aiogram или FastAPI, ровно как `reg_labels.py`/
`game_labels.py` в корне (вынесены туда ради aiogram-free потребителей: `miniapp/` и
`dashboard/` импортируют его напрямую, тест на «не тянет aiogram» — тот же приём, что у
`tests/test_miniapp_labels_drift.py::test_root_label_modules_do_not_load_aiogram`).

`web_theme.py` — ВТОРОЕ и последнее место в проекте, где разрешены литералы цветов (первое —
`tokens.css`), потому что пресет по своей природе есть набор конкретных значений — не догадка,
не подбор на глаз, а зафиксированные BRAND.md/UI-SPEC константы.

Контракт (D-03: пресет — стартовая точка, не жёсткий шаблон):
- `resolve_theme(settings)` берёт уже прочитанное вызывающей стороной отображение
  «ключ реестра -> значение» (модуль в БД не ходит) и отдаёт разрешённые ручки: активный
  пресет заполняет всё, валидная ручка поверх него побеждает, мусор/пусто -> значение
  активного пресета (fail-soft, тот же принцип, что `safe_accent` в `miniapp/routers/page.py`).
- `theme_css_text(resolved)` собирает CSS-текст, но НЕ доверяет уже пришедшим значениям
  слепо — каждое ещё раз проверяется тем же регэкспом перед подстановкой в фиксированный
  шаблон (T-19.1-05): двух путей склейки строки в CSS в проекте быть не должно.
"""
from __future__ import annotations

import re

# ── таблица пресетов (BRAND.md, UI-SPEC §Theme Presets) ────────────────────────────────────

PRESETS: dict[str, dict[str, str]] = {
    "bluebook": {
        "accent": "#037EF3",
        "secondary": "#F48924",
        "bg": "#F3F4F7",
        "heading_font": "raleway",
        "playful_tone": "off",
        "pattern_enabled": "off",
    },
    "youlead": {
        "accent": "#F85A40",
        "secondary": "#F48924",
        "bg": "#F3F4F8",
        "heading_font": "raleway_italic",
        "playful_tone": "on",
        "pattern_enabled": "on",
    },
}

DEFAULT_PRESET = "bluebook"

# «Своя» не входит в PRESETS — это не набор дефолтов, а состояние «хотя бы одна ручка
# отличается от базового пресета» (вычисляется вызывающей стороной сравнением значений,
# не хранится отдельным флагом). resolve_theme на неизвестное имя пресета (в т.ч. "custom"
# без собственных значений) откатывается к DEFAULT_PRESET — «Своя» всегда стартует от
# набора ручек, уже сохранённых поверх пресета, а не от второго набора дефолтов.

# «Имя ручки» -> «ключ реестра» — вызывающая сторона (page.py/dashboard/main.py) читает
# ровно эти ключи и передаёт словарь в resolve_theme, не дублируя список руками.
THEME_KEYS: dict[str, str] = {
    "preset": "miniapp_theme_preset",
    "accent": "miniapp_accent",  # существующий ключ, не дублируется (UI-SPEC: миграция)
    "secondary": "miniapp_theme_secondary",
    "bg": "miniapp_theme_bg",
    "heading_font": "miniapp_theme_heading_font",
    "playful_tone": "miniapp_theme_playful_tone",
    "pattern_enabled": "miniapp_theme_pattern_enabled",
}

_COLOR_HANDLES = ("accent", "secondary", "bg")
_ENUM_HANDLES = ("playful_tone", "pattern_enabled")

# Шрифт заголовков — фиксированный enum из 3 предзавендоренных начертаний (RESEARCH Q3):
# семейство совпадает с `--font-heading` из `tokens.css` (план 19.1-01), меняется только
# начертание (normal/italic) через отдельную переменную `--font-heading-style`.
FONT_STACKS: dict[str, tuple[str, str]] = {
    "raleway": ('"Raleway", "Lato", "Segoe UI", system-ui, sans-serif', "normal"),
    "raleway_italic": ('"Raleway", "Lato", "Segoe UI", system-ui, sans-serif', "italic"),
    "lato": ('"Lato", "Segoe UI", system-ui, -apple-system, sans-serif', "normal"),
}

_HEX6 = re.compile(r"^#[0-9A-Fa-f]{6}$")

# Ассеты оформления (D-08/D-15/D-16) — «имя поля ответа /app/api/me» -> «ключ реестра».
# Единственное место, где перечислены эти ключи: `page.py` читает их для /app/api/me,
# `files.py` строит из них же allow-list `can_read_file` — список не переписывается дважды.
ASSET_KEYS: dict[str, str] = {
    "cover_file_id": "miniapp_cover",
    "cover_dark_file_id": "miniapp_cover_dark",
    "logo_dark_file_id": "miniapp_logo_dark",
    "sticker_empty_file_id": "miniapp_sticker_empty",
    "sticker_success_file_id": "miniapp_sticker_success",
    "sticker_error_file_id": "miniapp_sticker_error",
    "sticker_top1_file_id": "miniapp_sticker_top1",
    "coin_icon_file_id": "miniapp_coin_icon",
}

# Тёмная поверхность Mini App (tokens.css `:root[data-theme="dark"] --surface`) — фон, против
# которого осветляется бренд-акцент/вторичный цвет под контраст ≥4.5:1 (D-07).
DARK_SURFACE = "#1C1F25"


# ── WCAG relative luminance / contrast ratio (RESEARCH, Code Examples) ─────────────────────

def _srgb_channel(c: float) -> float:
    return c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4


def relative_luminance(hex_color: str) -> float:
    r, g, b = (int(hex_color[i:i + 2], 16) / 255 for i in (1, 3, 5))
    r, g, b = _srgb_channel(r), _srgb_channel(g), _srgb_channel(b)
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def contrast_ratio(hex_a: str, hex_b: str) -> float:
    l1, l2 = sorted((relative_luminance(hex_a), relative_luminance(hex_b)), reverse=True)
    return (l1 + 0.05) / (l2 + 0.05)


def lighten_to_contrast(hex_color: str, bg_hex: str, target: float = 4.5, step: float = 0.04) -> str:
    """Детерминированный шаг к белому, пока контраст против `bg_hex` не достигнет `target`.

    Каждый канал только растёт (никогда не темнее исходного). Шаг — не меньше 1 единицы канала
    за итерацию (`max(1, round(...))`) — иначе геометрическое приближение к 255 стопорится
    раньше цели из-за округления вниз (254 + (255-254)*0.04 округляется обратно в 254, цикл
    крутился бы бесконечно). С минимальным шагом канал гарантированно доходит до 255 не более
    чем за 255 итераций — безнадёжный случай (4.5:1 недостижим даже при белом) упирается в
    белый и завершается, не зависает.
    """
    r, g, b = (int(hex_color[i:i + 2], 16) for i in (1, 3, 5))
    while contrast_ratio(f"#{r:02x}{g:02x}{b:02x}", bg_hex) < target and (r, g, b) != (255, 255, 255):
        r = min(255, r + max(1, round((255 - r) * step)))
        g = min(255, g + max(1, round((255 - g) * step)))
        b = min(255, b + max(1, round((255 - b) * step)))
    return f"#{r:02X}{g:02X}{b:02X}"


# ── resolve_theme ────────────────────────────────────────────────────────────────────────

def _active_preset_name(raw) -> str:
    return raw if isinstance(raw, str) and raw in PRESETS else DEFAULT_PRESET


def resolve_theme(settings: dict) -> dict:
    """`settings` — уже прочитанное отображение «ключ реестра -> значение» (см. `THEME_KEYS`).
    Отдаёт разрешённые ручки пресета плюс посчитанную тёмную пару акцент/вторичный."""
    preset_name = _active_preset_name(settings.get(THEME_KEYS["preset"]))
    preset = PRESETS[preset_name]

    resolved: dict = {"preset": preset_name}

    for handle in _COLOR_HANDLES:
        raw = settings.get(THEME_KEYS[handle])
        resolved[handle] = raw if isinstance(raw, str) and _HEX6.match(raw) else preset[handle]

    heading_raw = settings.get(THEME_KEYS["heading_font"])
    resolved["heading_font"] = heading_raw if heading_raw in FONT_STACKS else preset["heading_font"]

    for handle in _ENUM_HANDLES:
        raw = settings.get(THEME_KEYS[handle])
        resolved[handle] = raw if raw in ("on", "off") else preset[handle]

    resolved["accent_dark"] = lighten_to_contrast(resolved["accent"], DARK_SURFACE)
    resolved["secondary_dark"] = lighten_to_contrast(resolved["secondary"], DARK_SURFACE)
    return resolved


# ── theme_css_text ───────────────────────────────────────────────────────────────────────

def _safe_hex(value, fallback: str) -> str:
    return value if isinstance(value, str) and _HEX6.match(value) else fallback


def theme_css_text(resolved: dict) -> str:
    """Собирает `:root { … }` + `:root[data-theme="dark"] { … }`. Не доверяет `resolved`
    слепо (T-19.1-05) — каждое значение перепроверяется тем же регэкспом/enum'ом, что и в
    `resolve_theme`, прежде чем попасть в фиксированный шаблон подстановки."""
    preset_name = resolved.get("preset") if resolved.get("preset") in PRESETS else DEFAULT_PRESET
    preset = PRESETS[preset_name]

    accent = _safe_hex(resolved.get("accent"), preset["accent"])
    secondary = _safe_hex(resolved.get("secondary"), preset["secondary"])
    bg = _safe_hex(resolved.get("bg"), preset["bg"])

    heading_font = resolved.get("heading_font")
    heading_font = heading_font if heading_font in FONT_STACKS else preset["heading_font"]
    font_family, font_style = FONT_STACKS[heading_font]

    accent_dark = _safe_hex(resolved.get("accent_dark"), lighten_to_contrast(accent, DARK_SURFACE))
    secondary_dark = _safe_hex(resolved.get("secondary_dark"), lighten_to_contrast(secondary, DARK_SURFACE))

    return (
        ":root {\n"
        f"  --accent: {accent};\n"
        f"  --secondary: {secondary};\n"
        f"  --bg: {bg};\n"
        f"  --font-heading: {font_family};\n"
        f"  --font-heading-style: {font_style};\n"
        "}\n"
        ":root[data-theme=\"dark\"] {\n"
        f"  --accent: {accent_dark};\n"
        f"  --secondary: {secondary_dark};\n"
        "}\n"
    )
