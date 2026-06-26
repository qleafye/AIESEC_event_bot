import logging
import os
from datetime import datetime

import aiosqlite
from config import config

logger = logging.getLogger(__name__)


async def _column_exists(db: aiosqlite.Connection, table_name: str, column_name: str) -> bool:
    async with db.execute(f"PRAGMA table_info({table_name})") as cursor:
        rows = await cursor.fetchall()
    return any(row[1] == column_name for row in rows)


async def _ensure_column(db: aiosqlite.Connection, table_name: str, column_name: str, definition: str):
    if not await _column_exists(db, table_name, column_name):
        await db.execute(f"ALTER TABLE {table_name} ADD COLUMN {column_name} {definition}")

async def init_db():
    async with aiosqlite.connect(config.DB_PATH) as db:
        await db.execute('''
            CREATE TABLE IF NOT EXISTS users (
                telegram_id INTEGER PRIMARY KEY,
                username TEXT,
                full_name TEXT,
                email TEXT,
                age INTEGER,
                is_aiesec_member BOOLEAN,
                source TEXT,
                source_details TEXT,
                education_status TEXT,
                university TEXT,
                course TEXT,
                specialty TEXT,
                work_status BOOLEAN,
                work_sphere TEXT,
                missing_skills TEXT,
                expectations TEXT,
                phone TEXT,
                city TEXT,
                referrer_id INTEGER,
                registration_date TEXT,
                is_ambassador_candidate BOOLEAN DEFAULT 0
            )
        ''')

        await _ensure_column(db, "users", "phone", "TEXT")
        await _ensure_column(db, "users", "city", "TEXT")
        await _ensure_column(db, "users", "referrer_id", "INTEGER")
        await _ensure_column(db, "users", "local_committee", "TEXT")
        await _ensure_column(db, "users", "position", "TEXT")
        await _ensure_column(db, "users", "attendance_format", "TEXT")
        await _ensure_column(db, "users", "comments", "TEXT")
        await _ensure_column(db, "users", "expectations_ar", "TEXT")
        await _ensure_column(db, "users", "informal_day", "TEXT")

        # Phase 1 migrations (additive, idempotent — safe against ~590 live users)
        await _ensure_column(db, "users", "status", "TEXT DEFAULT 'approved'")
        await _ensure_column(db, "users", "resume_file_id", "TEXT")
        await _ensure_column(db, "users", "subscribed", "INTEGER")

        # Conference (RusCo) reg-flow fields — additive, default-off questions
        await _ensure_column(db, "users", "department", "TEXT")
        await _ensure_column(db, "users", "aiesec_role", "TEXT")
        await _ensure_column(db, "users", "needs_certificate", "TEXT")
        await _ensure_column(db, "users", "english_level", "TEXT")
        await _ensure_column(db, "users", "allergies", "TEXT")
        await _ensure_column(db, "users", "food_pref", "TEXT")
        await _ensure_column(db, "users", "arrival", "TEXT")
        await _ensure_column(db, "users", "housing", "TEXT")
        await _ensure_column(db, "users", "cc_shop", "TEXT")
        await _ensure_column(db, "users", "exp_organizers", "TEXT")
        await _ensure_column(db, "users", "exp_content", "TEXT")
        await _ensure_column(db, "users", "volunteer", "TEXT")

        await db.execute('''
            CREATE TABLE IF NOT EXISTS bot_settings (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            )
        ''')

        # Phase 1: append-only coins ledger (balance = SUM(delta), never UPDATE)
        await db.execute('''
            CREATE TABLE IF NOT EXISTS coins (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                delta INTEGER NOT NULL,
                reason TEXT,
                changed_by INTEGER,
                timestamp TEXT NOT NULL
            )
        ''')
        await db.execute('CREATE INDEX IF NOT EXISTS idx_coins_user ON coins(user_id)')

        # Phase 1: persistent dropout tracking (survives restart, independent of FSM)
        await db.execute('''
            CREATE TABLE IF NOT EXISTS reg_started (
                telegram_id INTEGER PRIMARY KEY,
                username TEXT,
                started_at TEXT NOT NULL
            )
        ''')

        await db.commit()

async def get_setting(key: str) -> str | None:
    async with aiosqlite.connect(config.DB_PATH) as db:
        async with db.execute(
            "SELECT value FROM bot_settings WHERE key = ?", (key,)
        ) as cursor:
            row = await cursor.fetchone()
            return row[0] if row else None


async def set_setting(key: str, value: str):
    async with aiosqlite.connect(config.DB_PATH) as db:
        await db.execute(
            "INSERT OR REPLACE INTO bot_settings (key, value) VALUES (?, ?)",
            (key, value),
        )
        await db.commit()


