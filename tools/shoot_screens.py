"""Phase 19.1 Plan 08: обход всех экранов обеих веб-поверхностей во всех заявленных
сочетаниях (пресет × тема × раскладка навигации) — вход в чек-лист `19.1-VISUAL-CHECKLIST.md`.

Обобщает `tools/make_theme_previews.py` (план 19.1-07): общий код (жизненный цикл demo-
сервера/headless-браузера/запись пресета) — в `tools/_shoot_common.py`, здесь — только обход
экранов.

Запуск:
    python tools/shoot_screens.py            # снять всё + проверить (ни одного пустого файла)
    python tools/shoot_screens.py --check     # только проверить уже снятое, без headless-Chrome

Механика (см. `<interfaces>`/задача 1 плана 19.1-08):
- Mini App — `.planning/phases/19-mini-app/demo_server.py` (уже существующий dev-артефакт,
  auth-подмена `?as=<telegram_id>`), плюс отдельный ВТОРОЙ прогон того же харнесса с пустым
  посевом (без задач/сдач/монет) — только так добываются подлинно пустые списочные состояния.
- Дашборд — `dashboard.main.create_app` поднят programmatically на соседнем порту той же demo.db
  (общий `DashboardConfig`, тот же приём, что `tests/test_dashboard_routes.py::_sign` для
  входа через `/auth/callback`).
- Раскладка навигации (`tabbar`/`toptabs`) — временная правка ОДНОЙ строки `NAV_LAYOUT` в
  `miniapp/static/js/app.js` (ровно тот механизм, которым переключает раскладку прод — см.
  `app.js:26-31`), побайтно восстанавливается в `finally` даже при исключении.
- Тёмная тема — мок `window.Telegram.WebApp.colorScheme` через CDP
  `Page.addScriptToEvaluateOnNewDocument` (headless Chrome не Telegram-клиент, `tg` иначе
  всегда `undefined` — dark недостижим для съёмки без мока).
- Экраны состояний (open-in-bot/expired/no-access/disabled/missing) — настоящие серверные пути
  (403/401/503/неизвестный hash), не подмена рендера в JS: expired получает намеренно
  просроченный `auth_date` через `&expired=1` (добавлено в demo_server.py этим планом, см.
  SUMMARY — правка локального dev-артефакта вне git, ничего не коммитит).

Fail-soft (T-19.1-30..32): если demo-сервер/headless-браузер не поднялись — печатает
диагностику и завершается с ненулевым кодом, ни одна картинка не рисуется руками.
"""
from __future__ import annotations

import json
import re
import sys
import time
from contextlib import contextmanager
from pathlib import Path

from _shoot_common import (
    DEMO_SERVER,
    MINIAPP_BASE_URL,
    MINIAPP_HEALTH_URL,
    REPO_ROOT,
    demo_db_path,
    ensure_repo_on_path,
    inject_tg_mock,
    make_chrome_driver,
    reset_demo_db,
    save_screenshot,
    start_demo_server_process,
    stop_process,
    wait_for_server,
    write_preset,
    write_setting,
)

SHOTS_DIR = REPO_ROOT / ".planning" / "phases" / "19.1-web-design-pass" / "shots"
APP_JS_PATH = REPO_ROOT / "miniapp" / "static" / "js" / "app.js"

MINIAPP_WINDOW = (390, 844)  # типичный телефон, как в make_theme_previews.py
DASHBOARD_WINDOW = (1280, 900)

ONBOARDING_KEY = "aiesec_miniapp_onboarding_seen_v1"
MIN_PNG_BYTES = 2000  # пустой/белый снимок PNG сжимается в единицы байт — порог отсева

# ── идентификаторы демо-людей (те же, что demo_server.py/tests/test_miniapp_routes.py) ──────
DELEGATE_ID = 900100
GAME_MANAGER_ID = 900600
ADMIN_ID = 900001

# ── экраны делегата/менеджера (маршруты app.js::ROUTES) ─────────────────────────────────────
DELEGATE_SCREENS = [
    ("hub", "#/hub"),
    ("tasks", "#/tasks"),
    ("task-card", "#/task/{task_id}"),
    ("submit", "#/submit/{task_id}"),
    ("coins", "#/coins"),
    ("leaderboard", "#/leaderboard"),
    ("profile", "#/profile"),
]
MANAGER_SCREENS = [
    ("hub-manager", "#/hub"),
    ("review", "#/review"),
    ("stats", "#/stats"),
    ("admin-tasks", "#/admin-tasks"),
    ("task-edit-new", "#/task-edit/new"),
    ("task-edit", "#/task-edit/{task_id}"),
    ("admin-coins", "#/admin-coins"),
    ("settings", "#/settings"),
]

