"""Доска приёмки `tools/uat_board/index.html` (прогон 20.10): три блока («Новое с 13.10»,
«Перепроверка», «Форум Москвы»), размещение разделов сессий и устойчивость ключей отметок.

Доска — один HTML с данными в JS. Ключ отметки считается как `<сессия>.<раздел>.<шаг>`; тестировщики
ставят отметки на сервере по этим ключам, поэтому ключи внутри файла уникальны и не зависят от
группировки в блоки.
"""
from __future__ import annotations
from tests._paths import REPO_ROOT

import re
from collections import Counter

BOARD = REPO_ROOT / "tools" / "uat_board" / "index.html"

_SESSION = re.compile(r'^\{ key:"(nw|rc|rh)", n:')
_SECTION = re.compile(r'^\s*\{ key:"(\w+)", title:')
_STEP = re.compile(r'^\s*\{id:"(\w+)"')
_REF = re.compile(r'^\s*\{ref:"([\w.]+)"')
BLOCK_ORDER = ["nov", "chk", "msk"]


def _board() -> str:
    return BOARD.read_text(encoding="utf-8")


def _step_keys(html: str) -> list[str]:
    """Ключи шагов и пунктов «Перед началом» из данных SESSIONS — тем же правилом, что и JS."""
    keys: list[str] = []
    sess = sec = None
    in_setup = False
    for line in html.splitlines():
        if line.startswith("const BLOCKS"):
            break
        m = _SESSION.match(line)
        if m:
            sess, sec, in_setup = m.group(1), None, False
            continue
        if sess and line.strip().startswith("setup:"):
            in_setup = True
            continue
        if sess and line.strip().startswith("sections:["):
            in_setup = False
            continue
        m = _SECTION.match(line)
        if sess and m and not in_setup:
            sec = m.group(1)
            continue
        m = _REF.match(line)
        if sess and m:
            keys.append(m.group(1))
            continue
        m = _STEP.match(line)
        if sess and m:
            keys.append(f"{sess}.pre.{m.group(1)}" if in_setup else f"{sess}.{sec}.{m.group(1)}")
    return keys


def _sections_of_sessions(html: str) -> set[str]:
    sections: set[str] = set()
    sess = None
    for line in html[: html.index("const BLOCKS")].splitlines():
        m = _SESSION.match(line)
        if m:
            sess = m.group(1)
            continue
        m = _SECTION.match(line)
        if sess and m:
            sections.add(f"{sess}.{m.group(1)}")
    return sections


def test_three_blocks_exist_in_order_and_every_section_is_placed_once():
    html = _board()
    block_js = html[html.index("const BLOCKS"):html.index("const SESS_TAG")]
    assert re.findall(r'^\{ key:"(\w+)", icon:', block_js, flags=re.M) == BLOCK_ORDER
    refs = re.findall(r'from:\["((?:nw|rc|rh)\.\w+)"\]', block_js)
    assert len(refs) == len(set(refs)), "раздел сессии попал в блоки дважды"
    sections = _sections_of_sessions(html)
    assert len(sections) >= 20
    assert sections == set(refs), sorted(sections ^ set(refs))
    # разделы каждой сессии лежат в «своём» блоке
    chunks = {k: block_js[block_js.index(f'{{ key:"{k}"'):] for k in BLOCK_ORDER}
    nov = chunks["nov"][: chunks["nov"].index('{ key:"chk"')]
    chk = chunks["chk"][: chunks["chk"].index('{ key:"msk"')]
    msk = chunks["msk"]
    assert set(re.findall(r'from:\["(\w+)\.', nov)) == {"nw"}
    assert set(re.findall(r'from:\["(\w+)\.', chk)) == {"rc"}
    assert set(re.findall(r'from:\["(\w+)\.', msk)) == {"rh"}


def test_step_keys_are_unique_and_stable():
    keys = _step_keys(_board())
    assert len(keys) > 100  # парсер реально нашёл шаги
    dup = [k for k, n in Counter(keys).items() if n > 1]
    assert not dup, f"повторяются ключи отметок: {dup[:10]}"
    assert all(re.fullmatch(r"(nw|rc|rh)\.\w+\.\w+", k) for k in keys), [k for k in keys if not re.fullmatch(r"(nw|rc|rh)\.\w+\.\w+", k)][:5]
    assert {"nw.prep.1", "rc.rules1.1", "rh.prep.1"} <= set(keys)
    assert {k.split(".")[0] for k in keys} == {"nw", "rc", "rh"}


def test_every_recheck_section_says_what_was_wrong_last_time():
    """Шаг ❌ прошлого прогона несёт поле was; в каждом разделе «Перепроверки» такой шаг есть."""
    html = _board()
    rc = html[html.index('{ key:"rc", n:'):html.index('{ key:"rh", n:')]
    parts = re.split(r'^\s*\{ key:"\w+", title:', rc, flags=re.M)[1:]
    assert len(parts) == 6
    for part in parts:
        assert re.search(r'^\s*was:"[^"]{20,}"', part, flags=re.M), part[:80]


def test_session_filter_chips_and_shared_features_kept():
    html = _board()
    assert 'const SESS_TAG = { nw: "Новое с 13.10", rc: "Перепроверка", rh: "Форум Москвы" };' in html
    assert 'const SESS_ORDER = ["nw", "rc", "rh"];' in html
    assert 'const LS = "uat-261020";' in html
    tabs = html[html.index("const TABS = {"):html.index("const tabOf")]
    assert re.findall(r'^\s+(\w): \{ name: "([^"]+)"', tabs, flags=re.M) == [
        ("D", "Делегат"), ("M", "Менеджер"), ("V", "Волонтёр"), ("O", "Владелец")]
    for needle in ('"/api/state"', '"/api/step"', "Кто проверяет", "Перед началом", "Если застряли",
                   "Повторная проверка", 'id="copy"', "overflow-x:hidden"):
        assert needle in html, needle
