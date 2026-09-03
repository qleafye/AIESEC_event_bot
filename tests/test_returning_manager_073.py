"""Phase 07.3 Plan 05 (RET-03) — manager-facing returning-delegate surfaces.

Covers three pure/near-pure render points that plan 01's `users.prev_season` column and
`get_returning_count()` accessor feed into, plus the ADMIN_GUIDE section explaining the
second sheet row to the manager:

- `handlers/admin.py::_render_application_card` — «🔁 Повторный: был(а) в …» badge line.
- `handlers/admin.py::render_stats_text` — «🔁 Повторных: N» global counter line.
- `docs/ADMIN_GUIDE.md` — new «Новый сезон и вернувшиеся делегаты» section, human language only.

pytest-asyncio is unavailable in this env — every async call goes through asyncio.run(),
config.DB_PATH points at a tmp_path file, no conftest.py (project convention, see
tests/test_season_data_073.py / tests/test_city_export_stats_phase72.py).
"""
import asyncio
from pathlib import Path

from config import config
from database import db
from handlers import admin as admin_mod
from handlers import admin_moderation  # Phase 13 (13-06): moderation moved out of admin.py


def _db_ready(tmp_path):
    config.DB_PATH = str(tmp_path / "test_returning_manager_073.db")
    asyncio.run(db.init_db())


def _seed(telegram_id: int, **overrides) -> None:
    data = {
        "telegram_id": telegram_id,
        "full_name": f"Delegate {telegram_id}",
        "registration_date": f"2026-08-18 09:{telegram_id:02d}:00",
    }
    data.update(overrides)
    asyncio.run(db.add_user(data))


# ── Task 1: moderation-card badge ────────────────────────────────────────────────────────

def test_card_shows_repeat_badge():
    # No apostrophe in the fixture value — html.escape (correctly, T-073-05-01) turns "'"
    # into "&#x27;", which would make a literal "YL'25" substring check misleading here.
    user = {"full_name": "Иван", "prev_season": "YL26"}
    out = admin_moderation._render_application_card(user, 1, 1)
    assert "🔁 Повторный: был(а) в YL26" in out


def test_card_legacy_badge_is_human():
    user = {"full_name": "Иван", "prev_season": "legacy"}
    out = admin_moderation._render_application_card(user, 1, 1)
    assert "🔁 Повторный: был(а) на прошлом событии" in out
    assert "legacy" not in out


def test_card_no_badge_for_newcomer():
    baseline = {"full_name": "Иван"}
    with_none = {"full_name": "Иван", "prev_season": None}
    with_empty = {"full_name": "Иван", "prev_season": ""}
    expected = admin_moderation._render_application_card(baseline, 1, 1)
    assert "Повторный" not in expected
    assert admin_moderation._render_application_card(with_none, 1, 1) == expected
    assert admin_moderation._render_application_card(with_empty, 1, 1) == expected


def test_card_escapes_prev_season():
    user = {"full_name": "Иван", "prev_season": "<b>x</b>"}
    out = admin_moderation._render_application_card(user, 1, 1)
    assert "<b>x</b>" not in out
    assert "&lt;b&gt;x&lt;/b&gt;" in out


def test_card_badge_after_track_line():
    user = {
        "full_name": "Иван",
        "participant_type": "party_overnight",
        "prev_season": "YL26",
    }
    out = admin_moderation._render_application_card(user, 1, 1)
    track_pos = out.index("🎉 Трек: вечеринка с ночёвкой")
    badge_pos = out.index("🔁 Повторный: был(а) в YL26")
    assert track_pos < badge_pos


# ── Task 1: stats-screen counter ─────────────────────────────────────────────────────────

def test_stats_has_returning_line(tmp_path):
    _db_ready(tmp_path)
    _seed(1, prev_season="YL'25")
    _seed(2, prev_season="YL'24")
    _seed(3)  # newcomer, no prev_season
    text = asyncio.run(admin_mod.render_stats_text())
    assert "🔁 Повторных: 2" in text


def test_stats_returning_line_outside_city_block(tmp_path):
    _db_ready(tmp_path)
    asyncio.run(db.set_setting("event_city_enabled", "off"))
    _seed(1, prev_season="YL'25")
    text = asyncio.run(admin_mod.render_stats_text())
    assert "🔁 Повторных: 1" in text
    assert "По городам" not in text


# ── Task 2: ADMIN_GUIDE section ──────────────────────────────────────────────────────────

_GUIDE_PATH = Path(__file__).resolve().parent.parent / "docs" / "ADMIN_GUIDE.md"


def _guide_text() -> str:
    return _GUIDE_PATH.read_text(encoding="utf-8")


def test_guide_documents_returning_delegate():
    text = _guide_text()
    assert "🔁 Повторный" in text
    assert "🚀 Обновить анкету" in text
    assert "🔄 Новый сезон" in text
    assert "ДВЕ строки" in text or "две строки" in text


def test_guide_has_no_code_identifiers():
    text = _guide_text()
    idx = text.index("## 24. Новый сезон и вернувшиеся делегаты")
    section = text[idx:]
    for identifier in ("prev_season", "event_season", "legacy", "season"):
        assert identifier not in section, f"код {identifier!r} не должен быть виден менеджеру"