NAV_LAYOUT_RE = re.compile(r'const NAV_LAYOUT = "(\w+)";')


# ── общие помощники съёмки Mini App ──────────────────────────────────────────────────────────

def _wait_screen_ready(driver, timeout_s: float = 10) -> None:
    from selenium.webdriver.support.ui import WebDriverWait

    def ready(d):
        return d.execute_script(
            "var s = document.getElementById('screen');"
            "return !!s && s.children.length > 0 && !s.querySelector('.loading');"
        )

    WebDriverWait(driver, timeout_s).until(ready)


def shoot_miniapp_screen(driver, base_url, telegram_id, hash_fragment, out_path,
                          skip_onboarding=True, task_id=None, wait_selector=True) -> None:
    url = f"{base_url}/app?as={telegram_id}"
    driver.get(url)
    if skip_onboarding:
        driver.execute_script(f"localStorage.setItem('{ONBOARDING_KEY}', '1');")
    else:
        driver.execute_script(f"localStorage.removeItem('{ONBOARDING_KEY}');")
    hash_ = hash_fragment.format(task_id=task_id) if task_id is not None else hash_fragment
    driver.get(f"{url}{hash_}")
    if wait_selector:
        _wait_screen_ready(driver)
    time.sleep(0.3)  # докрутка счётчика монет / шрифты — тот же запас, что в make_theme_previews.py
    save_screenshot(driver, out_path, MINIAPP_WINDOW)


@contextmanager
def nav_layout_override(layout: str):
    """Временно переключает `NAV_LAYOUT` в app.js (см. модульный докстринг) -- побайтно
    восстанавливает файл в finally, даже если съёмка внутри блока упала."""
    original = APP_JS_PATH.read_text(encoding="utf-8")
    if not NAV_LAYOUT_RE.search(original):
        raise RuntimeError(f"NAV_LAYOUT constant не найден в {APP_JS_PATH}")
    patched = NAV_LAYOUT_RE.sub(f'const NAV_LAYOUT = "{layout}";', original, count=1)
    APP_JS_PATH.write_text(patched, encoding="utf-8")
    try:
        yield
    finally:
        APP_JS_PATH.write_text(original, encoding="utf-8")
        restored = APP_JS_PATH.read_text(encoding="utf-8")
        if restored != original:
            # Не должно случиться (мы только что сами это записали) -- но если случилось,
            # кричим явно, а не оставляем правку раскладки тихо висеть в рабочем дереве.
            raise RuntimeError(f"{APP_JS_PATH} не восстановлен побайтно после NAV_LAYOUT override")


# ── дашборд ──────────────────────────────────────────────────────────────────────────────────

def _sign_login_payload(telegram_id: int, bot_token: str) -> dict:
    import hashlib
    import hmac as hmac_mod

    base = {"id": str(telegram_id), "first_name": "Демо", "auth_date": str(int(time.time()) - 5)}
    check_string = "\n".join(f"{k}={v}" for k, v in sorted(base.items()))
    secret = hashlib.sha256(bot_token.encode()).digest()
    signature = hmac_mod.new(secret, check_string.encode(), hashlib.sha256).hexdigest()
    return {**base, "hash": signature}


def _start_dashboard_thread(db_path: Path, bot_token: str, port: int):
    """Дашборд не делает собственного посева при импорте (в отличие от demo_server.py) --
    поднимается в фоновом потоке ТОГО ЖЕ процесса поверх уже засеянной demo.db, без второго
    subprocess."""
    import threading

    import uvicorn
    from dashboard.main import create_app as create_dashboard_app
    from tests.test_miniapp_routes import _cfg

    cfg = _cfg(str(db_path), bot_token=bot_token)
    app = create_dashboard_app(cfg=cfg)
    config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning")
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    return server, thread, cfg.bot_token


def shoot_dashboard(driver, base_url, bot_token, telegram_id, out_path, expect_denied=False) -> None:
    driver.delete_all_cookies()
    driver.get(f"{base_url}/health")  # первая навигация на origin -- иначе delete_all_cookies бесполезен
    payload = _sign_login_payload(telegram_id, bot_token)
    qs = "&".join(f"{k}={v}" for k, v in payload.items())
    driver.get(f"{base_url}/auth/callback?{qs}")
    driver.get(base_url)
    from selenium.webdriver.support.ui import WebDriverWait
    from selenium.webdriver.common.by import By
    selector = ".centered-card" if expect_denied else "body"
    WebDriverWait(driver, 10).until(lambda d: d.find_elements(By.CSS_SELECTOR, selector))
    time.sleep(0.3)
    save_screenshot(driver, out_path, DASHBOARD_WINDOW)


