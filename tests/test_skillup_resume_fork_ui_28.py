"""Phase 28 (28-05, SU-04, СкиллАп 5): развилка резюме — делегатская половина.

Задача 1/2: шов `handlers/reg_resume_fork.py` (экран R1, приём ссылки R2b, «Назад» на
развилку) + гейт в `handlers/reg_flow.py` («свободный текст мимо кнопок в режиме fork больше
не резюме»). pytest-asyncio недоступен в этом окружении — async через `asyncio.run()`, стиль
Fake-объектов aiogram — тот же приём, что `tests/test_reg_resume_draft.py`/
`tests/test_skillup_steps_28.py`.

Задача 3: паритет в Mini App — поведенческие тесты `form.js::field()` (развилка/маркер домена)
через node (без jsdom, тот же приём, что `tests/test_settings_toggle_js.py`) + структурные
сторожа `screens/form.js` (футер mini_portfolio, «Назад» на развилку) через
`tests/test_miniapp_frontend.py::_js_without_comments` (импорт, не правка) + HTTP-тесты
PATCH `resume_type` (`miniapp/routers/form.py`, deviation Rule 3 — без него развилка в
приложении не могла бы записать выбор ветки вообще).
"""
import asyncio
import json
import shutil
import subprocess
from pathlib import Path

import pytest
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import InlineKeyboardMarkup

from config import config
from database import db
from handlers import registration as reg
from handlers import reg_extra_steps
from handlers import reg_flow
from handlers import reg_resume_fork
from handlers.states import Registration

from tests.test_miniapp_frontend import _js_without_comments

ROOT = Path(__file__).resolve().parent.parent
FORM_JS = ROOT / "miniapp" / "static" / "js" / "form.js"
FORM_SCREEN_JS = ROOT / "miniapp" / "static" / "js" / "screens" / "form.js"

UID = 900805000


def _use_tmp_db(tmp_path, name="test_skillup_resume_fork_ui_28.db"):
    config.DB_PATH = str(tmp_path / name)
    asyncio.run(db.init_db())


def _state(uid: int) -> FSMContext:
    return FSMContext(storage=MemoryStorage(), key=StorageKey(bot_id=1, chat_id=uid, user_id=uid))


class _FakeUser:
    def __init__(self, uid):
        self.id = uid


class _FakeChat:
    def __init__(self, cid):
        self.id = cid


class _FakeMessage:
    """Минимальный message: обработчики трогают только `.chat.id`, `.from_user`, `.text` и
    `.answer` (тот же контур, что `_FakeMessage`/`_KBCapturingMessage` в
    `tests/test_skillup_steps_28.py`/`tests/test_reg_resume_draft.py`)."""

    def __init__(self, uid, text=None):
        self.chat = _FakeChat(uid)
        self.from_user = _FakeUser(uid)
        self.text = text
        self.sent = []
        self.document = None

    async def answer(self, text=None, reply_markup=None, parse_mode=None, *a, **k):
        self.sent.append((text, reply_markup, parse_mode))
        return "sent:%d" % len(self.sent)

    async def edit_reply_markup(self, reply_markup=None):
        return None

    def model_copy(self, update=None):
        new = _FakeMessage(self.from_user.id, text=self.text)
        new.sent = self.sent
        new.document = self.document
        if update and "from_user" in update:
            new.from_user = update["from_user"]
        return new


class _FakeDocument:
    def __init__(self, file_id="BQACresume", file_name="resume.pdf", file_size=1000):
        self.file_id = file_id
        self.file_name = file_name
        self.file_size = file_size


class _FakeCallback:
    def __init__(self, data, uid):
        self.data = data
        self.from_user = _FakeUser(uid)
        self.message = _FakeMessage(uid)
        self.answers = []

    async def answer(self, text=None, show_alert=False):
        self.answers.append((text, show_alert))
        return None


def _texts(msg: _FakeMessage):
    return [t for (t, _, _) in msg.sent]


def _inline_kbs(msg: _FakeMessage):
    return [rm for (_, rm, _) in msg.sent if isinstance(rm, InlineKeyboardMarkup)]


def _callback_datas(kb: InlineKeyboardMarkup):
    return [btn.callback_data for row in kb.inline_keyboard for btn in row if btn.callback_data]


# ══════════════════════════════════════════════════════════════════════════════════════════
# Задача 1: развилка в чате
# ══════════════════════════════════════════════════════════════════════════════════════════

