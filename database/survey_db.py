"""Слой БД анкеты из нескольких вопросов (опрос `polls.kind='survey'`).

Анкета живёт в той же таблице `polls`, что и обычный опрос (аудитория, расписание, статусы), но
вопросы лежат в `survey_questions`, а прогресс делегата и его ответы — в `survey_progress` и
`survey_answers`. Прогресс и черновик менеджера хранятся в БД, а не в FSM: FSM у бота в памяти и
сбрасывается при рестарте, а анкета на 10+ вопросов не должна теряться.

Переходы между вопросами — условный UPDATE по `current_qid` с проверкой `rowcount`: двойной тап
по кнопке даёт ровно один переход (тот же приём, что `claim_poll_sending`).

Соединение — `_db._connect()` через атрибут модуля, как у соседних *_db модулей.
"""
from __future__ import annotations

import json
import logging

import aiosqlite

from database import db as _db
from services.infra.timeutil import msk_now

logger = logging.getLogger(__name__)

QTYPES = ("single", "multi", "scale", "text", "table")


def _stamp() -> str:
    return msk_now().strftime("%Y-%m-%d %H:%M:%S")


def _dumps(value) -> str:
    return json.dumps(value, ensure_ascii=False)


def _loads(raw, default):
    if raw is None or raw == "":
        return default
    try:
        return json.loads(raw)
    except (TypeError, ValueError):
        return default


# ── Схема ────────────────────────────────────────────────────────────────────────────────────

async def ensure_schema(db: aiosqlite.Connection) -> None:
    """Аддитивная миграция `polls` и таблицы анкеты. Зовётся из `init_db`; коммит делает он.
    Старые строки `polls` получают kind='poll'."""
    await _db._ensure_column(db, "polls", "kind", "TEXT NOT NULL DEFAULT 'poll'")
    await _db._ensure_column(db, "polls", "intro_text", "TEXT")
    await _db._ensure_column(db, "polls", "outro_text", "TEXT")
    await db.execute(
        "CREATE TABLE IF NOT EXISTS survey_questions ("
        "id INTEGER PRIMARY KEY AUTOINCREMENT, "
        "poll_id INTEGER NOT NULL, position INTEGER NOT NULL, "
        "qtype TEXT NOT NULL, text TEXT NOT NULL, required INTEGER NOT NULL DEFAULT 1, "
        "max_choices INTEGER, "
        "options_json TEXT NOT NULL DEFAULT '[]', rows_json TEXT NOT NULL DEFAULT '[]', "
        "scale_lo TEXT, scale_hi TEXT, na_label TEXT)"
    )
    await db.execute(
        "CREATE INDEX IF NOT EXISTS idx_survey_q_poll ON survey_questions(poll_id, position)"
    )
    await db.execute(
        "CREATE TABLE IF NOT EXISTS survey_progress ("
        "poll_id INTEGER NOT NULL, user_id INTEGER NOT NULL, "
        "current_qid INTEGER, state TEXT NOT NULL DEFAULT 'active', "
        "await_json TEXT, draft_json TEXT, started_at TEXT, finished_at TEXT, "
        "PRIMARY KEY (poll_id, user_id))"
    )
    await db.execute(
        "CREATE INDEX IF NOT EXISTS idx_survey_progress_wait ON survey_progress(user_id, state)"
    )
    await db.execute(
        "CREATE TABLE IF NOT EXISTS survey_answers ("
        "poll_id INTEGER NOT NULL, user_id INTEGER NOT NULL, question_id INTEGER NOT NULL, "
        "sub TEXT NOT NULL DEFAULT '', value_json TEXT NOT NULL, answered_at TEXT, "
        "UNIQUE (poll_id, user_id, question_id, sub))"
    )


# ── Черновик анкеты (polls) ──────────────────────────────────────────────────────────────────

async def create_survey_draft(title: str, created_by: int, city: str | None = None) -> int:
    """Новая анкета в статусе 'draft'. question = название, options_json = '[]' (NOT NULL)."""
    async with _db._connect() as db:
        cursor = await db.execute(
            "INSERT INTO polls (question, options_json, is_anonymous, allows_multiple, created_by, "
            "created_at, city, audience_json, status, kind) "
            "VALUES (?, '[]', 0, 0, ?, ?, ?, '[]', 'draft', 'survey')",
            (title, created_by, _stamp(), city),
        )
        await db.commit()
        return cursor.lastrowid


