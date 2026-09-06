"""Phase 27 (27-02, LANG-01/02/03/05) — сторож хранилища переводов: миграции не ломают
дофазовую базу, машинный перевод не может затереть ручную правку менеджера (LANG-05),
дедупликация очереди в схеме (не в коде воркера), пагинация экрана правки (план 27-06).

pytest-asyncio в этом окружении нет (см. tests/test_db_phase5.py) — каждый async-вызов через
asyncio.run(), config.DB_PATH смотрит в tmp_path (конвенция проекта, conftest.py нет).
"""
import asyncio
import hashlib
import sqlite3

import pytest

from config import config
from database import db


def _db_ready(tmp_path, name="test_i18n_store_27.db"):
    config.DB_PATH = str(tmp_path / name)
    asyncio.run(db.init_db())


def test_init_db_is_idempotent(tmp_path):
    _db_ready(tmp_path)
    # Второй прогон на той же базе не падает (CREATE TABLE IF NOT EXISTS / _ensure_column).
    asyncio.run(db.init_db())


def test_pre_phase_database_opens_and_old_row_keeps_null_lang(tmp_path):
    """База «до фазы»: таблица `users` уже существует, но БЕЗ колонки `lang` (как на
    проде у 1000+ живых делегатов). init_db() обязан открыть её, добавить колонку через
    _ensure_column и не тронуть старую строку — новый делегатский язык у неё NULL."""
    config.DB_PATH = str(tmp_path / "pre_phase.db")
    conn = sqlite3.connect(config.DB_PATH)
    conn.execute('''
        CREATE TABLE users (
            telegram_id INTEGER PRIMARY KEY,
            username TEXT,
            full_name TEXT,
            registration_date TEXT
        )
    ''')
    conn.execute(
        "INSERT INTO users (telegram_id, username, full_name, registration_date) "
        "VALUES (?, ?, ?, ?)",
        (12345, "old_delegate", "Старый Делегат", "2026-01-01"),
    )
    conn.commit()
    conn.close()

    asyncio.run(db.init_db())

    user = asyncio.run(db.get_user(12345))
    assert user is not None, "старая запись должна открыться"
    assert user["lang"] is None, "у существующего делегата язык не выбран -> NULL"
    assert user["full_name"] == "Старый Делегат"


def test_upsert_translation_manual_wins_over_machine(tmp_path):
    _db_ready(tmp_path)

    async def go():
        src_hash = "abc123"
        # Ручная правка менеджера.
        await db.upsert_translation("en", src_hash, "Привет", "Manual hello", manual=1)
        # Машинный перевод пытается затереть её — LANG-05: не должен.
        await db.upsert_translation("en", src_hash, "Привет", "Machine hello", manual=0)
        row = await db.get_translation("en", src_hash)
        assert row["text"] == "Manual hello"
        assert row["manual"] == 1

        # А другая ручная правка — перезаписывает.
        await db.upsert_translation("en", src_hash, "Привет", "Manual hello v2", manual=1)
        row = await db.get_translation("en", src_hash)
        assert row["text"] == "Manual hello v2"

    asyncio.run(go())


def test_upsert_translation_machine_updates_machine(tmp_path):
    _db_ready(tmp_path)

    async def go():
        src_hash = "def456"
        await db.upsert_translation("en", src_hash, "Пока", "Bye (v1)", manual=0)
        await db.upsert_translation("en", src_hash, "Пока", "Bye (v2)", manual=0)
        row = await db.get_translation("en", src_hash)
        assert row["text"] == "Bye (v2)"
        assert row["manual"] == 0

    asyncio.run(go())


def _hash(text: str) -> str:
    """Дублирует src_hash из services/i18n.py (задача 3) намеренно — тест хранилища
    (задача 1) не должен зависеть от ядра перевода, которое появится позже в этом же плане;
    контракт хеша (`sha256(strip(text))[:32]`) фиксирован тестами задачи 3."""
    return hashlib.sha256(text.strip().encode("utf-8")).hexdigest()[:32]