def shoot_dashboard_login(driver, base_url, out_path) -> None:
    driver.delete_all_cookies()
    driver.get(f"{base_url}/login")
    time.sleep(0.2)
    save_screenshot(driver, out_path, DASHBOARD_WINDOW)


# ── manifest (список ожидаемых файлов) -- общий источник для съёмки и --check ───────────────

def build_manifest() -> list[str]:
    names = []
    for preset in ("bluebook", "youlead"):
        for name, _ in DELEGATE_SCREENS + MANAGER_SCREENS:
            names.append(f"miniapp-{name}-{preset}-light-hub.png")
    for name, _ in DELEGATE_SCREENS + MANAGER_SCREENS:
        names.append(f"miniapp-{name}-bluebook-dark-hub.png")
    for name in ("hub", "tasks"):
        for layout in ("tabbar", "toptabs"):
            names.append(f"miniapp-{name}-bluebook-light-{layout}.png")
    for name in ("hub-manager", "admin-tasks"):
        for layout in ("tabbar", "toptabs"):
            names.append(f"miniapp-{name}-bluebook-light-{layout}.png")
    names.append("miniapp-onboarding-bluebook-light-hub.png")
    for state in ("missing", "no-access", "disabled", "open-in-bot", "expired"):
        names.append(f"miniapp-state-{state}-bluebook-light-na.png")
    for name in ("tasks-empty", "coins-empty", "review-empty", "admin-tasks-empty"):
        names.append(f"miniapp-{name}-bluebook-light-hub.png")
    for preset in ("bluebook", "youlead"):
        names.append(f"dashboard-home-{preset}-light-na.png")
    names.append("dashboard-login-bluebook-light-na.png")
    names.append("dashboard-no-access-bluebook-light-na.png")
    names.append("dashboard-home-bluebook-oslight-na.png")  # системная тёмная ОС, D-06 light-lock
    return names


# ── основной прогон ──────────────────────────────────────────────────────────────────────────

