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
    # Строгий АЙСЕК: плита без паттерна, прямые заголовки. Тот же набор, что и был,
    # плюс явное «паттерна нет».
    "bluebook": {
        "accent": "#037EF3",
        "secondary": "#F48924",
        "bg": "#F3F4F7",
        "heading_font": "raleway",
        "playful_tone": "off",
        "pattern_enabled": "off",
        "plate_pattern": "none",
    },
    # «ЮЛид» (макеты 03.09, решение владельца): та же синяя палитра BRAND.md, но
    # курсивные заголовки-герои, фирменный паттерн на плите и тон на «ты».
    # Прежний оранжево-красный акцент версии 19.1-02 снят: принятые макеты — синие (D-09).
    "youlead": {
        "accent": "#037EF3",
        "secondary": "#F48924",
        "bg": "#F3F4F7",
        "heading_font": "raleway_italic",
        "playful_tone": "on",
        "pattern_enabled": "on",
        "plate_pattern": "youlead",
    },
    # «РилТолк» (форум RealTalk'26, quick 260904-183): бренд снят владельцем из Figma
    # REALTALK26 04.09.2026 (`refs/realtalk/BRAND.md`). Собственная палитра — фиолетовый
    # акцент + оранжевый вторичный, а не синяя BlueBook/YouLead; заголовки прямые (лого
    # RealTalk набрано прямым Raleway ExtraBold, курсив «ЮЛид» сюда не переносим), тон на «ты».
    "realtalk": {
        "accent": "#7552CC",
        "secondary": "#FF8B10",
        "bg": "#F3F4F7",
        "heading_font": "raleway",
        "playful_tone": "on",
        "pattern_enabled": "on",
        "plate_pattern": "realtalk",
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
    "plate_pattern": "miniapp_theme_pattern",
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

# Ручка `plate_pattern` (D-05, план 23.1-02) принимает либо встроенное имя из этого словаря,
# либо file_id картинки менеджера (regexp ниже — тот же, что FILE_ID_RE в
# `miniapp/routers/files.py`; дублировать импортом нельзя, `web_theme` обязан остаться чистым
# stdlib-модулем). Значение -> (url, background-size, x, y, opacity). Смещения — из
# mockups/NOTES.md (масштаб ×1.8, решение владельца 03.09); ассет уже несёт fill-opacity 0.2,
# поэтому дополнительная непрозрачность встроенного варианта равна 1.
#
# Quick 260903 (BACKLOG-0309-PATTERN): `.svg` (1,0 МБ, 567 `<path>`) заменён растром `.webp`
# (≤200 КБ) — CSS растягивает паттерн фоном на каждой плите без тайлинга, браузер растеризовал
# бы всю геометрию SVG на каждый paint. Исходник остаётся рядом (`youlead.svg`, НЕ удалён),
# растр воспроизводимо пересобирается `tools/make_pattern_raster.py`.
PLATE_PATTERNS: dict[str, tuple[str, str, str, str, str]] = {
    "none":    ("none", "1368px", "-162px", "-324px", "1"),
    "youlead": ('url("/app/static/pattern/youlead.webp")', "1368px", "-162px", "-324px", "1"),
    # Quick 260904-183 (BACKLOG-0904-REALTALK-PRESET): тот же viewBox (7236×5197), что и у
    # youlead.svg, поэтому геометрия плиты (смещения/размер) не меняется.
    "realtalk": ('url("/app/static/pattern/realtalk.webp")', "1368px", "-162px", "-324px", "1"),
}

# Непрозрачность паттерна менеджера (file_id): картинка приходит непрозрачной, 20% даёт тот же
# визуальный вес, что и у встроенного ассета (у которого прозрачность уже зашита в SVG).
_UPLOADED_PATTERN_OPACITY = "0.2"

_FILE_ID = re.compile(r"^[A-Za-z0-9_-]{20,200}$")

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
    "plate_pattern_file_id": "miniapp_theme_pattern",
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


def darken_to_contrast(hex_color: str, bg_hex: str, target: float = 4.5, step: float = 0.04) -> str:
    """Зеркало `lighten_to_contrast` — детерминированный шаг к чёрному, пока контраст против
    `bg_hex` не достигнет `target`. Каждый канал только убывает (никогда не светлее исходного).
    Найдено планом 19.1-08 (визуальная сверка): `--accent-text` (ссылки/числа/подписи, D-04
    "Reserved for... links (--accent-text)") был захардкожен в `tokens.css` и не менялся при
    смене пресета — YouLead оставался с синими числами поверх оранжевого hero. Этот хелпер
    даёт `resolve_theme` посчитать читаемый text-вариант акцента для ЛЮБОГО пресета/кастома, а
    не только для зашитого в токены BlueBook-значения."""
    r, g, b = (int(hex_color[i:i + 2], 16) for i in (1, 3, 5))
    while contrast_ratio(f"#{r:02x}{g:02x}{b:02x}", bg_hex) < target and (r, g, b) != (0, 0, 0):
        r = max(0, r - max(1, round(r * step)))
        g = max(0, g - max(1, round(g * step)))
        b = max(0, b - max(1, round(b * step)))
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

    # `plate_pattern` (D-05): валидно либо встроенное имя PLATE_PATTERNS, либо file_id
    # картинки менеджера — мусор/пусто откатывается к значению активного пресета.
    pattern_raw = settings.get(THEME_KEYS["plate_pattern"])
    if pattern_raw in PLATE_PATTERNS or (isinstance(pattern_raw, str) and _FILE_ID.match(pattern_raw)):
        resolved["plate_pattern"] = pattern_raw
    else:
        resolved["plate_pattern"] = preset["plate_pattern"]

    resolved["accent_dark"] = lighten_to_contrast(resolved["accent"], DARK_SURFACE)
    resolved["secondary_dark"] = lighten_to_contrast(resolved["secondary"], DARK_SURFACE)
    # `--accent-text` (ссылки/числа/подписи, D-04 "Reserved for... links") -- читаемый на белом
    # text-вариант акцента, пересчитывается на КАЖДЫЙ пресет/кастом (план 19.1-08: до этой
    # правки был захардкожен в tokens.css под BlueBook и не следовал за сменой пресета).
    # Тёмная тема уже даёт себе `accent_dark` контрастным против DARK_SURFACE (≥4.5:1) -- этого
    # же значения достаточно и для текстового применения, второй пересчёт не нужен.
    resolved["accent_text"] = darken_to_contrast(resolved["accent"], "#FFFFFF")
    resolved["accent_text_dark"] = resolved["accent_dark"]
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
    # `--accent-text` (план 19.1-08 — см. resolve_theme) пересчитывается здесь же, если
    # вызывающая сторона передала уже готовый resolved (обычный путь) -- иначе досчитывается
    # на месте тем же правилом, что и accent_dark/secondary_dark выше.
    accent_text = _safe_hex(resolved.get("accent_text"), darken_to_contrast(accent, "#FFFFFF"))
    accent_text_dark = _safe_hex(resolved.get("accent_text_dark"), accent_dark)

    # `--plate-pattern*` (T-23.1-04): значение перепроверяется здесь же, независимо от того,
    # прошло ли оно уже resolve_theme — тот же приём, что и у цветов выше (T-19.1-05). Строка
    # попадает в CSS ровно двумя путями: встроенное имя из PLATE_PATTERNS либо file_id за
    # /app/api/file/, третьей склейки быть не должно.
    pattern_value = resolved.get("plate_pattern")
    if pattern_value in PLATE_PATTERNS:
        pattern_url, pattern_size, pattern_x, pattern_y, pattern_opacity = PLATE_PATTERNS[pattern_value]
    elif isinstance(pattern_value, str) and _FILE_ID.match(pattern_value):
        _fallback_url, pattern_size, pattern_x, pattern_y, _fallback_opacity = PLATE_PATTERNS[preset["plate_pattern"]]
        pattern_url = f'url("/app/api/file/{pattern_value}")'
        pattern_opacity = _UPLOADED_PATTERN_OPACITY
    else:
        pattern_url, pattern_size, pattern_x, pattern_y, pattern_opacity = PLATE_PATTERNS[preset["plate_pattern"]]

    return (
        ":root {\n"
        f"  --accent: {accent};\n"
        f"  --accent-text: {accent_text};\n"
        f"  --secondary: {secondary};\n"
        f"  --bg: {bg};\n"
        f"  --font-heading: {font_family};\n"
        f"  --font-heading-style: {font_style};\n"
        f"  --plate-pattern: {pattern_url};\n"
        f"  --plate-pattern-size: {pattern_size};\n"
        f"  --plate-pattern-x: {pattern_x};\n"
        f"  --plate-pattern-y: {pattern_y};\n"
        f"  --plate-pattern-opacity: {pattern_opacity};\n"
        "}\n"
        ":root[data-theme=\"dark\"] {\n"
        f"  --accent: {accent_dark};\n"
        f"  --accent-text: {accent_text_dark};\n"
        f"  --secondary: {secondary_dark};\n"
        "}\n"
    )
