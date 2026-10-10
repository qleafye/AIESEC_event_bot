"""Приёмка 10.10: на главной приложения счётчик баллов писал «4 операций». Теперь шаблон из
реестра (`miniapp_coins_ops_count_text`, «{n} {операция|операции|операций}») через тот же
`formatCount` (ui.js), что у остальных счётчиков; у делегата и у менеджера одна функция.
"""
from tests._paths import REPO_ROOT
import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from miniapp.routers.page import SCREEN_TEXT_KEYS
from services.i18n_miniapp_manual import MANUAL_EN
from domain.settings.schema import SETTINGS_SCHEMA

ROOT = REPO_ROOT
HUB_JS = ROOT / "miniapp" / "static" / "js" / "screens" / "hub.js"
UI_JS = ROOT / "miniapp" / "static" / "js" / "ui.js"
TEMPLATE = SETTINGS_SCHEMA["miniapp_coins_ops_count_text"]["default"]


def test_template_in_registry_shell_and_english():
    assert TEMPLATE == "{n} {операция|операции|операций}"
    assert SCREEN_TEXT_KEYS["coins_ops_count"] == "miniapp_coins_ops_count_text"
    assert MANUAL_EN[TEMPLATE] == "{n} {operation|operations|operations}"


def test_hub_has_no_ops_literal_and_uses_shared_counter():
    src = HUB_JS.read_text(encoding="utf-8")
    assert "операций" not in src
    assert src.count("opsCount(") == 3  # объявление + главная делегата + плитка менеджера
    assert re.search(r'formatCount\(screenText\("coins_ops_count"\)', src)


@pytest.mark.parametrize("n, expected", [
    (1, "1 операция"), (4, "4 операции"), (5, "5 операций"), (11, "11 операций"),
    (21, "21 операция"), (0, "0 операций"),
])
def test_format_count_declines_ops(n, expected):
    node = shutil.which("node")
    if not node:
        pytest.skip("node не найден в PATH")
    script = (
        f"const m = await import({json.dumps(UI_JS.resolve().as_uri())});"
        f"console.log(m.formatCount({json.dumps(TEMPLATE, ensure_ascii=False)}, {n}));"
    )
    proc = subprocess.run(
        [node, "--input-type=module", "-e", script],
        cwd=str(ROOT), capture_output=True, text=True, encoding="utf-8", timeout=120,
    )
    assert proc.returncode == 0, proc.stderr[-2000:]
    assert proc.stdout.strip().splitlines()[-1] == expected
