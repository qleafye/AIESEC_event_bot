"""Слой БД теста компетенций: тест города, вопросы, варианты с баллами по компетенциям, уровни,
попытки делегатов.

Один тест на город (экран менеджера проще), но движок общий. Контент не сидится: миграция не
создаёт ни одного вопроса — тест наполняет менеджер. `content_version` растёт при любой правке
вопросов и вариантов, чтобы незаконченная попытка знала, по какой версии контента она начата.

Баллы варианта — `points_json` вида {"<competency_id>": баллы}. Ответы попытки хранятся сразу
по мере ответа (`answers_json` вида {"<question_id>": option_id}) — рестарт бота (FSM в памяти)
не теряет прогресс.

Соединение — `_db._connect()` через атрибут модуля, тот же приём, что у соседних *_db модулей.
"""
from __future__ import annotations

import json
import logging

import aiosqlite

from database import db as _db
from services.infra.timeutil import msk_now

logger = logging.getLogger(__name__)

SCORE_MODES = ("percent", "points")

_QUIZ_PATCH_FIELDS = ("title", "intro", "enabled", "allow_retake", "score_mode")
_LEVEL_PATCH_FIELDS = ("name", "threshold", "description")


def _stamp() -> str:
    return msk_now().strftime("%Y-%m-%d %H:%M:%S")


# ── Схема ────────────────────────────────────────────────────────────────────────────────────

async def ensure_schema(db: aiosqlite.Connection) -> None:
    """Таблицы теста. Зовётся из `database.db.init_db`; коммит делает init_db. Контент не
    сидится."""
    await db.execute(
        "CREATE TABLE IF NOT EXISTS quizzes ("
        "id INTEGER PRIMARY KEY AUTOINCREMENT, city TEXT NOT NULL, "
        "title TEXT NOT NULL DEFAULT '', intro TEXT NOT NULL DEFAULT '', "
        "enabled INTEGER NOT NULL DEFAULT 0, allow_retake INTEGER NOT NULL DEFAULT 0, "
        "score_mode TEXT NOT NULL DEFAULT 'percent', content_version INTEGER NOT NULL DEFAULT 1, "
        "created_at TEXT, updated_at TEXT)"
    )
    await db.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_quizzes_city ON quizzes(city)")
    await db.execute(
        "CREATE TABLE IF NOT EXISTS quiz_questions ("
        "id INTEGER PRIMARY KEY AUTOINCREMENT, quiz_id INTEGER NOT NULL, "
        "position INTEGER NOT NULL DEFAULT 0, text TEXT NOT NULL)"
    )
    await db.execute("CREATE INDEX IF NOT EXISTS idx_quiz_questions_quiz ON quiz_questions(quiz_id)")
    await db.execute(
        "CREATE TABLE IF NOT EXISTS quiz_options ("
        "id INTEGER PRIMARY KEY AUTOINCREMENT, question_id INTEGER NOT NULL, "
        "position INTEGER NOT NULL DEFAULT 0, text TEXT NOT NULL, "
        "points_json TEXT NOT NULL DEFAULT '{}')"
    )
    await db.execute("CREATE INDEX IF NOT EXISTS idx_quiz_options_question ON quiz_options(question_id)")
    await db.execute(
        "CREATE TABLE IF NOT EXISTS quiz_levels ("
        "id INTEGER PRIMARY KEY AUTOINCREMENT, quiz_id INTEGER NOT NULL, "
        "name TEXT NOT NULL, threshold INTEGER NOT NULL, description TEXT NOT NULL DEFAULT '')"
    )
    await db.execute("CREATE INDEX IF NOT EXISTS idx_quiz_levels_quiz ON quiz_levels(quiz_id)")
    await db.execute(
        "CREATE TABLE IF NOT EXISTS quiz_attempts ("
        "id INTEGER PRIMARY KEY AUTOINCREMENT, telegram_id INTEGER NOT NULL, "
        "quiz_id INTEGER NOT NULL, content_version INTEGER NOT NULL, started_at TEXT NOT NULL, "
        "finished_at TEXT, answers_json TEXT NOT NULL DEFAULT '{}', scores_json TEXT)"
    )
    await db.execute("CREATE INDEX IF NOT EXISTS idx_quiz_attempts_quiz ON quiz_attempts(quiz_id)")
    # не больше одной открытой попытки на делегата и тест (двойной тап «Начать тест»);
    # лишние открытые, если они уже есть, убираем — остаётся самая свежая
    await db.execute(
        "DELETE FROM quiz_attempts WHERE finished_at IS NULL AND id NOT IN "
        "(SELECT MAX(id) FROM quiz_attempts WHERE finished_at IS NULL GROUP BY telegram_id, quiz_id)"
    )
    await db.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_quiz_attempts_open "
        "ON quiz_attempts(telegram_id, quiz_id) WHERE finished_at IS NULL"
    )
    await db.execute(
        "CREATE INDEX IF NOT EXISTS idx_quiz_attempts_user ON quiz_attempts(telegram_id, quiz_id)"
    )