def run_full_pass() -> int:
    if not DEMO_SERVER.is_file():
        print(f"СТОП: demo-сервер не найден: {DEMO_SERVER}", file=sys.stderr)
        print(
            "Локальный dev-артефакт вне git (.planning/ в .gitignore) -- на этой машине его "
            "нет. Съёмка не подменяется рассуждением по коду -- передать владельцу на стенде "
            "(19-10).",
            file=sys.stderr,
        )
        return 1

    SHOTS_DIR.mkdir(parents=True, exist_ok=True)
    ensure_repo_on_path()
    import web_theme  # noqa: F401 -- прогрев импорта (fail fast)

    try:
        make_chrome_driver((100, 100)).quit()  # прогрев/проверка headless-браузера отдельно от съёмки
    except Exception as exc:  # noqa: BLE001
        print(f"СТОП: не удалось поднять headless-браузер: {exc}", file=sys.stderr)
        return 1

    reset_demo_db()
    proc = start_demo_server_process()
    try:
        if not wait_for_server(proc, MINIAPP_HEALTH_URL):
            out = proc.stdout.read().decode("utf-8", errors="replace") if proc.stdout else ""
            print("СТОП: demo-сервер (Mini App) не поднялся вовремя.", file=sys.stderr)
            if out:
                print(out[-2000:], file=sys.stderr)
            return 1

        db_path = demo_db_path()
        task_row = None
        for _ in range(20):
            task_row = None
            try:
                import sqlite3
                conn = sqlite3.connect(str(db_path))
                task_row = conn.execute("SELECT id FROM game_tasks ORDER BY id LIMIT 1").fetchone()
                conn.close()
            except Exception:
                pass
            if task_row:
                break
            time.sleep(0.2)
        if not task_row:
            print("СТОП: в demo.db нет ни одной задачи -- посев demo_server.py не сработал.", file=sys.stderr)
            return 1
        task_id = task_row[0]

        driver_light = make_chrome_driver(MINIAPP_WINDOW)
        try:
            # CDN telegram.org заблокирован в make_chrome_driver (см. _shoot_common.py) --
            # ставим ЯВНЫЙ light-мок, а не полагаемся на «tg остался undefined», чтобы
            # MainButton/BackButton/HapticFeedback вели себя одинаково предсказуемо во всех
            # снимках, а не зависели от доступности интернета на машине, где запускается скрипт.
            inject_tg_mock(driver_light, "light")
            # ── онбординг: снять ДО остальных делегатских шотов -- иначе localStorage уже
            # помечен как «видел» предыдущим прогоном тем же browser-профилем ──────────────
            write_preset(db_path, "bluebook")
            shoot_miniapp_screen(
                driver_light, MINIAPP_BASE_URL, DELEGATE_ID, "#/hub",
                SHOTS_DIR / "miniapp-onboarding-bluebook-light-hub.png",
                skip_onboarding=False,
            )
            print("OK: onboarding")

            # ── основной обход: bluebook light + youlead light, всё дерево экранов ─────────
            for preset in ("bluebook", "youlead"):
                write_preset(db_path, preset)
                for name, hash_tpl in DELEGATE_SCREENS:
                    tid = task_id if "{task_id}" in hash_tpl else None
                    shoot_miniapp_screen(
                        driver_light, MINIAPP_BASE_URL, DELEGATE_ID, hash_tpl,
                        SHOTS_DIR / f"miniapp-{name}-{preset}-light-hub.png", task_id=tid,
                    )
                for name, hash_tpl in MANAGER_SCREENS:
                    tid = task_id if "{task_id}" in hash_tpl else None
                    shoot_miniapp_screen(
                        driver_light, MINIAPP_BASE_URL, GAME_MANAGER_ID if name != "settings" else ADMIN_ID,
                        hash_tpl, SHOTS_DIR / f"miniapp-{name}-{preset}-light-hub.png", task_id=tid,
                    )
                print(f"OK: {preset} light — delegate+manager screens")

            write_preset(db_path, "bluebook")  # вернуть дефолт перед раскладками/состояниями

            # ── три раскладки навигации: hub уже снят выше -- добавляем tabbar/toptabs ─────
            for layout in ("tabbar", "toptabs"):
                with nav_layout_override(layout):
                    shoot_miniapp_screen(
                        driver_light, MINIAPP_BASE_URL, DELEGATE_ID, "#/hub",
                        SHOTS_DIR / f"miniapp-hub-bluebook-light-{layout}.png",
                    )
                    shoot_miniapp_screen(
                        driver_light, MINIAPP_BASE_URL, DELEGATE_ID, "#/tasks",
                        SHOTS_DIR / f"miniapp-tasks-bluebook-light-{layout}.png",
                    )
                    shoot_miniapp_screen(
                        driver_light, MINIAPP_BASE_URL, GAME_MANAGER_ID, "#/hub",
                        SHOTS_DIR / f"miniapp-hub-manager-bluebook-light-{layout}.png",
                    )
                    shoot_miniapp_screen(
                        driver_light, MINIAPP_BASE_URL, GAME_MANAGER_ID, "#/admin-tasks",
                        SHOTS_DIR / f"miniapp-admin-tasks-bluebook-light-{layout}.png",
                    )
                print(f"OK: nav layout {layout}")

            # ── экраны состояний (настоящие серверные пути, не рендер-подмена) ─────────────
            shoot_miniapp_screen(
                driver_light, MINIAPP_BASE_URL, DELEGATE_ID, "#/does-not-exist-screen",
                SHOTS_DIR / "miniapp-state-missing-bluebook-light-na.png",
            )
            shoot_miniapp_screen(
                driver_light, MINIAPP_BASE_URL, DELEGATE_ID, "#/review",
                SHOTS_DIR / "miniapp-state-no-access-bluebook-light-na.png",
            )
            write_setting(db_path, "miniapp_enabled", "off")
            driver_light.get(f"{MINIAPP_BASE_URL}/app")
            time.sleep(0.3)
            save_screenshot(driver_light, SHOTS_DIR / "miniapp-state-disabled-bluebook-light-na.png", MINIAPP_WINDOW)
            write_setting(db_path, "miniapp_enabled", "on")

            driver_light.delete_all_cookies()
            driver_light.get(f"{MINIAPP_BASE_URL}/app")
            time.sleep(0.3)
            save_screenshot(driver_light, SHOTS_DIR / "miniapp-state-open-in-bot-bluebook-light-na.png", MINIAPP_WINDOW)

            driver_light.get(f"{MINIAPP_BASE_URL}/app?as={DELEGATE_ID}&expired=1")
            time.sleep(0.3)
            save_screenshot(driver_light, SHOTS_DIR / "miniapp-state-expired-bluebook-light-na.png", MINIAPP_WINDOW)
            print("OK: state screens")

            # ── дашборд (общий процесс, отдельный порт, та же demo.db) ─────────────────────
            from tests.test_miniapp_auth import TOKEN as DEMO_BOT_TOKEN
            server, thread, bot_token = _start_dashboard_thread(db_path, DEMO_BOT_TOKEN, 8766)
            dash_url = "http://127.0.0.1:8766"
            try:
                for _ in range(40):
                    try:
                        import urllib.request
                        with urllib.request.urlopen(f"{dash_url}/health", timeout=1) as resp:
                            if resp.status == 200:
                                break
                    except Exception:
                        time.sleep(0.25)

                driver_dash = make_chrome_driver(DASHBOARD_WINDOW)
                try:
                    shoot_dashboard_login(driver_dash, dash_url, SHOTS_DIR / "dashboard-login-bluebook-light-na.png")
                    for preset in ("bluebook", "youlead"):
                        write_preset(db_path, preset)
                        shoot_dashboard(driver_dash, dash_url, bot_token, ADMIN_ID,
                                         SHOTS_DIR / f"dashboard-home-{preset}-light-na.png")
                    write_preset(db_path, "bluebook")
                    shoot_dashboard(driver_dash, dash_url, bot_token, DELEGATE_ID,
                                     SHOTS_DIR / "dashboard-no-access-bluebook-light-na.png", expect_denied=True)

                    # D-06: дашборд остаётся светлым даже при системной тёмной теме браузера.
                    driver_dash.execute_cdp_cmd(
                        "Emulation.setEmulatedMedia",
                        {"features": [{"name": "prefers-color-scheme", "value": "dark"}]},
                    )
                    shoot_dashboard(driver_dash, dash_url, bot_token, ADMIN_ID,
                                     SHOTS_DIR / "dashboard-home-bluebook-oslight-na.png")
                finally:
                    driver_dash.quit()
                print("OK: dashboard")
            finally:
                server.should_exit = True
                thread.join(timeout=5)

        finally:
            driver_light.quit()

        # ── тёмная тема Mini App (отдельный драйвер -- мок tg ставится один раз на сессию) ──
        driver_dark = make_chrome_driver(MINIAPP_WINDOW)
        try:
            inject_tg_mock(driver_dark, "dark")
            write_preset(db_path, "bluebook")
            for name, hash_tpl in DELEGATE_SCREENS:
                tid = task_id if "{task_id}" in hash_tpl else None
                shoot_miniapp_screen(
                    driver_dark, MINIAPP_BASE_URL, DELEGATE_ID, hash_tpl,
                    SHOTS_DIR / f"miniapp-{name}-bluebook-dark-hub.png", task_id=tid,
                )
            for name, hash_tpl in MANAGER_SCREENS:
                tid = task_id if "{task_id}" in hash_tpl else None
                shoot_miniapp_screen(
                    driver_dark, MINIAPP_BASE_URL, GAME_MANAGER_ID if name != "settings" else ADMIN_ID,
                    hash_tpl, SHOTS_DIR / f"miniapp-{name}-bluebook-dark-hub.png", task_id=tid,
                )
            print("OK: bluebook dark — delegate+manager screens")
        finally:
            driver_dark.quit()
    finally:
        stop_process(proc)

    # ── второй прогон: пустой посев -- подлинные пустые списки, не подделка CSS ─────────────
    rc = _run_empty_seed_pass()
    if rc != 0:
        return rc

    return 0