def test_fork_screen_shows_three_buttons(tmp_path):
    _use_tmp_db(tmp_path)
    uid = UID + 1

    async def go():
        await db.set_setting("reg_resume_mode", "fork")
        msg = _FakeMessage(uid)
        state = _state(uid)
        await state.update_data(participant_type="full", full_name="Тест Тестов")
        await reg._ask_step("resume", msg, state, 1, 5)
        return msg, await state.get_state()

    msg, state_name = asyncio.run(go())
    kbs = _inline_kbs(msg)
    assert len(kbs) == 1, "экран развилки обязан прийти с инлайн-клавиатурой"
    assert _callback_datas(kbs[0]) == ["regfork:file", "regfork:link", "regfork:mini"]
    assert state_name == Registration.resume.state


def test_pick_link_asks_link_step(tmp_path):
    _use_tmp_db(tmp_path)
    uid = UID + 2

    async def go():
        await db.set_setting("reg_resume_mode", "fork")
        await db.set_setting("reg_q_resume_link", "on")
        state = _state(uid)
        await state.update_data(
            participant_type="full", full_name="Тест", _reg_step=3, _reg_total=6,
        )
        callback = _FakeCallback("regfork:link", uid)
        await reg_resume_fork.regfork_pick(callback, state)
        data = await state.get_data()
        return data, callback.message, await state.get_state()

    data, msg, state_name = asyncio.run(go())
    assert data.get("resume_type") == "link"
    assert state_name == Registration.resume_link.state
    assert msg.sent, "вопрос про ссылку обязан быть задан"


def test_pick_mini_asks_three_substeps_in_order(tmp_path):
    _use_tmp_db(tmp_path)
    uid = UID + 3

    async def go():
        await db.set_setting("reg_resume_mode", "fork")
        await db.set_setting("reg_q_mini_projects", "on")
        await db.set_setting("reg_q_mini_portfolio", "on")
        await db.set_setting("reg_q_mini_direction", "on")
        state = _state(uid)
        await state.update_data(
            participant_type="full", full_name="Тест", _reg_step=3, _reg_total=6,
        )
        callback = _FakeCallback("regfork:mini", uid)
        await reg_resume_fork.regfork_pick(callback, state)
        state_after_pick = await state.get_state()

        msg1 = _FakeMessage(uid, text="Делал сайт для клиента, роль — фронтенд")
        await reg_extra_steps.process_mini_projects(msg1, state, bot=None)
        state_after_1 = await state.get_state()

        msg2 = _FakeMessage(uid, text="Пропустить")
        await reg_extra_steps.process_mini_portfolio(msg2, state, bot=None)
        state_after_2 = await state.get_state()
        return state_after_pick, state_after_1, state_after_2

    s0, s1, s2 = asyncio.run(go())
    assert s0 == Registration.mini_projects.state
    assert s1 == Registration.mini_portfolio.state
    assert s2 == Registration.mini_direction.state


def test_back_from_any_branch_returns_to_fork(tmp_path):
    _use_tmp_db(tmp_path)
    uid = UID + 4

    async def go():
        await db.set_setting("reg_resume_mode", "fork")
        state = _state(uid)
        await state.update_data(
            participant_type="full", full_name="Тест", resume_type="mini",
            _reg_step=3, _reg_total=6,
        )
        await state.set_state(Registration.mini_portfolio)
        msg = _FakeMessage(uid, text="⬅️ Назад")
        await reg_extra_steps.process_mini_portfolio(msg, state, bot=None)
        return await state.get_state(), msg

    state_name, msg = asyncio.run(go())
    assert state_name == Registration.resume.state
    kbs = _inline_kbs(msg)
    assert kbs and _callback_datas(kbs[0]) == ["regfork:file", "regfork:link", "regfork:mini"]


def test_back_resets_resume_type(tmp_path):
    _use_tmp_db(tmp_path)
    uid = UID + 5

    async def go():
        await db.set_setting("reg_resume_mode", "fork")
        state = _state(uid)
        await state.update_data(
            participant_type="full", full_name="Тест", resume_type="mini",
            _reg_step=3, _reg_total=6,
        )
        msg = _FakeMessage(uid)
        await reg_resume_fork.back_to_fork(msg, state)
        return await state.get_data()

    data = asyncio.run(go())
    assert data.get("resume_type") is None


