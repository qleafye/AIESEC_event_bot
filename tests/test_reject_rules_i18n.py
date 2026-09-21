"""Phase 31 Plan 04 (D-25): текст отказа правила автоотказа уходит в существующую очередь
машинного перевода при сохранении — та же дисциплина, что у делегатских текстов реестра
(Phase 27, `database.db._maybe_enqueue_city_label_translation`).

pytest-asyncio недоступен — async через asyncio.run(), config.DB_PATH -> tmp_path (см.
tests/test_i18n_store_27.py::_db_ready)."""
from __future__ import annotations

import asyncio

from config import config
from database import db
import services.reject_rules as rr
from services.i18n import src_hash

SUPERADMIN_ID = 900200001


def _ready(tmp_path, name="test_reject_rules_i18n.db"):
    config.DB_PATH = str(tmp_path / name)
    asyncio.run(db.init_db())
    config.ADMIN_IDS = [SUPERADMIN_ID]


def _run(coro):
    return asyncio.run(coro)


def _rule_fields(**overrides):
    fields = dict(
        city=None, tracks=["full"],
        conditions=[[{"step": "resume", "op": "no_file", "values": []}]],
        action="reject", reject_text="Без резюме заявку не рассмотрим.", enabled=1,
    )
    fields.update(overrides)
    return fields


def test_save_rule_enqueues_translation_when_delegate_lang_enabled(tmp_path):
    _ready(tmp_path)
    _run(db.set_setting("delegate_lang_enabled", "on"))

    text = "Без резюме заявку не рассмотрим."
    rule_id, error = _run(rr.save_rule(SUPERADMIN_ID, None, **_rule_fields(reject_text=text)))
    assert error is None

    queued = _run(db.list_pending_translations("en", limit=10))
    matching = [row for row in queued if row["origin_key"] == f"reject_rule__{rule_id}"]
    assert len(matching) == 1
    assert matching[0]["src_hash"] == src_hash(text)
    assert matching[0]["src_text"] == text


def test_save_rule_does_not_enqueue_translation_when_lang_disabled(tmp_path):
    _ready(tmp_path)
    # delegate_lang_enabled — дефолт off, ничего явно не включаем.
    rule_id, error = _run(rr.save_rule(SUPERADMIN_ID, None, **_rule_fields()))
    assert error is None
    queued = _run(db.list_pending_translations("en", limit=10))
    assert queued == []


def test_save_rule_survives_translation_queue_failure(tmp_path, monkeypatch):
    """T-31-04-05: сбой очереди перевода не должен потерять уже сохранённое правило."""
    _ready(tmp_path)
    _run(db.set_setting("delegate_lang_enabled", "on"))

    async def _boom(*args, **kwargs):
        raise RuntimeError("очередь недоступна")

    monkeypatch.setattr(rr, "enqueue_translation", _boom)

    rule_id, error = _run(rr.save_rule(SUPERADMIN_ID, None, **_rule_fields()))
    assert error is None
    assert rule_id is not None
    saved = _run(db.get_reject_rule(rule_id))
    assert saved is not None
    assert saved["reject_text"] == "Без резюме заявку не рассмотрим."


def test_reject_rule_translation_origin_key_is_scoped_per_rule(tmp_path):
    _ready(tmp_path)
    _run(db.set_setting("delegate_lang_enabled", "on"))

    id_one, _ = _run(rr.save_rule(SUPERADMIN_ID, None, **_rule_fields(reject_text="Текст А")))
    id_two, _ = _run(rr.save_rule(SUPERADMIN_ID, None, **_rule_fields(reject_text="Текст Б")))

    queued = _run(db.list_pending_translations("en", limit=10))
    origin_keys = {row["origin_key"] for row in queued}
    assert f"reject_rule__{id_one}" in origin_keys
    assert f"reject_rule__{id_two}" in origin_keys
