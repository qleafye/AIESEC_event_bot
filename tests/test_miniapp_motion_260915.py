"""Quick 260915-4mw (ANIM-01..06): поведенческая проверка примитивов движения
`miniapp/static/js/motion.js`, запущенная в голом node (тот же приём, что
`tests/test_swipe_js.py` — модуль импортируется без DOM/jsdom, минимальная заглушка DOM
собрана прямо в скрипте).

Задача 1 — `resolveMotionTier` (чистая функция, без DOM) + `slideIn`/`stagger`/`progressTo`
на минимальной заглушке узла (classList/style/offsetWidth/addEventListener/children) + инвариант
«off ничего не добавляет». Задачи 2-4 дописывают в этот же файл сторожей реестра/поверхностей
менеджера, подключения в app.js/form.js/status.js и лесенки списков/скелетонов — без node тесты
пропускаются с явной причиной, статические сторожа (`test_miniapp_frontend.py`) остаются гейтом
в любом случае.
"""
from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
MOTION_JS = ROOT / "miniapp" / "static" / "js" / "motion.js"

# Таблица кейсов resolveMotionTier (D-17 + quick 260915-4mw четвёртый сигнал `setting`).
CASES = {
    "reduced_floor_wins_over_auto_setting": {"reduced": True, "setting": "auto"},
    "reduced_floor_wins_over_micro_setting": {"reduced": True, "setting": "micro"},
    "auto_setting_on_healthy_device_is_full": {"setting": "auto"},
    "auto_setting_on_weak_android_is_micro": {"lowCoresAndroid": True, "setting": "auto"},
    "micro_setting_weakens_healthy_device": {"setting": "micro"},
    "off_setting_on_healthy_device_is_off": {"setting": "off"},
    "off_setting_on_low_battery_stays_off": {"lowBattery": True, "setting": "off"},
    "setting_never_strengthens_auto_downgrade": {"lowCoresAndroid": True, "setting": "auto"},
    "unknown_setting_treated_as_auto": {"setting": "banana"},
    "missing_setting_treated_as_auto": {},
}

EXPECTED_TIERS = {
    "reduced_floor_wins_over_auto_setting": "off",
    "reduced_floor_wins_over_micro_setting": "off",
    "auto_setting_on_healthy_device_is_full": "full",
    "auto_setting_on_weak_android_is_micro": "micro",
    "micro_setting_weakens_healthy_device": "micro",
    "off_setting_on_healthy_device_is_off": "off",
    "off_setting_on_low_battery_stays_off": "off",
    "setting_never_strengthens_auto_downgrade": "micro",
    "unknown_setting_treated_as_auto": "full",
    "missing_setting_treated_as_auto": "full",
}

