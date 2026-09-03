"""Quick 260904-2cj (QJRN-01/02/03/04): раздел «❓ Вопросы делегатов».

Задача 1 — общий слой статуса (`services/questions.py`, чистый модуль) + постраничные
аксессоры БД (`database.db.list_questions_page`/`count_questions_by_status`). Задача 2
дописывает сюда тесты экрана бота (тем же файлом, ниже отдельным блоком).

pytest-asyncio в проекте нет — каждый async-вызов через asyncio.run(); БД — tmp_path.
`_ready`/`_add_user` — те же хелперы, что у `tests/test_sheet_logs_260902.py`.
"""
import asyncio
from datetime import datetime, timedelta

from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage

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


# ══════════════════════════════════════════════════════════════════════════════════════════
# Задача 2 — экран «❓ Вопросы делегатов» бота + ответ из экрана
#
# FakeCallback/FakeMessage(FakeUser) — из tests/test_admin_sections_ia20.py (простые dummy для
# прямого вызова хендлера); ADMIN_ID/MANAGER_ID/STRANGER_ID/_roles_ready — из
# tests/test_roles_phase8.py. Полный dispatch через Router (капы/FSM-фильтры) не нужен —
# хендлеры зовутся напрямую, тот же приём, что у tests/test_coins_manual_260818.py.
# ══════════════════════════════════════════════════════════════════════════════════════════

from handlers import admin_questions
from handlers.states import QuestionAnswer
from tests.test_admin_sections_ia20 import FakeCallback
from tests.test_roles_phase8 import ADMIN_ID, FakeUser as _RolesFakeUser, MANAGER_ID, STRANGER_ID, _roles_ready


def _new_state(uid=ADMIN_ID) -> FSMContext:
    return FSMContext(storage=MemoryStorage(), key=StorageKey(bot_id=1, chat_id=uid, user_id=uid))


async def _set_answered_at(qid, stamp):
    async with db._connect() as conn:
        await conn.execute(
            "UPDATE delegate_questions SET answered_at = ? WHERE id = ?", (stamp, qid),
        )
        await conn.commit()


class _FakeBot:
    """`send_message` падает `fail_times` раз подряд, потом ведёт себя как обычно — тот же
    приём, что `tests/test_roles_phase8.py::_RaisingBot`."""

    def __init__(self, fail_times=0, exc=None):
        self.sent: list[tuple] = []
        self._fail_times = fail_times
        self._exc = exc or RuntimeError("сеть недоступна")

    async def send_message(self, chat_id, text, parse_mode=None, reply_markup=None):
        if self._fail_times > 0:
            self._fail_times -= 1
            raise self._exc
        self.sent.append((chat_id, text))


class _FakeAnswerMessage:
    """Дублирует и «дошедшее до aq_answer_step сообщение», и `callback.message» — свой класс:
    у FakeMessage из test_admin_sections_ia20 нет `.reply`/`.from_user`, нужных
    `_attempt_question_delivery`/`_deliver_question_reply`."""

    def __init__(self, text=None, user_id=ADMIN_ID):
        self.text = text
        self.html_text = text
        self.from_user = _RolesFakeUser(user_id)
        self.answers: list[tuple] = []

    async def answer(self, text, parse_mode=None, reply_markup=None):
        self.answers.append((text, parse_mode, reply_markup))

    async def reply(self, text, parse_mode=None, reply_markup=None):
        self.answers.append((text, parse_mode, reply_markup))


# ── render_questions_screen: состав строки по каждому статусу ──────────────────────────────

def test_render_screen_new_row_content(tmp_path):
    _roles_ready(tmp_path)
    qid = _run(db.create_question(STRANGER_ID, "Когда дедлайн?"))
    text, kb = _run(admin_questions.render_questions_screen(ADMIN_ID))
    assert f"#{qid}" in text
    assert "🆕 без ответа" in text
    assert f"<code>{STRANGER_ID}</code>" in text
    assert "Когда дедлайн?" in text
    buttons = [b.callback_data for row in kb.inline_keyboard for b in row]
    assert f"aq_answer:{qid}" in buttons


def test_render_screen_in_work_row_shows_claimant_and_stuck_flag(tmp_path):
    _roles_ready(tmp_path)
    qid = _run(db.create_question(STRANGER_ID, "Где встреча?"))
    _run(db.claim_question(qid, ADMIN_ID, "Админ Первый"))
    old_stamp = (datetime.utcnow() - timedelta(minutes=q.STUCK_AFTER_MINUTES + 5)).isoformat()
    _run(_set_answered_at(qid, old_stamp))

    text, kb = _run(admin_questions.render_questions_screen(ADMIN_ID))
    assert "✍️ взял(а) Админ Первый" in text
    assert "🔒 залип" in text
    buttons = [b.callback_data for row in kb.inline_keyboard for b in row]
    assert f"aq_answer:{qid}" in buttons  # ещё можно ответить — не отвечен