# ── Тест ─────────────────────────────────────────────────────────────────────────────────────

async def _one(sql: str, params: tuple) -> dict | None:
    async with _db._connect() as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(sql, params) as cursor:
            row = await cursor.fetchone()
    return dict(row) if row else None


async def _all(sql: str, params: tuple = ()) -> list[dict]:
    async with _db._connect() as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(sql, params) as cursor:
            return [dict(r) for r in await cursor.fetchall()]


async def get_quiz(quiz_id: int) -> dict | None:
    return await _one("SELECT * FROM quizzes WHERE id = ?", (int(quiz_id),))


async def get_quiz_for_city(city: str) -> dict | None:
    return await _one("SELECT * FROM quizzes WHERE city = ?", (city,))


async def get_or_create_quiz(city: str) -> dict:
    """Тест города; нет — создаётся выключенным и пустым."""
    async with _db._connect() as db:
        stamp = _stamp()
        await db.execute(
            "INSERT OR IGNORE INTO quizzes (city, created_at, updated_at) VALUES (?, ?, ?)",
            (city, stamp, stamp),
        )
        await db.commit()
    return await get_quiz_for_city(city)  # type: ignore[return-value]


async def update_quiz(quiz_id: int, **fields) -> bool:
    keys = [k for k in fields if k in _QUIZ_PATCH_FIELDS]
    if not keys:
        return False
    if "score_mode" in keys and fields["score_mode"] not in SCORE_MODES:
        return False
    sets = [f"{k} = ?" for k in keys] + ["updated_at = ?"]
    values = [fields[k] for k in keys] + [_stamp(), int(quiz_id)]
    async with _db._connect() as db:
        cursor = await db.execute(f"UPDATE quizzes SET {', '.join(sets)} WHERE id = ?", values)
        await db.commit()
        return bool(cursor.rowcount)


async def _bump(conn: aiosqlite.Connection, quiz_id: int) -> None:
    await conn.execute(
        "UPDATE quizzes SET content_version = content_version + 1, updated_at = ? WHERE id = ?",
        (_stamp(), int(quiz_id)),
    )


async def bump_content_version(quiz_id: int) -> int:
    async with _db._connect() as db:
        await _bump(db, quiz_id)
        await db.commit()
        async with db.execute(
            "SELECT content_version FROM quizzes WHERE id = ?", (int(quiz_id),),
        ) as cursor:
            row = await cursor.fetchone()
    return int(row[0]) if row else 0


async def count_questions(quiz_id: int) -> int:
    async with _db._connect() as db:
        async with db.execute(
            "SELECT COUNT(*) FROM quiz_questions WHERE quiz_id = ?", (int(quiz_id),),
        ) as cursor:
            return int((await cursor.fetchone())[0])


async def active_quiz_for_city(city: str) -> dict | None:
    """Тест, который можно проходить: включён и в нём есть хотя бы один вопрос."""
    quiz = await get_quiz_for_city(city)
    if not quiz or not quiz["enabled"] or not await count_questions(quiz["id"]):
        return None
    if await first_question_without_options(quiz["id"]) is not None:
        return None  # вопрос без вариантов нельзя ни пройти, ни пропустить
    return quiz


