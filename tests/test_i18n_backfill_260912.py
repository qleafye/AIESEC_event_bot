"""Квик 260912 (W5, Задача 4) — «догонялка перевода»: экран «🌐 Английские тексты» показывает
менеджеру, сколько строк корпуса делегат ещё увидит по-русски, и одной кнопкой закрывает эту
дыру. Дыра существует потому, что `bulk_seed` при включении тумблера (`database.db::
_maybe_enqueue_translation`) — одноразовый фоновый спавн: не переживает рестарт бота посреди
посева, не повторяется после сбоя, а строки, исчерпавшие `MAX_ATTEMPTS`, остаются в очереди
навсегда невидимо для менеджера (`progress()['total']` считает только строки, УЖЕ лежащие в
`translations`, а не размер корпуса).

pytest-asyncio в этом окружении нет — `asyncio.run()`, config.DB_PATH на tmp_path (см.
tests/test_i18n_worker_27.py).
"""
import asyncio

from config import config
from database import db
from handlers import admin_i18n
from services import i18n_worker


def _db_ready(tmp_path, name="test_i18n_backfill_260912.db"):
    config.DB_PATH = str(tmp_path / name)
    asyncio.run(db.init_db())


class _FakeMessage:
    def __init__(self):
        self.edits = []

    async def edit_text(self, text, parse_mode=None, reply_markup=None):
        self.edits.append((text, reply_markup))


class _FakeCallback:
    def __init__(self):
        self.message = _FakeMessage()
        self.answers = []  # list[(text, show_alert)]

    async def answer(self, text=None, show_alert=False):
        self.answers.append((text, show_alert))


def _patch_driver_never_called(monkeypatch):
    """Задача 4: догонялка только НАПОЛНЯЕТ очередь -- ни `reset_translation_attempts`, ни
    `bulk_seed`, ни рендер экрана не имеют права тронуть драйвер перевода (тяжёлая CPU-bound
    модель) синхронно, тот же сторож, что tests/test_i18n_worker_27.py применяет к drain()."""
    async def _fail_get_driver():
        raise AssertionError("get_driver() не должен вызываться из догонялки/рендера экрана")

    monkeypatch.setattr(i18n_worker, "get_driver", _fail_get_driver)


def _patch_corpus_everywhere(monkeypatch, fake_corpus):
    """`admin_i18n._corpus_gap` и `services.i18n_worker.bulk_seed` каждый импортировали
    `corpus` В СВОЙ модуль (`from services.i18n_sources import corpus`) -- переменные разные
    объекты, монки нужно ставить в оба модуля, иначе `bulk_seed` посеет РЕАЛЬНЫЙ корпус анкеты
    (сотни строк) поверх фейковых ожиданий теста."""
    monkeypatch.setattr(admin_i18n, "corpus", fake_corpus)
    monkeypatch.setattr(i18n_worker, "corpus", fake_corpus)


# ── database.db.reset_translation_attempts ──────────────────────────────────────────────────

def test_reset_translation_attempts_revives_stuck_rows(tmp_path):
    _db_ready(tmp_path)

    async def go():
        await db.enqueue_translation("en", "hash1", "Текст 1")
        await db.enqueue_translation("en", "hash2", "Текст 2")
        for _ in range(5):
            rows = await db.list_pending_translations("en", limit=32, max_attempts=999)
            for row in rows:
                await db.bump_translation_attempt(row["id"], "движок недоступен")

        # Обе строки исчерпали MAX_ATTEMPTS=5 -- ни одна больше не отдаётся drain()'у.
        assert await db.list_pending_translations("en", limit=32, max_attempts=5) == []

        reset_count = await db.reset_translation_attempts("en")
        assert reset_count == 2

        revived = await db.list_pending_translations("en", limit=32, max_attempts=5)
        assert {r["src_hash"] for r in revived} == {"hash1", "hash2"}
        assert all(r["attempts"] == 0 and r["last_error"] is None for r in revived)

    asyncio.run(go())


