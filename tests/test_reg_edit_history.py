"""Phase 21 Plan 05 Task 1 (FORM-SYNC-04, D-12, D-13, D-15, D-16): контракт узкого UPDATE
ответов (`update_user_answers`) и следа правок (`reg_answer_history`, `users.edited_at`/
`edited_source`), снятый ДО реализации (RED). Функции `update_user_answers`/
`record_answer_history`/`get_answer_history`/`mark_user_edited` в `database/db.py` ещё не
существуют — этот файл обязан падать на ImportError/AttributeError с их именами.

pytest-asyncio недоступен — async через asyncio.run(), фикстура временной БД — тот же приём,
что `tests/test_reg_resume_ttl_260820.py::_ready(tmp_path)`.

Phase 21 Plan 07 (FORM-SYNC-04, D-12, D-14, D-15): раздел «карточка» ниже проверяет
`handlers/admin_moderation.py` — пометки «✏️ Изменена»/«🔁 Повторная подача», кнопку/экран
«🕓 История» — и тумблер «toggle_reg_edit_remoderation» в разделе «📋 Заявки».
"""
import asyncio
from datetime import datetime, timedelta

from config import config
from database import db

USER_ID = 900300400

ALLOWED_COLUMNS = {"full_name", "phone", "university"}


def _ready(tmp_path, name="reg_edit_history.db"):
    config.DB_PATH = str(tmp_path / name)
    asyncio.run(db.init_db())


async def _seed_user():
    await db.add_user({
        "telegram_id": USER_ID,
        "full_name": "Иван Иванов",
        "phone": "+79990000000",
        "university": "СПбГУ",
        "referrer_id": 42,
        "registration_date": "2026-08-01 10:00:00",
        "source": "friend",
        "event_city": "msk",
    })
    await db.set_user_status(USER_ID, "approved")


# ── update_user_answers: узкий UPDATE по allowlist ─────────────────────────────────────────

def test_update_user_answers_writes_only_allowed_intersection(tmp_path):
    _ready(tmp_path)

    async def go():
        await _seed_user()
        n = await db.update_user_answers(
            USER_ID, {"full_name": "Пётр Петров", "status": "pending"},
            allowed_columns=ALLOWED_COLUMNS,
        )
        return n, await db.get_user(USER_ID)

    n, user = asyncio.run(go())
    assert n == 1  # только full_name — status вне allowlist
    assert user["full_name"] == "Пётр Петров"
    assert user["status"] == "approved"  # не тронут


def test_update_user_answers_does_not_touch_attribution_or_status(tmp_path):
    _ready(tmp_path)

    async def go():
        await _seed_user()
        await db.update_user_answers(
            USER_ID, {"phone": "+79991112233"}, allowed_columns=ALLOWED_COLUMNS,
        )
        return await db.get_user(USER_ID)

    user = asyncio.run(go())
    assert user["registration_date"] == "2026-08-01 10:00:00"
    assert user["referrer_id"] == 42
    assert user["username"] is None or user["username"] == user.get("username")
    assert user["source"] == "friend"
    assert user["status"] == "approved"
    assert user["payment_status"] == "not_paid"


def test_update_user_answers_empty_intersection_writes_nothing(tmp_path):
    _ready(tmp_path)

    async def go():
        await _seed_user()
        n = await db.update_user_answers(
            USER_ID, {"status": "pending", "referrer_id": 999},
            allowed_columns=ALLOWED_COLUMNS,
        )
        return n, await db.get_user(USER_ID)

    n, user = asyncio.run(go())
    assert n == 0
    assert user["status"] == "approved"
    assert user["referrer_id"] == 42


# ── record_answer_history / get_answer_history ─────────────────────────────────────────────