def test_file_or_text_mode_unchanged(tmp_path):
    """Дефолт (`reg_resume_mode` не задан) — экран резюме БЕЗ инлайн-кнопок, тот же
    `ReplyKeyboardRemove`, что всегда (D-06 byte-for-byte)."""
    _use_tmp_db(tmp_path)
    uid = UID + 6

    async def go():
        msg = _FakeMessage(uid)
        state = _state(uid)
        await state.update_data(participant_type="full", full_name="Тест")
        await reg._ask_step("resume", msg, state, 1, 5)
        return msg, await state.get_state()

    msg, state_name = asyncio.run(go())
    assert not _inline_kbs(msg), "старый режим не должен получить инлайн-клавиатуру развилки"
    assert state_name == Registration.resume.state


# ══════════════════════════════════════════════════════════════════════════════════════════
# Задача 2: свободный текст мимо кнопок больше не резюме
# ══════════════════════════════════════════════════════════════════════════════════════════

def test_free_text_in_fork_mode_does_not_become_resume(tmp_path):
    _use_tmp_db(tmp_path)
    uid = UID + 10

    async def go():
        await db.set_setting("reg_resume_mode", "fork")
        state = _state(uid)
        await state.update_data(participant_type="full", full_name="Тест")
        await state.set_state(Registration.resume)
        msg = _FakeMessage(uid, text="Вот моё резюме текстом, 3 года опыта")
        await reg_flow.process_resume_text(msg, state, bot=None)
        data = await state.get_data()
        return data, msg, await state.get_state()

    data, msg, state_name = asyncio.run(go())
    assert "resume_text" not in data, "текст мимо кнопок не должен молча стать резюме"
    assert state_name == Registration.resume.state, "шаг не продвигается без выбора ветки"
    assert msg.sent, "делегат обязан получить подсказку"


def test_document_in_fork_mode_still_accepted(tmp_path):
    _use_tmp_db(tmp_path)
    uid = UID + 11

    async def go():
        await db.set_setting("reg_resume_mode", "fork")
        state = _state(uid)
        await state.update_data(participant_type="full", full_name="Тест")
        await state.set_state(Registration.resume)
        msg = _FakeMessage(uid)
        msg.document = _FakeDocument()
        await reg_flow.process_resume(msg, state, bot=None)
        return await state.get_data()

    data = asyncio.run(go())
    assert data.get("resume_file_id") == "BQACresume"


def test_text_only_mode_gate_unchanged(tmp_path):
    _use_tmp_db(tmp_path)
    uid = UID + 12

    async def go():
        await db.set_setting("reg_resume_mode", "text_only")
        state = _state(uid)
        await state.update_data(participant_type="full", full_name="Тест")
        await state.set_state(Registration.resume)
        msg = _FakeMessage(uid, text="Мой опыт: маркетинг 2 года")
        await reg_flow.process_resume_text(msg, state, bot=None)
        return await state.get_data()

    data = asyncio.run(go())
    assert data.get("resume_text") == "Мой опыт: маркетинг 2 года"


def test_file_or_text_text_still_accepted(tmp_path):
    _use_tmp_db(tmp_path)
    uid = UID + 13

    async def go():
        state = _state(uid)
        await state.update_data(participant_type="full", full_name="Тест")
        await state.set_state(Registration.resume)
        msg = _FakeMessage(uid, text="Мой опыт: продажи 3 года")
        await reg_flow.process_resume_text(msg, state, bot=None)
        return await state.get_data()

    data = asyncio.run(go())
    assert data.get("resume_text") == "Мой опыт: продажи 3 года"


# ══════════════════════════════════════════════════════════════════════════════════════════
# Задача 3: паритет в Mini App
# ══════════════════════════════════════════════════════════════════════════════════════════

_FAKE_DOM_PRELUDE = """
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
    this._listeners = {};
    this.classList = new FakeClassList(this);
  }
  get className() { return this._className; }
  set className(v) { this._className = v; this.classList._fromString(v); }
  setAttribute(name, value) { this._attrs.set(name, String(value)); }
  getAttribute(name) { return this._attrs.has(name) ? this._attrs.get(name) : null; }
  addEventListener(type, fn) { (this._listeners[type] ||= []).push(fn); }
  dispatch(type, evt) { for (const fn of (this._listeners[type] || []).slice()) fn(evt); }
  appendChild(node) { this.children.push(node); return node; }
  append(...nodes) { for (const n of nodes) if (n != null && n !== false) this.appendChild(n); }
  replaceChildren(...nodes) { this.children = []; this.append(...nodes); }
  get textContent() {
    return this.children.filter((c) => c.nodeType === 3).map((c) => c.textContent).join("");
  }
  set textContent(v) { this.children = [new FakeText(v)]; }
  get value() { return this._value || ""; }
  set value(v) { this._value = v; }
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
"""