async def update_survey_texts(
    poll_id: int, *, title: str | None = None, intro: str | None = None, outro: str | None = None
) -> None:
    sets, params = [], []
    if title is not None:
        sets.append("question = ?")
        params.append(title)
    if intro is not None:
        sets.append("intro_text = ?")
        params.append(intro)
    if outro is not None:
        sets.append("outro_text = ?")
        params.append(outro)
    if not sets:
        return
    params.append(poll_id)
    async with _db._connect() as db:
        await db.execute(f"UPDATE polls SET {', '.join(sets)} WHERE id = ?", params)
        await db.commit()


async def publish_survey_draft(
    poll_id: int, *, audience: list[dict], city: str | None, scheduled_at: str
) -> int:
    """draft -> scheduled одним условным UPDATE. 0 = не черновик анкеты / уже опубликован."""
    async with _db._connect() as db:
        cursor = await db.execute(
            "UPDATE polls SET status = 'scheduled', audience_json = ?, city = ?, scheduled_at = ? "
            "WHERE id = ? AND status = 'draft' AND kind = 'survey'",
            (_dumps(audience or []), city, scheduled_at, poll_id),
        )
        await db.commit()
        return cursor.rowcount


async def copy_survey(poll_id: int, created_by: int) -> int:
    """Копия анкеты в новый черновик: те же тексты и вопросы, аудитория пустая."""
    async with _db._connect() as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT * FROM polls WHERE id = ?", (poll_id,)) as cur:
            src = await cur.fetchone()
        if src is None:
            raise ValueError(f"survey {poll_id} not found")
        cursor = await db.execute(
            "INSERT INTO polls (question, options_json, is_anonymous, allows_multiple, created_by, "
            "created_at, city, audience_json, status, kind, intro_text, outro_text) "
            "VALUES (?, '[]', 0, 0, ?, ?, ?, '[]', 'draft', 'survey', ?, ?)",
            (f"{src['question']} (копия)", created_by, _stamp(), src["city"],
             src["intro_text"], src["outro_text"]),
        )
        new_id = cursor.lastrowid
        await db.execute(
            "INSERT INTO survey_questions (poll_id, position, qtype, text, required, max_choices, "
            "options_json, rows_json, scale_lo, scale_hi, na_label) "
            "SELECT ?, position, qtype, text, required, max_choices, options_json, rows_json, "
            "scale_lo, scale_hi, na_label FROM survey_questions WHERE poll_id = ? "
            "ORDER BY position, id",
            (new_id, poll_id),
        )
        await db.commit()
        return new_id


# ── Вопросы ──────────────────────────────────────────────────────────────────────────────────

def _question_row(row) -> dict:
    d = dict(row)
    d["options"] = _loads(d.get("options_json"), [])
    d["rows"] = _loads(d.get("rows_json"), [])
    d["required"] = bool(d.get("required"))
    return d


async def list_survey_questions(poll_id: int) -> list[dict]:
    async with _db._connect() as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM survey_questions WHERE poll_id = ? ORDER BY position, id", (poll_id,)
        ) as cur:
            return [_question_row(r) for r in await cur.fetchall()]


async def get_survey_question(question_id: int) -> dict | None:
    async with _db._connect() as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM survey_questions WHERE id = ?", (question_id,)
        ) as cur:
            row = await cur.fetchone()
            return _question_row(row) if row else None


async def add_survey_question(
    poll_id: int,
    qtype: str,
    text: str,
    *,
    required: bool = True,
    max_choices: int | None = None,
    options: list[dict] | None = None,
    rows: list[str] | None = None,
    scale_lo: str | None = None,
    scale_hi: str | None = None,
    na_label: str | None = None,
) -> int:
    if qtype not in QTYPES:
        raise ValueError(f"unknown question type: {qtype}")
    async with _db._connect() as db:
        async with db.execute(
            "SELECT COALESCE(MAX(position), 0) FROM survey_questions WHERE poll_id = ?", (poll_id,)
        ) as cur:
            pos = (await cur.fetchone())[0] + 1
        cursor = await db.execute(
            "INSERT INTO survey_questions (poll_id, position, qtype, text, required, max_choices, "
            "options_json, rows_json, scale_lo, scale_hi, na_label) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (poll_id, pos, qtype, text, 1 if required else 0, max_choices,
             _dumps(options or []), _dumps(rows or []), scale_lo, scale_hi, na_label),
        )
        await db.commit()
        return cursor.lastrowid


