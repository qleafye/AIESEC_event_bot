"""Phase 13 (refac-split-god-files) Plan 07 — REFAC-03: module-size convention, CI-enforced.

The whole point of Phase 13 (13-01..13-06) was cutting `handlers/admin.py` (3067 lines) and
`handlers/registration.py` (2953 lines) down to a set of readable, single-concern seam files
sharing one `Router()` per aggregator. Splitting once is worthless if nothing stops the same
files from silently re-accreting handlers back to god-file size over the next dozen features.

This is that guard. See `CONVENTIONS.md` ("Размер модуля: ориентир ~800 строк") for the full
rationale and the human-readable version of the table below.

Design (per 13-07-PLAN.md's <interfaces> note):
- The ~800-line number is a REVIEW ORIENTEER, not the CI gate itself — a small, honest overage
  (a screen that just needs a few more lines) should not break the build.
- The CI gate is a HARD ceiling per file: `DEFAULT_CEILING` (850) for any handlers/*.py file
  not named below, or an explicit per-file ceiling in `KNOWN_OVERAGES` for files that legitimately
  sit above that today. Every entry in `KNOWN_OVERAGES` carries a one-line reason — it is a named,
  reviewed exception, not a blanket loosening of the rule.
- Raising a ceiling is a deliberate act: edit `KNOWN_OVERAGES` (or add a new entry) IN THE SAME
  COMMIT as the growth that needs it, with an updated reason. Do not bump a neighboring file's
  ceiling "while you're in here" — each entry is owned by whoever grew that file.
"""
from __future__ import annotations

from pathlib import Path

import pytest

HANDLERS_DIR = Path(__file__).resolve().parent.parent / "handlers"

# Soft guideline every handlers/*.py module SHOULD stay under (see CONVENTIONS.md).
GUIDELINE = 800

# Hard CI ceiling for any handlers/*.py file NOT explicitly listed in KNOWN_OVERAGES below.
# Deliberately a little above GUIDELINE (per 13-07-PLAN.md's 850-900 note) so a small, honest
# overage doesn't break the build -- genuine regrowth toward god-file size still gets caught.
DEFAULT_CEILING = 850

# Named, reviewed exceptions: files that already sit above DEFAULT_CEILING as of 13-07 for a
# documented reason. Ceiling per file = current line count + ~5% slack, rounded. To raise one:
# edit the number + reason together, in the same commit as the growth that needs it.
KNOWN_OVERAGES: dict[str, tuple[int, str]] = {
    "registration.py": (
        2310,
        "FSM-движок (_ask_step/_advance/_decide_status/...), cmd_start, "
        "finalize_registration -- документированный разрыв 13-03 (registration.py "
        "остаётся агрегатором-ядром, а не god-файлом: reg_flow.py/reg_steps.py уже "
        "вынесены и оба комфортно под GUIDELINE)",
    ),
    "admin_gamification.py": (
        2020,
        "геймификация -- самый большой шов 13-04, растёт вместе с фичами игры "
        "(Phase 14/16)",
    ),
    "admin_settings.py": (
        1840,
        "настройки+инструменты таблицы+выгрузки -- блоки не смежны в исходном "
        "god-файле, разрез на два файла потребовал бы двух точек шва-импорта "
        "(см. 13-06-SUMMARY.md, Known Gap)",
    ),
    "admin_cities.py": (
        960,
        "города + сброс/импорт сезона + дедупликация -- один связный экран (13-05)",
    ),
    "admin_broadcasts.py": (
        860,
        "рассылки + pending_albums (13-05)",
    ),
    "admin_roles.py": (
        860,
        "роли + settings-guide (13-04)",
    ),
    "admin.py": (
        840,
        "сам агрегатор-ядро после 13-06, уже на границе GUIDELINE",
    ),
    "user_actions.py": (
        900,
        "НЕ часть Phase 13 (не god-файл этого рефактора) -- перебор обнаружен при "
        "13-07 верификации, вырос отдельно за счёт карточки задания делегата "
        "(Phase 14/16); зафиксирован как отдельный известный разрыв, не переносим "
        "сюда рефактор Phase 13",
    ),
}


def _handler_modules() -> list[Path]:
    return sorted(HANDLERS_DIR.glob("*.py"))


def _line_count(path: Path) -> int:
    with path.open("r", encoding="utf-8") as fh:
        return sum(1 for _ in fh)


def test_known_overages_reference_real_files():
    """Guards the allowlist itself: no stale entries for files that got deleted/renamed."""
    existing = {p.name for p in _handler_modules()}
    missing = sorted(name for name in KNOWN_OVERAGES if name not in existing)
    assert not missing, (
        f"KNOWN_OVERAGES lists module(s) that no longer exist under handlers/: {missing}. "
        "Remove the stale entry (the file was likely renamed or deleted)."
    )


@pytest.mark.parametrize("path", _handler_modules(), ids=lambda p: p.name)
def test_handler_module_under_size_ceiling(path: Path):
    """Fails loud when a handlers/*.py module crosses its size ceiling.

    Not in KNOWN_OVERAGES -> DEFAULT_CEILING applies. In KNOWN_OVERAGES -> that file's own
    ceiling applies instead. Either way: if this test fails, the module has regrown toward
    god-file size (or crossed its already-generous named exception) and needs a split -- see
    CONVENTIONS.md for the shared-router seam-import technique this phase established. If the
    growth is legitimate and a split is genuinely not worth it right now, raise the ceiling for
    THIS file in KNOWN_OVERAGES above, in the same commit, with an updated reason.
    """
    lines = _line_count(path)
    if path.name in KNOWN_OVERAGES:
        ceiling, reason = KNOWN_OVERAGES[path.name]
        assert lines <= ceiling, (
            f"{path.name}: {lines} lines exceeds its named exception ceiling ({ceiling}). "
            f"Recorded reason for the existing exception: {reason!r}. If this new growth is "
            "legitimate, raise the ceiling in KNOWN_OVERAGES (tests/test_module_size_"
            "convention_260816.py) in the same commit, with an updated reason -- otherwise "
            "split the module along a feature/callback-prefix seam (see CONVENTIONS.md)."
        )
    else:
        assert lines <= DEFAULT_CEILING, (
            f"{path.name}: {lines} lines exceeds the default ceiling ({DEFAULT_CEILING}, "
            f"guideline is ~{GUIDELINE}). Either split the module along a feature/"
            "callback-prefix seam (see CONVENTIONS.md's shared-router technique), or, if the "
            "growth is a deliberate and reviewed exception, add a named entry to "
            "KNOWN_OVERAGES in this file, in the same commit, with a one-line reason."
        )
