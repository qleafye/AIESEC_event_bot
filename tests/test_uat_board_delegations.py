"""Доска приёмки `tools/uat_board/index.html`: два блока («Долг» / «Новое»), раздел «Делегации
вузов» и сохранность ключей отметок.

Доска — один HTML с данными в JS. Ключ отметки считается как `<сессия>.<раздел>.<шаг>`; по нему
тестировщики уже ставят отметки на сервере, поэтому ни один прежний ключ исчезнуть не должен.
"""
from __future__ import annotations
from tests._paths import REPO_ROOT

import re
import subprocess
from pathlib import Path

ROOT = REPO_ROOT
BOARD = ROOT / "tools" / "uat_board" / "index.html"

_SESSION = re.compile(r'^\{ key:"(s\d|bl)", n:')
_SECTION = re.compile(r'^\s*\{ key:"(\w+)", title:')
_STEP = re.compile(r'^\s*\{id:"(\w+)"')


def _step_keys(html: str) -> set[str]:
    """Ключи шагов и пунктов «Перед началом» из данных SESSIONS — тем же правилом, что и JS."""
    keys: set[str] = set()
    sess = sec = None
    in_setup = False
    for line in html.splitlines():
        if line.startswith("/* ───────── верхний уровень") or line.startswith("const BLOCKS"):
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
        m = _STEP.match(line)
        if sess and m:
            keys.add(f"{sess}.pre.{m.group(1)}" if in_setup else f"{sess}.{sec}.{m.group(1)}")
    return keys


def _board() -> str:
    return BOARD.read_text(encoding="utf-8")


def _head_board() -> str:
    # Эталон — доска, которой тестеры пользуются сейчас (до перестройки на два блока).
    out = subprocess.run(
        ["git", "show", "HEAD:tools/uat_board/index.html"],
        cwd=ROOT, capture_output=True, check=True,
    ).stdout
    return out.decode("utf-8")


def _deleg_block(html: str) -> str:
    i = html.index('{ key:"deleg"')
    j = html.index("\n  ]},", i)
    return html[i:j]


def test_deleg_section_has_enough_steps_and_real_texts():
    block = _deleg_block(_board())
    assert len(re.findall(r'\{id:"', block)) >= 12
    assert "soon:" not in block and "скоро" not in block
    for needle in (
        "🏫 Делегации", "UR REGS", "Проверить курс", "Не зашли", "Привязать", "📊 Дашборд",
        "ЦА в форме", "для делегаций выключены", "🎮 Геймификация для делегатов",
        "Приходит «Привет! Ты в списке делегации",
    ):
        assert needle in block, needle


def test_old_step_keys_all_survive():
    old = _step_keys(_head_board())
    new = _step_keys(_board())
    assert len(old) > 150  # парсер реально нашёл шаги
    missing = sorted(old - new)
    assert not missing, f"пропали ключи отметок: {missing[:10]}"


def test_new_deleg_keys_are_stable_and_unique():
    new = _step_keys(_board())
    deleg = {k for k in new if k.startswith("s2.deleg.")}
    assert len(deleg) >= 12
    assert "s2.deleg.1" in deleg


def test_two_blocks_exist_and_every_section_is_placed_once():
    html = _board()
    assert 'key:"debt"' in html and 'title:"Долг"' in html
    assert 'key:"new"' in html and 'title:"Новое"' in html
    block_js = html[html.index("const BLOCKS"):html.index("const SESS_TAG")]
    refs = re.findall(r'"((?:s\d|bl)\.\w+)"', block_js)
    assert len(refs) == len(set(refs)), "раздел сессии попал в блоки дважды"
    # Все разделы исходных сессий размещены в каком-то блоке.
    sections = set()
    sess = None
    for line in html[: html.index("const BLOCKS")].splitlines():
        m = _SESSION.match(line)
        if m:
            sess = m.group(1)
            continue
        m = _SECTION.match(line)
        if sess and m and not line.strip().startswith("setup"):
            sections.add(f"{sess}.{m.group(1)}")
    assert sections == set(refs), sorted(sections ^ set(refs))
    # «Новое» держит делегации и внешние формы.
    new_js = block_js[block_js.index('key:"new"'):]
    assert '"s2.deleg"' in new_js and '"s2.ext"' in new_js


def test_session_filter_chips_and_shared_features_kept():
    html = _board()
    for tag in ("Сессия 1 · 06.10", "Сессия 2 · 13.10", "Сессия 3 · 20.10", "Без сессии"):
        assert tag in html, tag
    for needle in ('"/api/state"', '"/api/step"', "Кто проверяет", "Перед началом", "Если застряли",
                   "Повторная проверка", 'id="copy"', "overflow-x:hidden"):
        assert needle in html, needle
