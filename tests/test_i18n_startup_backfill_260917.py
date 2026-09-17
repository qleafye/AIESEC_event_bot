"""Квик 260917-en (живая проверка 17.09, находка 1) — корпус анкеты (`services/i18n_sources.py
::corpus()`) расширяется кодом (новая группа `DELEGATE_GROUPS`, новый литерал), а `bulk_seed()`
исторически звался ТОЛЬКО в момент включения модуля (`delegate_lang_enabled` -> "on"). На
стенде, где модуль включён давно, расширение корпуса в очередь не попадало никогда.

Два фикса, две группы тестов:
1. `bulk_seed` теперь пропускает СТРОКУ, для которой в `translations` уже есть запись —
   ручная (`manual=1`) ИЛИ уже готовый машинный перевод (было: пропускал только `manual`) —
   идемпотентный вызов на каждом рестарте не гоняет argos по готовому корпусу заново.
2. `main.py` на старте, если модуль включён, зовёт `bulk_seed()` — регрессия проверяется
   структурно (сам `main.py` — не aiogram-free модуль, тянет весь бот, поэтому здесь только
   исходник сканируется на наличие врезки, тот же приём, что уже есть в проекте для
   аналогичных сторожей точек входа)."""
import asyncio
from pathlib import Path

from config import config
from database import db
from services import i18n_worker
from services.i18n import src_hash

ROOT = Path(__file__).resolve().parent.parent


def _db_ready(tmp_path, name="test_i18n_startup_backfill_260917.db"):
    config.DB_PATH = str(tmp_path / name)
    asyncio.run(db.init_db())


# ── bulk_seed: не переставляет уже переведённые строки ────────────────────────────────────

def test_bulk_seed_skips_already_machine_translated_row(tmp_path):
    """До фикса: bulk_seed пропускал только manual=1 — уже переведённая (машинно, не вручную)
    строка ставилась в очередь ЗАНОВО при повторном вызове (например, на каждом рестарте
    бота), хотя перевод для неё уже есть."""
    _db_ready(tmp_path)

    from services.i18n_sources import corpus

    items = asyncio.run(corpus())
    assert items
    _origin, sample_text = items[0]
    text_hash = src_hash(sample_text)

    # Строка уже переведена машиной (manual=0) — не результат ручной правки менеджера.
    asyncio.run(db.upsert_translation("en", text_hash, sample_text, "Machine translation", manual=0))

    queued = asyncio.run(i18n_worker.bulk_seed())

    pending = asyncio.run(db.list_pending_translations("en", limit=10_000))
    pending_hashes = {row["src_hash"] for row in pending}
    assert text_hash not in pending_hashes, "уже переведённая строка не должна попасть в очередь заново"
    assert queued == len(items) - 1


def test_bulk_seed_still_queues_genuinely_untranslated_rows(tmp_path):
    """Не регрессия в обратную сторону: строки без ЛЮБОГО перевода по-прежнему ставятся."""
    _db_ready(tmp_path)

    queued = asyncio.run(i18n_worker.bulk_seed())
    assert queued > 0

    from services.i18n_sources import corpus

    items = asyncio.run(corpus())
    assert queued == len(items)


def test_bulk_seed_idempotent_on_repeated_startup_after_drain(tmp_path, monkeypatch):
    """Симулирует реальный стендовый сценарий: bulk_seed на первом старте -> drain переводит
    всё -> бот перезапускается -> bulk_seed зовётся снова (main.py) -> НОЛЬ новых строк."""
    _db_ready(tmp_path)

    class _StubDriver:
        def translate_batch(self, texts):
            return [f"EN:{t}" for t in texts]

        def unload(self):
            pass

    async def _fake_get_driver():
        return _StubDriver()

    monkeypatch.setattr(i18n_worker, "get_driver", _fake_get_driver)

    first_queued = asyncio.run(i18n_worker.bulk_seed())
    assert first_queued > 0

    # Осушаем очередь полностью (несколько батчей — BATCH_SIZE=32, корпус обычно больше).
    total_drained = 0
    for _ in range(50):
        done = asyncio.run(i18n_worker.drain(limit_batches=1))
        total_drained += done
        if done == 0:
            break
    assert total_drained == first_queued

    still_pending = asyncio.run(db.list_pending_translations("en", limit=10_000))
    assert still_pending == []

    # «Рестарт бота» — bulk_seed зовётся снова (main.py, идемпотентная досылка).
    second_queued = asyncio.run(i18n_worker.bulk_seed())
    assert second_queued == 0

    still_pending_after = asyncio.run(db.list_pending_translations("en", limit=10_000))
    assert still_pending_after == []


# ── main.py: врезка вызывается только когда модуль включён ────────────────────────────────

def test_main_py_calls_bulk_seed_guarded_by_module_toggle():
    """Структурный сторож (не импортирует main.py целиком — тянет весь бот, ленивый импорт
    внутри main() и без event loop это не проверить дёшево): исходник обязан вызывать
    `bulk_seed()` только внутри ветки `delegate_lang_enabled` == "on", а не безусловно —
    иначе выключенный модуль на каждом старте зря бы читал БД по всему корпусу."""
    source = (ROOT / "main.py").read_text(encoding="utf-8")
    assert "from services.i18n_worker import bulk_seed" in source
    idx = source.index("from services.i18n_worker import bulk_seed")
    # Гейт `delegate_lang_enabled` должен стоять НЕПОСРЕДСТВЕННО перед вызовом (в этом же
    # try-блоке), не где-то ещё в файле — окно в 200 символов покрывает "if await
    # get_setting_typed(...) == "on":" сразу над импортом.
    window = source[max(0, idx - 200): idx]
    assert 'get_setting_typed("delegate_lang_enabled")' in window
    assert '== "on"' in window
