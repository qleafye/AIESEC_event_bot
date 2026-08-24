"""Phase 19.1 Plan 07 (D-20): воспроизводимая съёмка превью пресетов оформления.

Снимает статичный PNG хаба делегата (`#/hub`) для каждого пресета из `web_theme.PRESETS`,
поднимая ЛОКАЛЬНЫЙ demo-сервер (`.planning/phases/19-mini-app/demo_server.py`, тот же приём,
что и остальные превью/скрины фазы 19.1) и снимая реальный отрендеренный экран headless-
браузером — НЕ рисунок и НЕ AI-генерация (правило D-04 «никаких AI-иллюстраций» распространено
и на превью: превью должно быть тем, что менеджер реально увидит, иначе оно вводит в заблуждение).

Запуск:
    python tools/make_theme_previews.py

Результат: по одному PNG на пресет в `assets/theme-preview/{preset}.png` (390x844 — типичный
телефон), перезаписывает существующие файлы. Переснимать нужно после каждой правки хаба/
токенов — команда одна, ручной операции нет.

Требует: `.planning/phases/19-mini-app/demo_server.py` (локальный dev-артефакт, НЕ в git --
`.planning/` в `.gitignore`, см. канонические ссылки 19.1-CONTEXT.md) + `selenium` (headless
Chrome/Chromium в системе, Selenium Manager сам подтягивает нужный chromedriver) + `PIL`.

Fail-soft (T-19.1-28, план 19.1-07 задача 3): если demo-сервер или headless-браузер не
поднялись в этой среде — скрипт печатает диагностику и завершается с ненулевым кодом, НЕ
рисует картинку руками и не подменяет её генерацией. Бот при отсутствии файла превью
переходит на fail-soft-ветку из задачи 1 (тот же вопрос текстом).

Общий код с `tools/shoot_screens.py` (план 19.1-08) живёт в `tools/_shoot_common.py` --
жизненный цикл demo-сервера/headless-браузера/запись пресета не дублируются между скриптами.
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

# Python сам вставляет каталог запускаемого скрипта в sys.path[0] -- `_shoot_common.py`
# (сосед в tools/) импортируется без ручной правки path при `python tools/make_theme_previews.py`.
from _shoot_common import (
    DEMO_SERVER,
    MINIAPP_BASE_URL,
    MINIAPP_HEALTH_URL,
    REPO_ROOT,
    demo_db_path,
    ensure_repo_on_path,
    make_chrome_driver,
    reset_demo_db,
    save_screenshot,
    start_demo_server_process,
    stop_process,
    wait_for_server,
    write_preset,
)

PREVIEW_DIR = REPO_ROOT / "assets" / "theme-preview"

DELEGATE_ID = 900100  # тот же id, что demo_server.py/tests/test_miniapp_routes.py::DELEGATE_ID

# hub.js::ONBOARDING_KEY — источник истины там; здесь только читаем/пишем тот же ключ, чтобы
# снимать РЕАЛЬНЫЙ хаб (hero+плитки), а не одноразовый привет-экран поверх него.
ONBOARDING_KEY = "aiesec_miniapp_onboarding_seen_v1"

WINDOW_SIZE = (390, 844)  # типичный телефон (D-20 план: «390×844»)


def _shoot(driver, out_path: Path) -> None:
    from selenium.webdriver.common.by import By
    from selenium.webdriver.support.ui import WebDriverWait
    from selenium.webdriver.support import expected_conditions as EC

    url = f"{MINIAPP_BASE_URL}/app?as={DELEGATE_ID}"
    driver.get(url)
    driver.execute_script(f"localStorage.setItem('{ONBOARDING_KEY}', '1');")
    driver.get(f"{url}#/hub")
    WebDriverWait(driver, 10).until(EC.presence_of_element_located((By.CLASS_NAME, "hero")))
    time.sleep(0.3)  # докрутка счётчика монет / шрифты — небольшой запас после появления .hero
    save_screenshot(driver, out_path, WINDOW_SIZE)


def main() -> int:
    if not DEMO_SERVER.is_file():
        print(f"СТОП: demo-сервер не найден: {DEMO_SERVER}", file=sys.stderr)
        print(
            "Это локальный dev-артефакт вне git (.planning/ в .gitignore) -- на этой машине "
            "его нет. Превью не рисуются руками и не генерируются -- см. SUMMARY плана "
            "19.1-07 (fail-soft-ветка задачи 1: бот спрашивает подтверждение текстом).",
            file=sys.stderr,
        )
        return 1

    PREVIEW_DIR.mkdir(parents=True, exist_ok=True)
    ensure_repo_on_path()
    import web_theme  # noqa: F401 -- прогрев импорта до попытки поднять браузер (fail fast)

    reset_demo_db()  # идемпотентность повторного запуска (Rule 1 — см. _shoot_common.py)
    proc = start_demo_server_process()
    try:
        if not wait_for_server(proc, MINIAPP_HEALTH_URL):
            out = proc.stdout.read().decode("utf-8", errors="replace") if proc.stdout else ""
            print("СТОП: demo-сервер не поднялся вовремя.", file=sys.stderr)
            if out:
                print(out[-2000:], file=sys.stderr)
            return 1

        try:
            driver = make_chrome_driver(WINDOW_SIZE)
        except Exception as exc:  # noqa: BLE001 -- любая причина недоступности браузера
            print(f"СТОП: не удалось поднять headless-браузер: {exc}", file=sys.stderr)
            print(
                "Превью не рисуются руками и не подменяются AI-генерацией -- см. SUMMARY "
                "плана 19.1-07.",
                file=sys.stderr,
            )
            return 1

        db_path = demo_db_path()
        try:
            for preset_name in web_theme.PRESETS:
                write_preset(db_path, preset_name)
                out_path = PREVIEW_DIR / f"{preset_name}.png"
                _shoot(driver, out_path)
                print(f"OK: {out_path}")
        finally:
            driver.quit()
    finally:
        stop_process(proc)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
