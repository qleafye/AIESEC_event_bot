"""Phase 19.1 Plan 08: общий код съёмки веб-поверхностей — вынесен из
`tools/make_theme_previews.py` (план 19.1-07), чтобы `tools/shoot_screens.py` (этот план) не
дублировал подъём demo-сервера/headless-браузера/запись пресета.

Ничего специфичного для конкретного скрипта здесь нет: жизненный цикл demo-сервера
(`.planning/phases/19-mini-app/demo_server.py`, локальный dev-артефакт вне git), поднятие
headless Chrome (Selenium Manager сам подтягивает chromedriver), запись ручек пресета в
demo.db тем же приёмом, что `miniapp_preset_apply` в проде.
"""
from __future__ import annotations

import os
import subprocess
import sys
import sqlite3
import time
import urllib.error
import urllib.request
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
DEMO_SERVER = REPO_ROOT / ".planning" / "phases" / "19-mini-app" / "demo_server.py"

MINIAPP_BASE_URL = "http://127.0.0.1:8765"
MINIAPP_HEALTH_URL = f"{MINIAPP_BASE_URL}/app/health"

STARTUP_TIMEOUT_S = 20


def demo_db_path() -> Path:
    # demo_server.py: `SCRATCH = Path(__file__).parent; db_path = _use_tmp_db(SCRATCH, "demo.db")`
    return DEMO_SERVER.parent / "demo.db"


def reset_demo_db() -> None:
    """Удаляет demo.db (+ -wal/-shm) перед стартом. Без этого повторный запуск падает:
    `_standard_seed()`/`_seed()` в `demo_server.py` делают обычный `INSERT` (не upsert) в
    `staff`/`users`/`tasks` — второй прогон на уже существующей БД валится на UNIQUE
    constraint. Обнаружено при подготовке этого плана (Rule 1 — баг воспроизводимости
    `make_theme_previews.py`, чинится здесь и там же переиспользуется)."""
    db_path = demo_db_path()
    for suffix in ("", "-wal", "-shm"):
        p = db_path.with_name(db_path.name + suffix) if suffix else db_path
        p.unlink(missing_ok=True)


def wait_for_server(proc: subprocess.Popen, health_url: str, timeout_s: float = STARTUP_TIMEOUT_S) -> bool:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            return False  # процесс уже упал
        try:
            with urllib.request.urlopen(health_url, timeout=1) as resp:
                if resp.status == 200:
                    return True
        except (urllib.error.URLError, ConnectionError, TimeoutError):
            time.sleep(0.3)
    return False


def start_demo_server_process() -> subprocess.Popen:
    """Поднимает `demo_server.py` как subprocess. cwd=REPO_ROOT (не DEMO_SERVER.parent!) --
    `config.py` читает `.env` относительно cwd, а `demo_server.py` сам резолвит свои пути
    через `__file__` (найдено в плане 19.1-07, деviation #2)."""
    return subprocess.Popen(
        [sys.executable, str(DEMO_SERVER)],
        cwd=str(REPO_ROOT),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )


def stop_process(proc: subprocess.Popen) -> None:
    proc.terminate()
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()


def write_preset(db_path: Path, preset_name: str) -> None:
    """Пишет ВСЕ ручки пресета разом, тем же приёмом, что `miniapp_preset_apply` в
    `handlers/admin_miniapp_theme.py` — снимок должен показывать ровно то, что получит
    менеджер после «Применить», не какое-то отдельное демо-состояние."""
    import web_theme  # локальный импорт: REPO_ROOT уже в sys.path (см. ensure_repo_on_path())

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


