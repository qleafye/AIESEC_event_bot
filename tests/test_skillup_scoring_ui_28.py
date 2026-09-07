"""Phase 28 Plan 08 (SU-08, 28-UI-SPEC §8) — правила балла кнопками.

Задача 1: бот-экран `handlers/admin_reg_scoring.py` — чекбокс-пикеры ЧЕТЫРЁХ скоринговых
множеств строятся из ТЕКУЩЕГО списка вариантов вопроса анкеты (`reg_engine.options(step_key)`),
а не замороженного словаря (в этом разница с `type: "multi"`, см. `admin_modcard.py`). Харнесс
— прямой вызов хендлеров с фейковым `CallbackQuery` (без реального aiogram-диспетчера), тот же
приём, что `tests/test_admin_percity_menu.py`.

Задача 2: веб-настройки — атрибут ключа `options_from_step` резолвится РОУТЕРОМ
(`miniapp/routers/settings.py::_item_for`) в актуальные подписи, а `item_spec` остаётся чистым
конструктором. Харнесс — `tests/test_miniapp_routes.py` (FastAPI TestClient) для серверной
части и node-подпроцесс (без jsdom, тот же приём, что `tests/test_settings_toggle_js.py`) для
`form.js::settingSpec`/`multiControl`.

pytest-asyncio в проекте не используется — асинхронщина через `asyncio.run()`, БД — временная
(`config.DB_PATH = tmp_path / "..."` + `database.db.init_db()`), как в соседних тестах фазы.
"""
from __future__ import annotations

import asyncio
import json
import shutil
import subprocess
from pathlib import Path

import pytest

import handlers.admin_reg_scoring as admin_reg_scoring
from config import config
from database import db as bot_db
from handlers.admin_caps import required_capability
from moderation_card import EMPTY_SENTINEL
from settings_schema import get_setting_typed

from tests.test_miniapp_routes import (
    ADMIN_ID,
    _cfg,
    _client,
    _hdr,
    _set,
    _standard_seed,
    _use_tmp_db,
)

ROOT = Path(__file__).resolve().parent.parent
FORM_JS = ROOT / "miniapp" / "static" / "js" / "form.js"


def _run(coro):
    return asyncio.run(coro)


def _admin_ready(tmp_path, name="skillup_scoring_ui_28.db"):
    config.DB_PATH = str(tmp_path / name)
    _run(bot_db.init_db())


class FakeUser:
    def __init__(self, uid):
        self.id = uid


class FakeMessage:
    def __init__(self):
        self.text = None
        self.markup = None
        self.edit_calls = 0

    async def edit_text(self, text, parse_mode=None, reply_markup=None):
        self.text = text
        self.markup = reply_markup
        self.edit_calls += 1


class FakeCallback:
    def __init__(self, data, user_id=1):
        self.data = data
        self.from_user = FakeUser(user_id)
        self.message = FakeMessage()
        self.answers = []

    async def answer(self, text=None, show_alert=False):
        self.answers.append((text, show_alert))


def _kb_callbacks(kb):
    return [b.callback_data for row in kb.inline_keyboard for b in row]


# ══════════════════════════════════════════════════════════════════════════════════════════
# Задача 1: экран «🧮 Правила балла» в боте
# ══════════════════════════════════════════════════════════════════════════════════════════

def test_screen_lists_current_options(tmp_path):
    _admin_ready(tmp_path)
    _run(bot_db.set_setting("study_field_options", "Backend\nFrontend\nDesign"))
    cb = FakeCallback("admin_reg_scoring")
    _run(admin_reg_scoring.admin_reg_scoring(cb))
    assert "Backend" in cb.message.text
    assert "Frontend" in cb.message.text
    assert "Design" in cb.message.text
    callbacks = _kb_callbacks(cb.message.markup)
    assert "scoring_toggle:score_it_fields:0" in callbacks
    assert "scoring_toggle:score_it_fields:1" in callbacks
    assert "scoring_toggle:score_it_fields:2" in callbacks

    # Менеджер поменял список вариантов вопроса — пикер сразу видит новый набор.
    _run(bot_db.set_setting("study_field_options", "OnlyOne"))
    cb2 = FakeCallback("admin_reg_scoring")
    _run(admin_reg_scoring.admin_reg_scoring(cb2))
    assert "OnlyOne" in cb2.message.text
    assert "Backend" not in cb2.message.text
    callbacks2 = _kb_callbacks(cb2.message.markup)
    assert "scoring_toggle:score_it_fields:0" in callbacks2
    assert "scoring_toggle:score_it_fields:1" not in callbacks2


