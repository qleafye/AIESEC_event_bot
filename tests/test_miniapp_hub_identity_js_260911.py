"""Квик 260911-6i9 (W3 «Личность и приватность», пункт 1) — общий сборщик «аватар + имя»
`miniapp/static/js/person.js` (листовой модуль без импортов) и структурные сторожа его
использования на хабе (`screens/hub.js`) и в профиле (`screens/profile.js`).

Node-подпроцесс без jsdom (тот же фейковый DOM, что `tests/test_miniapp_form_controls_js_260911.py`).
`screens/hub.js` в node НЕ импортируется — `app.js` на верхнем уровне читает `window.Telegram`/
`document.body.dataset` и в последней строке зовёт `start()`; поведение хаба/профиля здесь
проверяется структурно (греп по исходнику), поведение самого сборщика — реальным вызовом
`personNode` через node.
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from tests.test_miniapp_frontend import SCREENS_DIR, _HEX_OR_RGB_COLOR, _STRING_LITERAL, _js_without_comments

ROOT = Path(__file__).resolve().parent.parent
_CYRILLIC = re.compile(r"[А-Яа-яЁё]")

PERSON_JS = ROOT / "miniapp" / "static" / "js" / "person.js"
HUB_JS = SCREENS_DIR / "hub.js"
PROFILE_JS = SCREENS_DIR / "profile.js"
APP_CSS = ROOT / "miniapp" / "static" / "app.css"


# ══════════════════════════════════════════════════════════════════════════════════════════
# person.js — сам модуль: ни одного кириллического литерала, никакого DOM-приёма мимо h()
# ══════════════════════════════════════════════════════════════════════════════════════════

def test_person_js_has_no_cyrillic_string_literals():
    text = _js_without_comments(PERSON_JS)
    for m in _STRING_LITERAL.finditer(text):
        assert not _CYRILLIC.search(m.group(0)), m.group(0)


# ══════════════════════════════════════════════════════════════════════════════════════════
# Поведение personNode — node-подпроцесс, фейковый DOM (тот же приём, что form_controls)
# ══════════════════════════════════════════════════════════════════════════════════════════

_FAKE_DOM_PRELUDE = """
class FakeClassList {
  constructor(el) { this._el = el; this._set = new Set(); }
  add(...names) { for (const n of names) this._set.add(n); this._sync(); }
  remove(...names) { for (const n of names) this._set.delete(n); this._sync(); }
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
    this.parent = null;
    this._listeners = {};
    this.classList = new FakeClassList(this);
  }
  get className() { return this._className; }
  set className(v) { this._className = v; this.classList._fromString(v); }
  setAttribute(name, value) { this._attrs.set(name, String(value)); }
  getAttribute(name) { return this._attrs.has(name) ? this._attrs.get(name) : null; }
  addEventListener(type, fn) { (this._listeners[type] ||= []).push(fn); }
  dispatch(type, evt) { for (const fn of (this._listeners[type] || []).slice()) fn(evt); }
  appendChild(node) { this.children.push(node); node.parent = this; return node; }
  append(...nodes) { for (const n of nodes) if (n != null && n !== false) this.appendChild(n); }
  replaceWith(node) {
    if (!this.parent) return;
    const idx = this.parent.children.indexOf(this);
    if (idx >= 0) this.parent.children[idx] = node;
    node.parent = this.parent;
  }
  get textContent() {
    return this.children.filter((c) => c.nodeType === 3).map((c) => c.textContent).join("");
  }
  set textContent(v) { this.children = [new FakeText(v)]; }
}

