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

Требует: `.planning/phases/19-mini-app/demo_server.py` (локальный dev-артефакт, НЕ в git —
`.planning/` в `.gitignore`, см. канонические ссылки 19.1-CONTEXT.md) + `selenium` (headless
Chrome/Chromium в системе, Selenium Manager сам подтягивает нужный chromedriver) + `PIL`.

Fail-soft (T-19.1-28, план 19.1-07 задача 3): если demo-сервер или headless-браузер не
поднялись в этой среде — скрипт печатает диагностику и завершается с ненулевым кодом, НЕ
рисует картинку руками и не подменяет её генерацией. Бот при отсутствии файла превью
переходит на fail-soft-ветку из задачи 1 (тот же вопрос текстом).
"""
from __future__ import annotations

import subprocess
import sys
import sqlite3
import time
import urllib.error
import urllib.request
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
DEMO_SERVER = REPO_ROOT / ".planning" / "phases" / "19-mini-app" / "demo_server.py"
PREVIEW_DIR = REPO_ROOT / "assets" / "theme-preview"

BASE_URL = "http://127.0.0.1:8765"
HEALTH_URL = f"{BASE_URL}/app/health"
DELEGATE_ID = 900100  # тот же id, что demo_server.py/tests/test_miniapp_routes.py::DELEGATE_ID

# hub.js::ONBOARDING_KEY — источник истины там; здесь только читаем/пишем тот же ключ, чтобы
# снимать РЕАЛЬНЫЙ хаб (hero+плитки), а не одноразовый привет-экран поверх него.
ONBOARDING_KEY = "aiesec_miniapp_onboarding_seen_v1"

WINDOW_SIZE = (390, 844)  # типичный телефон (D-20 план: «390×844»)
STARTUP_TIMEOUT_S = 20


def _wait_for_server(proc: subprocess.Popen, timeout_s: float = STARTUP_TIMEOUT_S) -> bool:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            return False  # процесс уже упал
        try:
            with urllib.request.urlopen(HEALTH_URL, timeout=1) as resp:
                if resp.status == 200:
                    return True
        except (urllib.error.URLError, ConnectionError, TimeoutError):
            time.sleep(0.3)
    return False


def _demo_db_path() -> Path:
    # demo_server.py: `SCRATCH = Path(__file__).parent; db_path = _use_tmp_db(SCRATCH, "demo.db")`
    return DEMO_SERVER.parent / "demo.db"


def _write_preset(db_path: Path, preset_name: str) -> None:
    """Пишет ВСЕ ручки пресета разом, тем же приёмом, что `miniapp_preset_apply` в
    `handlers/admin_miniapp_theme.py` — превью должно показывать ровно то, что получит
    менеджер после «Применить», не какое-то отдельное демо-состояние."""
    import web_theme  # локальный импорт: REPO_ROOT уже в sys.path (см. main())

    conn = sqlite3.connect(str(db_path))
    try:
        rows = [(web_theme.THEME_KEYS[handle], value) for handle, value in web_theme.PRESETS[preset_name].items()]
        rows.append((web_theme.THEME_KEYS["preset"], preset_name))
        conn.executemany(
            "INSERT INTO bot_settings (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            rows,
        )
        conn.commit()
    finally:
        conn.close()


def _shoot(driver, out_path: Path) -> None:
    from PIL import Image
    from selenium.webdriver.common.by import By
    from selenium.webdriver.support.ui import WebDriverWait
    from selenium.webdriver.support import expected_conditions as EC

    url = f"{BASE_URL}/app?as={DELEGATE_ID}"
    driver.get(url)
    driver.execute_script(f"localStorage.setItem('{ONBOARDING_KEY}', '1');")
    driver.get(f"{url}#/hub")
    WebDriverWait(driver, 10).until(EC.presence_of_element_located((By.CLASS_NAME, "hero")))
    time.sleep(0.3)  # докрутка счётчика монет / шрифты — небольшой запас после появления .hero

    tmp_path = out_path.with_suffix(".raw.png")
    driver.save_screenshot(str(tmp_path))
    try:
        with Image.open(tmp_path) as img:
            img = img.convert("RGB")
            if img.size != WINDOW_SIZE:
                # headless-снимок может прийти с devicePixelRatio != 1 -- сжимаем к целевому
                # размеру телефона, не обрезая содержимое (что снято, то и видно).
                img = img.resize(WINDOW_SIZE, Image.LANCZOS)
            img.save(out_path, format="PNG", optimize=True)
    finally:
        tmp_path.unlink(missing_ok=True)


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
    sys.path.insert(0, str(REPO_ROOT))
    import web_theme  # noqa: F401 -- прогрев импорта до попытки поднять браузер (fail fast)

    # cwd=REPO_ROOT (не DEMO_SERVER.parent!) -- `config.py` читает `.env` относительно cwd
    # (`SettingsConfigDict(env_file=".env")`), а demo_server.py сам вставляет REPO_ROOT в
    # sys.path и резолвит свои пути через `__file__`, так что cwd для его собственных путей
    # значения не имеет.
    proc = subprocess.Popen(
        [sys.executable, str(DEMO_SERVER)],
        cwd=str(REPO_ROOT),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )
    try:
        if not _wait_for_server(proc):
            out = proc.stdout.read().decode("utf-8", errors="replace") if proc.stdout else ""
            print("СТОП: demo-сервер не поднялся вовремя.", file=sys.stderr)
            if out:
                print(out[-2000:], file=sys.stderr)
            return 1

        try:
            from selenium import webdriver
            from selenium.webdriver.chrome.options import Options

            opts = Options()
            opts.add_argument("--headless=new")
            opts.add_argument(f"--window-size={WINDOW_SIZE[0]},{WINDOW_SIZE[1]}")
            opts.add_argument("--hide-scrollbars")
            driver = webdriver.Chrome(options=opts)
        except Exception as exc:  # noqa: BLE001 -- любая причина недоступности браузера
            print(f"СТОП: не удалось поднять headless-браузер: {exc}", file=sys.stderr)
            print(
                "Превью не рисуются руками и не подменяются AI-генерацией -- см. SUMMARY "
                "плана 19.1-07.",
                file=sys.stderr,
            )
            return 1

        db_path = _demo_db_path()
        try:
            for preset_name in web_theme.PRESETS:
                _write_preset(db_path, preset_name)
                out_path = PREVIEW_DIR / f"{preset_name}.png"
                _shoot(driver, out_path)
                print(f"OK: {out_path}")
        finally:
            driver.quit()
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