async def first_question_without_options(quiz_id: int) -> tuple[int, dict] | None:
    """(номер, вопрос) первого вопроса без единого варианта ответа; None — все с вариантами."""
    options = await list_options_for_quiz(quiz_id)
    for number, question in enumerate(await list_questions(quiz_id), start=1):
        if not options.get(question["id"]):
            return number, question
    return None


# ── Вопросы ──────────────────────────────────────────────────────────────────────────────────

async def list_questions(quiz_id: int) -> list[dict]:
    return await _all(
        "SELECT * FROM quiz_questions WHERE quiz_id = ? ORDER BY position, id", (int(quiz_id),),
    )


async def get_question(qid: int) -> dict | None:
    return await _one("SELECT * FROM quiz_questions WHERE id = ?", (int(qid),))


async def create_question(quiz_id: int, text: str) -> int:
    async with _db._connect() as db:
        async with db.execute(
            "SELECT COALESCE(MAX(position), 0) + 1 FROM quiz_questions WHERE quiz_id = ?",
            (int(quiz_id),),
        ) as cursor:
            pos = int((await cursor.fetchone())[0])
        cursor = await db.execute(
            "INSERT INTO quiz_questions (quiz_id, position, text) VALUES (?, ?, ?)",
            (int(quiz_id), pos, text.strip()),
        )
        await _bump(db, quiz_id)
        await db.commit()
        return int(cursor.lastrowid)


async def update_question_text(qid: int, text: str) -> bool:
    q = await get_question(qid)
    if not q:
        return False
    async with _db._connect() as db:
        await db.execute("UPDATE quiz_questions SET text = ? WHERE id = ?", (text.strip(), int(qid)))
        await _bump(db, q["quiz_id"])
        await db.commit()
    return True


async def move_question(qid: int, delta: int) -> bool:
    q = await get_question(qid)
    if not q:
        return False
    ids = [x["id"] for x in await list_questions(q["quiz_id"])]
    idx = ids.index(int(qid))
    new = idx + (1 if delta > 0 else -1)
    if new < 0 or new >= len(ids):
        return False
    ids[idx], ids[new] = ids[new], ids[idx]
    async with _db._connect() as db:
        for pos, rid in enumerate(ids, start=1):
            await db.execute("UPDATE quiz_questions SET position = ? WHERE id = ?", (pos, rid))
        await _bump(db, q["quiz_id"])
        await db.commit()
    return True


async def delete_question(qid: int) -> bool:
    q = await get_question(qid)
    if not q:
        return False
    async with _db._connect() as db:
        await db.execute("DELETE FROM quiz_options WHERE question_id = ?", (int(qid),))
        await db.execute("DELETE FROM quiz_questions WHERE id = ?", (int(qid),))
        await _bump(db, q["quiz_id"])
        await db.commit()
    return True


# ── Варианты ─────────────────────────────────────────────────────────────────────────────────

def _parse_points(raw: str | None) -> dict[int, int]:
    try:
        data = json.loads(raw or "{}")
    except ValueError:
        return {}
    out: dict[int, int] = {}
    for k, v in (data or {}).items():
        try:
            out[int(k)] = int(v)
        except (TypeError, ValueError):
            continue
    return out


def _option(row: dict) -> dict:
    row = dict(row)
    row["points"] = _parse_points(row.pop("points_json", None))
    return row


async def list_options(question_id: int) -> list[dict]:
    rows = await _all(
        "SELECT * FROM quiz_options WHERE question_id = ? ORDER BY position, id",
        (int(question_id),),
    )
    return [_option(r) for r in rows]


async def list_options_for_quiz(quiz_id: int) -> dict[int, list[dict]]:
    rows = await _all(
        "SELECT o.* FROM quiz_options o JOIN quiz_questions q ON q.id = o.question_id "
        "WHERE q.quiz_id = ? ORDER BY o.position, o.id", (int(quiz_id),),
    )
    out: dict[int, list[dict]] = {q["id"]: [] for q in await list_questions(quiz_id)}
    for r in rows:
        out.setdefault(r["question_id"], []).append(_option(r))
    return out


async def get_option(oid: int) -> dict | None:
    row = await _one("SELECT * FROM quiz_options WHERE id = ?", (int(oid),))
    return _option(row) if row else None