# Минимальная заглушка DOM (нет jsdom в проекте, тот же приём, что ATTACH_SWIPE_NODE_SCRIPT в
# test_swipe_js.py) — только то, что использует slideIn/stagger/progressTo: classList,
# style, offsetWidth, addEventListener({once}), children. requestAnimationFrame — вручную
# флашится (flushRAF), чтобы проверить и промежуточное, и конечное состояние progressTo.
NODE_SCRIPT = """
globalThis.document = { documentElement: { dataset: { motion: "full" } } };
let queuedCb = null;
globalThis.requestAnimationFrame = (cb) => { queuedCb = cb; return 1; };
function flushRAF() { const cb = queuedCb; queuedCb = null; if (cb) cb(0); }

class FakeClassList {
  constructor() { this._set = new Set(); this._everAdded = new Set(); }
  add(...names) { for (const n of names) { this._set.add(n); this._everAdded.add(n); } }
  remove(...names) { for (const n of names) this._set.delete(n); }
  contains(name) { return this._set.has(name); }
  toArray() { return [...this._set]; }
  everAdded(name) { return this._everAdded.has(name); } // slideIn снимает m-from-* синхронно
  // ДО возврата из функции (реflow + снятие класса в одном такте) — без журнала «когда-либо
  // добавляли» проверить, что стартовое направление вообще ставилось, было бы нечем.
}

class FakeEl {
  constructor() {
    this.classList = new FakeClassList();
    this.style = {};
    this._listeners = {};
    this.offsetWidth = 100;
    this.children = [];
  }
  addEventListener(type, fn, opts) {
    (this._listeners[type] ||= []).push({ fn, once: !!(opts && opts.once) });
  }
  dispatch(type) {
    const list = (this._listeners[type] || []).slice();
    for (const { fn } of list) fn();
    this._listeners[type] = (this._listeners[type] || []).filter((l) => !l.once);
  }
}

const m = await import(%(url)s);

// ── resolveMotionTier: таблица кейсов ──────────────────────────────────────────────────────
const cases = %(cases)s;
const tierResults = {};
for (const [name, input] of Object.entries(cases)) {
  tierResults[name] = m.resolveMotionTier(input);
}

// ── slideIn: "full" добавляет и снимает классы, "off" не трогает узел вовсе ─────────────────
document.documentElement.dataset.motion = "full";
const elFwd = new FakeEl();
m.slideIn(elFwd, "fwd");
// m-from-right ставится и снимается синхронно в одном такте (reflow между ними) — наблюдаемо
// только через журнал «когда-либо добавляли», не через текущее состояние classList.
const slideInFwdDuring = { anim: elFwd.classList.contains("m-anim"), everHadFromRight: elFwd.classList.everAdded("m-from-right") };
elFwd.dispatch("transitionend");
const slideInFwdAfterEnd = elFwd.classList.toArray();

document.documentElement.dataset.motion = "full";
const elBack = new FakeEl();
m.slideIn(elBack, "back");
const slideInBackHasFromLeft = elBack.classList.everAdded("m-from-left");

document.documentElement.dataset.motion = "off";
const elOff = new FakeEl();
m.slideIn(elOff, "fwd");
const slideInOffClasses = elOff.classList.toArray();
const slideInOffStyle = Object.keys(elOff.style);

// ── stagger: первые max детей получают m-rise + возрастающую задержку, off — ничего ────────
document.documentElement.dataset.motion = "full";
const container = new FakeEl();
container.children = Array.from({ length: 10 }, () => new FakeEl());
m.stagger(container, { from: 0 });
const risenCount = container.children.filter((c) => c.classList.contains("m-rise")).length;
const delays = container.children.slice(0, 8).map((c) => c.style.animationDelay);
container.children[0].dispatch("animationend");
const firstAfterEnd = { hasRise: container.children[0].classList.contains("m-rise"), delay: container.children[0].style.animationDelay };

document.documentElement.dataset.motion = "off";
const containerOff = new FakeEl();
containerOff.children = Array.from({ length: 5 }, () => new FakeEl());
m.stagger(containerOff, { from: 0 });
const offStaggerTouched = containerOff.children.some((c) => c.classList.toArray().length > 0 || Object.keys(c.style).length > 0);

// stagger с from>0 — «Показать ещё» анимирует только новые строки.
document.documentElement.dataset.motion = "full";
const containerFrom = new FakeEl();
containerFrom.children = Array.from({ length: 6 }, () => new FakeEl());
m.stagger(containerFrom, { from: 4 });
const untouchedBeforeFrom = containerFrom.children.slice(0, 4).every((c) => !c.classList.contains("m-rise"));
const touchedFromIndex = containerFrom.children.slice(4).every((c) => c.classList.contains("m-rise"));

// ── progressTo: full доезжает в два шага (rAF), off — сразу конечное значение, оба зажаты ──
document.documentElement.dataset.motion = "full";
const fillFull = new FakeEl();
m.progressTo(fillFull, 0.2, 0.6);
const progressBeforeFlush = fillFull.style.transform;
flushRAF();
const progressAfterFlush = fillFull.style.transform;

document.documentElement.dataset.motion = "off";
const fillOff = new FakeEl();
m.progressTo(fillOff, 0.2, 0.6);
const progressOffFinal = fillOff.style.transform;

document.documentElement.dataset.motion = "full";
const fillClamp = new FakeEl();
m.progressTo(fillClamp, -1, 2);
flushRAF();
const progressClampFinal = fillClamp.style.transform;

console.log(JSON.stringify({
  exports: Object.keys(m).sort(),
  tierResults,
  slideInFwdDuring, slideInFwdAfterEnd, slideInBackHasFromLeft, slideInOffClasses, slideInOffStyle,
  risenCount, delays, firstAfterEnd, offStaggerTouched, untouchedBeforeFrom, touchedFromIndex,
  progressBeforeFlush, progressAfterFlush, progressOffFinal, progressClampFinal,
}));
"""


@pytest.fixture(scope="module")
def result() -> dict:
    node = shutil.which("node")
    if not node:
        pytest.skip("node не найден в PATH — поведенческий тест motion.js пропущен")
    script = NODE_SCRIPT % {
        "url": json.dumps(MOTION_JS.resolve().as_uri()),
        "cases": json.dumps(CASES, ensure_ascii=False),
    }
    proc = subprocess.run(
        [node, "--input-type=module", "-e", script],
        cwd=str(ROOT), capture_output=True, text=True, encoding="utf-8", timeout=120,
    )
    assert proc.returncode == 0, proc.stderr[-3000:]
    return json.loads(proc.stdout.strip().splitlines()[-1])


def test_module_exports_all_primitives(result):
    for name in (
        "resolveMotionTier", "applyMotionTier", "motionTier",
        "slideIn", "stagger", "progressTo", "confetti", "countUp", "haptic",
    ):
        assert name in result["exports"], name


def test_resolve_motion_tier_matches_expected_table(result):
    for name, expected in EXPECTED_TIERS.items():
        assert result["tierResults"][name] == expected, name