NODE_SCRIPT = _FAKE_DOM_PRELUDE + """
const m = await import(%(url)s);

// 1) resume-fork (R1): три full-width кнопки, тап -> onChange(code) сразу.
const calls = [];
const forkSpec = {
  key: "resume", type: "resume-fork", label: "Резюме",
  fork_options: [
    { code: "file", label: "\\ud83d\\udcce Загрузить файл", icon: "upload" },
    { code: "link", label: "\\ud83d\\udd17 Дать ссылку", icon: "link" },
    { code: "mini", label: "\\ud83e\\udd85 У меня нет резюме", icon: "x" },
  ],
};
const forkWrap = m.field(h, forkSpec, null, (v) => calls.push(v));
const stack = forkWrap.querySelector(".choice-stack");
const buttons = stack ? stack.children.filter((c) => c.tagName === "BUTTON") : [];
buttons[1].dispatch("click", {});

// 2) url control (R2b): домен из вайтлиста -> нейтральный маркер с check-circle-2.
const urlSpec = {
  key: "resume_link", type: "url", label: "Ссылка",
  link_whitelist: ["hh.ru", "github.com"],
  whitelist_hint_text: "{domain} — сайт из списка проверенных",
  other_hint_text: "Личный сайт — тоже подойдёт",
  invalid_hint_text: "Пришлите ссылку целиком, начиная с http:// или https://",
};

const wrapWhitelist = m.field(h, urlSpec, null, () => {});
const inputWhitelist = wrapWhitelist._nodes.control;
inputWhitelist.value = "https://hh.ru/resume/1";
inputWhitelist.dispatch("blur", {});
const markerWhitelist = wrapWhitelist.querySelector(".resume-link-marker");

// 3) тот же контрол — домен НЕ из вайтлиста -> тот же нейтральный визуал, другой текст/иконка.
const wrapOther = m.field(h, urlSpec, null, () => {});
const inputOther = wrapOther._nodes.control;
inputOther.value = "https://my-portfolio.example";
inputOther.dispatch("blur", {});
const markerOther = wrapOther.querySelector(".resume-link-marker");

// 4) невалидный URL -> error-состояние маркера, не нейтральная пометка.
const wrapInvalid = m.field(h, urlSpec, null, () => {});
const inputInvalid = wrapInvalid._nodes.control;
inputInvalid.value = "not-a-url-at-all";
inputInvalid.dispatch("blur", {});
const markerInvalid = wrapInvalid.querySelector(".resume-link-marker");

// 5) пустое поле -> маркер скрыт вовсе (ни ошибки, ни нейтральной пометки).
const wrapEmpty = m.field(h, urlSpec, null, () => {});
const inputEmpty = wrapEmpty._nodes.control;
inputEmpty.dispatch("blur", {});
const markerEmpty = wrapEmpty.querySelector(".resume-link-marker");

function allText(el) {
  let s = "";
  for (const c of (el.children || [])) {
    if (c.nodeType === 3) s += c.textContent;
    else s += allText(c);
  }
  return s;
}

console.log(JSON.stringify({
  buttonCount: buttons.length,
  buttonClasses: buttons.map((b) => b.className),
  forkCalls: calls,
  whitelist: {
    hidden: markerWhitelist.classList.contains("hidden"),
    isError: markerWhitelist.classList.contains("is-error"),
    text: allText(markerWhitelist),
  },
  other: {
    hidden: markerOther.classList.contains("hidden"),
    isError: markerOther.classList.contains("is-error"),
    text: allText(markerOther),
  },
  invalid: {
    hidden: markerInvalid.classList.contains("hidden"),
    isError: markerInvalid.classList.contains("is-error"),
    text: allText(markerInvalid),
  },
  empty: {
    hidden: markerEmpty.classList.contains("hidden"),
  },
}));
"""


@pytest.fixture(scope="module")
def js_result() -> dict:
    node = shutil.which("node")
    if not node:
        pytest.skip("node не найден в PATH — поведенческий тест развилки резюме form.js пропущен")
    script = NODE_SCRIPT % {"url": json.dumps(FORM_JS.resolve().as_uri())}
    proc = subprocess.run(
        [node, "--input-type=module", "-e", script],
        cwd=str(ROOT), capture_output=True, text=True, encoding="utf-8", timeout=120,
    )
    assert proc.returncode == 0, proc.stderr[-3000:]
    return json.loads(proc.stdout.strip().splitlines()[-1])


