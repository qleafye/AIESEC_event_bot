"""Quick 260904-2cj (QJRN-01/02/03/04): раздел «❓ Вопросы делегатов».

Задача 1 — общий слой статуса (`services/questions.py`, чистый модуль) + постраничные
аксессоры БД (`database.db.list_questions_page`/`count_questions_by_status`). Задача 2
дописывает сюда тесты экрана бота (тем же файлом, ниже отдельным блоком).

pytest-asyncio в проекте нет — каждый async-вызов через asyncio.run(); БД — tmp_path.
`_ready`/`_add_user` — те же хелперы, что у `tests/test_sheet_logs_260902.py`.
"""
import asyncio
from datetime import datetime, timedelta

import cities
from config import config
from database import db
from services import questions as q

from tests.test_sheet_logs_260902 import _ready, _add_user


def _run(coro):
    return asyncio.run(coro)


# ══════════════════════════════════════════════════════════════════════════════════════════
# services/questions.py — чистые функции
# ══════════════════════════════════════════════════════════════════════════════════════════

def test_question_status_new_without_answered_by():
    row = {"answered_by": None, "delivered_at": None}
    assert q.question_status(row) == q.STATUS_NEW


def test_question_status_in_work_claimed_not_delivered():
    row = {"answered_by": 1, "answered_by_name": "Админ", "delivered_at": None}
    assert q.question_status(row) == q.STATUS_IN_WORK


def test_question_status_answered_when_delivered():
    row = {"answered_by": 1, "delivered_at": "2026-09-04T10:00:00"}
    assert q.question_status(row) == q.STATUS_ANSWERED


def test_question_status_answered_even_if_answered_by_empty_legacy_row():
    # Легаси-строка: доставлено, но захват (answered_by) почему-то пуст — всё равно «отвечен».
    row = {"answered_by": None, "delivered_at": "2026-09-04T10:00:00"}
    assert q.question_status(row) == q.STATUS_ANSWERED


def test_status_label_human_for_every_status():
    assert q.status_label({"answered_by": None, "delivered_at": None}) == "🆕 без ответа"
    assert q.status_label({"answered_by": 1, "delivered_at": None}) == "✍️ в работе"
    assert q.status_label({"answered_by": 1, "delivered_at": "x"}) == "✅ отвечен"


def test_is_stuck_true_only_for_in_work_past_threshold():
    old_stamp = (datetime.utcnow() - timedelta(minutes=q.STUCK_AFTER_MINUTES + 1)).isoformat()
    row = {"answered_by": 1, "delivered_at": None, "answered_at": old_stamp}
    assert q.is_stuck(row) is True


def test_is_stuck_false_when_claimed_a_minute_ago():
    fresh_stamp = (datetime.utcnow() - timedelta(minutes=1)).isoformat()
    row = {"answered_by": 1, "delivered_at": None, "answered_at": fresh_stamp}
    assert q.is_stuck(row) is False


def test_is_stuck_false_for_answered_status_even_if_old():
    old_stamp = (datetime.utcnow() - timedelta(minutes=q.STUCK_AFTER_MINUTES + 1)).isoformat()
    row = {"answered_by": 1, "delivered_at": "x", "answered_at": old_stamp}
    assert q.is_stuck(row) is False


def test_is_stuck_false_for_new_status():
    row = {"answered_by": None, "delivered_at": None, "answered_at": None}
    assert q.is_stuck(row) is False


def test_is_stuck_fail_soft_on_empty_or_broken_answered_at():
    assert q.is_stuck({"answered_by": 1, "delivered_at": None, "answered_at": None}) is False
    assert q.is_stuck({"answered_by": 1, "delivered_at": None, "answered_at": "мусор"}) is False


def test_format_stamp_parses_both_formats():
    assert q.format_stamp("2026-09-04 10:00:00") == "04.09.2026 10:00"
    assert q.format_stamp("2026-09-04T10:00:00") == "04.09.2026 10:00"


def test_format_stamp_unparsed_as_is_and_empty_string():
    assert q.format_stamp("не дата") == "не дата"
    assert q.format_stamp(None) == ""
    assert q.format_stamp("") == ""


# ══════════════════════════════════════════════════════════════════════════════════════════
# database/db.py — постраничные аксессоры + паритет с чистой функцией
# ══════════════════════════════════════════════════════════════════════════════════════════

