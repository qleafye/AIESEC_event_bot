"""Порог приглашённых на уже существующей «Моя ссылка». Дефолт 5.

Счёт — сколько человек завершили анкету по ссылке. Ступени и статус амбассадора
сюда не входят. Новой кнопки меню нет.
"""
from __future__ import annotations

from pathlib import Path

from config import config
from database import db
from domain.settings.schema import SETTINGS_SCHEMA
from keyboards.builders import MENU_BUTTONS
from services.amb import invite_threshold
from tests._dbtpl import fast_init_db
from tests._enroll38 import add_user, run

_FORBIDDEN = ("отбор", "не прошёл", "не прошел", "отказ")
_TEXT_KEYS = (
    "amb_invite_threshold_with_count_text",
    "amb_invite_threshold_done_with_count_text",
    "amb_invite_threshold_left_text",
    "amb_invite_threshold_done_text",
)
_ROOT = Path(__file__).resolve().parents[1]


def _ready(tmp_path):
    config.DB_PATH = str(tmp_path / "invite29.db")
    config.ADMIN_IDS = [1]
    fast_init_db()


async def _tr(key: str) -> str:
    return SETTINGS_SCHEMA[key]["default"]


async def _referred(owner: int, n: int):
    await add_user(owner, season=None)
    for i in range(n):
        tid = owner * 100 + i
        await add_user(tid, season=None)
        async with db._connect() as conn:
            await conn.execute(
                "UPDATE users SET referrer_id = ? WHERE telegram_id = ?", (owner, tid),
            )
            await conn.commit()


def test_threshold_default_is_5():
    spec = SETTINGS_SCHEMA["amb_invite_threshold"]
    assert spec["default"] == 5
    assert spec["min"] == 1
    assert spec["group"] == "amb"
    assert spec["label"] == "👥 Порог приглашённых"
    assert "referral_threshold" not in SETTINGS_SCHEMA


def test_count_is_completed_referrals_not_qualified(tmp_path):
    _ready(tmp_path)
    run(_referred(30, 2))
    before = run(db.get_user(30)).get("is_ambassador")
    text = run(invite_threshold.threshold_line(30, progress_shown=False, tr=_tr))
    assert "2" in text
    assert run(db.get_user(30)).get("is_ambassador") == before
    src = (_ROOT / "services" / "amb" / "invite_threshold.py").read_text(encoding="utf-8")
    assert "referral_counts" not in src
    assert "is_ambassador" not in src


def test_left_floors_at_zero(tmp_path):
    _ready(tmp_path)
    run(_referred(40, 5))
    done = run(invite_threshold.threshold_line(40, progress_shown=False, tr=_tr))
    assert "уже пройден" in done
    assert "осталось 0" not in done
    assert "-1" not in done
    run(_referred(41, 6))
    more = run(invite_threshold.threshold_line(41, progress_shown=False, tr=_tr))
    assert "уже пройден" in more
    assert "осталось" not in more


def test_screen_line_includes_count_when_progress_hidden(tmp_path):
    _ready(tmp_path)
    run(_referred(50, 2))
    text = run(invite_threshold.threshold_line(50, progress_shown=False, tr=_tr))
    assert "2" in text
    assert "5" in text
    assert text == "По твоей ссылке анкету завершили: 2. До порога 5 осталось 3."


def test_screen_line_omits_count_when_progress_shown(tmp_path):
    _ready(tmp_path)
    run(_referred(60, 2))
    text = run(invite_threshold.threshold_line(60, progress_shown=True, tr=_tr))
    assert text == "До порога 5 осталось 3."
    assert "2" not in text


def test_defaults_have_no_forbidden_words():
    for key in _TEXT_KEYS:
        default = SETTINGS_SCHEMA[key]["default"].lower()
        for word in _FORBIDDEN:
            assert word not in default, key
        assert SETTINGS_SCHEMA[key]["group"] == "amb"


def test_no_new_menu_button():
    blob = " ".join(f"{key} {text}" for key, text in MENU_BUTTONS)
    assert "amb_invite" not in blob
    screen = (_ROOT / "handlers" / "game" / "referral_screen.py").read_text(encoding="utf-8")
    hub = (_ROOT / "miniapp" / "routers" / "hub.py").read_text(encoding="utf-8")
    assert screen.count("threshold_line(") == 1
    assert hub.count("threshold_line(") == 1