async def _quiz_id_of_question(conn: aiosqlite.Connection, qid: int) -> int | None:
    async with conn.execute("SELECT quiz_id FROM quiz_questions WHERE id = ?", (int(qid),)) as cur:
        row = await cur.fetchone()
    return int(row[0]) if row else None


async def create_option(question_id: int, text: str) -> int:
    async with _db._connect() as db:
        quiz_id = await _quiz_id_of_question(db, question_id)
        async with db.execute(
            "SELECT COALESCE(MAX(position), 0) + 1 FROM quiz_options WHERE question_id = ?",
            (int(question_id),),
        ) as cursor:
            pos = int((await cursor.fetchone())[0])
        cursor = await db.execute(
            "INSERT INTO quiz_options (question_id, position, text) VALUES (?, ?, ?)",
            (int(question_id), pos, text.strip()),
        )
        if quiz_id is not None:
            await _bump(db, quiz_id)
        await db.commit()
        return int(cursor.lastrowid)


async def _quiz_id_of_option(conn: aiosqlite.Connection, oid: int) -> int | None:
    async with conn.execute(
        "SELECT q.quiz_id FROM quiz_options o JOIN quiz_questions q ON q.id = o.question_id "
        "WHERE o.id = ?", (int(oid),),
    ) as cur:
        row = await cur.fetchone()
    return int(row[0]) if row else None


async def update_option_text(oid: int, text: str) -> bool:
    async with _db._connect() as db:
        quiz_id = await _quiz_id_of_option(db, oid)
        if quiz_id is None:
            return False
        await db.execute("UPDATE quiz_options SET text = ? WHERE id = ?", (text.strip(), int(oid)))
        await _bump(db, quiz_id)
        await db.commit()
    return True


async def set_option_points(oid: int, competency_id: int, points: int) -> bool:
    """Баллы варианта по компетенции; 0 убирает компетенцию из варианта."""
    async with _db._connect() as db:
        quiz_id = await _quiz_id_of_option(db, oid)
        if quiz_id is None:
            return False
        async with db.execute("SELECT points_json FROM quiz_options WHERE id = ?", (int(oid),)) as cur:
            current = _parse_points((await cur.fetchone())[0])
        if int(points) == 0:
            current.pop(int(competency_id), None)
        else:
            current[int(competency_id)] = int(points)
        await db.execute(
            "UPDATE quiz_options SET points_json = ? WHERE id = ?",
            (json.dumps({str(k): v for k, v in current.items()}), int(oid)),
        )
        await _bump(db, quiz_id)
        await db.commit()
    return True


async def delete_option(oid: int) -> bool:
    async with _db._connect() as db:
        quiz_id = await _quiz_id_of_option(db, oid)
        if quiz_id is None:
            return False
        await db.execute("DELETE FROM quiz_options WHERE id = ?", (int(oid),))
        await _bump(db, quiz_id)
        await db.commit()
    return True


# ── Уровни ───────────────────────────────────────────────────────────────────────────────────

async def list_levels(quiz_id: int) -> list[dict]:
    return await _all(
        "SELECT * FROM quiz_levels WHERE quiz_id = ? ORDER BY threshold, id", (int(quiz_id),),
    )


async def get_level(lid: int) -> dict | None:
    return await _one("SELECT * FROM quiz_levels WHERE id = ?", (int(lid),))


async def create_level(quiz_id: int, name: str, threshold: int, description: str = "") -> int:
    async with _db._connect() as db:
        cursor = await db.execute(
            "INSERT INTO quiz_levels (quiz_id, name, threshold, description) VALUES (?, ?, ?, ?)",
            (int(quiz_id), name.strip(), int(threshold), description.strip()),
        )
        await db.commit()
        return int(cursor.lastrowid)


async def update_level(lid: int, **fields) -> bool:
    keys = [k for k in fields if k in _LEVEL_PATCH_FIELDS]
    if not keys:
        return False
    sets = [f"{k} = ?" for k in keys]
    values = [fields[k] for k in keys] + [int(lid)]
    async with _db._connect() as db:
        cursor = await db.execute(f"UPDATE quiz_levels SET {', '.join(sets)} WHERE id = ?", values)
        await db.commit()
        return bool(cursor.rowcount)