async def update_survey_question_text(question_id: int, text: str) -> None:
    async with _db._connect() as db:
        await db.execute("UPDATE survey_questions SET text = ? WHERE id = ?", (text, question_id))
        await db.commit()


async def _renumber(db: aiosqlite.Connection, poll_id: int) -> None:
    async with db.execute(
        "SELECT id FROM survey_questions WHERE poll_id = ? ORDER BY position, id", (poll_id,)
    ) as cur:
        ids = [r[0] for r in await cur.fetchall()]
    for i, qid in enumerate(ids, start=1):
        await db.execute("UPDATE survey_questions SET position = ? WHERE id = ?", (i, qid))


async def delete_survey_question(question_id: int) -> None:
    async with _db._connect() as db:
        async with db.execute(
            "SELECT poll_id FROM survey_questions WHERE id = ?", (question_id,)
        ) as cur:
            row = await cur.fetchone()
        if row is None:
            return
        await db.execute("DELETE FROM survey_questions WHERE id = ?", (question_id,))
        await _renumber(db, row[0])
        await db.commit()


async def move_survey_question(question_id: int, direction: int) -> bool:
    """Сдвиг вопроса на одну позицию (-1 вверх, +1 вниз). False — на краю или вопроса нет."""
    if direction not in (-1, 1):
        return False
    async with _db._connect() as db:
        async with db.execute(
            "SELECT poll_id FROM survey_questions WHERE id = ?", (question_id,)
        ) as cur:
            row = await cur.fetchone()
        if row is None:
            return False
        poll_id = row[0]
        await _renumber(db, poll_id)
        async with db.execute(
            "SELECT id FROM survey_questions WHERE poll_id = ? ORDER BY position", (poll_id,)
        ) as cur:
            ids = [r[0] for r in await cur.fetchall()]
        i = ids.index(question_id)
        j = i + direction
        if j < 0 or j >= len(ids):
            await db.commit()
            return False
        ids[i], ids[j] = ids[j], ids[i]
        for pos, qid in enumerate(ids, start=1):
            await db.execute("UPDATE survey_questions SET position = ? WHERE id = ?", (pos, qid))
        await db.commit()
        return True


# ── Получатели и прогресс ────────────────────────────────────────────────────────────────────

async def survey_recipient(poll_id: int, user_id: int) -> bool:
    """Анкета была доставлена этому человеку (строка poll_messages с status='ok')."""
    async with _db._connect() as db:
        async with db.execute(
            "SELECT 1 FROM poll_messages WHERE poll_id = ? AND chat_id = ? AND status = 'ok'",
            (poll_id, user_id),
        ) as cur:
            return await cur.fetchone() is not None


def _progress_row(row) -> dict:
    d = dict(row)
    d["await"] = _loads(d.get("await_json"), None)
    d["draft"] = _loads(d.get("draft_json"), None)
    return d


async def get_survey_progress(poll_id: int, user_id: int) -> dict | None:
    async with _db._connect() as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM survey_progress WHERE poll_id = ? AND user_id = ?", (poll_id, user_id)
        ) as cur:
            row = await cur.fetchone()
            return _progress_row(row) if row else None


async def start_survey_progress(poll_id: int, user_id: int, first_qid: int) -> dict:
    """Идемпотентный старт: повторный вызов возвращает уже идущий прогресс, не сбрасывая его."""
    async with _db._connect() as db:
        await db.execute(
            "INSERT OR IGNORE INTO survey_progress (poll_id, user_id, current_qid, state, started_at) "
            "VALUES (?, ?, ?, 'active', ?)",
            (poll_id, user_id, first_qid, _stamp()),
        )
        await db.commit()
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM survey_progress WHERE poll_id = ? AND user_id = ?", (poll_id, user_id)
        ) as cur:
            return _progress_row(await cur.fetchone())


async def advance_survey(poll_id: int, user_id: int, from_qid: int, to_qid: int) -> bool:
    """Атомарный переход from_qid -> to_qid. False — прогресс уже ушёл дальше (двойной тап)."""
    async with _db._connect() as db:
        cursor = await db.execute(
            "UPDATE survey_progress SET current_qid = ?, state = 'active', "
            "await_json = NULL, draft_json = NULL "
            "WHERE poll_id = ? AND user_id = ? AND current_qid = ? AND state != 'done'",
            (to_qid, poll_id, user_id, from_qid),
        )
        await db.commit()
        return cursor.rowcount > 0


