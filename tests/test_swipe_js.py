"""Phase 23 план 05 (APP-TINDER-03/04, D-04, T-23-22/T-23-23/T-23-24): поведенческая проверка
чистой функции `swipeDecision` из `miniapp/static/js/swipe.js`, запущенной в node (в проекте
нет JS-тестраннера; тот же приём, что `tests/test_settings_search_js.py` — модуль импортируется
в голом node без DOM, на уровне `swipeDecision` он к document/window не обращается).

Таблица кейсов — контракт D-04/<behavior> плана: тап/дрожание пальца не двигает карточку;
преобладание вертикали (или угол круче ~30° от горизонтали) отдаёт жест прокрутке; горизонталь
короче порога — карточка возвращается на место (progress/tilt); горизонталь за порогом
(`max(COMMIT_PX, width*COMMIT_RATIO)`) — approve/reject; мёртвая зона у левого края
(`EDGE_GUARD`) не даёт решить, даже если сдвиг перешёл порог (T-23-24).

Без node тест пропускается с явной причиной — статические сторожа
`tests/test_miniapp_frontend.py::test_swipe_js_exports_pure_and_no_dom_outside_attach_swipe`/
`test_swipe_js_thresholds_are_named_constants` остаются гейтом в любом случае.
"""
from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SWIPE_JS = ROOT / "miniapp" / "static" / "js" / "swipe.js"

# Жесты по контракту (dx/dy/width/startX) — startX по умолчанию далеко от края (150), чтобы
# EDGE_GUARD не примешивался к кейсам, которые его не проверяют.
GESTURES = {
    "plain_tap": {"dx": 0, "dy": 0, "width": 320, "startX": 150},
    "small_jitter": {"dx": 5, "dy": 2, "width": 320, "startX": 150},
    "vertical_scroll": {"dx": 5, "dy": 40, "width": 320, "startX": 150},
    # |dy|(15) < |dx|(20) — простое "|dy| > |dx|" его бы пропустило, но угол ~37° круче 30°:
    # именно это отличает вторую половину условия (VERTICAL_SLOPE_MAX) от первой.
    "steep_but_not_vertical": {"dx": 20, "dy": 15, "width": 320, "startX": 150},
    "partial_drag_right": {"dx": 40, "dy": 0, "width": 300, "startX": 150},
    "partial_drag_left": {"dx": -40, "dy": 0, "width": 300, "startX": 150},
    "commit_approve": {"dx": 110, "dy": 0, "width": 300, "startX": 150},
    "commit_reject": {"dx": -110, "dy": 0, "width": 300, "startX": 150},
    "narrow_width_floor": {"dx": 90, "dy": 0, "width": 100, "startX": 150},
    "edge_zone_blocks_commit": {"dx": 110, "dy": 0, "width": 300, "startX": 10},
    "edge_zone_blocks_short_drag": {"dx": 5, "dy": 0, "width": 300, "startX": 10},
    "no_start_x_still_commits": {"dx": 110, "dy": 0, "width": 300},
}

NODE_SCRIPT = """
const m = await import(%(url)s);
const gestures = %(gestures)s;
const out = { results: {}, constants: {
  HORIZONTAL_MIN: m.HORIZONTAL_MIN,
  COMMIT_PX: m.COMMIT_PX,
  COMMIT_RATIO: m.COMMIT_RATIO,
  MAX_TILT: m.MAX_TILT,
  EDGE_GUARD: m.EDGE_GUARD,
  VERTICAL_SLOPE_MAX: m.VERTICAL_SLOPE_MAX,
}, exports: Object.keys(m).sort() };
for (const [name, g] of Object.entries(gestures)) {
  out.results[name] = m.swipeDecision(g);
}
console.log(JSON.stringify(out));
"""


@pytest.fixture(scope="module")
def result() -> dict:
    node = shutil.which("node")
    if not node:
        pytest.skip("node не найден в PATH — поведенческий тест жеста swipe.js пропущен")
    script = NODE_SCRIPT % {
        "url": json.dumps(SWIPE_JS.resolve().as_uri()),
        "gestures": json.dumps(GESTURES, ensure_ascii=False),
    }
    proc = subprocess.run(
        [node, "--input-type=module", "-e", script],
        cwd=str(ROOT), capture_output=True, text=True, encoding="utf-8", timeout=120,
    )
    assert proc.returncode == 0, proc.stderr[-3000:]
    return json.loads(proc.stdout.strip().splitlines()[-1])


def test_module_exports_swipe_decision_and_attach_swipe(result):
    for name in ("swipeDecision", "attachSwipe"):
        assert name in result["exports"], name


def test_plain_tap_and_small_jitter_have_no_action_or_flags(result):
    for case in ("plain_tap", "small_jitter"):
        r = result["results"][case]
        assert r["action"] is None, case
        assert "vertical" not in r and "edge" not in r, case


def test_vertical_dominant_gesture_lets_scroll_win_without_moving_card(result):
    for case in ("vertical_scroll", "steep_but_not_vertical"):
        r = result["results"][case]
        assert r["action"] is None, case
        assert r["vertical"] is True, case
        assert "progress" not in r, f"{case}: вертикальный жест не двигает карточку вовсе"


def test_partial_horizontal_drag_returns_progress_and_tilt_without_committing(result):
    right = result["results"]["partial_drag_right"]
    left = result["results"]["partial_drag_left"]
    assert right["action"] is None and left["action"] is None
    assert 0 < right["progress"] < 1
    assert 0 < left["progress"] < 1
    # Наклон растёт в сторону сдвига (вправо — положительный, влево — отрицательный).
    assert right["tilt"] > 0
    assert left["tilt"] < 0
    assert abs(right["tilt"]) <= result["constants"]["MAX_TILT"] + 1e-9
    assert abs(left["tilt"]) <= result["constants"]["MAX_TILT"] + 1e-9