async def delete_setting(key: str):
    async with aiosqlite.connect(config.DB_PATH) as db:
        await db.execute("DELETE FROM bot_settings WHERE key = ?", (key,))
        await db.commit()


async def add_user(data: dict):
    async with aiosqlite.connect(config.DB_PATH) as db:
        # ON CONFLICT DO UPDATE (not INSERT OR REPLACE): REPLACE is DELETE+INSERT and would
        # wipe columns absent from this list (e.g. status) on re-registration. status is
        # owned by the approval flow + migration default only — never touched here.
        await db.execute('''
            INSERT INTO users (
                telegram_id, username, full_name, email, age,
                is_aiesec_member, source, source_details,
                education_status, university, course, specialty,
                work_status, work_sphere,
                missing_skills, expectations, phone, city,
                referrer_id, registration_date,
                is_ambassador_candidate,
                local_committee, position, attendance_format,
                comments, expectations_ar, informal_day, resume_file_id,
                department, aiesec_role, needs_certificate, english_level,
                allergies, food_pref, arrival, housing, cc_shop,
                exp_organizers, exp_content, volunteer
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(telegram_id) DO UPDATE SET
                username=excluded.username,
                full_name=excluded.full_name,
                email=excluded.email,
                age=excluded.age,
                is_aiesec_member=excluded.is_aiesec_member,
                source=excluded.source,
                source_details=excluded.source_details,
                education_status=excluded.education_status,
                university=excluded.university,
                course=excluded.course,
                specialty=excluded.specialty,
                work_status=excluded.work_status,
                work_sphere=excluded.work_sphere,
                missing_skills=excluded.missing_skills,
                expectations=excluded.expectations,
                phone=excluded.phone,
                city=excluded.city,
                referrer_id=excluded.referrer_id,
                registration_date=excluded.registration_date,
                is_ambassador_candidate=excluded.is_ambassador_candidate,
                local_committee=excluded.local_committee,
                position=excluded.position,
                attendance_format=excluded.attendance_format,
                comments=excluded.comments,
                expectations_ar=excluded.expectations_ar,
                informal_day=excluded.informal_day,
                resume_file_id=COALESCE(excluded.resume_file_id, users.resume_file_id),
                department=excluded.department,
                aiesec_role=excluded.aiesec_role,
                needs_certificate=excluded.needs_certificate,
                english_level=excluded.english_level,
                allergies=excluded.allergies,
                food_pref=excluded.food_pref,
                arrival=excluded.arrival,
                housing=excluded.housing,
                cc_shop=excluded.cc_shop,
                exp_organizers=excluded.exp_organizers,
                exp_content=excluded.exp_content,
                volunteer=excluded.volunteer
        ''', (
            data['telegram_id'],
            data.get('username'),
            data.get('full_name', ''),
            data.get('email', '-'),
            data.get('age'),
            data.get('is_aiesec_member', False),
            data.get('source', '-'),
            data.get('source_details'),
            data.get('education_status', '-'),
            data.get('university'),
            data.get('course'),
            data.get('specialty'),
            data.get('work_status', False),
            data.get('work_sphere'),
            data.get('missing_skills', '-'),
            data.get('expectations', '-'),
            data.get('phone'),
            data.get('city'),
            data.get('referrer_id'),
            data['registration_date'],
            data.get('is_ambassador_candidate', False),
            data.get('local_committee'),
            data.get('position'),
            data.get('attendance_format'),
            data.get('comments'),
            data.get('expectations_ar'),
            data.get('informal_day'),
            data.get('resume_file_id'),
            data.get('department'),
            data.get('aiesec_role'),
            data.get('needs_certificate'),
            data.get('english_level'),
            data.get('allergies'),
            data.get('food_pref'),
            data.get('arrival'),
            data.get('housing'),
            data.get('cc_shop'),
            data.get('exp_organizers'),
            data.get('exp_content'),
            data.get('volunteer'),
        ))
        await db.commit()

