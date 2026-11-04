"""Плитки хаба без счётчика не висят на «…», развилка резюме не рисует эмодзи рядом с иконкой."""
from __future__ import annotations

from tests._paths import REPO_ROOT

HUB_JS = REPO_ROOT / "miniapp" / "static" / "js" / "screens" / "hub.js"
FORM_JS = REPO_ROOT / "miniapp" / "static" / "js" / "form.js"


def test_hub_tiles_do_not_hang_on_ellipsis():
    """Старый хаб ставил «…» на каждую плитку и снимал его только у плиток со счётчиком.
    Текущий хаб — три режима, подписи заданы явно, зависшего многоточия нет."""
    text = HUB_JS.read_text(encoding="utf-8")
    body = text[text.index("async function renderManagerHub("):]
    assert "MANAGER_FETCHERS" not in body
    assert 'meta: "…"' not in body


def test_resume_fork_drops_the_emoji_beside_the_icon():
    text = FORM_JS.read_text(encoding="utf-8")
    fn = text[text.index("function resumeForkControl"):text.index("const RESUME_FORK_LOCAL_BRANCHES")]
    assert "icon(opt.icon)" in fn
    assert "labelText(opt.label)" in fn
    assert "text: opt.label" not in fn
