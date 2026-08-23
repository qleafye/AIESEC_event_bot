"""Phase 19 Plan 03 Task 1 (WEBAPP-01, D-01): подписи анкеты и геймы вынесены в корневые
aiogram-free модули `reg_labels.py` / `game_labels.py`; в `handlers/` остались шимы.

Сторожа:
- шимы реэкспортируют ТЕ ЖЕ объекты (`is`, не `==`) — перенос, а не копия;
- `import reg_labels, game_labels` в чистом подпроцессе не загружает `aiogram`, а
  `import handlers.game_labels` — загружает (доказательство, что шим нужен);
- `miniapp` импортирует именно корневые модули;
- состав ключей `REG_LABELS` не изменился относительно снимка.
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent

REG_LABELS_KEYS_SNAPSHOT = [
    "reg_q_age", "reg_q_vk", "reg_q_email", "reg_q_phone", "reg_q_city", "reg_q_source",
    "reg_q_lc", "reg_q_position", "reg_q_education", "reg_q_university", "reg_q_course",
    "reg_q_specialty", "reg_q_work", "reg_q_work_sphere", "reg_q_skills", "reg_q_expectations",
    "reg_q_informal_day", "reg_q_attendance", "reg_q_comments", "reg_q_department",
    "reg_q_aiesec_role", "reg_q_certificate", "reg_q_alumni_status", "reg_q_english",
    "reg_q_allergies", "reg_q_food", "reg_q_arrival", "reg_q_housing", "reg_q_bed_sharing",
    "reg_q_bed_partner", "reg_q_transport", "reg_q_payment_date", "reg_q_cc_shop",
    "reg_q_exp_organizers", "reg_q_exp_content", "reg_q_volunteer", "reg_q_arrival_date",
    "reg_q_birth_date", "reg_q_study_field", "reg_q_goal", "reg_q_formats", "reg_q_ambassador",
    "reg_q_resume",
]

GAME_LABELS_PUBLIC = ["category_label", "proof_types_label", "render_task_card_text", "task_deadline_short"]


def _loaded_aiogram(code: str) -> list[str]:
    env = dict(os.environ)
    env["PYTHONPATH"] = str(ROOT) + os.pathsep + env.get("PYTHONPATH", "")
    env["PYTHONIOENCODING"] = "utf-8"
    snippet = (
        code + "\nimport sys\n"
        "print(sorted(m for m in sys.modules if m == 'aiogram' or m.startswith('aiogram.')))"
    )
    proc = subprocess.run(
        [sys.executable, "-c", snippet], cwd=str(ROOT), env=env,
        capture_output=True, text=True, encoding="utf-8", timeout=300,
    )
    assert proc.returncode == 0, proc.stderr[-3000:]
    return eval(proc.stdout.strip().splitlines()[-1])  # noqa: S307 — список строк из нашего же print


# ── identity: перенос, а не копия ────────────────────────────────────────────────────────

def test_reg_schema_reexports_same_objects():
    import reg_labels
    from handlers import reg_schema

    assert reg_schema.REG_LABELS is reg_labels.REG_LABELS
    assert reg_schema.STATUS_LABELS is reg_labels.STATUS_LABELS


def test_admin_broadcasts_payment_labels_same_object():
    import reg_labels
    from handlers import admin_broadcasts

    assert admin_broadcasts._PAYMENT_STATUS_LABELS is reg_labels.PAYMENT_STATUS_LABELS


@pytest.mark.parametrize("name", GAME_LABELS_PUBLIC + ["_CATEGORY_KEY", "_PROOF_TYPE_KEY"])
def test_handlers_game_labels_shim_reexports_same_objects(name):
    import game_labels
    from handlers import game_labels as shim

    assert getattr(shim, name) is getattr(game_labels, name)


def test_game_labels_public_names_declared():
    import game_labels

    assert sorted(game_labels.__all__) == sorted(GAME_LABELS_PUBLIC)


# ── aiogram-free ────────────────────────────────────────────────────────────────────────

def test_root_label_modules_do_not_load_aiogram():
    assert _loaded_aiogram("import reg_labels, game_labels") == []


def test_handlers_game_labels_loads_aiogram_so_shim_is_needed():
    assert _loaded_aiogram("import handlers.game_labels") != []


def test_miniapp_imports_root_modules_not_handlers():
    for path in (ROOT / "miniapp").rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        for line in text.splitlines():
            stripped = line.strip()
            assert not stripped.startswith(("from handlers", "import handlers")), (
                f"{path.relative_to(ROOT)}: {stripped}"
            )


# ── снимок ключей ───────────────────────────────────────────────────────────────────────

def test_reg_labels_keys_snapshot():
    import reg_labels

    assert list(reg_labels.REG_LABELS) == REG_LABELS_KEYS_SNAPSHOT
    assert reg_labels.STATUS_LABELS == {"pending": "Новая", "approved": "Одобрена", "rejected": "Отклонена"}
    assert set(reg_labels.PAYMENT_STATUS_LABELS) == {"not_paid", "overdue", "receipt_sent", "paid"}


def test_profile_columns_cover_only_known_labels():
    """Каждый вопрос профиля — ключ REG_LABELS (опечатка в карте колонок ловится здесь)."""
    import reg_labels
    from miniapp.routers.profile import PROFILE_COLUMNS

    assert set(PROFILE_COLUMNS) <= set(reg_labels.REG_LABELS)