def _run_empty_seed_pass() -> int:
    import textwrap

    scratch = DEMO_SERVER.parent
    empty_db = scratch / "demo_empty.db"
    for suffix in ("", "-wal", "-shm"):
        p = empty_db.with_name(empty_db.name + suffix) if suffix else empty_db
        p.unlink(missing_ok=True)

    script_path = scratch / "_demo_server_empty.py"
    script_path.write_text(textwrap.dedent(f'''\
        """Сгенерировано tools/shoot_screens.py (план 19.1-08) -- пустой посев для съёмки
        подлинно пустых списочных состояний. Локальный dev-артефакт, не коммитится."""
        import sys
        from http.cookies import SimpleCookie
        from pathlib import Path

        ROOT = Path(r"{REPO_ROOT}")
        sys.path.insert(0, str(ROOT))

        import uvicorn  # noqa: E402
        from tests.test_miniapp_routes import (  # noqa: E402
            DELEGATE_ID, GAME_MANAGER_ID, ADMIN_ID, _cfg, _seed, _use_tmp_db,
        )
        from tests.test_miniapp_auth import make_init_data  # noqa: E402
        from miniapp.main import create_app  # noqa: E402

        SCRATCH = Path(r"{scratch}")
        db_path = _use_tmp_db(SCRATCH, "demo_empty.db")
        _seed(
            staff=[(GAME_MANAGER_ID, "game_manager", None)],
            users=[(DELEGATE_ID, "approved")],
            settings={{"miniapp_enabled": "on", "miniapp_section_settings": "on",
                      "event_name": "форума YouLead"}},
        )

        inner = create_app(cfg=_cfg(db_path))


        async def app(scope, receive, send):
            if scope["type"] != "http":
                return await inner(scope, receive, send)
            headers = dict(scope["headers"])
            qs = scope.get("query_string", b"").decode()
            as_id = None
            for part in qs.split("&"):
                if part.startswith("as="):
                    as_id = int(part[3:])
            if as_id is None:
                c = SimpleCookie(headers.get(b"cookie", b"").decode())
                if "demo_as" in c:
                    as_id = int(c["demo_as"].value)
            if as_id:
                scope["headers"] = [(k, v) for k, v in scope["headers"] if k != b"x-telegram-init-data"] + [
                    (b"x-telegram-init-data", make_init_data(user_id=as_id).encode())]

            async def send_wrap(msg):
                if msg["type"] == "http.response.start" and as_id:
                    msg["headers"] = list(msg.get("headers", [])) + [
                        (b"set-cookie", f"demo_as={{as_id}}; Path=/".encode())]
                await send(msg)

            await inner(scope, receive, send_wrap)


        if __name__ == "__main__":
            uvicorn.run(app, host="127.0.0.1", port=8769, log_level="warning")
    '''), encoding="utf-8")

    base_url = "http://127.0.0.1:8769"
    health_url = f"{base_url}/app/health"

    import subprocess
    proc = subprocess.Popen(
        [sys.executable, str(script_path)], cwd=str(REPO_ROOT),
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
    )
    try:
        if not wait_for_server(proc, health_url):
            out = proc.stdout.read().decode("utf-8", errors="replace") if proc.stdout else ""
            print("СТОП: demo-сервер (пустой посев) не поднялся вовремя.", file=sys.stderr)
            if out:
                print(out[-2000:], file=sys.stderr)
            return 1

        driver = make_chrome_driver(MINIAPP_WINDOW)
        try:
            shoot_miniapp_screen(driver, base_url, DELEGATE_ID, "#/tasks",
                                  SHOTS_DIR / "miniapp-tasks-empty-bluebook-light-hub.png")
            shoot_miniapp_screen(driver, base_url, DELEGATE_ID, "#/coins",
                                  SHOTS_DIR / "miniapp-coins-empty-bluebook-light-hub.png")
            shoot_miniapp_screen(driver, base_url, GAME_MANAGER_ID, "#/review",
                                  SHOTS_DIR / "miniapp-review-empty-bluebook-light-hub.png")
            shoot_miniapp_screen(driver, base_url, GAME_MANAGER_ID, "#/admin-tasks",
                                  SHOTS_DIR / "miniapp-admin-tasks-empty-bluebook-light-hub.png")
            print("OK: empty-state screens")
        finally:
            driver.quit()
    finally:
        stop_process(proc)
        script_path.unlink(missing_ok=True)

    return 0


def run_check() -> int:
    manifest = build_manifest()
    missing = []
    empty = []
    for name in manifest:
        path = SHOTS_DIR / name
        if not path.is_file():
            missing.append(name)
        elif path.stat().st_size < MIN_PNG_BYTES:
            empty.append(name)
    if missing:
        print(f"ОТСУТСТВУЮТ ({len(missing)}):")
        for name in missing:
            print(f"  {name}")
    if empty:
        print(f"ПОДОЗРИТЕЛЬНО ПУСТЫЕ (< {MIN_PNG_BYTES} байт, {len(empty)}):")
        for name in empty:
            print(f"  {name}")
    if missing or empty:
        return 1
    print(f"OK: все {len(manifest)} снимков на месте и не пустые.")
    return 0


def main() -> int:
    if "--check" in sys.argv[1:]:
        return run_check()
    rc = run_full_pass()
    if rc != 0:
        return rc
    return run_check()


if __name__ == "__main__":
    raise SystemExit(main())