def test_render_screen_in_work_fresh_claim_not_stuck(tmp_path):
    _roles_ready(tmp_path)
    qid = _run(db.create_question(STRANGER_ID, "Где встреча?"))
    _run(db.claim_question(qid, ADMIN_ID, "Админ Первый"))
    text, _ = _run(admin_questions.render_questions_screen(ADMIN_ID))
    assert "🔒 залип" not in text


def test_render_screen_answered_row_shows_answer_and_no_button(tmp_path):
    _roles_ready(tmp_path)
    qid = _run(db.create_question(STRANGER_ID, "Оплата?"))
    _run(db.claim_question(qid, ADMIN_ID, "Админ Первый"))
    _run(db.set_question_answer(qid, "Уже оплачено"))

    text, kb = _run(admin_questions.render_questions_screen(ADMIN_ID))
    assert "✅ отвечен" in text
    assert "Уже оплачено" in text
    buttons = [b.callback_data for row in kb.inline_keyboard for b in row]
    assert f"aq_answer:{qid}" not in buttons


# ── фильтры, счётчики, пагинация, пустые состояния ──────────────────────────────────────────

def test_render_screen_counters_and_filter_narrows_list(tmp_path):
    _roles_ready(tmp_path)
    new_qid = _run(db.create_question(STRANGER_ID, "Новый"))
    in_work_qid = _run(db.create_question(STRANGER_ID, "В работе"))
    _run(db.claim_question(in_work_qid, ADMIN_ID, "Админ"))

    text_all, _ = _run(admin_questions.render_questions_screen(ADMIN_ID))
    assert "без ответа: 1 · в работе: 1 · отвечено: 0" in text_all
    assert f"#{new_qid}" in text_all and f"#{in_work_qid}" in text_all
    # Открытие без явного фильтра не навязывает «Показаны»/«Страница».
    assert "Показаны:" not in text_all

    text_new, _ = _run(admin_questions.render_questions_screen(ADMIN_ID, status="new"))
    assert f"#{new_qid}" in text_new
    assert f"#{in_work_qid}" not in text_new
    assert "Показаны: Без ответа" in text_new
    assert "Страница 1 из 1" in text_new


def test_render_screen_pagination_preserves_filter(tmp_path):
    _roles_ready(tmp_path)
    for i in range(7):
        qid = _run(db.create_question(STRANGER_ID, f"Вопрос {i}"))
        _run(db.claim_question(qid, ADMIN_ID, "Админ"))

    text, kb = _run(admin_questions.render_questions_screen(ADMIN_ID, status="in_work", offset=0))
    assert "Страница 1 из 2" in text
    nav_callbacks = [b.callback_data for row in kb.inline_keyboard for b in row]
    assert "aq:in_work:6" in nav_callbacks


def test_render_screen_empty_default_and_filtered_texts(tmp_path):
    _roles_ready(tmp_path)
    text_default, _ = _run(admin_questions.render_questions_screen(ADMIN_ID))
    assert "Вопросов пока нет." in text_default

    _run(db.create_question(STRANGER_ID, "Вопрос"))
    text_filtered, _ = _run(admin_questions.render_questions_screen(ADMIN_ID, status="answered"))
    assert "попробуйте фильтр «Все»" in text_filtered


# ── хендлеры: открытие, фильтр/страница, старый алиас ───────────────────────────────────────

def test_admin_questions_callback_opens_screen(tmp_path):
    _roles_ready(tmp_path)
    _run(db.create_question(STRANGER_ID, "Вопрос"))
    cb = FakeCallback("admin_questions", user_id=ADMIN_ID)
    _run(admin_questions.admin_questions(cb))
    assert cb.message.edit_calls == 1
    assert "❓" in cb.message.text


def test_aq_page_switches_filter(tmp_path):
    _roles_ready(tmp_path)
    qid = _run(db.create_question(STRANGER_ID, "Вопрос"))
    cb = FakeCallback("aq:new:0", user_id=ADMIN_ID)
    _run(admin_questions.aq_page(cb))
    assert f"#{qid}" in cb.message.text
    assert "Показаны: Без ответа" in cb.message.text


def test_aq_page_invalid_offset_shows_alert(tmp_path):
    _roles_ready(tmp_path)
    cb = FakeCallback("aq:all:bogus", user_id=ADMIN_ID)
    _run(admin_questions.aq_page(cb))
    assert cb.answers == [("Некорректная страница", True)]