def test_full_horizontal_drag_commits_approve_or_reject(result):
    assert result["results"]["commit_approve"]["action"] == "approve"
    assert result["results"]["commit_reject"]["action"] == "reject"
    assert result["results"]["commit_approve"]["progress"] == 1
    assert result["results"]["commit_reject"]["progress"] == 1


def test_narrow_width_still_uses_commit_px_floor(result):
    # width=100 -> width*COMMIT_RATIO ниже COMMIT_PX, порог остаётся COMMIT_PX (D-04:
    # max(COMMIT_PX, width*COMMIT_RATIO)) — dx=90 не хватает, решения ещё нет.
    r = result["results"]["narrow_width_floor"]
    commit_px = result["constants"]["COMMIT_PX"]
    commit_ratio = result["constants"]["COMMIT_RATIO"]
    assert commit_px > 100 * commit_ratio, "тестовый кейс не проверяет пол COMMIT_PX"
    assert r["action"] is None
    assert r["progress"] == pytest.approx(90 / commit_px)


def test_edge_dead_zone_blocks_commit_even_past_threshold(result):
    committed_elsewhere = result["results"]["commit_approve"]
    assert committed_elsewhere["action"] == "approve"  # тот же dx, но не у края — решает
    blocked = result["results"]["edge_zone_blocks_commit"]
    assert blocked["action"] is None
    assert blocked["edge"] is True
    short_at_edge = result["results"]["edge_zone_blocks_short_drag"]
    assert short_at_edge["action"] is None
    assert short_at_edge["edge"] is True


def test_missing_start_x_does_not_trigger_edge_guard(result):
    # startX не передан (например, начальная синтетическая проверка) — мёртвая зона не может
    # сработать на неизвестных координатах, жест решается по обычным правилам.
    r = result["results"]["no_start_x_still_commits"]
    assert r["action"] == "approve"
    assert "edge" not in r


# ── attachSwipe: тап/драг, начатый на интерактивном потомке (владелец 03.09) ────────────────
# «Показать всё» переставала открываться после того, как attachSwipe стал ловить pointerdown на
# всей карточке и звать el.setPointerCapture — браузер ретаргетит все последующие pointer- и
# производные click-события на захвативший элемент, а не на исходную кнопку. Минимальный
# фейковый DOM (без jsdom в проекте, тот же приём файла) — только то, что использует attachSwipe:
# addEventListener/removeEventListener, closest(), setPointerCapture, style, clientWidth.
ATTACH_SWIPE_NODE_SCRIPT = """
globalThis.document = { documentElement: { dataset: { motion: "" } } };

class FakeEl {
  constructor(tag, opts = {}) {
    this.tagName = tag.toUpperCase();
    this.style = {};
    this.clientWidth = opts.clientWidth ?? 300;
    this._listeners = {};
    this.parent = opts.parent || null;
    this.capturedIds = [];
  }
  addEventListener(type, fn) { (this._listeners[type] ||= []).push(fn); }
  removeEventListener(type, fn) {
    this._listeners[type] = (this._listeners[type] || []).filter((f) => f !== fn);
  }
  dispatch(type, evt) { for (const fn of (this._listeners[type] || []).slice()) fn(evt); }
  setPointerCapture(id) { this.capturedIds.push(id); }
  closest(selector) {
    const tags = selector.split(",").map((s) => s.trim().toUpperCase());
    let node = this;
    while (node) {
      if (tags.includes(node.tagName)) return node;
      node = node.parent;
    }
    return null;
  }
}

const m = await import(%(url)s);
const card = new FakeEl("article", { clientWidth: 300 });
const button = new FakeEl("button", { parent: card });

const commits = [];
const cancels = [];
m.attachSwipe(card, {
  onCommit: (a) => commits.push(a),
  onCancel: (d) => cancels.push(d),
});

// Драг far-right, начатый НА КНОПКЕ — не должен ни захватить указатель, ни решить жест.
card.dispatch("pointerdown", { pointerId: 1, target: button, clientX: 150, clientY: 0 });
card.dispatch("pointermove", { pointerId: 1, target: button, clientX: 260, clientY: 0 });
card.dispatch("pointerup", { pointerId: 1, target: button, clientX: 260, clientY: 0 });

// Тот же драг, начатый на самой карточке — жест решает как обычно (approve).
card.dispatch("pointerdown", { pointerId: 2, target: card, clientX: 150, clientY: 0 });
card.dispatch("pointermove", { pointerId: 2, target: card, clientX: 260, clientY: 0 });
card.dispatch("pointerup", { pointerId: 2, target: card, clientX: 260, clientY: 0 });

console.log(JSON.stringify({ commits, cancels, captured: card.capturedIds }));
"""


@pytest.fixture(scope="module")
def interactive_result() -> dict:
    node = shutil.which("node")
    if not node:
        pytest.skip("node не найден в PATH — поведенческий тест attachSwipe пропущен")
    script = ATTACH_SWIPE_NODE_SCRIPT % {"url": json.dumps(SWIPE_JS.resolve().as_uri())}
    proc = subprocess.run(
        [node, "--input-type=module", "-e", script],
        cwd=str(ROOT), capture_output=True, text=True, encoding="utf-8", timeout=120,
    )
    assert proc.returncode == 0, proc.stderr[-3000:]
    return json.loads(proc.stdout.strip().splitlines()[-1])


def test_pointerdown_on_interactive_descendant_does_not_capture_or_decide(interactive_result):
    assert interactive_result["captured"] == [2]
    assert interactive_result["cancels"] == []


def test_pointerdown_on_card_itself_still_commits_swipe_decision(interactive_result):
    assert interactive_result["commits"] == ["approve"]