globalThis.document = {
  createElement(tag) { return new FakeElement(tag); },
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
"""

NODE_SCRIPT = _FAKE_DOM_PRELUDE + """
const m = await import(%(url)s);

// 1) аватар + имя: первый узел img.plate-avatar с нужным src, рядом h1 с именем.
const withAvatar = m.personNode(h, { avatarUrl: "/app/api/file/x?t=1", initials: "ИП", name: "Иван Петров" });
const avatarImg = withAvatar.children[0];
const textBlock = withAvatar.children[1];
const h1WithAvatar = textBlock.children[0];

// 2) img бросает error -> подмена на .plate-mono с инициалами.
avatarImg.dispatch("error", {});
const afterError = withAvatar.children[0];

// 3) без avatarUrl -> сразу .plate-mono, img не создаётся вовсе.
const noAvatar = m.personNode(h, { initials: "ИП", name: "Иван Петров" });
const noAvatarFirst = noAvatar.children[0];

// 4) без avatarUrl и без initials -> узла аватара нет совсем.
const noAvatarNoInitials = m.personNode(h, { name: "Иван Петров" });

// 5) sub непустой/пустой (initials заданы, чтобы узел монограммы был на месте children[0] —
// индекс текстового блока не гулял бы от наличия/отсутствия аватара).
const withSub = m.personNode(h, { initials: "И", name: "Иван", sub: "@ivan · Москва" });
const subBlock = withSub.children[1];
const withoutSub = m.personNode(h, { initials: "И", name: "Иван" });
const noSubBlock = withoutSub.children[1];

// 6) compact.
const compact = m.personNode(h, { initials: "И", name: "Иван" }, { compact: true });
const plain = m.personNode(h, { initials: "И", name: "Иван" });

// 7) пустое name -> h1 существует с пустым текстом.
const emptyName = m.personNode(h, { initials: "И" });
const emptyNameTextBlock = emptyName.children[1];

console.log(JSON.stringify({
  rootClass: withAvatar.className,
  avatarTag: avatarImg.tagName,
  avatarClass: avatarImg.className,
  avatarSrc: avatarImg.getAttribute("src"),
  h1Text: h1WithAvatar.textContent,
  afterErrorTag: afterError.tagName,
  afterErrorClass: afterError.className,
  afterErrorText: afterError.textContent,
  noAvatarFirstTag: noAvatarFirst.tagName,
  noAvatarFirstClass: noAvatarFirst.className,
  noAvatarFirstText: noAvatarFirst.textContent,
  noAvatarNoInitialsChildrenCount: noAvatarNoInitials.children.length,
  subText: subBlock.children[1] ? subBlock.children[1].textContent : null,
  subChildCount: subBlock.children.length,
  noSubChildCount: noSubBlock.children.length,
  compactClass: compact.className,
  plainClass: plain.className,
  emptyNameH1Text: emptyNameTextBlock.children[0].textContent,
  emptyNameH1Exists: emptyNameTextBlock.children[0].tagName === "H1",
}));
"""


@pytest.fixture(scope="module")
def js_result() -> dict:
    node = shutil.which("node")
    if not node:
        pytest.skip("node не найден в PATH — поведенческий тест personNode пропущен")
    script = NODE_SCRIPT % {"url": json.dumps(PERSON_JS.resolve().as_uri())}
    proc = subprocess.run(
        [node, "--input-type=module", "-e", script],
        cwd=str(ROOT), capture_output=True, text=True, encoding="utf-8", timeout=120,
    )
    assert proc.returncode == 0, proc.stderr[-3000:]
    return json.loads(proc.stdout.strip().splitlines()[-1])


def test_person_node_avatar_and_name(js_result):
    assert js_result["rootClass"] == "plate-person"
    assert js_result["avatarTag"] == "IMG"
    assert js_result["avatarClass"] == "plate-avatar"
    assert js_result["avatarSrc"] == "/app/api/file/x?t=1"
    assert js_result["h1Text"] == "Иван Петров"


def test_person_node_avatar_error_falls_back_to_monogram(js_result):
    assert js_result["afterErrorTag"] == "SPAN"
    assert js_result["afterErrorClass"] == "plate-mono"
    assert js_result["afterErrorText"] == "ИП"


def test_person_node_without_avatar_url_skips_img_entirely(js_result):
    assert js_result["noAvatarFirstTag"] == "SPAN"
    assert js_result["noAvatarFirstClass"] == "plate-mono"
    assert js_result["noAvatarFirstText"] == "ИП"


def test_person_node_without_avatar_and_initials_has_no_avatar_node(js_result):
    # Текстовый блок остаётся единственным ребёнком корня — узла аватара нет совсем.
    assert js_result["noAvatarNoInitialsChildrenCount"] == 1


def test_person_node_sub_shown_only_when_non_empty(js_result):
    assert js_result["subChildCount"] == 2
    assert js_result["subText"] == "@ivan · Москва"
    assert js_result["noSubChildCount"] == 1


def test_person_node_compact_modifier_toggles_class(js_result):
    assert "plate-person--sm" in js_result["compactClass"]
    assert "plate-person--sm" not in js_result["plainClass"]


def test_person_node_empty_name_still_renders_h1(js_result):
    assert js_result["emptyNameH1Exists"] is True
    assert js_result["emptyNameH1Text"] == ""


# ══════════════════════════════════════════════════════════════════════════════════════════
# Структурные сторожа: hub.js использует общий сборщик, ровно один запрос /profile
# ══════════════════════════════════════════════════════════════════════════════════════════

def test_hub_js_imports_person_node_from_shared_module():
    text = _js_without_comments(HUB_JS)
    assert 'from "../person.js"' in text
    assert "personNode" in text


def test_hub_js_calls_profile_endpoint_exactly_once():
    text = _js_without_comments(HUB_JS)
    assert text.count('api("/profile")') == 1


def test_hub_js_person_slot_created_before_promise_all_settled():
    text = _js_without_comments(HUB_JS)
    slot_idx = text.index("const personSlot = h(")
    settled_idx = text.index("Promise.allSettled")
    assert slot_idx < settled_idx


def test_hub_js_person_slot_is_first_child_of_hub_plate():
    text = _js_without_comments(HUB_JS)
    match = re.search(r'h\("section",\s*\{\s*class:\s*"plate plate--hub"\s*\},\s*personSlot', text)
    assert match, "personSlot не первый узел section.plate.plate--hub"


def test_hub_js_still_sets_payment_status_label_with_d08_comment():
    text = _js_without_comments(HUB_JS)
    assert "payment_status_label" in text
    raw_text = HUB_JS.read_text(encoding="utf-8")
    assert "D-08" in raw_text


def test_hub_js_uses_profile_response_fields_for_person():
    text = _js_without_comments(HUB_JS)
    for field in ("avatar_url", "initials", "display_name"):
        assert field in text


# ══════════════════════════════════════════════════════════════════════════════════════════
# Структурные сторожа: profile.js собран тем же personNode, второй копии логики нет
# ══════════════════════════════════════════════════════════════════════════════════════════

def test_profile_js_imports_and_uses_person_node():
    text = _js_without_comments(PROFILE_JS)
    assert 'from "../person.js"' in text
    assert "personNode(h" in text


def test_profile_js_no_longer_has_own_avatar_monogram_literals():
    text = _js_without_comments(PROFILE_JS)
    for literal in ("plate-mono", "plate-avatar", "\"plate-person\""):
        assert literal not in text, literal


# ══════════════════════════════════════════════════════════════════════════════════════════
# app.css: компактный модификатор + шкала типографики не расширена
# ══════════════════════════════════════════════════════════════════════════════════════════

def test_app_css_has_compact_person_modifier():
    css = APP_CSS.read_text(encoding="utf-8")
    assert ".plate-person--sm" in css


def test_app_css_font_size_scale_not_expanded():
    css = APP_CSS.read_text(encoding="utf-8")
    sizes = sorted(set(re.findall(r"font-size:\s*[^;]+;", css)))
    assert len(sizes) == 21, sizes


def test_app_css_person_sm_rules_have_no_literal_colors():
    css = APP_CSS.read_text(encoding="utf-8")
    start = css.index(".plate-person--sm {")
    end = css.index("\n\n", start)
    block = css[start:end]
    assert not _HEX_OR_RGB_COLOR.findall(block), "литеральный цвет в .plate-person--sm"