def test_show_stuck_questions_alias_opens_in_work_filter(tmp_path):
    """Старый callback `admin_stuck_questions` (клавиатуры, живущие в чатах) продолжает
    работать — теперь как алиас на фильтр «в работе» журнала."""
    _roles_ready(tmp_path)
    from handlers import admin as admin_mod

    _run(db.create_question(STRANGER_ID, "Где встреча?"))
    qid = _run(db.create_question(STRANGER_ID, "Другой вопрос"))
    _run(db.claim_question(qid, ADMIN_ID, "Админ Первый"))

    cb = FakeCallback("admin_stuck_questions", user_id=ADMIN_ID)
    _run(admin_mod.show_stuck_questions(cb))
    assert "Показаны: В работе" in cb.message.text
    assert str(STRANGER_ID) in cb.message.text
    assert "Админ Первый" in cb.message.text


# ── ответ из экрана: захват -> доставка -> «✅ отвечен» ──────────────────────────────────────

def test_aq_answer_start_prompts_and_sets_state(tmp_path):
    _roles_ready(tmp_path)
    qid = _run(db.create_question(STRANGER_ID, "Вопрос"))
    state = _new_state(ADMIN_ID)
    cb = FakeCallback(f"aq_answer:{qid}", user_id=ADMIN_ID)
    _run(admin_questions.aq_answer_start(cb, state))
    assert _run(state.get_state()) == QuestionAnswer.text.state
    assert _run(state.get_data()) == {"aq_qid": qid, "aq_user_id": STRANGER_ID}
    assert cb.message.sent


def test_answer_from_screen_full_path_claim_deliver_answered(tmp_path):
    _roles_ready(tmp_path)
    qid = _run(db.create_question(STRANGER_ID, "Когда дедлайн?"))
    state = _new_state(ADMIN_ID)
    cb = FakeCallback(f"aq_answer:{qid}", user_id=ADMIN_ID)
    _run(admin_questions.aq_answer_start(cb, state))

    msg = _FakeAnswerMessage("Дедлайн 1 сентября", user_id=ADMIN_ID)
    bot = _FakeBot()
    _run(admin_questions.aq_answer_step(msg, state, bot))

    assert (STRANGER_ID, "💬 <b>Ответ от организаторов:</b>\n\nДедлайн 1 сентября") in bot.sent
    row = _run(db.get_question(qid))
    assert row["delivered_at"] is not None
    assert row["answer_text"] == "Дедлайн 1 сентября"
    assert _run(state.get_state()) is None

    final_text = msg.answers[-1][0]
    assert "✅ отвечен" in final_text
    assert f"#{qid}" in final_text


def test_answer_from_screen_second_claimant_rejected_delegate_gets_nothing(tmp_path):
    _roles_ready(tmp_path)
    _run(db.add_staff(MANAGER_ID, "reg_manager", ADMIN_ID))
    qid = _run(db.create_question(STRANGER_ID, "Когда дедлайн?"))
    _run(db.claim_question(qid, ADMIN_ID, "Админ Первый"))

    state = _new_state(MANAGER_ID)
    _run(state.update_data(aq_qid=qid, aq_user_id=STRANGER_ID))
    _run(state.set_state(QuestionAnswer.text))
    msg = _FakeAnswerMessage("Другой ответ", user_id=MANAGER_ID)
    bot = _FakeBot()
    _run(admin_questions.aq_answer_step(msg, state, bot))

    assert bot.sent == []
    assert any("уже ответил(а) Админ Первый" in a[0] for a in msg.answers)


def test_answer_from_screen_delivery_failure_keeps_claim_and_explains(tmp_path):
    _roles_ready(tmp_path)
    qid = _run(db.create_question(STRANGER_ID, "Когда дедлайн?"))
    state = _new_state(ADMIN_ID)
    _run(state.update_data(aq_qid=qid, aq_user_id=STRANGER_ID))
    _run(state.set_state(QuestionAnswer.text))
    msg = _FakeAnswerMessage("Ответ", user_id=ADMIN_ID)
    bot = _FakeBot(fail_times=1)
    _run(admin_questions.aq_answer_step(msg, state, bot))

    row = _run(db.get_question(qid))
    assert row["delivered_at"] is None
    assert row["answered_by"] == ADMIN_ID  # захват не отпущен
    assert any("Не удалось отправить ответ" in a[0] for a in msg.answers)


def test_aq_answer_cancel_resets_state_without_reaching_delegate(tmp_path):
    _roles_ready(tmp_path)
    for cancel_text in ("Отмена", "/cancel"):
        state = _new_state(ADMIN_ID)
        _run(state.set_state(QuestionAnswer.text))
        msg = _FakeAnswerMessage(cancel_text, user_id=ADMIN_ID)
        _run(admin_questions.aq_answer_cancel(msg, state))
        assert _run(state.get_state()) is None
        assert msg.answers and "отменено" in msg.answers[0][0].lower()