def write_setting(db_path: Path, key: str, value: str) -> None:
    conn = sqlite3.connect(str(db_path))
    try:
        conn.execute(
            "INSERT INTO bot_settings (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )
        conn.commit()
    finally:
        conn.close()


def query_one(db_path: Path, sql: str, params: tuple = ()):
    conn = sqlite3.connect(str(db_path))
    try:
        row = conn.execute(sql, params).fetchone()
        return row
    finally:
        conn.close()


def ensure_repo_on_path() -> None:
    if str(REPO_ROOT) not in sys.path:
        sys.path.insert(0, str(REPO_ROOT))


def shoot_scale() -> float:
    """Множитель плотности снимка: `SHOOT_SCALE=2` -- телефонные PNG выходят в 2x
    (780x1688 вместо 390x844), CSS-вьюпорт при этом не меняется (390x844) -- лечит жалобу
    владельца «сжатые фотки», источник которой -- 1x захват при рендере на телефоне 2-3x."""
    try:
        return float(os.environ.get("SHOOT_SCALE", "1"))
    except ValueError:
        return 1.0


def make_chrome_driver(window_size: tuple[int, int], disable_cache: bool = True):
    from selenium import webdriver
    from selenium.webdriver.chrome.options import Options

    opts = Options()
    opts.add_argument("--headless=new")
    opts.add_argument(f"--window-size={window_size[0]},{window_size[1]}")
    opts.add_argument("--hide-scrollbars")
    scale = shoot_scale()
    if scale != 1:
        opts.add_argument(f"--force-device-scale-factor={scale}")
    driver = webdriver.Chrome(options=opts)
    if disable_cache:
        # Между снимками tools/shoot_screens.py временно правит app.js (переключение
        # NAV_LAYOUT, план 19.1-08 задача 1) -- без сброса HTTP-кэша браузер отдал бы старую
        # версию модуля из памяти вместо только что записанной на диск.
        driver.execute_cdp_cmd("Network.enable", {})
        driver.execute_cdp_cmd("Network.setCacheDisabled", {"cacheDisabled": True})
        # `miniapp/templates/app.html` подключает НАСТОЯЩИЙ SDK с CDN telegram.org -- если на
        # машине есть интернет, он реально грузится в headless Chrome и молча ЗАТИРАЕТ мок
        # `window.Telegram.WebApp`, поставленный `inject_tg_mock` через
        # `Page.addScriptToEvaluateOnNewDocument` (тот выполняется раньше, но CDN-скрипт в
        # <head> переопределяет объект целиком, а не мёржит). Найдено планом 19.1-08 при
        # проверке тёмной темы -- без блокировки съёмка dark оказывалась не тёмной. Блокируем
        # запрос -- тег падает молча (fail), инжектированный мок остаётся единственным `tg`.
        driver.execute_cdp_cmd("Network.setBlockedURLs", {"urls": ["*telegram.org*"]})
    return driver


# Минимальный мок `window.Telegram.WebApp` для CDP `Page.addScriptToEvaluateOnNewDocument`.
# Headless Chrome не является Telegram-клиентом -- без мока `tg` в app.js всегда `undefined`,
# и тёмная тема (которая читается ТОЛЬКО из `tg.colorScheme`, D-06/D-07) физически недостижима
# для съёмки. Остальные методы -- no-op заглушки, ничего не проверяют в самом приложении.
_TG_MOCK_TEMPLATE = """
window.Telegram = window.Telegram || {};
window.Telegram.WebApp = {
  colorScheme: %(color_scheme)r,
  themeParams: %(theme_params)s,
  initData: "",
  platform: "web",
  contentSafeAreaInset: {top: 0, bottom: 0, left: 0, right: 0},
  safeAreaInset: {top: 0, bottom: 0, left: 0, right: 0},
  ready() {}, expand() {}, close() {},
  disableVerticalSwipes() {},
  onEvent() {}, offEvent() {},
  BackButton: { show() {}, hide() {}, onClick() {}, offClick() {} },
  MainButton: { show() {}, hide() {}, setText() {}, onClick() {}, offClick() {}, enable() {}, disable() {} },
  HapticFeedback: { notificationOccurred() {}, impactOccurred() {} },
  // D13 (quick 260904-de4): без этих двух методов headless-съёмка шага телефона не покажет
  // кнопку «Поделиться номером» (canShareContact в screens/form.js требует обоих) — падать
  // при клике незачем, съёмка кнопку не нажимает, но она обязана хотя бы быть на экране.
  isVersionAtLeast(v) { return true; },
  requestContact(cb) { cb(true, { status: "sent", responseUnsafe: { contact: { phone_number: "+79161234567" } } }); },
};
"""


def inject_tg_mock(driver, color_scheme: str = "light") -> None:
    """Ставит мок ДО первого исполнения любого document (`Page.addScriptToEvaluateOnNewDocument`)
    -- должно быть вызвано один раз до первого `driver.get(...)` в сессии."""
    import json

    theme_params = {"bg_color": "#131519", "text_color": "#F5F6F8"} if color_scheme == "dark" else {}
    script = _TG_MOCK_TEMPLATE % {
        "color_scheme": color_scheme,
        "theme_params": json.dumps(theme_params),
    }
    driver.execute_cdp_cmd("Page.addScriptToEvaluateOnNewDocument", {"source": script})


def save_screenshot(driver, out_path: Path, window_size: tuple[int, int]) -> None:
    """headless-снимок может прийти с devicePixelRatio != 1 (в т.ч. специально задранным
    через SHOOT_SCALE/--force-device-scale-factor, см. shoot_scale()) -- сжимаем/дотягиваем
    к целевому размеру окна * SHOOT_SCALE, не обрезая содержимое (что снято, то и видно)."""
    from PIL import Image

    scale = shoot_scale()
    target_size = (round(window_size[0] * scale), round(window_size[1] * scale))
    tmp_path = out_path.with_suffix(".raw.png")
    driver.save_screenshot(str(tmp_path))
    try:
        with Image.open(tmp_path) as img:
            img = img.convert("RGB")
            if img.size != target_size:
                img = img.resize(target_size, Image.LANCZOS)
            img.save(out_path, format="PNG", optimize=True)
    finally:
        tmp_path.unlink(missing_ok=True)