async def finish_survey(poll_id: int, user_id: int, from_qid: int) -> bool:
    async with _db._connect() as db:
        cursor = await db.execute(
            "UPDATE survey_progress SET state = 'done', finished_at = ?, "
            "await_json = NULL, draft_json = NULL "
            "WHERE poll_id = ? AND user_id = ? AND current_qid = ? AND state != 'done'",
            (_stamp(), poll_id, user_id, from_qid),
        )
        await db.commit()
        return cursor.rowcount > 0


async def set_survey_await(poll_id: int, user_id: int, qid: int, wait: dict | None) -> bool:
    """Ожидание свободного текста (wait) или снятие ожидания (None). Только для текущего вопроса."""
    async with _db._connect() as db:
        cursor = await db.execute(
            "UPDATE survey_progress SET state = ?, await_json = ? "
            "WHERE poll_id = ? AND user_id = ? AND current_qid = ? AND state != 'done'",
            ("awaiting_text" if wait else "active", _dumps(wait) if wait else None,
             poll_id, user_id, qid),
        )
        await db.commit()
        return cursor.rowcount > 0


async def set_survey_draft(poll_id: int, user_id: int, qid: int, draft: dict | None) -> bool:
    async with _db._connect() as db:
        cursor = await db.execute(
            "UPDATE survey_progress SET draft_json = ? "
            "WHERE poll_id = ? AND user_id = ? AND current_qid = ? AND state != 'done'",
            (_dumps(draft) if draft is not None else None, poll_id, user_id, qid),
        )
        await db.commit()
        return cursor.rowcount > 0


async def get_survey_wait(user_id: int) -> dict | None:
    """Ожидание текста у делегата — только для анкеты в рассылке или открытой, чтобы ранние
    получатели во время рассылки тоже были пойманы. Самое свежее."""
    async with _db._connect() as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT p.poll_id, p.current_qid, p.await_json FROM survey_progress p "
            "JOIN polls ON polls.id = p.poll_id "
            "WHERE p.user_id = ? AND p.state = 'awaiting_text' "
            "AND polls.status IN ('sending', 'open') AND polls.kind = 'survey' "
            "ORDER BY p.started_at DESC, p.poll_id DESC LIMIT 1",
            (user_id,),
        ) as cur:
            row = await cur.fetchone()
    if row is None:
        return None
    return {
        "poll_id": row["poll_id"],
        "current_qid": row["current_qid"],
        "await": _loads(row["await_json"], None) or {},
    }


# ── Ответы и выгрузка ────────────────────────────────────────────────────────────────────────

async def upsert_survey_answer(
    poll_id: int, user_id: int, question_id: int, sub: str, value
) -> None:
    async with _db._connect() as db:
        await db.execute(
            "INSERT INTO survey_answers (poll_id, user_id, question_id, sub, value_json, answered_at) "
            "VALUES (?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(poll_id, user_id, question_id, sub) DO UPDATE SET "
            "value_json = excluded.value_json, answered_at = excluded.answered_at",
            (poll_id, user_id, question_id, sub, _dumps(value), _stamp()),
        )
        await db.commit()


async def list_survey_answers(poll_id: int) -> list[dict]:
    async with _db._connect() as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM survey_answers WHERE poll_id = ? ORDER BY user_id, question_id, sub",
            (poll_id,),
        ) as cur:
            out = []
            for r in await cur.fetchall():
                d = dict(r)
                d["value"] = _loads(d.get("value_json"), None)
                out.append(d)
            return out


async def list_survey_progress(poll_id: int) -> list[dict]:
    async with _db._connect() as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT p.user_id, p.state, p.started_at, p.finished_at, "
            "u.full_name, u.username, u.event_city "
            "FROM survey_progress p LEFT JOIN users u ON u.telegram_id = p.user_id "
            "WHERE p.poll_id = ? ORDER BY p.started_at, p.user_id",
            (poll_id,),
        ) as cur:
            return [dict(r) for r in await cur.fetchall()]


async def count_survey_progress(poll_id: int) -> tuple[int, int]:
    """(начали, закончили)."""
    async with _db._connect() as db:
        async with db.execute(
            "SELECT COUNT(*), COALESCE(SUM(CASE WHEN state = 'done' THEN 1 ELSE 0 END), 0) "
            "FROM survey_progress WHERE poll_id = ?",
            (poll_id,),
        ) as cur:
            row = await cur.fetchone()
            return int(row[0]), int(row[1])
