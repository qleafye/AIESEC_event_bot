"""Phase 27 (27-03, LANG-04/LANG-05) — сторож воркера очереди перевода: пустая очередь не
трогает драйвер вовсе, батч режется на BATCH_SIZE-кратные куски, сбой драйвера оставляет
строки в очереди с бампом `attempts`, ручная правка менеджера не подвергается машинному
переводу, пустой/отброшенный перевод — не ошибка, `attempts >= 5` не попадает в выборку,
`bulk_seed` не плодит дублей, интервал-джоба выключенного модуля не читает очередь ни разу.

pytest-asyncio в этом окружении нет (см. tests/test_db_phase5.py) — каждый async-вызов через
asyncio.run(), config.DB_PATH смотрит в tmp_path.

Стаб-драйвер: модель argos-translate-lt в тестах НЕ участвует (Pitfall 9 замера 27-01) —
`services.i18n_worker.get_driver` подменяется на фейковую async-фабрику, возвращающую
`_StubDriver` (реализует ровно контракт `TranslationDriver.translate_batch`/`unload`).
"""
import asyncio

import pytest

from config import config
from database import db
from services import i18n_worker


def _db_ready(tmp_path, name="test_i18n_worker_27.db"):
    config.DB_PATH = str(tmp_path / name)
    asyncio.run(db.init_db())


class _StubDriver:
    """Реализация `TranslationDriver` для тестов — считает вызовы/размеры чанков, позволяет
    подменить логику перевода конкретного теста через `translate_fn`."""

    def __init__(self, translate_fn=None):
        self.chunk_sizes: list[int] = []
        self.batches: list[list[str]] = []
        self.unloaded = False
        self._translate_fn = translate_fn or (lambda texts: [f"EN:{t}" for t in texts])

    def translate_batch(self, texts):
        self.chunk_sizes.append(len(texts))
        self.batches.append(list(texts))
        return self._translate_fn(texts)

    def unload(self):
        self.unloaded = True


def _patch_driver(monkeypatch, stub):
    async def _fake_get_driver():
        return stub

    monkeypatch.setattr(i18n_worker, "get_driver", _fake_get_driver)


def _patch_driver_never_called(monkeypatch):
    async def _fail_get_driver():
        raise AssertionError("get_driver() не должен вызываться на пустой/manual-only очереди")

    monkeypatch.setattr(i18n_worker, "get_driver", _fail_get_driver)


# ── пустая очередь ───────────────────────────────────────────────────────────────────────

def test_drain_empty_queue_never_calls_driver(tmp_path, monkeypatch):
    _db_ready(tmp_path)
    _patch_driver_never_called(monkeypatch)

    done = asyncio.run(i18n_worker.drain())
    assert done == 0


# ── батчи ────────────────────────────────────────────────────────────────────────────────

def test_drain_batches_40_rows_into_32_plus_8(tmp_path, monkeypatch):
    _db_ready(tmp_path)
    stub = _StubDriver()
    _patch_driver(monkeypatch, stub)

    async def seed():
        for i in range(40):
            await db.enqueue_translation("en", f"hash{i:03d}", f"Текст {i}")

    asyncio.run(seed())

    done = asyncio.run(i18n_worker.drain(limit_batches=2))
    assert done == 40
    assert stub.chunk_sizes == [32, 8]

    remaining = asyncio.run(db.list_pending_translations("en", limit=100))
    assert remaining == []


# ── сбой драйвера ────────────────────────────────────────────────────────────────────────

def test_drain_driver_failure_keeps_rows_and_bumps_attempts(tmp_path, monkeypatch):
    _db_ready(tmp_path)

    def _boom(texts):
        raise RuntimeError("движок недоступен")

    stub = _StubDriver(translate_fn=_boom)
    _patch_driver(monkeypatch, stub)

    asyncio.run(db.enqueue_translation("en", "hashA", "Привет"))

    done = asyncio.run(i18n_worker.drain())
    assert done == 0

    remaining = asyncio.run(db.list_pending_translations("en", limit=100))
    assert len(remaining) == 1
    assert remaining[0]["attempts"] == 1
    assert remaining[0]["last_error"] is not None

    # Переведённой строки при сбое не появляется.
    row = asyncio.run(db.get_translation("en", "hashA"))
    assert row is None


