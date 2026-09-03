"""Quick 260903 (BACKLOG-0309-PATTERN): воспроизводимая пересборка растрового паттерна плиты
из исходного SVG.

`miniapp/static/pattern/youlead.svg` — 1 069 469 байт, 567 `<path>`, viewBox 7236×5197. CSS
растягивает его фоном на каждой плите (`background-repeat: no-repeat`), значит браузер
растеризует всю эту геометрию на каждый paint. Этот скрипт снимает SVG headless-Chromium'ом
(Playwright — `cairosvg` в venv нет) один раз и пересохраняет как WebP — исходник остаётся в
репозитории, растр правится только пересборкой, руками не редактируется.

Запуск:
    python tools/make_pattern_raster.py

Результат: `miniapp/static/pattern/youlead.webp`, ширина 2160px (верхняя граница
`--plate-pattern-size`, `.plate--onboarding` в app.css) — прозрачный фон (`fill-opacity: 0.2`
зашита в самом ассете, `omit_background=True` обязателен, иначе появится серая плашка).

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

import sys
from pathlib import Path

from PIL import Image
from playwright.sync_api import sync_playwright

REPO_ROOT = Path(__file__).resolve().parent.parent
SVG_PATH = REPO_ROOT / "miniapp" / "static" / "pattern" / "youlead.svg"
WEBP_PATH = REPO_ROOT / "miniapp" / "static" / "pattern" / "youlead.webp"

# Верхняя граница `--plate-pattern-size` (app.css: `.plate--onboarding` — 2160px, самая
# крупная из плит от 972px до 2160px). Тайлинга нет (`background-repeat: no-repeat`),
# поэтому один растр на этой ширине покрывает все размеры плит без потери резкости.
TARGET_WIDTH = 2160

# Соотношение сторон — из viewBox исходного SVG (7236×5197): высота вьюпорта = ширина *
# (5197 / 7236), округлённая до целого.
_SVG_VIEWBOX_W = 7236
_SVG_VIEWBOX_H = 5197
TARGET_HEIGHT = round(TARGET_WIDTH * _SVG_VIEWBOX_H / _SVG_VIEWBOX_W)

MAX_BYTES = 200 * 1024
RGB_QUALITY = 80  # фиксирован — RGB-канал почти не влияет на вес этого ассета (см. докстринг)
ALPHA_QUALITY_LADDER = (75, 70, 65, 60, 55, 50)  # ниже 50 не опускаемся


def _shoot_png(svg_path: Path) -> bytes:
    """Открывает SVG headless-Chromium'ом на прозрачном фоне и возвращает PNG-скриншот
    ровно TARGET_WIDTH×TARGET_HEIGHT (viewport = размер картинки, без полей вокруг).

    SVG-разметка вставляется В HTML НАПРЯМУЮ (не `<img src="file://…">`) — Chromium отказывает
    `<img>` в загрузке `file://`-ресурса изнутри страницы, отданной через `page.set_content`
    (`Not allowed to load local resource`, проверено эмпирически на этой машине), а инлайновый
    `<svg>` такому ограничению не подчиняется. CSS `width`/`height` на самом `<svg>`
    масштабирует содержимое через уже заданный в файле `viewBox` без искажений — целевые
    TARGET_WIDTH/TARGET_HEIGHT посчитаны с тем же соотношением сторон, что и viewBox исходника."""
    svg_markup = svg_path.read_text(encoding="utf-8")
    html = (
        "<!doctype html><html><head><style>"
        "html,body{margin:0;padding:0;background:transparent;}"
        f"svg{{display:block;width:{TARGET_WIDTH}px;height:{TARGET_HEIGHT}px;}}"
        "</style></head><body>"
        + svg_markup +
        "</body></html>"
    )
    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        try:
            page = browser.new_page(viewport={"width": TARGET_WIDTH, "height": TARGET_HEIGHT})
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


def main() -> int:
    if not SVG_PATH.is_file():
        print(f"СТОП: исходный SVG не найден: {SVG_PATH}", file=sys.stderr)
        return 1

    print(f"Снимаю {SVG_PATH.name} на {TARGET_WIDTH}x{TARGET_HEIGHT}, прозрачный фон…")
    png_bytes = _shoot_png(SVG_PATH)

    alpha_quality, size = _save_webp_under_limit(png_bytes, WEBP_PATH)
    with Image.open(WEBP_PATH) as im:
        dims = im.size

    print(
        f"OK: {WEBP_PATH} — {dims[0]}x{dims[1]}, quality={RGB_QUALITY}, "
        f"alpha_quality={alpha_quality}, {size / 1024:.1f} КБ"
    )
    if size > MAX_BYTES:
        print(
            f"ВНИМАНИЕ: {size / 1024:.1f} КБ превышает цель 200 КБ даже на alpha_quality="
            f"{ALPHA_QUALITY_LADDER[-1]} — не опускаемся ниже, но результат стоит проверить "
            "глазами.",
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
