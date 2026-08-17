"""Phase 09.1 Plan 01 (GAME-05, A) — free-form submission: game_submission_parts table +
accessors, `SETTINGS_SCHEMA` "game" group, delegate multi-part accumulate FSM, manager
proof-type checkboxes, moderation card rendering all parts.

pytest-asyncio is unavailable in this env -- every async helper is driven via asyncio.run(),
config.DB_PATH points at a tmp_path file (same convention as every other phase-9 test file).
"""
import asyncio

from config import config
from database import db


def _db_ready(tmp_path):
    config.DB_PATH = str(tmp_path / "test_game_free_proof_091.db")
    asyncio.run(db.init_db())


# ── Task 1: game_submission_parts + accessors ────────────────────────────────────────────

def test_add_and_list_submission_parts_in_order(tmp_path):
    _db_ready(tmp_path)
    task_id = asyncio.run(db.create_task("t", "Light", 15, "photo,text", "2099-01-01 00:00:00", None))
    sub_id = asyncio.run(db.create_submission(task_id, 111, "photo", "file_abc", "2026-08-20 10:00:00"))
    asyncio.run(db.add_submission_part(sub_id, 0, "photo", "file_abc", None))
    asyncio.run(db.add_submission_part(sub_id, 1, "text", "готово", None))
    parts = asyncio.run(db.list_submission_parts(sub_id))
    assert [p["kind"] for p in parts] == ["photo", "text"]
    assert [p["ord"] for p in parts] == [0, 1]


def test_get_submission_parts_or_legacy_synthesizes_one_part_for_old_row(tmp_path):
    _db_ready(tmp_path)
    with_content = {"id": 1, "content_type": "photo", "content": "file_xyz"}
    parts = asyncio.run(db.get_submission_parts_or_legacy(with_content))
    assert len(parts) == 1
    assert parts[0] == {"ord": 0, "kind": "photo", "content": "file_xyz", "caption": None}


def test_get_submission_parts_or_legacy_maps_pdf_to_document(tmp_path):
    _db_ready(tmp_path)
    row = {"id": 2, "content_type": "pdf", "content": "file_pdf"}
    parts = asyncio.run(db.get_submission_parts_or_legacy(row))
    assert parts[0]["kind"] == "document"


def test_get_submission_parts_or_legacy_ignores_legacy_columns_when_parts_exist(tmp_path):
    _db_ready(tmp_path)
    task_id = asyncio.run(db.create_task("t", "Light", 15, "text", "2099-01-01 00:00:00", None))
    sub_id = asyncio.run(db.create_submission(task_id, 111, "text", "legacy content", "2026-08-20 10:00:00"))
    asyncio.run(db.add_submission_part(sub_id, 0, "link", "https://example.com", None))
    submission = asyncio.run(db.get_submission(sub_id))
    parts = asyncio.run(db.get_submission_parts_or_legacy(submission))
    assert len(parts) == 1
    assert parts[0]["kind"] == "link"
    assert parts[0]["content"] == "https://example.com"


def test_get_submission_parts_or_legacy_empty_content_returns_empty_list(tmp_path):
    _db_ready(tmp_path)
    row = {"id": 3, "content_type": "text", "content": ""}
    parts = asyncio.run(db.get_submission_parts_or_legacy(row))
    assert parts == []


def test_parse_proof_types_multi():
    assert db.parse_proof_types("photo,text") == ["photo", "text"]


def test_parse_proof_types_single():
    assert db.parse_proof_types("photo") == ["photo"]


def test_parse_proof_types_empty_string():
    assert db.parse_proof_types("") == []


def test_parse_proof_types_none():
    assert db.parse_proof_types(None) == []


def test_parse_proof_types_drops_unknown_codes():
    assert db.parse_proof_types("photo,bogus,text") == ["photo", "text"]


def test_parse_proof_types_preserves_canonical_order_not_input_order():
    assert db.parse_proof_types("text,photo") == ["photo", "text"]


def test_init_db_twice_is_idempotent(tmp_path):
    _db_ready(tmp_path)
    asyncio.run(db.init_db())  # second call on the same file must not raise


def test_game_settings_schema_has_nine_keys_in_game_group():
    import settings_schema as s
    keys = [k for k, v in s.SETTINGS_SCHEMA.items() if v["group"] == "game"]
    assert len(keys) == 9
    for k in keys:
        assert s.SETTINGS_SCHEMA[k]["default"] not in (None, "")


def test_game_settings_group_registered_in_admin():
    import handlers.admin as a
    assert ("🎮 Геймификация", "game", a._GAME_FIELD_ORDER) in a.SETTINGS_GROUPS