def test_toggle_persists_and_orders(tmp_path):
    _admin_ready(tmp_path)
    _run(bot_db.set_setting("study_field_options", "Backend\nFrontend\nDesign"))
    # Отмечаем в порядке 2, потом 0 — хранение остаётся в порядке ВАРИАНТОВ, не нажатий.
    _run(admin_reg_scoring.scoring_toggle(FakeCallback("scoring_toggle:score_it_fields:2")))
    _run(admin_reg_scoring.scoring_toggle(FakeCallback("scoring_toggle:score_it_fields:0")))
    assert _run(get_setting_typed("score_it_fields")) == ["Backend", "Design"]


def test_empty_set_uses_sentinel_not_default(tmp_path):
    _admin_ready(tmp_path)
    _run(bot_db.set_setting("study_field_options", "Backend\nFrontend"))
    _run(admin_reg_scoring.scoring_toggle(FakeCallback("scoring_toggle:score_it_fields:0")))
    # Снимаем единственную галочку — пустой набор пишется сентинелом, не удалением ключа
    # (реестровый default этого ключа — None, но «менеджер явно снял все галочки» и
    # «никогда не трогал» — разные состояния, тот же приём, что modcard_fields).
    _run(admin_reg_scoring.scoring_toggle(FakeCallback("scoring_toggle:score_it_fields:0")))
    assert _run(bot_db.get_setting("score_it_fields")) == EMPTY_SENTINEL


def test_stale_label_shown_separately(tmp_path):
    _admin_ready(tmp_path)
    _run(bot_db.set_setting("study_field_options", "Backend\nFrontend"))
    _run(bot_db.set_setting("score_it_fields", "OldField\nBackend"))
    cb = FakeCallback("admin_reg_scoring")
    _run(admin_reg_scoring.admin_reg_scoring(cb))
    assert "⚠️ OldField — варианта больше нет" in cb.message.text
    assert "✅ Backend" in cb.message.text
    assert "scoring_drop:score_it_fields:0" in _kb_callbacks(cb.message.markup)

    # Убираем пропавший вариант кнопкой — Backend остаётся отмеченным, значение не теряется.
    cb2 = FakeCallback("scoring_drop:score_it_fields:0")
    _run(admin_reg_scoring.scoring_drop(cb2))
    assert _run(get_setting_typed("score_it_fields")) == ["Backend"]
    assert cb2.answers[0][0] == "OldField: убрано"
    assert "⚠️" not in cb2.message.text


def test_screen_requires_capability():
    assert required_capability(callback_data="admin_reg_scoring") == "settings"
    assert required_capability(callback_data="scoring_toggle:score_it_fields:0") == "settings"
    assert required_capability(callback_data="scoring_limit:score_course_from:3") == "settings"
    assert required_capability(callback_data="scoring_drop:score_it_fields:0") == "settings"
    assert required_capability(callback_data="scoring_noop") == "settings"


def test_no_raw_keys_in_texts(tmp_path):
    _admin_ready(tmp_path)
    _run(bot_db.set_setting("score_it_fields", "OldField"))
    text = _run(admin_reg_scoring.render_scoring_text())
    for code in (
        "score_it_fields", "score_senior_statuses", "score_readiness_counts",
        "score_experience_counts", "score_course_from", "score_stack_from",
        "study_field", "education_status", "readiness", "experience",
    ):
        assert code not in text, f"код ключа/шага «{code}» просочился в текст экрана"