def test_editing_russian_source_changes_hash_old_translation_unreachable(tmp_path):
    """Правка русского исходника (сама строка меняется) даёт другой src_hash. Старая
    строка перевода остаётся в таблице (аудит), но fetch_translations по НОВОМУ хешу её
    не находит — устаревший перевод физически недостижим."""
    _db_ready(tmp_path)

    async def go():
        old_text = "Привет, делегат!"
        new_text = "Привет, участник!"
        old_hash, new_hash = _hash(old_text), _hash(new_text)
        assert old_hash != new_hash

        await db.upsert_translation("en", old_hash, old_text, "Hello, delegate!", manual=0)
        tr_map = await db.fetch_translations("en")
        assert tr_map.get(old_hash) == "Hello, delegate!"
        assert new_hash not in tr_map  # новый хеш ещё не переведён

    asyncio.run(go())


def test_enqueue_translation_dedup(tmp_path):
    _db_ready(tmp_path)

    async def go():
        first_id = await db.enqueue_translation("en", "hash1", "Текст")
        second_id = await db.enqueue_translation("en", "hash1", "Текст")
        assert first_id is not None
        assert second_id is None  # UNIQUE(lang, src_hash) + INSERT OR IGNORE
        pending = await db.list_pending_translations("en")
        assert len(pending) == 1

    asyncio.run(go())


def test_list_pending_translations_excludes_exhausted(tmp_path):
    _db_ready(tmp_path)

    async def go():
        await db.enqueue_translation("en", "ok_hash", "Раз")
        await db.enqueue_translation("en", "dead_hash", "Два")
        pending = await db.list_pending_translations("en")
        dead_row = next(r for r in pending if r["src_hash"] == "dead_hash")
        for _ in range(5):
            await db.bump_translation_attempt(dead_row["id"], "boom")

        pending_after = await db.list_pending_translations("en", max_attempts=5)
        hashes = {r["src_hash"] for r in pending_after}
        assert "dead_hash" not in hashes
        assert "ok_hash" in hashes

    asyncio.run(go())


def test_drop_translation_queue(tmp_path):
    _db_ready(tmp_path)

    async def go():
        row_id = await db.enqueue_translation("en", "h1", "Текст")
        dropped = await db.drop_translation_queue([row_id])
        assert dropped == 1
        assert await db.list_pending_translations("en") == []
        # Повторное удаление того же id — no-op, не падает.
        assert await db.drop_translation_queue([row_id]) == 0
        assert await db.drop_translation_queue([]) == 0

    asyncio.run(go())


def test_list_translations_pagination(tmp_path):
    _db_ready(tmp_path)

    async def go():
        for i in range(25):
            await db.upsert_translation("en", f"hash{i:02d}", f"Текст {i}", f"Text {i}")
        rows, total = await db.list_translations("en", offset=0, limit=10)
        assert total == 25
        assert len(rows) == 10
        rows2, total2 = await db.list_translations("en", offset=20, limit=10)
        assert total2 == 25
        assert len(rows2) == 5

    asyncio.run(go())


def test_list_translations_state_filter_manual(tmp_path):
    _db_ready(tmp_path)

    async def go():
        await db.upsert_translation("en", "manual_hash", "Раз", "One", manual=1)
        await db.upsert_translation("en", "machine_hash", "Два", "Two", manual=0)
        manual_rows, manual_total = await db.list_translations("en", state="manual")
        assert manual_total == 1
        assert manual_rows[0]["src_hash"] == "manual_hash"

    asyncio.run(go())


def test_list_translations_rejects_unknown_state(tmp_path):
    _db_ready(tmp_path)
    with pytest.raises(ValueError):
        asyncio.run(db.list_translations("en", state="bogus"))


def test_set_user_lang_writes_and_rejects_garbage(tmp_path):
    _db_ready(tmp_path)

    async def go():
        await db.add_user({
            "telegram_id": 777, "full_name": "Делегат", "registration_date": "2026-01-01",
        })
        await db.set_user_lang(777, "en")
        user = await db.get_user(777)
        assert user["lang"] == "en"

        await db.set_user_lang(777, "garbage")
        user = await db.get_user(777)
        assert user["lang"] == "en"  # мусор не записан, старое значение осталось

        await db.set_user_lang(777, None)
        user = await db.get_user(777)
        assert user["lang"] is None

    asyncio.run(go())