def test_reset_translation_attempts_only_touches_its_own_lang(tmp_path):
    _db_ready(tmp_path)

    async def go():
        await db.enqueue_translation("en", "hash1", "Текст 1")
        await db.enqueue_translation("fr", "hash1", "Текст 1")
        reset_count = await db.reset_translation_attempts("en")
        assert reset_count == 1

    asyncio.run(go())


# ── handlers.admin_i18n._corpus_gap ──────────────────────────────────────────────────────────

def test_corpus_gap_counts_untranslated_corpus_lines(tmp_path, monkeypatch):
    _db_ready(tmp_path)

    async def _fake_corpus():
        return [
            ("lit:a", "Строка A"),
            ("lit:b", "Строка B"),
            ("lit:c", "Строка C"),
        ]

    monkeypatch.setattr(admin_i18n, "corpus", _fake_corpus)

    async def go():
        # "Строка A" переведена (упала в translations), B и C -- нет ни в очереди, ни в карте.
        await db.upsert_translation("en", admin_i18n.compute_src_hash("Строка A"), "Строка A", "A")
        total, without = await admin_i18n._corpus_gap()
        assert total == 3
        assert without == 2

    asyncio.run(go())


def test_corpus_gap_zero_when_everything_translated(tmp_path, monkeypatch):
    _db_ready(tmp_path)

    async def _fake_corpus():
        return [("lit:a", "Строка A")]

    monkeypatch.setattr(admin_i18n, "corpus", _fake_corpus)

    async def go():
        await db.upsert_translation("en", admin_i18n.compute_src_hash("Строка A"), "Строка A", "A")
        total, without = await admin_i18n._corpus_gap()
        assert (total, without) == (1, 0)

    asyncio.run(go())


# ── Экран списка: строка про дыру корпуса + кнопка всегда видна ─────────────────────────────

def test_render_list_shows_corpus_gap_line_and_button_with_count(tmp_path, monkeypatch):
    _db_ready(tmp_path)

    async def _fake_corpus():
        return [("lit:a", "Строка A"), ("lit:b", "Строка B")]

    monkeypatch.setattr(admin_i18n, "corpus", _fake_corpus)

    async def go():
        text, kb = await admin_i18n.render_i18n_list("all", 0)
        assert "В корпусе анкеты 2 строк" in text
        assert "по-русски 2" in text
        seed_buttons = [
            btn for row in kb.inline_keyboard for btn in row
            if btn.callback_data == "admin_i18n_seed"
        ]
        assert len(seed_buttons) == 1
        assert "2" in seed_buttons[0].text
        assert seed_buttons[0].text.startswith("⟳")

    asyncio.run(go())


def test_render_list_button_visible_with_no_count_when_gap_is_zero(tmp_path, monkeypatch):
    _db_ready(tmp_path)

    async def _fake_corpus():
        return [("lit:a", "Строка A")]

    monkeypatch.setattr(admin_i18n, "corpus", _fake_corpus)

    async def go():
        await db.upsert_translation("en", admin_i18n.compute_src_hash("Строка A"), "Строка A", "A")
        text, kb = await admin_i18n.render_i18n_list("all", 0)
        seed_buttons = [
            btn for row in kb.inline_keyboard for btn in row
            if btn.callback_data == "admin_i18n_seed"
        ]
        assert len(seed_buttons) == 1  # кнопка ВСЕГДА видна, даже когда дыры нет
        assert seed_buttons[0].text == "⟳ Догнать перевод"

    asyncio.run(go())


# ── admin_i18n_seed: наполняет очередь, оживляет застрявшие, не трогает ручные правки ────────