def test_limit_threshold_changes_via_preset(tmp_path):
    _admin_ready(tmp_path)
    cb = FakeCallback("scoring_limit:score_course_from:5")
    _run(admin_reg_scoring.scoring_limit(cb))
    assert _run(get_setting_typed("score_course_from")) == 5
    assert "5" in cb.answers[0][0]


# ══════════════════════════════════════════════════════════════════════════════════════════
# Задача 2: options_from_step в веб-настройках
# ══════════════════════════════════════════════════════════════════════════════════════════

def _setup_web(tmp_path, name="skillup_scoring_ui_28_web.db"):
    db_path = _use_tmp_db(tmp_path, name)
    _standard_seed()
    return _client(_cfg(db_path))


def _items(body):
    out = []
    for section in body["sections"]:
        out.extend(section["toggles"])
        for group in section["groups"]:
            out.extend(group["items"])
    return out


def _item(body, key):
    return next(i for i in _items(body) if i["key"] == key)


def test_settings_all_resolves_options_from_step(tmp_path):
    client = _setup_web(tmp_path)
    _set("study_field_options", "Backend\nFrontend")
    body = client.get("/app/api/admin/settings/all", headers=_hdr(ADMIN_ID)).json()
    item = _item(body, "score_it_fields")
    assert item["options"] == ["Backend", "Frontend"]
    assert item["options_from_step"] == "study_field"

    # Правка вариантов вопроса — ответ API меняется на следующем запросе, без второй записи.
    _set("study_field_options", "Backend\nFrontend\nDesign")
    body2 = client.get("/app/api/admin/settings/all", headers=_hdr(ADMIN_ID)).json()
    assert _item(body2, "score_it_fields")["options"] == ["Backend", "Frontend", "Design"]


def test_batch_saves_labels_as_list(tmp_path):
    client = _setup_web(tmp_path)
    _set("study_field_options", "Backend\nFrontend")
    resp = client.post(
        "/app/api/admin/settings/batch",
        json={"changes": [{"key": "score_it_fields", "value": "Backend;Frontend"}], "base": {}, "confirm": []},
        headers=_hdr(ADMIN_ID),
    )
    body = resp.json()
    assert body["saved"] == ["score_it_fields"] and body["errors"] == {}
    # Путь сохранения не меняется — по-прежнему type "list", разделитель "\n" в БД.
    assert _run(bot_db.get_setting("score_it_fields")) == "Backend\nFrontend"


def test_batch_empty_scoring_set_is_valid_not_rejected(tmp_path):
    """Снятие ВСЕХ галочек через веб-батч — законный ввод («ни один вариант не даёт балл»),
    не «менеджер стёр текст» (та же поблажка, что у `multi`, settings_ops.validate_batch_item)."""
    client = _setup_web(tmp_path)
    _set("study_field_options", "Backend\nFrontend")
    _set("score_it_fields", "Backend")
    resp = client.post(
        "/app/api/admin/settings/batch",
        json={"changes": [{"key": "score_it_fields", "value": ""}], "base": {}, "confirm": []},
        headers=_hdr(ADMIN_ID),
    )
    body = resp.json()
    assert body["saved"] == ["score_it_fields"] and body["errors"] == {}
    assert _run(bot_db.get_setting("score_it_fields")) == EMPTY_SENTINEL


def test_stale_value_marked_not_dropped(tmp_path):
    client = _setup_web(tmp_path)
    _set("study_field_options", "Backend\nFrontend")
    _set("score_it_fields", "OldField\nBackend")
    body = client.get("/app/api/admin/settings/all", headers=_hdr(ADMIN_ID)).json()
    item = _item(body, "score_it_fields")
    assert item["stale_options"] == ["OldField"]
    assert item["stale_option_text"] == "Варианта больше нет"
    assert item["value"] == ["OldField", "Backend"]  # значение не теряется молча