# ── ручная правка ────────────────────────────────────────────────────────────────────────

def test_drain_removes_manual_row_without_translating(tmp_path, monkeypatch):
    _db_ready(tmp_path)
    stub = _StubDriver()
    _patch_driver(monkeypatch, stub)

    async def seed():
        # Ручная правка менеджера УЖЕ есть для этого src_hash.
        await db.upsert_translation("en", "hashM", "Привет", "Manual hello", manual=1)
        await db.enqueue_translation("en", "hashM", "Привет")
        # Обычная строка рядом — чтобы убедиться, что она таки переводится.
        await db.enqueue_translation("en", "hashN", "Текст без правки")

    asyncio.run(seed())

    done = asyncio.run(i18n_worker.drain())
    assert done == 2

    # Драйвер получил ТОЛЬКО обычную строку — manual-строка не попала ни в один чанк.
    assert stub.batches == [["Текст без правки"]]

    manual_row = asyncio.run(db.get_translation("en", "hashM"))
    assert manual_row["text"] == "Manual hello"  # не тронуто машиной

    other_row = asyncio.run(db.get_translation("en", "hashN"))
    assert other_row["text"] == "EN:Текст без правки"
    assert other_row["manual"] == 0

    remaining = asyncio.run(db.list_pending_translations("en", limit=100))
    assert remaining == []


def test_drain_calls_get_driver_only_when_translatable_rows_remain(tmp_path, monkeypatch):
    """Очередь непуста, но состоит ЦЕЛИКОМ из manual-строк — драйвер всё равно не должен
    грузиться (owner-decision 27-01: модель только пока есть что реально переводить)."""
    _db_ready(tmp_path)
    _patch_driver_never_called(monkeypatch)

    async def seed():
        await db.upsert_translation("en", "hashM", "Привет", "Manual hello", manual=1)
        await db.enqueue_translation("en", "hashM", "Привет")

    asyncio.run(seed())

    done = asyncio.run(i18n_worker.drain())
    assert done == 1
    remaining = asyncio.run(db.list_pending_translations("en", limit=100))
    assert remaining == []


# ── пустой/отброшенный перевод — не ошибка ─────────────────────────────────────────────────

def test_drain_empty_translation_is_not_an_error(tmp_path, monkeypatch):
    _db_ready(tmp_path)
    stub = _StubDriver(translate_fn=lambda texts: ["" for _ in texts])
    _patch_driver(monkeypatch, stub)

    asyncio.run(db.enqueue_translation("en", "hashE", "Текст без перевода"))

    done = asyncio.run(i18n_worker.drain())
    assert done == 1

    row = asyncio.run(db.get_translation("en", "hashE"))
    assert row is not None
    assert row["text"] == ""
    assert row["manual"] == 0

    remaining = asyncio.run(db.list_pending_translations("en", limit=100))
    assert remaining == []


def test_drain_discards_translation_with_broken_markup(tmp_path, monkeypatch):
    """HTML-тэг ломается движком (Pitfall замера 27-01) — apply() в i18n_glossary отбрасывает
    перевод, drain() пишет '' и убирает строку из очереди без ошибки/ретрая."""
    _db_ready(tmp_path)

    def _mangle_tags(texts):
        # Съедает всё после первого сентинела — имитирует битую разметку движка.
        return [t.split("ZQ")[0] if "ZQ" in t else t for t in texts]

    stub = _StubDriver(translate_fn=_mangle_tags)
    _patch_driver(monkeypatch, stub)

    asyncio.run(db.enqueue_translation("en", "hashT", "Текст с <b>тегом</b>"))

    done = asyncio.run(i18n_worker.drain())
    assert done == 1

    row = asyncio.run(db.get_translation("en", "hashT"))
    assert row["text"] == ""


# ── attempts >= 5 не попадает в выборку ────────────────────────────────────────────────────

def test_drain_excludes_rows_at_max_attempts(tmp_path, monkeypatch):
    _db_ready(tmp_path)

    async def seed_and_exhaust():
        row_id = await db.enqueue_translation("en", "hashX", "Безнадёжная строка")
        for _ in range(5):
            await db.bump_translation_attempt(row_id, "снова сбой")

    asyncio.run(seed_and_exhaust())

    _patch_driver_never_called(monkeypatch)
    done = asyncio.run(i18n_worker.drain())
    assert done == 0  # строка сдалась и не попадает в выборку list_pending_translations