def test_admin_i18n_seed_queues_new_and_revives_stuck(tmp_path, monkeypatch):
    _db_ready(tmp_path)
    _patch_driver_never_called(monkeypatch)

    async def _fake_corpus():
        return [
            ("lit:new", "Совсем новая строка"),      # никогда не ставилась в очередь
            ("lit:stuck", "Застрявшая строка"),        # в очереди, но исчерпала попытки
            ("lit:manual", "Ручной перевод менеджера"),  # уже переведена вручную
        ]

    _patch_corpus_everywhere(monkeypatch, _fake_corpus)

    async def go():
        stuck_hash = admin_i18n.compute_src_hash("Застрявшая строка")
        await db.enqueue_translation("en", stuck_hash, "Застрявшая строка", origin_key="lit:stuck")
        for _ in range(5):
            await db.bump_translation_attempt(
                (await db.list_pending_translations("en", limit=32, max_attempts=999))[0]["id"],
                "движок недоступен",
            )
        manual_hash = admin_i18n.compute_src_hash("Ручной перевод менеджера")
        await db.upsert_translation(
            "en", manual_hash, "Ручной перевод менеджера", "Manager's own translation", manual=1,
        )

        cb = _FakeCallback()
        await admin_i18n.admin_i18n_seed(cb)

        # Новая строка встала в очередь.
        new_hash = admin_i18n.compute_src_hash("Совсем новая строка")
        pending = await db.list_pending_translations("en", limit=32, max_attempts=5)
        pending_hashes = {r["src_hash"] for r in pending}
        assert new_hash in pending_hashes
        # Застрявшая строка ожила (attempts сброшены -- снова отдаётся drain()'у).
        assert stuck_hash in pending_hashes
        # Ручная правка НЕ попала в очередь заново.
        assert manual_hash not in pending_hashes

        # Алерт называет обе метрики и предупреждает про фон.
        assert cb.answers, "не было ни одного callback.answer()"
        alert_text, show_alert = cb.answers[-1]
        assert show_alert is True
        assert "новых строк в очереди: 1" in alert_text
        assert "застрявших строк оживлено: 1" in alert_text
        assert "фон" in alert_text

        # Экран перерисован (не молчание).
        assert cb.message.edits

    asyncio.run(go())


def test_admin_i18n_seed_reports_nothing_to_catch_up(tmp_path, monkeypatch):
    """«Нечего догонять» — весь корпус либо под ручной правкой (bulk_seed её не трогает по
    построению), либо уже в очереди со свежими попытками (reset_translation_attempts не
    находит, что оживлять). Просто «уже есть непустой перевод» -- НЕ этот случай: bulk_seed
    переставляет в очередь и уже переведённые машиной строки (LANG-05 запрещает трогать
    только `manual=1`), так и остаётся возможность форсировать полный повторный перевод той
    же кнопкой."""
    _db_ready(tmp_path)
    _patch_driver_never_called(monkeypatch)

    async def _fake_corpus():
        return [("lit:a", "Строка с ручным переводом")]

    _patch_corpus_everywhere(monkeypatch, _fake_corpus)

    async def go():
        await db.upsert_translation(
            "en", admin_i18n.compute_src_hash("Строка с ручным переводом"),
            "Строка с ручным переводом", "Manager's manual translation", manual=1,
        )
        cb = _FakeCallback()
        await admin_i18n.admin_i18n_seed(cb)

        alert_text, show_alert = cb.answers[-1]
        assert show_alert is True
        assert "нечего" in alert_text.lower()
        assert cb.message.edits  # экран всё равно перерисован, не тишина

    asyncio.run(go())


def test_admin_i18n_seed_does_not_touch_synchronous_driver(tmp_path, monkeypatch):
    """Сторож T-W5-03: наполнение очереди не грузит модель перевода синхронно -- патч
    get_driver на исключение доказывает, что он не вызывается ни из reset, ни из bulk_seed,
    ни из перерисовки экрана."""
    _db_ready(tmp_path)
    _patch_driver_never_called(monkeypatch)

    async def _fake_corpus():
        return [("lit:a", "Строка")]

    _patch_corpus_everywhere(monkeypatch, _fake_corpus)

    async def go():
        cb = _FakeCallback()
        await admin_i18n.admin_i18n_seed(cb)  # не должно поднять AssertionError из get_driver

    asyncio.run(go())


# ── ADMIN_CAPS: новый callback размечен, вне карты прав его бы не существовало ──────────────

def test_admin_i18n_seed_is_capability_mapped():
    from handlers.admin_caps import ADMIN_CAPS
    assert ADMIN_CAPS.get("admin_i18n_seed") == "settings"