def test_multi_type_keys_unchanged(tmp_path):
    client = _setup_web(tmp_path)
    body = client.get("/app/api/admin/settings/all", headers=_hdr(ADMIN_ID)).json()
    item = _item(body, "modcard_fields")
    assert item["type"] == "multi"
    assert item["options_from_step"] is None
    assert item["stale_options"] == []
    assert item["stale_option_text"] is None


# ── form.js: settingSpec()/multiControl (node-подпроцесс, без jsdom) ────────────────────────

NODE_SCRIPT = """
class FakeClassList {
  constructor(el) { this._el = el; this._set = new Set(); }
  add(...names) { for (const n of names) this._set.add(n); this._sync(); }
  remove(...names) { for (const n of names) this._set.delete(n); this._sync(); }
  toggle(name, force) {
    const has = this._set.has(name);
    const next = force === undefined ? !has : Boolean(force);
    if (next) this._set.add(name); else this._set.delete(name);
    this._sync();
    return next;
  }
  contains(name) { return this._set.has(name); }
  _sync() { this._el._className = [...this._set].join(" "); }
  _fromString(v) { this._set = new Set(String(v || "").split(/\\s+/).filter(Boolean)); }
}

class FakeText { constructor(text) { this.nodeType = 3; this.textContent = String(text); } }

class FakeElement {
  constructor(tag) {
    this.tagName = String(tag).toUpperCase();
    this._attrs = new Map();
    this._className = "";
    this.children = [];
    this.parentNode = null;
    this._listeners = {};
    this.classList = new FakeClassList(this);
  }
  get className() { return this._className; }
  set className(v) { this._className = v; this.classList._fromString(v); }
  setAttribute(name, value) { this._attrs.set(name, String(value)); }
  getAttribute(name) { return this._attrs.has(name) ? this._attrs.get(name) : null; }
  addEventListener(type, fn) { (this._listeners[type] ||= []).push(fn); }
  removeEventListener(type, fn) {
    this._listeners[type] = (this._listeners[type] || []).filter((f) => f !== fn);
  }
  dispatch(type, evt) { for (const fn of (this._listeners[type] || []).slice()) fn(evt); }
  appendChild(node) {
    this.children.push(node);
    if (node && node.nodeType !== 3) node.parentNode = this;
    return node;
  }
  append(...nodes) { for (const n of nodes) if (n != null && n !== false) this.appendChild(n); }
  remove() {
    if (!this.parentNode) return;
    const idx = this.parentNode.children.indexOf(this);
    if (idx !== -1) this.parentNode.children.splice(idx, 1);
    this.parentNode = null;
  }
  get textContent() {
    return this.children.filter((c) => c.nodeType === 3).map((c) => c.textContent).join("");
  }
  set textContent(v) { this.children = [new FakeText(v)]; }
  querySelector(sel) {
    const cls = sel.startsWith(".") ? sel.slice(1) : null;
    const stack = [...this.children];
    while (stack.length) {
      const node = stack.shift();
      if (!node || node.nodeType === 3) continue;
      if (cls && node.classList && node.classList.contains(cls)) return node;
      if (node.children) stack.push(...node.children);
    }
    return null;
  }
}

globalThis.document = {
  createElement(tag) { return new FakeElement(tag); },
  createElementNS(ns, tag) { return new FakeElement(tag); },
  createTextNode(text) { return new FakeText(text); },
};

function h(tag, attrs, ...children) {
  const el = document.createElement(tag);
  if (attrs) {
    for (const [key, value] of Object.entries(attrs)) {
      if (value == null || value === false) continue;
      if (key === "class") el.className = value;
      else if (key === "text") el.textContent = value;
      else if (key.startsWith("on") && typeof value === "function") el.addEventListener(key.slice(2).toLowerCase(), value);
      else el.setAttribute(key, value === true ? "" : String(value));
    }
  }
  for (const child of children.flat()) {
    if (child == null || child === false) continue;
    el.append(typeof child === "object" ? child : document.createTextNode(String(child)));
  }
  return el;
}

function textOf(node) {
  if (!node) return "";
  if (node.nodeType === 3) return node.textContent;
  return (node.children || []).map(textOf).join("");
}

function collectByClass(root, cls) {
  const out = [];
  const stack = [root];
  while (stack.length) {
    const node = stack.shift();
    if (!node || node.nodeType === 3) continue;
    if (node.classList && node.classList.contains(cls)) out.push(node);
    if (node.children) stack.push(...node.children);
  }
  return out;
}

const m = await import(%(url)s);

// ── settingSpec: list+options_from_step -> multi; обычный list -> list; multi -> multi ────
const specDynamic = m.settingSpec({
  key: "score_it_fields", type: "list", label: "IT-направления",
  options: ["Backend", "Frontend"], options_from_step: "study_field",
  stale_options: ["OldField"], stale_option_text: "Варианта больше нет",
});
const specPlainList = m.settingSpec({
  key: "source_options", type: "list", label: "Источники", options: null, options_from_step: null,
});
const specMulti = m.settingSpec({
  key: "modcard_fields", type: "multi", label: "Поля карточки", options: ["Возраст", "Город"],
});

// ── чекбоксы по options_from_step + пропавшая подпись отдельной строкой ───────────────────
const calls = [];
const wrap = m.field(h, specDynamic, ["OldField", "Backend"], (v) => calls.push(v));
const control = wrap._nodes.control;
const checkRows = collectByClass(control, "check");
const staleRows = checkRows.filter((r) => r.classList.contains("is-stale"));
const plainRows = checkRows.filter((r) => !r.classList.contains("is-stale"));
const checkedTexts = plainRows.filter((r) => r.children[0].checked).map((r) => textOf(r).trim());
const staleRowText = staleRows[0] ? textOf(staleRows[0]) : null;

const removeBtn = staleRows[0].children[staleRows[0].children.length - 1];
removeBtn.dispatch("click", {});
const callsAfterRemove = [...calls];
const staleRowsAfterRemove = collectByClass(control, "check").filter((r) => r.classList.contains("is-stale"));

console.log(JSON.stringify({
  specDynamicType: specDynamic.type,
  specPlainListType: specPlainList.type,
  specMultiType: specMulti.type,
  plainRowCount: plainRows.length,
  staleRowCount: staleRows.length,
  staleRowText,
  checkedTexts,
  callsAfterRemove,
  staleRowCountAfterRemove: staleRowsAfterRemove.length,
}));
"""