async def get_user(telegram_id: int):
    db_path = os.path.abspath(config.DB_PATH)
    async with aiosqlite.connect(config.DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute('SELECT * FROM users WHERE telegram_id = ?', (telegram_id,)) as cursor:
            row = await cursor.fetchone()
            if row:
                return dict(row)
            logger.info(f"get_user: {telegram_id} not found in {db_path}")
            return None

async def get_user_by_username(username: str):
    async with aiosqlite.connect(config.DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        if not username.startswith('@'):
            username = f"@{username}"

        async with db.execute('SELECT * FROM users WHERE username = ? COLLATE NOCASE', (username,)) as cursor:
            row = await cursor.fetchone()
            if row:
                return dict(row)
            return None

async def get_referrals(telegram_id: int) -> list[str]:
    async with aiosqlite.connect(config.DB_PATH) as db:
        async with db.execute(
            "SELECT full_name FROM users WHERE referrer_id = ?", (telegram_id,)
        ) as cursor:
            return [row[0] for row in await cursor.fetchall()]


async def get_all_users_ids():
    async with aiosqlite.connect(config.DB_PATH) as db:
        async with db.execute('SELECT telegram_id FROM users') as cursor:
            return [row[0] for row in await cursor.fetchall()]


async def get_all_users_dicts() -> list[dict]:
    async with aiosqlite.connect(config.DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute('SELECT * FROM users ORDER BY registration_date') as cursor:
            return [dict(row) for row in await cursor.fetchall()]

async def get_stats():
    async with aiosqlite.connect(config.DB_PATH) as db:
        async with db.execute('SELECT COUNT(*) FROM users') as cursor:
            total = (await cursor.fetchone())[0]

        async with db.execute('''
            SELECT university, COUNT(*) as cnt
            FROM users
            WHERE university IS NOT NULL AND TRIM(university) != '' AND university != '-'
            GROUP BY university
            ORDER BY cnt DESC
            LIMIT 3
        ''') as cursor:
            top_universities = await cursor.fetchall()

    return total, top_universities


async def get_monthly_registration_stats():
    async with aiosqlite.connect(config.DB_PATH) as db:
        async with db.execute('''
            SELECT substr(registration_date, 1, 7) as month, COUNT(*) as cnt
            FROM users
            WHERE registration_date IS NOT NULL AND TRIM(registration_date) != ''
            GROUP BY month
            ORDER BY month DESC
        ''') as cursor:
            return await cursor.fetchall()

async def get_source_stats():
    async with aiosqlite.connect(config.DB_PATH) as db:
        async with db.execute('''
            SELECT source, COUNT(*) as cnt
            FROM users
            WHERE source IS NOT NULL AND TRIM(source) != '' AND source != '-'
            GROUP BY source
            ORDER BY cnt DESC
        ''') as cursor:
            return await cursor.fetchall()


async def export_users_csv():
    async with aiosqlite.connect(config.DB_PATH) as db:
        async with db.execute('SELECT * FROM users') as cursor:
            headers = [description[0] for description in cursor.description]
            rows = await cursor.fetchall()
            if "phone" in headers:
                phone_index = headers.index("phone")
                headers = [header for header in headers if header != "phone"]
                rows = [tuple(value for index, value in enumerate(row) if index != phone_index) for row in rows]
            return headers, rows


# ── Phase 1: coins ledger (append-only) ──────────────────────────────────────

async def add_coins(user_id: int, delta: int, reason: str | None = None, changed_by: int | None = None):
    """Append a ledger row. Never UPDATE — balance is the derived SUM(delta)."""
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    async with aiosqlite.connect(config.DB_PATH) as db:
        await db.execute(
            "INSERT INTO coins (user_id, delta, reason, changed_by, timestamp) VALUES (?, ?, ?, ?, ?)",
            (user_id, delta, reason, changed_by, timestamp),
        )
        await db.commit()


async def get_balance(user_id: int) -> int:
    async with aiosqlite.connect(config.DB_PATH) as db:
        async with db.execute(
            "SELECT COALESCE(SUM(delta), 0) FROM coins WHERE user_id = ?", (user_id,)
        ) as cursor:
            row = await cursor.fetchone()
            return int(row[0]) if row else 0


async def get_leaderboard(limit: int = 10) -> list[dict]:
    """Top users by summed balance, joined to users for display name."""
    async with aiosqlite.connect(config.DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute('''
            SELECT c.user_id AS user_id,
                   COALESCE(SUM(c.delta), 0) AS balance,
                   u.full_name AS full_name,
                   u.username AS username
            FROM coins c
            LEFT JOIN users u ON u.telegram_id = c.user_id
            GROUP BY c.user_id
            ORDER BY balance DESC
            LIMIT ?
        ''', (limit,)) as cursor:
            return [dict(row) for row in await cursor.fetchall()]


async def get_user_rank(user_id: int) -> int | None:
    """1-based rank by summed balance; None if the user has no ledger rows."""
    async with aiosqlite.connect(config.DB_PATH) as db:
        async with db.execute(
            "SELECT COALESCE(SUM(delta), 0) FROM coins WHERE user_id = ?", (user_id,)
        ) as cursor:
            row = await cursor.fetchone()
            if row is None or row[0] is None:
                return None
            my_balance = int(row[0])
        async with db.execute('''
            SELECT COUNT(*) FROM (
                SELECT user_id, SUM(delta) AS bal
                FROM coins
                GROUP BY user_id
                HAVING bal > ?
            )
        ''', (my_balance,)) as cursor:
            greater = (await cursor.fetchone())[0]
        # confirm the user actually has rows in the ledger
        async with db.execute(
            "SELECT 1 FROM coins WHERE user_id = ? LIMIT 1", (user_id,)
        ) as cursor:
            if await cursor.fetchone() is None:
                return None
        return greater + 1


# ── Phase 1: reg_started dropout tracking (independent of FSM) ────────────────

async def mark_reg_started(telegram_id: int, username: str | None):
    started_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    async with aiosqlite.connect(config.DB_PATH) as db:
        await db.execute('''
            INSERT INTO reg_started (telegram_id, username, started_at)
            VALUES (?, ?, ?)
            ON CONFLICT(telegram_id) DO UPDATE SET
                username=excluded.username,
                started_at=excluded.started_at
        ''', (telegram_id, username, started_at))
        await db.commit()


async def clear_reg_started(telegram_id: int):
    async with aiosqlite.connect(config.DB_PATH) as db:
        await db.execute("DELETE FROM reg_started WHERE telegram_id = ?", (telegram_id,))
        await db.commit()


async def get_incomplete_user_ids() -> list[int]:
    async with aiosqlite.connect(config.DB_PATH) as db:
        async with db.execute("SELECT telegram_id FROM reg_started") as cursor:
            return [row[0] for row in await cursor.fetchall()]


# ── Phase 1: subscription flag ───────────────────────────────────────────────

async def set_user_subscribed(telegram_id: int, subscribed: bool):
    async with aiosqlite.connect(config.DB_PATH) as db:
        await db.execute(
            "UPDATE users SET subscribed = ? WHERE telegram_id = ?",
            (1 if subscribed else 0, telegram_id),
        )
        await db.commit()


async def get_non_subscriber_ids() -> list[int]:
    async with aiosqlite.connect(config.DB_PATH) as db:
        async with db.execute(
            "SELECT telegram_id FROM users WHERE subscribed = 0"
        ) as cursor:
            return [row[0] for row in await cursor.fetchall()]


# ── Phase 2: approval flow ───────────────────────────────────────────────────

async def set_user_status(telegram_id: int, status: str):
    """Set one user's approval status. Used after add_user to land pending/approved."""
    async with aiosqlite.connect(config.DB_PATH) as db:
        await db.execute(
            "UPDATE users SET status = ? WHERE telegram_id = ?",
            (status, telegram_id),
        )
        await db.commit()


async def approve_user_atomic(telegram_id: int) -> bool:
    """Atomically approve one pending user. True iff this call flipped the row
    (rowcount==1) — a concurrent second approve returns False (no double approval)."""
    async with aiosqlite.connect(config.DB_PATH) as db:
        cursor = await db.execute(
            "UPDATE users SET status = 'approved' WHERE telegram_id = ? AND status = 'pending'",
            (telegram_id,),
        )
        await db.commit()
        return cursor.rowcount == 1


async def reject_user(telegram_id: int) -> bool:
    """Atomically reject one pending user. True iff one row flipped."""
    async with aiosqlite.connect(config.DB_PATH) as db:
        cursor = await db.execute(
            "UPDATE users SET status = 'rejected' WHERE telegram_id = ? AND status = 'pending'",
            (telegram_id,),
        )
        await db.commit()
        return cursor.rowcount == 1


async def get_pending_users(limit: int = 1, offset: int = 0) -> list[dict]:
    """Pending applications, oldest first (registration_date then telegram_id)."""
    async with aiosqlite.connect(config.DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM users WHERE status = 'pending' "
            "ORDER BY registration_date ASC, telegram_id ASC LIMIT ? OFFSET ?",
            (limit, offset),
        ) as cursor:
            return [dict(row) for row in await cursor.fetchall()]


async def get_pending_count() -> int:
    async with aiosqlite.connect(config.DB_PATH) as db:
        async with db.execute(
            "SELECT COUNT(*) FROM users WHERE status = 'pending'"
        ) as cursor:
            row = await cursor.fetchone()
            return int(row[0]) if row and row[0] is not None else 0


async def approve_all_pending() -> list[int]:
    """Flip every pending row to approved in one atomic statement; return the
    telegram_ids that flipped (each once). RETURNING requires sqlite >= 3.35
    (bundled in CPython 3.10+). Older sqlite fallback: BEGIN IMMEDIATE; SELECT
    ids WHERE pending; UPDATE WHERE pending; COMMIT (the IMMEDIATE lock makes the
    snapshot atomic)."""
    async with aiosqlite.connect(config.DB_PATH) as db:
        async with db.execute(
            "UPDATE users SET status = 'approved' WHERE status = 'pending' RETURNING telegram_id"
        ) as cursor:
            rows = await cursor.fetchall()
        await db.commit()
        return [row[0] for row in rows]
