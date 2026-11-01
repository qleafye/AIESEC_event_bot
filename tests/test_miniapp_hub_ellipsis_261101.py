"""Плитки хаба без счётчика не висят на «…», развилка резюме не рисует эмодзи рядом с иконкой."""
from __future__ import annotations

from tests._paths import REPO_ROOT

HUB_JS = REPO_ROOT / "miniapp" / "static" / "js" / "screens" / "hub.js"
FORM_JS = REPO_ROOT / "miniapp" / "static" / "js" / "form.js"


def test_hub_ellipsis_only_while_a_counter_loads():
    text = HUB_JS.read_text(encoding="utf-8")
    body = text[text.index("async function renderManagerHub("):]
    assert 'meta: MANAGER_FETCHERS[item.hash] ? "…" : ""' in body
    tail = body[body.index("const fetcher = MANAGER_FETCHERS"):]
    assert 'small.textContent = ""' in tail[:800]


def test_resume_fork_drops_the_emoji_beside_the_icon():
    text = FORM_JS.read_text(encoding="utf-8")
    fn = text[text.index("function resumeForkControl"):text.index("const RESUME_FORK_LOCAL_BRANCHES")]
    assert "icon(opt.icon)" in fn
    assert "labelText(opt.label)" in fn
    assert "text: opt.label" not in fn
