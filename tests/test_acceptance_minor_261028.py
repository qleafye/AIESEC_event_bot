"""28.10.2026 — мелкие находки приёмки (разбор регистрации 10.10 и бэклог).

- Менеджерские плитки Mini App рождались с подписью «…»: у плиток без загрузчика счётчика
  («Отбор заявок», «Частые вопросы», «Сканер») и при ошибке запроса «…» висел навсегда.
  Теперь подпись — либо скелетон на время реального запроса, либо ничего.
- На кнопках развилки резюме в Mini App стояли две иконки: Lucide слева и эмодзи из подписи
  реестра. Эмодзи снимается `labelText`, подписи в реестре (и в чате) прежние.
- Кнопка шага мультивыбора была подписана своим ключом («Продолжить»), остальные шаги
  мастера — «Дальше». Один ключ `reg_form_next_cta_text`; сохранённое старое значение
  уважается, пока менеджер не задал общий.
- «🔄 Сверить состав чата» показывалась без подключённого чата и на нажатие отвечала
  «не подключён» — теперь без привязки кнопки нет.
- Вуз «СПбГЭУ» не находился по своей аббревиатуре.
"""
from __future__ import annotations

import asyncio
import json
import re
import shutil
import subprocess

import pytest

from tests._paths import REPO_ROOT

HUB_JS = REPO_ROOT / "miniapp" / "static" / "js" / "screens" / "hub.js"
UI_JS = REPO_ROOT / "miniapp" / "static" / "js" / "ui.js"
FORM_JS = REPO_ROOT / "miniapp" / "static" / "js" / "form.js"
APP_CSS = REPO_ROOT / "miniapp" / "static" / "app.css"


def _code_lines(path):
    return [ln for ln in path.read_text(encoding="utf-8").splitlines() if not ln.lstrip().startswith("//")]


# ── плитки менеджера ───────────────────────────────────────────────────────────────────


def test_hub_has_no_ellipsis_meta():
    src = HUB_JS.read_text(encoding="utf-8")
    assert 'meta: "…"' not in src
    assert not any("…" in ln for ln in _code_lines(HUB_JS))


def test_hub_single_fetcher_predicate():
    src = HUB_JS.read_text(encoding="utf-8")
    assert "function managerFetcherFor(" in src
    assert src.count("managerFetcherFor(") >= 3  # объявление + плитка + цикл запросов
    # своя копия проверки права в цикле запросов не живёт — только внутри предиката
    assert src.count("me.caps.includes(fetcher.cap)") == 1


def test_hub_skeleton_set_and_cleared():
    src = HUB_JS.read_text(encoding="utf-8")
    assert "skeleton--meta" in src
    body = src[src.index("async function renderManagerHub("):]
    body = body[:body.index("\nconst HUB_MODE_KEY")]
    assert re.search(r"catch\s*\(_\)\s*\{[^}]*textContent\s*=\s*\"\"", body, re.S)
    assert re.search(r"classList\.remove\(\s*\"skeleton\",\s*\"skeleton--meta\"\s*\)", body)


def test_css_skeleton_meta_rule():
    css = APP_CSS.read_text(encoding="utf-8")
    m = re.search(r"\.skeleton--meta\s*\{([^}]*)\}", css)
    assert m, ".skeleton--meta не объявлен"
    rule = m.group(1)
    assert "width" in rule and "height" in rule
    assert not re.search(r"#[0-9a-fA-F]{3,8}\b|rgba?\(", rule)


# ── развилка резюме ────────────────────────────────────────────────────────────────────


def test_resume_fork_buttons_strip_emoji():
    src = FORM_JS.read_text(encoding="utf-8")
    assert re.search(r'import \{[^}]*\blabelText\b[^}]*\} from "\./ui\.js";', src)
    body = src[src.index("function resumeForkControl("):]
    body = body[:body.index("\n}\n")]
    assert '"aria-label": labelText(opt.label)' in body
    assert 'h("span", { text: labelText(opt.label) })' in body
    assert "icon(opt.icon)" in body
    assert "text: opt.label" not in src


def test_resume_fork_labels_without_leading_emoji():
    from domain.settings.schema import SETTINGS_SCHEMA

    node = shutil.which("node")
    if not node:
        pytest.skip("node не найден в PATH")
    keys = ["reg_resume_fork_file_label", "reg_resume_fork_link_label",
            "reg_resume_fork_text_label", "reg_resume_fork_none_label"]
    labels = [SETTINGS_SCHEMA[k]["default"] for k in keys]
    script = (
        f"const m = await import({json.dumps(UI_JS.resolve().as_uri())});"
        f"console.log(JSON.stringify({json.dumps(labels, ensure_ascii=False)}.map(m.labelText)));"
    )
    proc = subprocess.run(
        [node, "--input-type=module", "-e", script],
        cwd=str(REPO_ROOT), capture_output=True, text=True, encoding="utf-8", timeout=120,
    )
    assert proc.returncode == 0, proc.stderr[-2000:]
    out = json.loads(proc.stdout.strip().splitlines()[-1])
    assert out[0] == "Загрузить файл"
    for text in out:
        assert text and (text[0].isalnum()), text