@pytest.fixture(scope="module")
def js_result() -> dict:
    node = shutil.which("node")
    if not node:
        pytest.skip("node не найден в PATH — поведенческий тест form.js пропущен")
    script = NODE_SCRIPT % {"url": json.dumps(FORM_JS.resolve().as_uri())}
    proc = subprocess.run(
        [node, "--input-type=module", "-e", script],
        cwd=str(ROOT), capture_output=True, text=True, encoding="utf-8", timeout=120,
    )
    assert proc.returncode == 0, proc.stderr[-3000:]
    return json.loads(proc.stdout.strip().splitlines()[-1])


def test_form_js_renders_checkboxes_for_options_from_step(js_result):
    assert js_result["specDynamicType"] == "multi"
    assert js_result["specPlainListType"] == "list"
    assert js_result["specMultiType"] == "multi"
    assert js_result["plainRowCount"] == 2  # Backend/Frontend — из spec.options
    assert js_result["checkedTexts"] == ["Backend"]  # только отмеченный ранее вариант


def test_stale_value_marked_not_dropped_js(js_result):
    assert js_result["staleRowCount"] == 1
    assert "OldField" in js_result["staleRowText"]
    assert "Варианта больше нет" in js_result["staleRowText"]
    # Крестик убирает пропавший вариант из значения, оставляя валидные отмеченные.
    assert js_result["callsAfterRemove"] == [["Backend"]]
    assert js_result["staleRowCountAfterRemove"] == 0