def test_record_and_get_answer_history_newest_first(tmp_path):
    _ready(tmp_path)

    async def go():
        await _seed_user()
        await db.record_answer_history(
            USER_ID,
            [{"column": "full_name", "old": "Иван Иванов", "new": "Пётр Петров"}],
            source="miniapp", season="2026",
        )
        await db.record_answer_history(
            USER_ID,
            [{"column": "phone", "old": "+79990000000", "new": "+79991112233"}],
            source="bot", season="2026",
        )
        return await db.get_answer_history(USER_ID, limit=5)

    rows = asyncio.run(go())
    assert len(rows) == 2
    # новыми вперёд -> последняя запись (phone/bot) первая
    assert rows[0]["source"] == "bot"
    assert rows[1]["source"] == "miniapp"
    assert isinstance(rows[0]["changes"], list)
    assert rows[0]["changes"][0]["column"] == "phone"


def test_record_answer_history_empty_changes_no_row(tmp_path):
    _ready(tmp_path)

    async def go():
        await _seed_user()
        await db.record_answer_history(USER_ID, [], source="bot", season="2026")
        return await db.get_answer_history(USER_ID, limit=5)

    rows = asyncio.run(go())
    assert rows == []


# ── mark_user_edited ─────────────────────────────────────────────────────────────────────

def test_mark_user_edited_sets_and_updates_fields(tmp_path):
    _ready(tmp_path)

    async def go():
        await _seed_user()
        await db.mark_user_edited(USER_ID, "miniapp")
        first = await db.get_user(USER_ID)
        # искусственно откатываем edited_at, чтобы убедиться, что повторный вызов его продвигает
        async with db._connect() as conn:
            stamp = (datetime.now() - timedelta(hours=1)).strftime("%Y-%m-%d %H:%M:%S")
            await conn.execute(
                "UPDATE users SET edited_at = ? WHERE telegram_id = ?", (stamp, USER_ID)
            )
            await conn.commit()
        rolled_back = await db.get_user(USER_ID)
        await db.mark_user_edited(USER_ID, "bot")
        second = await db.get_user(USER_ID)
        return first, rolled_back, second

    first, rolled_back, second = asyncio.run(go())
    assert first["edited_at"] is not None
    assert first["edited_source"] == "miniapp"
    assert second["edited_at"] > rolled_back["edited_at"]
    assert second["edited_source"] == "bot"


# ── миграция на непустой БД: старые данные не теряются ─────────────────────────────────────

def test_new_tables_and_columns_do_not_wipe_existing_data(tmp_path):
    _ready(tmp_path)

    async def go():
        await _seed_user()
        await db.init_db()  # повторный вызов на непустой БД
        return await db.get_user(USER_ID)

    user = asyncio.run(go())
    assert user is not None
    assert user["full_name"] == "Иван Иванов"
    assert "edited_at" in user
    assert "edited_source" in user


# ── Task 1 (21-07): карточка заявки — «✏️ Изменена» / «🔁 Повторная подача» / «🕓 История» ──

from handlers import admin_moderation  # noqa: E402
from handlers.admin_moderation import (  # noqa: E402
    _render_application_card,
    _appr_card_kb,
    _edit_badges_for,
    appr_history,
)
from tests.test_admin_sections_ia20 import FakeCallback  # noqa: E402  (образец мока, не копия)

CARD_USER_ID = 900300401


def _ready_card(tmp_path, name="reg_edit_history_card.db"):
    config.DB_PATH = str(tmp_path / name)
    asyncio.run(db.init_db())


def test_card_shows_edited_line_when_present():
    out = _render_application_card({"full_name": "Иван"}, 1, 1, edited_line="✏️ Изменена 02.09 14:00")
    assert "Изменена" in out


def test_card_no_edited_line_when_edited_at_empty():
    out = _render_application_card({"full_name": "Иван"}, 1, 1)
    assert "Изменена" not in out


def test_card_shows_resubmit_line_only_when_given():
    with_resubmit = _render_application_card({"full_name": "Иван"}, 1, 1, resubmit_line="🔁 Повторная подача")
    without = _render_application_card({"full_name": "Иван"}, 1, 1)
    assert "Повторная подача" in with_resubmit
    assert "Повторная подача" not in without


