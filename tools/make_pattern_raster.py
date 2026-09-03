"""Quick 260903 (BACKLOG-0309-PATTERN), параметризовано quick 260904-183 (BACKLOG-0904-REALTALK-
PRESET): воспроизводимая пересборка растрового паттерна плиты из исходного SVG — по имени
паттерна, а не только для `youlead`.

`miniapp/static/pattern/{name}.svg` — CSS растягивает его фоном на каждой плите
(`background-repeat: no-repeat`), значит браузер растеризует всю эту геометрию на каждый paint.
Этот скрипт снимает SVG headless-Chromium'ом (Playwright — `cairosvg` в venv нет) один раз и
пересохраняет как WebP — исходник остаётся в репозитории, растр правится только пересборкой,
руками не редактируется.

Запуск:
    python tools/make_pattern_raster.py               # пересобрать все паттерны из PATTERNS
    python tools/make_pattern_raster.py realtalk       # пересобрать только один
    python tools/make_pattern_raster.py youlead realtalk

Результат: `miniapp/static/pattern/{name}.webp`, ширина 2160px (верхняя граница
`--plate-pattern-size`, `.plate--onboarding` в app.css) — прозрачный фон (`fill-opacity: 0.2`
зашита в самом ассете, `omit_background=True` обязателен, иначе появится серая плашка). Высота
считается из `viewBox` самого SVG (не зашита константой) — у следующего паттерна пропорции
могут не совпасть с 7236×5197.

Цель — ≤200 КБ. ОТКЛОНЕНИЕ ОТ ПЕРВОНАЧАЛЬНОГО ПЛАНА (Rule 1, обнаружено экспериментально):
рычаг `quality` (RGB-канал) почти не двигает вес — картинка почти вся прозрачная/полу-
прозрачная тонкая обводка текста, а не смешанные цвета, RGB-канал сам по себе лёгкий что на
quality=80, что на 30. Реальный вес несёт АЛЬФА-канал (сама геометрия паттерна закодирована
прозрачностью). Поэтому ступеньки — по `alpha_quality` (Pillow: `Image.save(..., "WEBP",
alpha_quality=N)`), `quality` держим фиксированным на 80 (RGB дешёвый, незачем портить края
текста). Ниже 50 по alpha_quality не опускаемся — glyph-очертания на 20% прозрачности
начинают заметно дрожать (проверено глазами, side-by-side с оригиналом).
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

from PIL import Image
from playwright.sync_api import sync_playwright

REPO_ROOT = Path(__file__).resolve().parent.parent
PATTERN_DIR = REPO_ROOT / "miniapp" / "static" / "pattern"

# Имена = имена файлов `{name}.svg`/`{name}.webp` в miniapp/static/pattern/. Единственное место
# в скрипте, где перечислены известные паттерны — CLI без аргументов пересобирает все отсюда.
PATTERNS: tuple[str, ...] = ("youlead", "realtalk")

# Верхняя граница `--plate-pattern-size` (app.css: `.plate--onboarding` — 2160px, самая
# крупная из плит от 972px до 2160px). Тайлинга нет (`background-repeat: no-repeat`),
# поэтому один растр на этой ширине покрывает все размеры плит без потери резкости.
TARGET_WIDTH = 2160

MAX_BYTES = 200 * 1024
RGB_QUALITY = 80  # фиксирован — RGB-канал почти не влияет на вес этого ассета (см. докстринг)
ALPHA_QUALITY_LADDER = (75, 70, 65, 60, 55, 50)  # ниже 50 не опускаемся

_VIEWBOX_RE = re.compile(r'viewBox\s*=\s*"[\s,]*[\d.+-]+[\s,]+[\d.+-]+[\s,]+([\d.]+)[\s,]+([\d.]+)"')


def _target_height_from_viewbox(svg_path: Path) -> int:
    """Читает `viewBox` из SVG и считает высоту растра из пропорций вьюпорта — не зашиваем
    7236×5197 второй раз константой: у следующего паттерна viewBox может быть другим."""
    text = svg_path.read_text(encoding="utf-8")
    match = _VIEWBOX_RE.search(text)
    if not match:
        raise ValueError(f"в {svg_path.name} нет viewBox — не могу посчитать пропорции растра")
    vb_w, vb_h = float(match.group(1)), float(match.group(2))
    return round(TARGET_WIDTH * vb_h / vb_w)


def _shoot_png(svg_path: Path, target_height: int) -> bytes:
    """Открывает SVG headless-Chromium'ом на прозрачном фоне и возвращает PNG-скриншот
    ровно TARGET_WIDTH×target_height (viewport = размер картинки, без полей вокруг).

    SVG-разметка вставляется В HTML НАПРЯМУЮ (не `<img src="file://…">`) — Chromium отказывает
    `<img>` в загрузке `file://`-ресурса изнутри страницы, отданной через `page.set_content`
    (`Not allowed to load local resource`, проверено эмпирически на этой машине), а инлайновый
    `<svg>` такому ограничению не подчиняется. CSS `width`/`height` на самом `<svg>`
    масштабирует содержимое через уже заданный в файле `viewBox` без искажений — целевые
    TARGET_WIDTH/target_height посчитаны с тем же соотношением сторон, что и viewBox исходника."""
    svg_markup = svg_path.read_text(encoding="utf-8")
    html = (
        "<!doctype html><html><head><style>"
        "html,body{margin:0;padding:0;background:transparent;}"
        f"svg{{display:block;width:{TARGET_WIDTH}px;height:{target_height}px;}}"
        "</style></head><body>"
        + svg_markup +
        "</body></html>"
    )
    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        try:
            page = browser.new_page(viewport={"width": TARGET_WIDTH, "height": target_height})
            page.set_content(html)
            page.wait_for_load_state("networkidle")
            return page.screenshot(omit_background=True)
        finally:
            browser.close()


def _save_webp_under_limit(png_bytes: bytes, out_path: Path) -> tuple[int, int]:
    """Пересохраняет PNG в WebP, снижая `alpha_quality` по ALPHA_QUALITY_LADDER (RGB `quality`
    фиксирован на RGB_QUALITY — см. докстринг модуля), пока размер не уложится в MAX_BYTES.
    Возвращает (итоговый alpha_quality, итоговый вес в байтах)."""
    import io

    image = Image.open(io.BytesIO(png_bytes)).convert("RGBA")
    last_alpha_quality = ALPHA_QUALITY_LADDER[-1]
    for alpha_quality in ALPHA_QUALITY_LADDER:
        last_alpha_quality = alpha_quality
        image.save(out_path, "WEBP", quality=RGB_QUALITY, alpha_quality=alpha_quality, method=6)
        size = out_path.stat().st_size
        if size <= MAX_BYTES:
            return alpha_quality, size
    return last_alpha_quality, out_path.stat().st_size


def _build_one(name: str) -> bool:
    """Возвращает True, если растр уложился в MAX_BYTES (иначе печатает предупреждение и
    возвращает False — вызывающая сторона решает, каким кодом завершиться)."""
    svg_path = PATTERN_DIR / f"{name}.svg"
    webp_path = PATTERN_DIR / f"{name}.webp"

    if not svg_path.is_file():
        print(f"СТОП: исходный SVG не найден: {svg_path}", file=sys.stderr)
        return False

    target_height = _target_height_from_viewbox(svg_path)
    print(f"Снимаю {svg_path.name} на {TARGET_WIDTH}x{target_height}, прозрачный фон…")
    png_bytes = _shoot_png(svg_path, target_height)

    alpha_quality, size = _save_webp_under_limit(png_bytes, webp_path)
    with Image.open(webp_path) as im:
        dims = im.size

    print(
        f"OK: {webp_path} — {dims[0]}x{dims[1]}, quality={RGB_QUALITY}, "
        f"alpha_quality={alpha_quality}, {size / 1024:.1f} КБ"
    )
    if size > MAX_BYTES:
        print(
            f"ВНИМАНИЕ: {size / 1024:.1f} КБ превышает цель 200 КБ даже на alpha_quality="
            f"{ALPHA_QUALITY_LADDER[-1]} — не опускаемся ниже, но результат стоит проверить "
            "глазами.",
            file=sys.stderr,
        )
        return False
    return True


def main(argv: list[str]) -> int:
    names = list(argv) if argv else list(PATTERNS)
    unknown = [name for name in names if name not in PATTERNS]
    if unknown:
        print(
            f"СТОП: неизвестное имя паттерна: {', '.join(unknown)} — известные: "
            f"{', '.join(PATTERNS)}",
            file=sys.stderr,
        )
        return 1

    ok = True
    for name in names:
        if not _build_one(name):
            ok = False
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
