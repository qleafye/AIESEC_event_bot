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


# ── одна подпись «Дальше» в мастере ────────────────────────────────────────────────────


def _db(tmp_path, name="acceptance_minor_261028.db"):
    from config import config
    from tests._dbtpl import fast_init_db

    config.DB_PATH = str(tmp_path / name)
    fast_init_db()


def _continue_button():
    from domain.regform import engine

    return asyncio.run(engine._v2_texts_for("multi", "projects", None))["continue_button"]


def test_continue_key_removed_from_registry():
    from domain.settings.schema import SETTINGS_SCHEMA

    assert "reg_form_continue_button_text" not in SETTINGS_SCHEMA
    assert SETTINGS_SCHEMA["reg_form_next_cta_text"]["default"] == "Дальше"


def test_multi_continue_defaults_to_next_cta(tmp_path):
    _db(tmp_path)
    assert _continue_button() == "Дальше"


def test_multi_continue_follows_next_cta(tmp_path):
    from database.db import set_setting

    _db(tmp_path)
    asyncio.run(set_setting("reg_form_next_cta_text", "Вперёд"))
    assert _continue_button() == "Вперёд"


def test_multi_continue_legacy_value_kept(tmp_path):
    from database.db import set_setting

    _db(tmp_path)
    asyncio.run(set_setting("reg_form_continue_button_text", "Продолжить ✨"))
    assert _continue_button() == "Продолжить ✨"


def test_multi_continue_next_cta_wins_over_legacy(tmp_path):
    from database.db import set_setting

    _db(tmp_path)
    asyncio.run(set_setting("reg_form_continue_button_text", "Продолжить ✨"))
    asyncio.run(set_setting("reg_form_next_cta_text", "Вперёд"))
    assert _continue_button() == "Вперёд"


def test_multi_continue_blank_legacy_ignored(tmp_path):
    from database.db import set_setting

    _db(tmp_path)
    asyncio.run(set_setting("reg_form_continue_button_text", "   "))
    assert _continue_button() == "Дальше"


# ── «🔄 Сверить состав чата» только при подключённом чате ──────────────────────────────


def _manage_callbacks():
    from handlers.settings.admin_sections import build_section_keyboard
    from tests.test_chat_refresh_admin_260914 import ADMIN_ID

    kb = asyncio.run(build_section_keyboard("manage", ADMIN_ID))
    return {b.callback_data: b.text for row in kb.inline_keyboard for b in row if b.callback_data}


def test_reconcile_hidden_without_bound_chat(tmp_path):
    from tests.test_chat_refresh_admin_260914 import _ready

    _ready(tmp_path)
    cbs = _manage_callbacks()
    assert "admin_chat_reconcile" not in cbs
    assert "admin_chat_cleanup" in cbs and "admin_chat_rating" in cbs


def test_reconcile_shown_with_bound_chat(tmp_path):
    from services import chat_tracking
    from tests.test_chat_refresh_admin_260914 import _ready

    _ready(tmp_path)
    asyncio.run(chat_tracking.bind_chat(None, -1004489658418, "Чат", None))
    assert _manage_callbacks().get("admin_chat_reconcile") == "🔄 Сверить состав чата"


def test_reconcile_row_stays_in_registry():
    from handlers.settings.admin_sections import section_rows

    assert ("screen", "admin_chat_reconcile", "🔄 Сверить состав чата") in section_rows("manage")


# ── «СПбГЭУ» в справочнике вузов ───────────────────────────────────────────────────────

SPBGEU = "Санкт-Петербургский государственный экономический университет"


def _lookup_db(tmp_path):
    from services.registration import lookup

    _db(tmp_path, "acceptance_minor_lookup_261028.db")
    lookup.invalidate_rank_cache()


@pytest.mark.parametrize("q", ["СПбГЭУ", "спбгэу", "СПБГЭУ"])
def test_spbgeu_found_by_abbreviation(tmp_path, q):
    from services.registration.lookup import search_lookup

    _lookup_db(tmp_path)
    found = asyncio.run(search_lookup("university", q, limit=5))
    assert found and found[0]["canonical"] == SPBGEU


def test_spbgeu_alias_unique_in_snapshot():
    from services.registration.lookup import normalize_alias

    data = json.loads((REPO_ROOT / "data" / "lookup" / "universities_ru.json").read_text(encoding="utf-8"))
    entries = data["items"]
    owners = [e["canonical"] for e in entries
              if "спбгэу" in {normalize_alias(a) for a in [e["canonical"], *e.get("aliases", [])]}]
    assert owners == [SPBGEU]


def test_spbgeu_alias_restored_on_restart(tmp_path):
    from database.db import _connect, init_db

    _lookup_db(tmp_path)

    async def go():
        async with _connect() as conn:
            await conn.execute("DELETE FROM lookup_entries WHERE kind = 'university' AND alias_norm = 'спбгэу'")
            await conn.commit()
        await init_db()
        async with _connect() as conn:
            cur = await conn.execute(
                "SELECT canonical FROM lookup_entries WHERE kind = 'university' AND alias_norm = 'спбгэу'"
            )
            return [r[0] for r in await cur.fetchall()]

    assert asyncio.run(go()) == [SPBGEU]
