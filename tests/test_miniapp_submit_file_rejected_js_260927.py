"""Квик 27.09: экран сдачи (`screens/submit.js`) на отказ Telegram по файлу.

Сервер теперь отвечает 400 `file_rejected` с текстом реестра, когда Telegram не принял файл
(`miniapp_upload_file_rejected_text`). Экран обязан показать этот текст (фолбэк —
`limits.file_rejected_text` из GET /uploads/limits), а текст «не удалось загрузить — ещё раз»
(`limits.upload_failed_text`, с именем файла вместо `{name}`) оставить только сбою связи / 502. Клиентская догадка kind до ответа сервера совпадает с серверным
правилом: фото — только JPEG/PNG/GIF.

Node-подпроцесс с фейковым DOM из `tests/test_miniapp_error_states_js_260911.py`.
"""
from __future__ import annotations

import json

import pytest

from tests.test_miniapp_error_states_js_260911 import SCREENS_DIR, _FAKE_DOM_PRELUDE, _run_script
from tests.test_miniapp_error_states_js_260911 import node  # noqa: F401 — фикстура

_SCRIPT = _FAKE_DOM_PRELUDE + """
document.body.dataset.screenTexts = JSON.stringify({ load_error: "ошибка", retry: "повтор" });
globalThis.FormData = class { append() {} };
const mod = await import(%(url)s);

const limits = {
  max_parts: 5, max_bytes: 1000000, photo_max_bytes: 500000, max_text: 500,
  too_large_text: "слишком большой", empty_hint: "нечего отправлять",
  file_rejected_text: "не принят",
  upload_failed_text: "сбой «{name}», ещё раз",
};

function allNodes(node, out = []) {
  for (const c of node.children || []) {
    if (!c || c.nodeType === 3) continue;
    out.push(c);
    allNodes(c, out);
  }
  return out;
}
function deepText(node) {
  if (!node) return "";
  if (node.nodeType === 3) return node.textContent;
  return (node.children || []).map(deepText).join("");
}
const tick = () => new Promise((r) => setTimeout(r, 0));

async function mount(uploadImpl) {
  const root = document.createElement("div");
  async function api(path) {
    if (path.startsWith("/tasks/")) return { id: 1, can_submit: true, status: "new", title: "T", proof_hint: "фото" };
    if (path.startsWith("/uploads/limits")) return limits;
    if (path === "/uploads") return uploadImpl();
    throw new Error("unexpected " + path);
  }
  await mod.render(root, { id: "1" }, { h, api, navigate: () => {}, setMainButton: () => {}, tg: null, me: {} });
  const input = allNodes(root).find((n) => n.tagName === "INPUT" && n.getAttribute("type") === "file");
  return { root, input };
}

async function uploadCase(file, uploadImpl) {
  const { root, input } = await mount(uploadImpl);
  input.files = [file];
  input.dispatch("change", {});
  for (let i = 0; i < 5; i++) await tick();
  const errNode = root.querySelector(".error-state");
  return errNode ? deepText(errNode) : null;
}

function apiErr(status, reason, payload) {
  const e = err(status, reason);
  e.payload = payload;
  return e;
}

const jpg = { name: "pic.jpg", size: 100, type: "image/jpeg" };

const rejectedWithText = await uploadCase(jpg, async () => { throw apiErr(400, "file_rejected", { reason: "file_rejected", text: "серверный текст" }); });
const rejectedNoText = await uploadCase(jpg, async () => { throw apiErr(400, "file_rejected", {}); });
const network = await uploadCase(jpg, async () => { throw new TypeError("Failed to fetch"); });
const unavailable = await uploadCase(jpg, async () => { throw apiErr(502, "telegram_unavailable", { reason: "telegram_unavailable" }); });
const tooLarge = await uploadCase(jpg, async () => { throw apiErr(413, "too_large", { reason: "too_large" }); });

// Догадка kind до ответа сервера: загрузка «висит», читаем счётчик (фото — первая группа).
async function guessCase(file) {
  const { root, input } = await mount(() => new Promise(() => {}));
  input.files = [file];
  input.dispatch("change", {});
  await tick();
  const counter = root.querySelector(".parts-counter");
  const groups = counter.children.map((g) => deepText(g));
  return { photo: groups[0], document: groups[1] };
}
const heicGuess = await guessCase({ name: "IMG.HEIC", size: 100, type: "image/heic" });
const jpegGuess = await guessCase({ name: "pic.jpg", size: 100, type: "image/jpeg" });

console.log(JSON.stringify({ rejectedWithText, rejectedNoText, network, unavailable, tooLarge, heicGuess, jpegGuess }));
"""


@pytest.fixture(scope="module")
def result(node) -> dict:  # noqa: F811
    url = json.dumps((SCREENS_DIR / "submit.js").resolve().as_uri())
    return _run_script(node, _SCRIPT % {"url": url})


def test_file_rejected_shows_server_text(result):
    assert "серверный текст" in result["rejectedWithText"]
    assert "сбой" not in result["rejectedWithText"]


def test_file_rejected_without_payload_falls_back_to_limits_text(result):
    assert "не принят" in result["rejectedNoText"]
    assert "сбой" not in result["rejectedNoText"]


def test_network_failure_shows_registry_text_with_filename(result):
    assert result["network"] == "сбой «pic.jpg», ещё раз"


def test_telegram_unavailable_shows_registry_text_with_filename(result):
    assert result["unavailable"] == "сбой «pic.jpg», ещё раз"


def test_413_still_shows_too_large_text(result):
    assert "слишком большой" in result["tooLarge"]


def test_client_kind_guess_matches_server_rule(result):
    assert result["heicGuess"] == {"photo": "0", "document": "1"}
    assert result["jpegGuess"] == {"photo": "1", "document": "0"}
