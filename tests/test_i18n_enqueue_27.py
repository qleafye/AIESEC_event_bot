"""Phase 27 (27-03, LANG-04/LANG-08/LANG-09) — сторож врезки в `database.db.set_setting`:
делегатский текст сам ставится в очередь перевода, сбой очереди не мешает записи настройки,
модуль-выключено/админский-ключ/consent-ключ очередь не трогают, `list`-значения
разворачиваются построчно, `delegate_lang_enabled="on"` запускает `bulk_seed` фоном.

pytest-asyncio в этом окружении нет (см. tests/test_db_phase5.py) — каждый async-вызов через
asyncio.run(), config.DB_PATH смотрит в tmp_path.
"""
import asyncio

import pytest

from config import config
from database import db
from services.i18n import src_hash


def _db_ready(tmp_path, name="test_i18n_enqueue_27.db"):
    config.DB_PATH = str(tmp_path / name)
    asyncio.run(db.init_db())


async def _enable_module(monkeypatch=None, spawn_bulk_seed=False):
    """Включает модуль напрямую в bot_settings (не через set_setting — чтобы не запускать
    bulk_seed побочным эффектом в тестах, которые его не проверяют)."""
    async with db._connect() as conn:
        await conn.execute(
            "INSERT OR REPLACE INTO bot_settings (key, value) VALUES (?, ?)",
            ("delegate_lang_enabled", "on"),
        )
        await conn.commit()


# ── модуль выключен ──────────────────────────────────────────────────────────────────────

def test_disabled_module_does_not_enqueue(tmp_path):
    _db_ready(tmp_path)
    asyncio.run(db.set_setting("reg_prompt_city", "Из какого ты города?"))

    pending = asyncio.run(db.list_pending_translations("en", limit=100))
    assert pending == []
    # Настройка при этом записалась как обычно — модуль не влияет на запись.
    assert asyncio.run(db.get_setting("reg_prompt_city")) == "Из какого ты города?"


# ── модуль включён: делегатский ключ ────────────────────────────────────────────────────

def test_enabled_module_enqueues_delegate_dynamic_key(tmp_path):
    _db_ready(tmp_path)
    asyncio.run(_enable_module())

    asyncio.run(db.set_setting("reg_prompt_city", "Из какого ты города?"))

    pending = asyncio.run(db.list_pending_translations("en", limit=100))
    assert len(pending) == 1
    row = pending[0]
    assert row["src_hash"] == src_hash("Из какого ты города?")
    assert row["src_text"] == "Из какого ты города?"
    assert row["origin_key"] == "reg_prompt_city"


# ── админский ключ ───────────────────────────────────────────────────────────────────────

def test_admin_key_never_enqueued_even_when_module_enabled(tmp_path):
    _db_ready(tmp_path)
    asyncio.run(_enable_module())

    asyncio.run(db.set_setting("main_sheet_tab", "Основная"))
    asyncio.run(db.set_setting("dashboard_block_funnel", "on"))
    # Метка для менеджера в карточке заявки — физически в группе reg, но не для делегата.
    asyncio.run(db.set_setting("reg_edited_admin_label", "Изменена {date}"))

    pending = asyncio.run(db.list_pending_translations("en", limit=100))
    assert pending == []


# ── consent-ключ ─────────────────────────────────────────────────────────────────────────

def test_consent_key_never_enqueued(tmp_path):
    _db_ready(tmp_path)
    asyncio.run(_enable_module())

    asyncio.run(db.set_setting("consent_button_text", "Согласен(-на)"))

    pending = asyncio.run(db.list_pending_translations("en", limit=100))
    assert pending == []


# ── сбой очереди не мешает записи настройки ────────────────────────────────────────────────

def test_enqueue_failure_does_not_break_set_setting(tmp_path, monkeypatch):
    _db_ready(tmp_path)
    asyncio.run(_enable_module())

    async def _boom(*args, **kwargs):
        raise RuntimeError("очередь недоступна")

    monkeypatch.setattr(db, "enqueue_translation", _boom)

    asyncio.run(db.set_setting("reg_prompt_city", "Из какого ты города?"))

    # Настройка сохранена, несмотря на бросающий стаб очереди.
    assert asyncio.run(db.get_setting("reg_prompt_city")) == "Из какого ты города?"


# ── list-ключ разворачивается построчно ─────────────────────────────────────────────────

def test_list_key_enqueues_one_row_per_line(tmp_path):
    _db_ready(tmp_path)
    asyncio.run(_enable_module())

    asyncio.run(db.set_setting("source_options", "Соцсети АЙСЕК\nУниверситетские каналы\nДрузья"))

    pending = asyncio.run(db.list_pending_translations("en", limit=100))
    texts = sorted(row["src_text"] for row in pending)
    assert texts == ["Друзья", "Соцсети АЙСЕК", "Университетские каналы"]
    assert all(row["origin_key"] == "source_options" for row in pending)


# ── повторное сохранение того же значения не плодит строк ──────────────────────────────

def test_saving_same_value_twice_does_not_duplicate(tmp_path):
    _db_ready(tmp_path)
    asyncio.run(_enable_module())

    asyncio.run(db.set_setting("reg_prompt_city", "Из какого ты города?"))
    asyncio.run(db.set_setting("reg_prompt_city", "Из какого ты города?"))

    pending = asyncio.run(db.list_pending_translations("en", limit=100))
    assert len(pending) == 1


# ── delegate_lang_enabled=on запускает bulk_seed ────────────────────────────────────────

def test_enabling_module_spawns_bulk_seed(tmp_path, monkeypatch):
    _db_ready(tmp_path)

    calls = []

    async def _fake_bulk_seed(lang="en"):
        calls.append(lang)
        return 0

    from services import i18n_worker

    monkeypatch.setattr(i18n_worker, "bulk_seed", _fake_bulk_seed)

    async def go():
        await db.set_setting("delegate_lang_enabled", "on")
        await asyncio.sleep(0)  # дать фоновой задаче (services.background.spawn) отработать

    asyncio.run(go())
    assert calls == ["en"]


def test_disabling_module_does_not_spawn_bulk_seed(tmp_path, monkeypatch):
    _db_ready(tmp_path)

    calls = []

    async def _fake_bulk_seed(lang="en"):
        calls.append(lang)
        return 0

    from services import i18n_worker

    monkeypatch.setattr(i18n_worker, "bulk_seed", _fake_bulk_seed)

    async def go():
        await db.set_setting("delegate_lang_enabled", "off")
        await asyncio.sleep(0)

    asyncio.run(go())
    assert calls == []