def test_count_questions_by_status_all_equals_sum_of_three(tmp_path):
    _ready(tmp_path)

    async def go():
        await _add_user(1)
        new_qid = await db.create_question(1, "Вопрос 1")
        in_work_qid = await db.create_question(1, "Вопрос 2")
        await db.claim_question(in_work_qid, 999, "Менеджер")
        answered_qid = await db.create_question(1, "Вопрос 3")
        await db.claim_question(answered_qid, 999, "Менеджер")
        await db.set_question_answer(answered_qid, "Ответ")

        counts = await db.count_questions_by_status()
        assert counts == {"all": 3, "new": 1, "in_work": 1, "answered": 1}
        assert counts["all"] == counts["new"] + counts["in_work"] + counts["answered"]
        assert new_qid  # использован только для наглядности сида

    _run(go())


def test_list_questions_page_parity_with_pure_status_across_all_buckets(tmp_path):
    """Паритет-тест (обязателен): бакет из SQL-выборки совпадает с `question_status(row)` для
    КАЖДОЙ строки, по всем трём статусам разом — второй логики статуса в проекте быть не
    может."""
    _ready(tmp_path)

    async def go():
        await _add_user(1)
        ids = {
            "new": await db.create_question(1, "Новый"),
            "in_work": await db.create_question(1, "В работе"),
            "answered": await db.create_question(1, "Отвечен"),
        }
        await db.claim_question(ids["in_work"], 999, "Менеджер")
        await db.claim_question(ids["answered"], 999, "Менеджер")
        await db.set_question_answer(ids["answered"], "Ответ")

        for status in q.STATUSES:
            rows = await db.list_questions_page(status=status, limit=100)
            assert rows, status
            for row in rows:
                assert q.question_status(row) == status, (status, row["id"])

    _run(go())


def test_list_questions_page_unknown_status_treated_as_all(tmp_path):
    _ready(tmp_path)

    async def go():
        await _add_user(1)
        await db.create_question(1, "Вопрос")
        rows = await db.list_questions_page(status="bogus", limit=100)
        assert len(rows) == 1

    _run(go())


def test_list_questions_page_default_order_freshest_first(tmp_path):
    _ready(tmp_path)

    async def go():
        await _add_user(1)
        first = await db.create_question(1, "Первый")
        second = await db.create_question(1, "Второй")
        rows = await db.list_questions_page(limit=100)
        assert [r["id"] for r in rows] == [second, first]

    _run(go())


def test_list_questions_page_in_work_order_oldest_stuck_first(tmp_path):
    _ready(tmp_path)

    async def go():
        await _add_user(1)
        first = await db.create_question(1, "Первый")
        second = await db.create_question(1, "Второй")
        # Захват первого — позже (запишем более раннюю answered_at вторым явно через
        # повторный claim порядка вызовов: claim_question сам ставит now(), поэтому второй
        # вызов будет иметь answered_at >= первого — сохраняем порядок вызовов).
        await db.claim_question(first, 999, "Менеджер А")
        await db.claim_question(second, 999, "Менеджер Б")
        rows = await db.list_questions_page(status="in_work", limit=100)
        assert [r["id"] for r in rows] == [first, second]

    _run(go())


def test_list_questions_page_city_scope_hides_other_city(tmp_path):
    """Менеджер, привязанный к городу (не по умолчанию — равенство, не исключение), не видит
    в выборке и не считает в счётчиках вопрос делегата другого города."""
    _ready(tmp_path)

    async def go():
        await db.set_setting("event_city_enabled", "on")
        await _add_user(1, city="spb")
        await _add_user(2, city="tyumen")
        spb_qid = await db.create_question(1, "Вопрос из Питера")
        tyumen_qid = await db.create_question(2, "Вопрос из Тюмени")

        scope = cities.city_scope("spb")
        rows = await db.list_questions_page(city_scope=scope, limit=100)
        ids = {r["id"] for r in rows}
        assert spb_qid in ids
        assert tyumen_qid not in ids

        counts = await db.count_questions_by_status(city_scope=scope)
        assert counts["all"] == 1

    _run(go())


def test_list_questions_page_orphan_kept_without_city_scope(tmp_path):
    _ready(tmp_path)

    async def go():
        qid = await db.create_question(999999, "Вопрос без пользователя в users")
        rows = await db.list_questions_page(limit=100)
        assert qid in {r["id"] for r in rows}

    _run(go())
