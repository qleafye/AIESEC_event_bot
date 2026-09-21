"""Phase 32 Plan 13 (D-04/D-13/D-16/D-17/D-30): руководство, шпаргалка и встроенная справка
знают амбассадорский слой.

Проверяет:
- в docs/ADMIN_GUIDE.md и docs/ADMIN_CHEATSHEET.md есть обязательные слова («волна»,
  «амбассадор», «приглашённый», «штраф») — менеджер может найти этот раздел;
- ни в одном из двух доков нет «сырых» кодовых имён настроек фазы (латиница-бренды уже
  покрыты tests/test_ru_brand_wording_260824.py::test_human_docs_have_no_owner_latin_brand);
- каждый ключ, задокументированный этим планом во встроенной справке
  (handlers.admin_roles.SETTINGS_GUIDE_SECTIONS), реально существует в SETTINGS_SCHEMA и его
  `where` начинается с подписи настоящего раздела (переиспользует ту же идиому, что
  test_admin_sections_ia20.py::test_settings_guide_where_starts_with_a_real_section, второй
  сторож на ту же тему не заводим);
- .planning/phases/32-ambassador-waves/32-UAT.md существует и достаточно длинный для
  четырнадцати пунктов живой приёмки (план прямо требует min_lines в must_haves.artifacts).
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

import handlers.admin_roles as roles
import handlers.admin_sections as sec
from settings_schema import SETTINGS_SCHEMA

DOCS_ROOT = Path(__file__).resolve().parent.parent / "docs"
GUIDE = DOCS_ROOT / "ADMIN_GUIDE.md"
CHEATSHEET = DOCS_ROOT / "ADMIN_CHEATSHEET.md"

REQUIRED_WORDS = ("волн", "амбассадор", "приглаш", "штраф")

# Единый список ключей фазы 32, которые обязаны быть видны во встроенной справке
# (`handlers.admin_roles.SETTINGS_GUIDE_SECTIONS`) — параметрический тест ниже читает ЭТОТ
# список, а не дублирует ключи по одному ассертами.
PHASE_32_GUIDE_KEYS = [
    "ambassador_referral_coins",
    "game_late_penalty_percent",
    "wave_prize_places",
    "wave_rating_show_names",
    "wave_start_message_text",
]

# Более широкий список кодовых имён настроек фазы (план 32-02) — ни один из этих snake_case
# литералов не должен появляться в тексте ДЛЯ ЧЕЛОВЕКА (CLAUDE.md: код менеджеру не
# показываем), кроме как внутри явного примера в обратных кавычках.
PHASE_32_CODE_NAMES = (
    "ambassador_referral_coins", "game_late_penalty_percent", "wave_prize_places",
    "wave_rating_show_names", "dashboard_block_ambassadors", "wave_start_message_text",
    "wave_start_button_text", "wave_deadline_reminder_text", "wave_results_announce_text",
    "wave_results_winner_text", "wave_results_prize_text", "wave_rating_header_text",
    "wave_rating_own_line_text", "wave_rating_closed_text", "ambassador_block_header_text",
    "game_task_no_deadline_text", "game_task_penalty_hint_text", "ambassador_path_prompt_text",
    "ambassador_path_label_invite", "ambassador_path_label_content", "ambassador_path_label_none",
    "ambassador_leave_button_text", "ambassador_leave_confirm_text", "ambassador_leave_done_text",
    "wave_end_manager_text",
)

_INLINE_CODE_RE = re.compile(r"`[^`]*`")
_FENCE_MARK = "```"


def _text_without_code(text: str) -> str:
    """Тот же приём, что и tests/test_ru_brand_wording_260824.py::_iter_cleaned_doc_lines —
    вырезает код-заборы и инлайновый код, чтобы примеры в обратных кавычках были легальны."""
    out_lines = []
    in_fence = False
    for line in text.splitlines():
        if line.strip().startswith(_FENCE_MARK):
            in_fence = not in_fence
            continue
        if in_fence:
            continue
        out_lines.append(_INLINE_CODE_RE.sub(" ", line))
    return "\n".join(out_lines)


@pytest.mark.parametrize("doc_path", [GUIDE, CHEATSHEET])
def test_required_words_present(doc_path: Path):
    text = doc_path.read_text(encoding="utf-8").lower()
    missing = [w for w in REQUIRED_WORDS if w not in text]
    assert not missing, f"{doc_path.name}: нет слов {missing}"


@pytest.mark.parametrize("doc_path", [GUIDE, CHEATSHEET])
def test_no_raw_setting_code_names_outside_examples(doc_path: Path):
    cleaned = _text_without_code(doc_path.read_text(encoding="utf-8"))
    offenders = [name for name in PHASE_32_CODE_NAMES if name in cleaned]
    assert not offenders, f"{doc_path.name}: кодовые имена настроек в тексте для человека: {offenders}"


@pytest.mark.parametrize("key", PHASE_32_GUIDE_KEYS)
def test_key_documented_in_settings_guide(key: str):
    assert key in SETTINGS_SCHEMA, f"{key} не заведён в SETTINGS_SCHEMA"
    entries = {
        entry["key"]: entry
        for _title, _subtitle, entries in roles.SETTINGS_GUIDE_SECTIONS
        for entry in entries
    }
    assert key in entries, f"{key} отсутствует в SETTINGS_GUIDE_SECTIONS"
    labels = [label for _token, label, _rows in sec.SECTIONS]
    where = entries[key]["where"]
    assert any(where.startswith(label) for label in labels), (key, where)


def test_uat_checklist_scaffold_exists_and_long_enough():
    uat_path = (
        Path(__file__).resolve().parent.parent
        / ".planning" / "phases" / "32-ambassador-waves" / "32-UAT.md"
    )
    if not uat_path.exists():
        pytest.skip(".planning/ вне git worktree — черновик приёмки живёт в основном репозитории")
    text = uat_path.read_text(encoding="utf-8")
    assert len(text.splitlines()) >= 60, "32-UAT.md короче min_lines плана"
