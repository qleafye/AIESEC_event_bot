"""Задача «Mini App на английском» — сторож `services/i18n_miniapp_manual.py`: сид пишет
`manual=1` переводы, идемпотентен, не перезаписывает НАСТОЯЩУЮ ручную правку менеджера (строку
с `manual=1` и origin_key, отличным от нашего маркера), и переведённые строки реально находятся
через `services.i18n.tr()` (та же карта `translations`, что читает `load_map`).

pytest-asyncio в проекте нет (см. соседние тесты Phase 27) — каждый async-вызов через
asyncio.run(), config.DB_PATH смотрит в tmp_path.
"""
import asyncio

from config import config
from database import db
from services import i18n
from services.i18n_miniapp_manual import MANUAL_EN, ORIGIN, seed


def _db_ready(tmp_path, name="test_i18n_miniapp_manual.db"):
    config.DB_PATH = str(tmp_path / name)
    asyncio.run(db.init_db())


def test_seed_applies_every_manual_entry(tmp_path):
    _db_ready(tmp_path)
    result = asyncio.run(seed())
    assert result["applied"] == len(MANUAL_EN)
    assert result["skipped_manager_edit"] == 0


def test_seed_is_idempotent(tmp_path):
    _db_ready(tmp_path)
    asyncio.run(seed())
    result = asyncio.run(seed())
    assert result["applied"] == len(MANUAL_EN)
    assert result["skipped_manager_edit"] == 0


def test_seed_does_not_overwrite_manager_manual_edit(tmp_path):
    _db_ready(tmp_path)
    ru_text = next(iter(MANUAL_EN))
    manager_text = "A manager's own hand-written translation"
    asyncio.run(db.upsert_translation(
        "en", i18n.src_hash(ru_text), ru_text, manager_text, manual=1, origin_key="admin_edit",
    ))
    result = asyncio.run(seed())
    assert result["skipped_manager_edit"] == 1
    assert result["applied"] == len(MANUAL_EN) - 1
    row = asyncio.run(db.get_translation("en", i18n.src_hash(ru_text)))
    assert row["text"] == manager_text
    assert row["origin_key"] == "admin_edit"


def test_seeded_translation_resolves_through_tr(tmp_path):
    _db_ready(tmp_path)
    asyncio.run(seed())
    tr_map = asyncio.run(i18n.load_map("en"))
    ru_text, en_text = next(iter(MANUAL_EN.items()))
    assert i18n.tr(ru_text, "en", tr_map) == en_text


def test_reseeding_after_upgrade_updates_our_own_previous_seed(tmp_path):
    """Наша же прошлая версия сида (тот же `ORIGIN`) — не «менеджер редактировал», её можно
    и нужно перезаписать новой редакцией строки без вмешательства человека."""
    _db_ready(tmp_path)
    ru_text = next(iter(MANUAL_EN))
    asyncio.run(db.upsert_translation(
        "en", i18n.src_hash(ru_text), ru_text, "stale old seed text", manual=1, origin_key=ORIGIN,
    ))
    result = asyncio.run(seed())
    assert result["skipped_manager_edit"] == 0
    row = asyncio.run(db.get_translation("en", i18n.src_hash(ru_text)))
    assert row["text"] == MANUAL_EN[ru_text]