def test_resume_fork_renders_three_full_width_buttons(js_result):
    assert js_result["buttonCount"] == 3
    for cls in js_result["buttonClasses"]:
        assert "btn" in cls and "secondary" in cls, cls
        assert "accent" not in cls
    # Тап по второй кнопке (индекс 1, «Дать ссылку») сразу зовёт onChange("link") — тап и есть
    # переход, никакого «Дальше» на этом экране.
    assert js_result["forkCalls"] == ["link"]


def test_url_marker_neutral_for_both_cases(js_result):
    whitelist = js_result["whitelist"]
    other = js_result["other"]
    assert whitelist["hidden"] is False
    assert whitelist["isError"] is False
    assert "hh.ru" in whitelist["text"]
    assert other["hidden"] is False
    assert other["isError"] is False
    assert "Личный сайт" in other["text"]


def test_url_invalid_shows_error_not_marker(js_result):
    invalid = js_result["invalid"]
    assert invalid["hidden"] is False
    assert invalid["isError"] is True
    assert "http" in invalid["text"]
    assert js_result["empty"]["hidden"] is True


# ── screens/form.js: футер mini_portfolio + «Назад» на развилку (структурные сторожа) ───────

def test_portfolio_footer_has_three_buttons():
    text = _js_without_comments(FORM_SCREEN_JS)
    assert "spec.skip_label" in text
    assert "goSkip" in text
    # Третья кнопка футера рисуется по наличию skip_label в спеке (reg_engine.step_spec) —
    # квик 260911-2kb (пункт 3) расширил публикацию этой подписи с одного mini_portfolio на
    # все шаги `_SKIP_ALLOWED_STEPS`, сам механизм рендера футера не поменялся ни на байт.
    # Область — от `isForkPick` (начало сборки футера) до самого вызова
    # `setMainButton(isForkPick...)`, оба маркера встречаются в файле ровно один раз.
    footer_start = text.index("const isForkPick")
    footer_end = text.index("setMainButton(isForkPick", footer_start)
    footer_body = text[footer_start:footer_end]
    assert "spec.skip_label" in footer_body
    assert "onClick: goSkip" in footer_body


def test_back_returns_to_fork_screen():
    text = _js_without_comments(FORM_SCREEN_JS)
    assert "FORK_BACK_STEPS" in text
    for step_key in ("resume_link", "mini_projects", "mini_portfolio", "mini_direction"):
        assert f'"{step_key}"' in text
    # Область — от объявления `goBack` до следующего стабильного маркера drawStep()
    # (`const showProgress`), оба встречаются в файле ровно один раз.
    go_back_start = text.index("function goBack(")
    go_back_end = text.index("const showProgress", go_back_start)
    go_back_body = text[go_back_start:go_back_end]
    assert 'stepIndexFromKey(state.specs, "resume")' in go_back_body
    assert "resumeForkBranch = null" in go_back_body


# ── miniapp/routers/form.py: PATCH resume_type (deviation Rule 3, необходим для паритета) ──

from database import db as bot_db  # noqa: E402
from tests.test_miniapp_routes import (  # noqa: E402
    DELEGATE_ID, _cfg, _client, _hdr, _set, _standard_seed, _use_tmp_db as _use_tmp_routes_db,
)


@pytest.fixture
def http_client(tmp_path):
    path = _use_tmp_routes_db(tmp_path, "test_skillup_resume_fork_ui_28_http.db")
    _standard_seed()
    return _client(_cfg(path))


def test_patch_resume_type_invalid_token_is_bad_field(http_client):
    resp = http_client.patch(
        "/app/api/reg/draft", headers=_hdr(DELEGATE_ID),
        json={"version": 0, "answers": {"resume_type": "garbage"}},
    )
    assert resp.status_code == 400
    assert resp.json() == {"reason": "bad_field", "field": "resume_type"}


def test_patch_resume_type_link_persists_and_reveals_resume_link_step(http_client):
    _set("reg_resume_mode", "fork")
    _set("reg_q_resume", "on")
    _set("reg_q_resume_link", "on")
    resp = http_client.patch(
        "/app/api/reg/draft", headers=_hdr(DELEGATE_ID),
        json={"version": 0, "answers": {"resume_type": "link"}, "step": "resume"},
    )
    assert resp.status_code == 200, resp.text
    row = asyncio.run(bot_db.get_reg_draft(DELEGATE_ID))
    assert row["answers"]["resume_type"] == "link"
    assert row["step"] == "resume_link"