def test_edit_badges_for_empty_edited_at_returns_nothing(tmp_path):
    _ready_card(tmp_path)
    result = asyncio.run(_edit_badges_for({"telegram_id": CARD_USER_ID, "edited_at": None}))
    assert result == (None, None, False)


def test_edit_badges_for_reads_registry_template_and_maps_source(tmp_path):
    _ready_card(tmp_path)
    user = {"telegram_id": CARD_USER_ID, "edited_at": "2026-09-02 14:12:00", "edited_source": "miniapp"}
    edited_line, resubmit_line, has_history = asyncio.run(_edit_badges_for(user))
    assert edited_line is not None
    assert "02.09 14:12" in edited_line
    assert "в приложении" in edited_line
    assert "miniapp" not in edited_line  # код источника человеку не показываем
    assert resubmit_line is None  # истории ещё нет -> признака повторной подачи тоже нет
    assert has_history is False


def test_edit_badges_for_detects_resubmit_from_history(tmp_path):
    _ready_card(tmp_path)

    async def go():
        await db.record_answer_history(
            CARD_USER_ID,
            [{"column": "status", "old": "rejected", "new": "pending"}],
            source="miniapp", season="2026",
        )
        user = {"telegram_id": CARD_USER_ID, "edited_at": "2026-09-02 14:12:00", "edited_source": "miniapp"}
        return await _edit_badges_for(user)

    edited_line, resubmit_line, has_history = asyncio.run(go())
    assert resubmit_line is not None
    assert "Повторная подача" in resubmit_line
    assert has_history is True


def test_kb_no_history_button_without_history():
    kb = asyncio.run(_appr_card_kb(1, False, 1, has_history=False))
    texts = [b.text for row in kb.inline_keyboard for b in row]
    assert not any("История" in t for t in texts)


def test_kb_history_button_with_history_and_correct_callback(tmp_path):
    _ready_card(tmp_path)
    kb = asyncio.run(_appr_card_kb(CARD_USER_ID, False, 1, has_history=True))
    texts = [b.text for row in kb.inline_keyboard for b in row]
    assert any("История" in t for t in texts)
    btn = next(b for row in kb.inline_keyboard for b in row if "История" in b.text)
    assert btn.callback_data == f"appr_history:{CARD_USER_ID}"


def test_appr_history_shows_human_labels_dates_and_escapes(tmp_path):
    _ready_card(tmp_path)

    async def go():
        await db.record_answer_history(
            CARD_USER_ID,
            [{"column": "phone", "old": "<b>old</b>", "new": "+79990000000"}],
            source="miniapp", season="2026",
        )
        cb = FakeCallback(f"appr_history:{CARD_USER_ID}")
        await appr_history(cb)
        return cb

    cb = asyncio.run(go())
    sent_text = cb.message.sent[-1][0]
    assert "Телефон" in sent_text          # человеческая подпись, не "phone"
    assert "phone" not in sent_text.replace("Телефон", "")
    assert "&lt;b&gt;old&lt;/b&gt;" in sent_text  # сырой HTML экранирован
    assert "<b>old</b>" not in sent_text
    assert "в приложении" in sent_text     # источник словом, не кодом
    assert "miniapp" not in sent_text


def test_appr_history_empty_says_no_edits_yet(tmp_path):
    _ready_card(tmp_path)
    cb = FakeCallback("appr_history:900300999")
    asyncio.run(appr_history(cb))
    sent_text = cb.message.sent[-1][0]
    assert "правок пока нет" in sent_text.lower()


def test_appr_history_skips_status_marker_not_shown_as_a_field():
    # маркер повторной подачи (column="status") уже показан отдельной строкой карточки
    # (_edit_badges_for) — экран истории не должен дублировать его как поле анкеты "status".
    out_columns = admin_moderation._COLUMN_TO_LABEL
    assert "status" not in out_columns