# ── выгрузка модели ──────────────────────────────────────────────────────────────────────

def test_drain_unloads_driver_after_queue_empties(tmp_path, monkeypatch):
    _db_ready(tmp_path)
    stub = _StubDriver()
    _patch_driver(monkeypatch, stub)

    asyncio.run(db.enqueue_translation("en", "hashU", "Последняя строка"))
    asyncio.run(i18n_worker.drain())

    assert stub.unloaded is True


# ── bulk_seed ────────────────────────────────────────────────────────────────────────────

def test_bulk_seed_does_not_duplicate_on_second_call(tmp_path):
    _db_ready(tmp_path)

    first = asyncio.run(i18n_worker.bulk_seed())
    assert first > 0

    after_first = asyncio.run(db.list_pending_translations("en", limit=10_000))
    assert len(after_first) == first

    second = asyncio.run(i18n_worker.bulk_seed())
    assert second == 0  # UNIQUE(lang, src_hash) + INSERT OR IGNORE — второй вызов не плодит строк

    after_second = asyncio.run(db.list_pending_translations("en", limit=10_000))
    assert len(after_second) == first


def test_bulk_seed_skips_rows_with_manual_translation(tmp_path):
    _db_ready(tmp_path)

    from services.i18n import src_hash
    from services.i18n_sources import corpus

    items = asyncio.run(corpus())
    assert items, "корпус не должен быть пустым на дефолтном реестре"
    sample_key, sample_text = items[0]

    asyncio.run(db.upsert_translation("en", src_hash(sample_text), sample_text, "Manual override", manual=1))

    queued = asyncio.run(i18n_worker.bulk_seed())
    pending = asyncio.run(db.list_pending_translations("en", limit=10_000))
    pending_hashes = {row["src_hash"] for row in pending}
    assert src_hash(sample_text) not in pending_hashes
    assert queued == len(items) - 1


# ── progress() ───────────────────────────────────────────────────────────────────────────

def test_progress_counts_are_consistent(tmp_path):
    _db_ready(tmp_path)

    async def seed():
        await db.upsert_translation("en", "h1", "Раз", "One", manual=0)
        await db.upsert_translation("en", "h2", "Два", "Two", manual=1)
        await db.upsert_translation("en", "h3", "Три", None, manual=0)
        await db.upsert_translation("en", "h4", "Четыре", "", manual=0)

    asyncio.run(seed())

    summary = asyncio.run(i18n_worker.progress())
    assert summary == {"total": 4, "done": 1, "manual": 1, "failed": 1, "pending": 1}


# ── интервал-джоба (services/scheduler.py) ─────────────────────────────────────────────────

def test_translation_drain_job_skips_when_module_disabled(tmp_path, monkeypatch):
    _db_ready(tmp_path)
    from services import scheduler

    called = []

    async def _fake_drain(*args, **kwargs):
        called.append(True)
        return 0

    monkeypatch.setattr(i18n_worker, "drain", _fake_drain)
    # delegate_lang_enabled по умолчанию "off" — ключ вообще не установлен в bot_settings.
    asyncio.run(scheduler.translation_drain_job())
    assert called == []


def test_translation_drain_job_calls_drain_when_module_enabled(tmp_path, monkeypatch):
    _db_ready(tmp_path)
    from services import scheduler

    async def enable():
        # Пишем настройку напрямую в bot_settings (не через set_setting — врезка задачи 3
        # тестируется отдельно в test_i18n_enqueue_27.py, здесь важен только сам факт "on").
        import aiosqlite
        async with aiosqlite.connect(config.DB_PATH) as conn:
            await conn.execute(
                "INSERT OR REPLACE INTO bot_settings (key, value) VALUES (?, ?)",
                ("delegate_lang_enabled", "on"),
            )
            await conn.commit()

    asyncio.run(enable())

    called = []

    async def _fake_drain(*args, **kwargs):
        called.append(True)
        return 0

    monkeypatch.setattr(i18n_worker, "drain", _fake_drain)
    asyncio.run(scheduler.translation_drain_job())
    assert called == [True]