async def delete_level(lid: int) -> bool:
    async with _db._connect() as db:
        cursor = await db.execute("DELETE FROM quiz_levels WHERE id = ?", (int(lid),))
        await db.commit()
        return bool(cursor.rowcount)


# ── Замена контента ──────────────────────────────────────────────────────────────────────────

async def replace_content(quiz_id: int, questions: list[dict]) -> int:
    """Заменяет вопросы и варианты теста одной транзакцией (импорт из файла/вставка).
    questions: [{text, options: [{text, points: {competency_id: баллы}}]}]. Уровни и попытки
    не трогаются. Возвращает новую content_version."""
    async with _db._connect() as db:
        await db.execute("BEGIN IMMEDIATE")
        try:
            await db.execute(
                "DELETE FROM quiz_options WHERE question_id IN "
                "(SELECT id FROM quiz_questions WHERE quiz_id = ?)", (int(quiz_id),),
            )
            await db.execute("DELETE FROM quiz_questions WHERE quiz_id = ?", (int(quiz_id),))
            for qpos, q in enumerate(questions, start=1):
                cursor = await db.execute(
                    "INSERT INTO quiz_questions (quiz_id, position, text) VALUES (?, ?, ?)",
                    (int(quiz_id), qpos, str(q["text"]).strip()),
                )
                qid = cursor.lastrowid
                for opos, o in enumerate(q.get("options") or [], start=1):
                    pts = {str(int(k)): int(v) for k, v in (o.get("points") or {}).items() if int(v)}
                    await db.execute(
                        "INSERT INTO quiz_options (question_id, position, text, points_json) "
                        "VALUES (?, ?, ?, ?)",
                        (qid, opos, str(o["text"]).strip(), json.dumps(pts)),
                    )
            await _bump(db, quiz_id)
            await db.commit()
        except Exception:
            await db.rollback()
            raise
        async with db.execute(
            "SELECT content_version FROM quizzes WHERE id = ?", (int(quiz_id),),
        ) as cursor:
            row = await cursor.fetchone()
    return int(row[0]) if row else 0


# ── Попытки ──────────────────────────────────────────────────────────────────────────────────

def _int_keys(raw: str | None) -> dict:
    try:
        data = json.loads(raw or "{}")
    except ValueError:
        return {}
    out: dict[int, int] = {}
    for k, v in (data or {}).items():
        try:
            out[int(k)] = v if isinstance(v, (int, float, dict)) else int(v)
        except (TypeError, ValueError):
            continue
    return out


def _attempt(row: dict | None) -> dict | None:
    if not row:
        return None
    row = dict(row)
    row["answers"] = _int_keys(row.pop("answers_json", None))
    scores_raw = row.pop("scores_json", None)
    row["scores"] = _int_keys(scores_raw) if scores_raw is not None else None
    return row


async def create_attempt(telegram_id: int, quiz_id: int, content_version: int) -> int:
    async with _db._connect() as db:
        cursor = await db.execute(
            "INSERT OR IGNORE INTO quiz_attempts (telegram_id, quiz_id, content_version, started_at) "
            "VALUES (?, ?, ?, ?)", (int(telegram_id), int(quiz_id), int(content_version), _stamp()),
        )
        await db.commit()
        if cursor.rowcount:
            return int(cursor.lastrowid)
    # параллельный старт: открытая попытка уже есть — отдаём её, а не создаём вторую
    existing = await get_open_attempt(telegram_id, quiz_id)
    return int(existing["id"]) if existing else 0


async def get_attempt(attempt_id: int) -> dict | None:
    return _attempt(await _one("SELECT * FROM quiz_attempts WHERE id = ?", (int(attempt_id),)))


async def get_open_attempt(telegram_id: int, quiz_id: int) -> dict | None:
    return _attempt(await _one(
        "SELECT * FROM quiz_attempts WHERE telegram_id = ? AND quiz_id = ? "
        "AND finished_at IS NULL ORDER BY id DESC LIMIT 1", (int(telegram_id), int(quiz_id)),
    ))