def test_setting_never_strengthens_the_auto_downgraded_tier(result):
    # Один и тот же вход ({lowCoresAndroid: true, setting: "auto"}) под двумя именами (D-Motion:
    # «настройка не может усилить уровень») — оба обязаны остаться "micro".
    assert result["tierResults"]["auto_setting_on_weak_android_is_micro"] == "micro"
    assert result["tierResults"]["setting_never_strengthens_auto_downgrade"] == "micro"


def test_slide_in_adds_anim_and_direction_class_then_cleans_up_on_transitionend(result):
    assert result["slideInFwdDuring"] == {"anim": True, "everHadFromRight": True}
    assert result["slideInFwdAfterEnd"] == [], "m-anim должен сняться по transitionend"


def test_slide_in_back_direction_uses_from_left(result):
    assert result["slideInBackHasFromLeft"] is True


def test_stagger_marks_first_max_children_with_increasing_delay_and_cleans_up(result):
    assert result["risenCount"] == 8  # max по умолчанию — не больше 8 строк
    assert result["delays"] == [f"{i * 36}ms" for i in range(8)]
    assert result["firstAfterEnd"] == {"hasRise": False, "delay": ""}


def test_stagger_with_from_offset_only_touches_new_rows(result):
    assert result["untouchedBeforeFrom"] is True
    assert result["touchedFromIndex"] is True


def test_progress_to_moves_from_start_to_target_across_one_animation_frame(result):
    assert result["progressBeforeFlush"] == "scaleX(0.2)"
    assert result["progressAfterFlush"] == "scaleX(0.6)"


def test_progress_to_clamps_values_into_0_1_range(result):
    assert result["progressClampFinal"] == "scaleX(1)"


def test_off_tier_slide_in_and_stagger_add_zero_classes_and_zero_styles(result):
    # Инвариант «off ничего не добавляет» (verification §3): DOM и порядок узлов совпадают с
    # сегодняшними. progressTo — единственное узаконенное исключение (ставит конечный scaleX,
    # это состояние полосы, а не движение), поэтому здесь не проверяется.
    assert result["slideInOffClasses"] == []
    assert result["slideInOffStyle"] == []
    assert result["offStaggerTouched"] is False


def test_off_tier_progress_to_sets_final_scale_without_intermediate_state(result):
    assert result["progressOffFinal"] == "scaleX(0.6)"


# ── Задача 2: ключ реестра «Анимации приложения» + обе поверхности менеджера ─────────────────
# Статические сторожа на исходный текст (тот же приём, что многие тесты test_miniapp_frontend.py
# — grep по файлу), а не полный HTTP-раунд-трип: ключ реестра проверяется напрямую импортом
# SETTINGS_SCHEMA, поверхности — присутствием ожидаемых строк в исходниках.

from settings_schema import SETTINGS_SCHEMA  # noqa: E402

PAGE_PY = ROOT / "miniapp" / "routers" / "page.py"
APP_HTML = ROOT / "miniapp" / "templates" / "app.html"
ADMIN_MINIAPP_PY = ROOT / "handlers" / "admin_miniapp.py"


def test_miniapp_motion_key_registered_with_expected_options_labels_and_default():
    entry = SETTINGS_SCHEMA["miniapp_motion"]
    assert entry["type"] == "enum"
    assert entry["group"] == "miniapp"
    assert entry["options"] == ["auto", "micro", "off"]
    assert entry["default"] == "auto"
    # CLAUDE.md «бот для людей»: подписи человеческие, не совпадают с кодом варианта.
    for code in entry["options"]:
        label = entry["option_labels"][code]
        assert label != code
        assert isinstance(label, str) and label.strip()


def test_shell_context_and_disabled_fallback_carry_motion_setting():
    text = PAGE_PY.read_text(encoding="utf-8")
    assert '"motion_setting": read_setting(conn, "miniapp_motion") or "auto"' in text
    assert '"motion_setting": "auto"' in text  # аварийный словарь render_disabled_page


def test_app_html_body_carries_motion_setting_attribute():
    text = APP_HTML.read_text(encoding="utf-8")
    assert 'data-motion-setting="{{ motion_setting }}"' in text


def test_miniapp_settings_keyboard_has_cycle_motion_button_and_handler():
    text = ADMIN_MINIAPP_PY.read_text(encoding="utf-8")
    assert 'callback_data="miniapp_cycle_motion"' in text
    assert '@router.callback_query(F.data == "miniapp_cycle_motion")' in text
    # Циклит ровно auto -> micro -> off -> auto (D-Motion: менеджер может только ослабить,
    # но выключить и снова включить обязан мочь одной и той же кнопкой по кругу).
    assert '_MOTION_CYCLE = {"auto": "micro", "micro": "off", "off": "auto"}' in text


def test_render_miniapp_settings_text_shows_motion_label_not_code():
    text = ADMIN_MINIAPP_PY.read_text(encoding="utf-8")
    assert "✨ Анимации приложения:" in text
    assert 'SETTINGS_SCHEMA["miniapp_motion"]["option_labels"]' in text