async def get_last_finished_attempt(telegram_id: int, quiz_id: int) -> dict | None:
    return _attempt(await _one(
        "SELECT * FROM quiz_attempts WHERE telegram_id = ? AND quiz_id = ? "
        "AND finished_at IS NOT NULL ORDER BY id DESC LIMIT 1", (int(telegram_id), int(quiz_id)),
    ))


async def record_answer(attempt_id: int, question_id: int, option_id: int) -> bool:
    """Сохраняет ответ (повторный ответ на вопрос перезаписывает прежний). False — попытка
    уже закрыта или не найдена."""
    async with _db._connect() as db:
        await db.execute("BEGIN IMMEDIATE")
        try:
            async with db.execute(
                "SELECT answers_json, finished_at FROM quiz_attempts WHERE id = ?",
                (int(attempt_id),),
            ) as cursor:
                row = await cursor.fetchone()
            if row is None or row[1] is not None:
                await db.rollback()
                return False
            answers = _int_keys(row[0])
            answers[int(question_id)] = int(option_id)
            await db.execute(
                "UPDATE quiz_attempts SET answers_json = ? WHERE id = ?",
                (json.dumps({str(k): v for k, v in answers.items()}), int(attempt_id)),
            )
            await db.commit()
        except Exception:
            await db.rollback()
            raise
    return True


async def finish_attempt(attempt_id: int, scores: dict) -> bool:
    """Закрывает попытку с итоговыми баллами {competency_id: баллы}. False — уже закрыта."""
    async with _db._connect() as db:
        cursor = await db.execute(
            "UPDATE quiz_attempts SET finished_at = ?, scores_json = ? "
            "WHERE id = ? AND finished_at IS NULL",
            (_stamp(), json.dumps({str(k): v for k, v in scores.items()}), int(attempt_id)),
        )
        await db.commit()
        return bool(cursor.rowcount)


async def delete_open_attempts(telegram_id: int, quiz_id: int) -> int:
    async with _db._connect() as db:
        cursor = await db.execute(
            "DELETE FROM quiz_attempts WHERE telegram_id = ? AND quiz_id = ? "
            "AND finished_at IS NULL", (int(telegram_id), int(quiz_id)),
        )
        await db.commit()
        return int(cursor.rowcount or 0)


async def count_open_attempts(quiz_id: int) -> int:
    async with _db._connect() as db:
        async with db.execute(
            "SELECT COUNT(*) FROM quiz_attempts WHERE quiz_id = ? AND finished_at IS NULL",
            (int(quiz_id),),
        ) as cursor:
            return int((await cursor.fetchone())[0])


async def attempt_counts(quiz_id: int) -> dict:
    """{started, finished} — уникальные люди: начали (любая попытка) и закончили."""
    async with _db._connect() as db:
        async with db.execute(
            "SELECT COUNT(DISTINCT telegram_id), "
            "COUNT(DISTINCT CASE WHEN finished_at IS NOT NULL THEN telegram_id END) "
            "FROM quiz_attempts WHERE quiz_id = ?", (int(quiz_id),),
        ) as cursor:
            row = await cursor.fetchone()
    return {"started": int(row[0] or 0), "finished": int(row[1] or 0)}


async def list_finished_scores(quiz_id: int) -> list[dict]:
    """Последняя законченная попытка каждого: [{telegram_id, attempt_id, content_version,
    finished_at, scores}]."""
    rows = await _all(
        "SELECT a.* FROM quiz_attempts a WHERE a.quiz_id = ? AND a.finished_at IS NOT NULL "
        "AND a.id = (SELECT MAX(b.id) FROM quiz_attempts b WHERE b.telegram_id = a.telegram_id "
        "AND b.quiz_id = a.quiz_id AND b.finished_at IS NOT NULL) ORDER BY a.telegram_id",
        (int(quiz_id),),
    )
    out = []
    for r in rows:
        att = _attempt(r)
        out.append({
            "telegram_id": att["telegram_id"], "attempt_id": att["id"],
            "content_version": att["content_version"], "finished_at": att["finished_at"],
            "scores": att["scores"] or {},
        })
    return out
