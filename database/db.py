import logging
import os
import re
from datetime import datetime

import aiosqlite
from config import config

logger = logging.getLogger(__name__)

# WR-08: SQLite can't bind identifiers (table/column names), so migrations interpolate them
# into the SQL string. Every current caller passes a hardcoded literal, so there's no
# injection path today — this guard makes any FUTURE dynamic/user-derived identifier fail
# loudly instead of silently opening an injection primitive.
_IDENTIFIER_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def _assert_identifier(name: str) -> str:
    if not _IDENTIFIER_RE.fullmatch(name or ""):
        raise ValueError(f"Unsafe SQL identifier: {name!r}")
    return name


async def _column_exists(db: aiosqlite.Connection, table_name: str, column_name: str) -> bool:
    _assert_identifier(table_name)
    async with db.execute(f"PRAGMA table_info({table_name})") as cursor:
        rows = await cursor.fetchall()
    return any(row[1] == column_name for row in rows)


async def _ensure_column(db: aiosqlite.Connection, table_name: str, column_name: str, definition: str):
    _assert_identifier(table_name)
    _assert_identifier(column_name)
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
        await _ensure_column(db, "users", "resume_text", "TEXT")  # резюме текстом (альтернатива файлу)
        await _ensure_column(db, "users", "resume_url", "TEXT")  # Nextcloud share link на файл-резюме
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

        # Phase 8 (ROLE-02, D-11): staff roster -- who holds which role, audited (added_by/
        # added_at). Composite PRIMARY KEY naturally supports multi-role (D-08, one row per
        # role held) and rejects a duplicate (telegram_id, role) pair without extra machinery.
        await db.execute('''
            CREATE TABLE IF NOT EXISTS staff (
                telegram_id INTEGER NOT NULL,
                role TEXT NOT NULL,
                added_by INTEGER,
                added_at TEXT NOT NULL,
                PRIMARY KEY (telegram_id, role)
            )
        ''')
        # Phase 09.1 (C, ROLE-03): manager <-> city binding. NULL = all cities (same semantics
        # as every other event_city column) -- every pre-existing row keeps working unchanged.
        # Binding is by telegram_id alone (one city regardless of how many roles a person
        # holds), so the value is duplicated across every (telegram_id, role) row for that
        # person rather than normalized into a separate table -- there are at most a few dozen
        # staff rows, and a single-table SELECT/UPDATE stays simpler than a join.
        await _ensure_column(db, "staff", "city", "TEXT")

        # Phase 1: persistent dropout tracking (survives restart, independent of FSM)
        await db.execute('''
            CREATE TABLE IF NOT EXISTS reg_started (
                telegram_id INTEGER PRIMARY KEY,
                username TEXT,
                started_at TEXT NOT NULL
            )
        ''')
        # Phase 3 (SCHED-03): one-shot dropout-nudge stamp (additive, D-15 — reuse reg_started)
        await _ensure_column(db, "reg_started", "nudged_at", "TEXT")
        # Dropout analytics: last question shown before the user abandoned (step_key). NULL
        # for rows created before this column existed / before the user saw any question.
        await _ensure_column(db, "reg_started", "last_step", "TEXT")
        # Quick k4y: JSON snapshot of already-answered registration fields (FSM data at the
        # moment of the last question). NULL for rows created before this column existed —
        # no backfill possible, those rows render as "-" on the «Незавершённые» tab. Additive,
        # idempotent — safe against ~590 live records.
        await _ensure_column(db, "reg_started", "partial_data", "TEXT")

        # Phase 3 (SCHED-01): scheduled-broadcast payload store. APScheduler owns the
        # trigger (data/jobs.sqlite); this row holds the message/filter payload keyed by id.
        await db.execute('''
            CREATE TABLE IF NOT EXISTS scheduled_broadcasts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                text TEXT,
                photo_file_id TEXT,
                filter_spec TEXT,
                scheduled_at TEXT NOT NULL,
                status TEXT DEFAULT 'pending',
                created_by INTEGER,
                created_at TEXT
            )
        ''')

        # Phase 4 migrations (additive, idempotent — safe against ~590 live users)
        await _ensure_column(db, "users", "payment_status", "TEXT DEFAULT 'not_paid'")
        await _ensure_column(db, "users", "payment_option", "TEXT")
        await _ensure_column(db, "users", "receipt_file_id", "TEXT")
        await _ensure_column(db, "users", "payment_due", "TEXT")
        await _ensure_column(db, "users", "paid_at", "TEXT")

        # YL'26 reg fields (additive, default-off questions — no impact on live flow)
        await _ensure_column(db, "users", "arrival_date", "TEXT")
        await _ensure_column(db, "users", "birth_date", "TEXT")
        await _ensure_column(db, "users", "study_field", "TEXT")
        await _ensure_column(db, "users", "goal", "TEXT")          # multi-select, CSV
        await _ensure_column(db, "users", "formats", "TEXT")       # multi-select, CSV
        await _ensure_column(db, "users", "vk_username", "TEXT")   # ник в ВК (@username) — YL'26
        await _ensure_column(db, "users", "transport", "TEXT")     # трансфер до площадки / самостоятельно
        await _ensure_column(db, "users", "payment_plan_date", "TEXT")  # планируемая дата оплаты взноса
        await _ensure_column(db, "users", "bed_sharing", "TEXT")   # конфа: готов делить двуспальную кровать
        await _ensure_column(db, "users", "bed_partner", "TEXT")   # конфа: с кем именно (условно)
        await _ensure_column(db, "users", "alumni_status", "TEXT")  # аламни / айсекер / ни то, ни другое

        # Phase 4 (CONS-01/02, D-02): per-user consent audit trail. UNIQUE dedupes
        # re-taps; index supports the per-user lookup.
        await db.execute('''
            CREATE TABLE IF NOT EXISTS user_consents (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                consent_key TEXT NOT NULL,
                accepted_at TEXT NOT NULL,
                UNIQUE(user_id, consent_key)
            )
        ''')
        await db.execute('CREATE INDEX IF NOT EXISTS idx_consents_user ON user_consents(user_id)')

        # Phase 5 migrations (TRACK-01, D-01/D-02) — additive, idempotent, safe against ~590 live users
        await _ensure_column(db, "users", "participant_type", "TEXT DEFAULT 'full'")
        await _ensure_column(db, "reg_started", "participant_type", "TEXT")

        # Phase 8 (ROLE-01, D-13/D-14): one row per delegate question, created ONCE before
        # the D-13 fan-out to every moderate_reg holder (not per-recipient) -- the atomic
        # claim below is keyed by this row's id, never by a recipient's own copy of the
        # notification (08-RESEARCH Pitfall 6). answered_by_name is captured AT claim time:
        # the claiming manager isn't guaranteed a `users` row, so a later get_user() lookup
        # could come back empty.
        await db.execute('''
            CREATE TABLE IF NOT EXISTS delegate_questions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                question_text TEXT NOT NULL,
                asked_at TEXT NOT NULL,
                answered_by INTEGER,
                answered_by_name TEXT,
                answered_at TEXT,
                answer_text TEXT
            )
        ''')
        # T-08-33 (quick task): "delivered" cannot be derived from answer_text -- a
        # successful reply with no text (photo/voice/sticker) writes "" there too, which is
        # indistinguishable from "never delivered" if a reader treats an empty string as
        # falsy. delivered_at is the single unambiguous signal, stamped ONLY on a successful
        # send to the delegate (set_question_answer below), never on a claim alone.
        await _ensure_column(db, "delegate_questions", "delivered_at", "TEXT")

        # Phase 07.1 migrations (CITY-01) — additive, idempotent; NO backfill. ~590 rows are
        # accumulated PAST data (only ~100 are live current-event applications); writing
        # "Москва" into old rows would fabricate a fact in storage. NULL means "registered
        # before cities existed" and must stay distinguishable from an explicit Moscow pick.
        # "Москва" is substituted ONLY on read, exclusively via cities.normalize_city — no
        # reader may write that default back into the DB.
        await _ensure_column(db, "users", "event_city", "TEXT")
        await _ensure_column(db, "reg_started", "event_city", "TEXT")

        # Phase 9 (GAME-01/02/03): task model + submission queue. Tasks are real rows (not a
        # serialized list in bot_settings) — D-07/D-06 audience/second-axis extensions land as
        # one `_ensure_column` later, never a storage rewrite. `deadline_at` is stored in the
        # same ISO-sortable format as services/scheduler.py's `_fmt_dt` ("%Y-%m-%d %H:%M:%S").
        await db.execute('''
            CREATE TABLE IF NOT EXISTS game_tasks (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                text TEXT NOT NULL,
                category TEXT NOT NULL,
                coins INTEGER NOT NULL,
                proof_type TEXT NOT NULL,
                deadline_at TEXT NOT NULL,
                created_by INTEGER,
                created_at TEXT NOT NULL
            )
        ''')
        # Phase 09.1 (B, CONTEXT.md "Задания по городам"): NULL = all cities, same semantics
        # as users.event_city (07.1/07.2). No backfill -- every pre-existing task keeps
        # meaning "all cities", which is exactly what it already behaved as.
        await _ensure_column(db, "game_tasks", "event_city", "TEXT")

        # `content` stores a file_id for photo/pdf proof, raw text for text/link proof — the
        # project never writes uploaded files to disk (README/CLAUDE.md file_id pattern).
        await db.execute('''
            CREATE TABLE IF NOT EXISTS game_submissions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                task_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                content_type TEXT NOT NULL,
                content TEXT NOT NULL,
                submitted_at TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending',
                reviewed_by INTEGER,
                reviewed_at TEXT,
                coins_awarded INTEGER,
                reject_reason TEXT
            )
        ''')
        # D-05 as a schema-level invariant, not a Python pre-INSERT check: while a submission
        # for (task_id, user_id) is anything other than 'rejected', a second INSERT for that
        # same pair cannot land, even under two concurrent inserts (SQLite raises
        # IntegrityError, which create_submission below turns into a `None` return — T-09-01).
        await db.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_game_submissions_active "
            "ON game_submissions(task_id, user_id) WHERE status != 'rejected'"
        )

        # Phase 09.1 (A): free-form multi-part submissions. `game_submissions.content`/
        # `content_type` stay NOT NULL for backward compatibility -- old rows are read as
        # ONE implicit part (get_submission_parts_or_legacy), never migrated into this table.
        # No FOREIGN KEY -- mirrors game_submissions.task_id being a bare INTEGER, the project
        # does not use SQLite FK constraints anywhere else.
        await db.execute('''
            CREATE TABLE IF NOT EXISTS game_submission_parts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                submission_id INTEGER NOT NULL,
                ord INTEGER NOT NULL,
                kind TEXT NOT NULL,
                content TEXT,
                caption TEXT
            )
        ''')
        await db.execute(
            "CREATE INDEX IF NOT EXISTS idx_game_submission_parts_sub "
            "ON game_submission_parts(submission_id, ord)"
        )

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
                comments, expectations_ar, informal_day, resume_file_id, resume_text, resume_url,
                department, aiesec_role, needs_certificate, english_level,
                allergies, food_pref, arrival, housing, cc_shop,
                exp_organizers, exp_content, volunteer,
                payment_status, payment_option, receipt_file_id, payment_due, paid_at,
                arrival_date, birth_date, study_field, goal, formats, vk_username,
                transport, payment_plan_date, bed_sharing, bed_partner, participant_type,
                alumni_status, event_city
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                resume_text=COALESCE(excluded.resume_text, users.resume_text),
                resume_url=COALESCE(excluded.resume_url, users.resume_url),
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
                volunteer=excluded.volunteer,
                receipt_file_id=COALESCE(excluded.receipt_file_id, users.receipt_file_id),
                -- WR-06: payment_status/payment_option/payment_due/paid_at are deliberately
                -- omitted here so re-registration (e.g. a rejected user) never wipes payment
                -- state. They are owned by update_payment_status / the payment flow. COALESCE
                -- would not help payment_status (its bound value defaults to 'not_paid', never
                -- NULL), so omission is the correct guard.
                arrival_date=excluded.arrival_date,
                birth_date=excluded.birth_date,
                study_field=excluded.study_field,
                goal=excluded.goal,
                formats=excluded.formats,
                vk_username=excluded.vk_username,
                transport=excluded.transport,
                payment_plan_date=excluded.payment_plan_date,
                bed_sharing=excluded.bed_sharing,
                bed_partner=excluded.bed_partner,
                participant_type=excluded.participant_type,
                alumni_status=excluded.alumni_status,
                event_city=excluded.event_city
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
            data.get('resume_text'),
            data.get('resume_url'),
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
            data.get('payment_status') or 'not_paid',
            data.get('payment_option'),
            data.get('receipt_file_id'),
            data.get('payment_due'),
            data.get('paid_at'),
            data.get('arrival_date'),
            data.get('birth_date'),
            data.get('study_field'),
            data.get('goal'),
            data.get('formats'),
            data.get('vk_username'),
            data.get('transport'),
            data.get('payment_plan_date'),
            data.get('bed_sharing'),
            data.get('bed_partner'),
            data.get('participant_type', 'full'),
            data.get('alumni_status'),
            data.get('event_city'),
        ))
        await db.commit()

async def get_user(telegram_id: int):
    async with aiosqlite.connect(config.DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute('SELECT * FROM users WHERE telegram_id = ?', (telegram_id,)) as cursor:
            row = await cursor.fetchone()
            if row:
                return dict(row)
            # IN-01: only compute the abspath on the (rare) not-found logging path.
            logger.info(f"get_user: {telegram_id} not found in {os.path.abspath(config.DB_PATH)}")
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
            # IN-04: coalesce NULL full_name so the referral list never renders "• None".
            "SELECT COALESCE(NULLIF(full_name, ''), 'Без имени') FROM users WHERE referrer_id = ?",
            (telegram_id,),
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


# Readable RU labels for the full CSV dump. Any column not listed falls back to its raw
# DB name, so a newly-added column still exports (just with its technical name).
CSV_HEADER_LABELS = {
    "telegram_id": "ID Telegram", "username": "Username", "full_name": "ФИО",
    "email": "Email", "age": "Возраст", "phone": "Телефон", "vk_username": "ВК",
    "is_aiesec_member": "Член AIESEC", "source": "Источник", "source_details": "Детали источника",
    "referrer_id": "ID реферера", "registration_date": "Дата регистрации",
    "education_status": "Образование", "university": "ВУЗ", "course": "Курс",
    "specialty": "Специальность", "study_field": "Направление обучения",
    "work_status": "Работает", "work_sphere": "Сфера работы", "missing_skills": "Не хватает навыков",
    "expectations": "Ожидания (общие)", "expectations_ar": "Ожидания (AR)",
    "exp_organizers": "Ожидания: организация", "exp_content": "Ожидания: контент",
    "comments": "Доп. комментарии", "city": "Город", "local_committee": "Локальный комитет",
    "position": "Позиция", "department": "Департамент", "aiesec_role": "Роль AIESEC",
    "needs_certificate": "Справка в ВУЗ", "english_level": "Английский",
    "alumni_status": "Аламни/айсекер",
    "attendance_format": "Формат участия", "informal_day": "Неформальный день",
    "goal": "Цель участия", "formats": "Форматы форума", "is_ambassador_candidate": "Амбассадор",
    "allergies": "Аллергии", "food_pref": "Питание", "arrival": "Приезд",
    "arrival_date": "Дата приезда", "birth_date": "Дата рождения", "housing": "Проживание",
    "transport": "Трансфер", "cc_shop": "CC-shop", "volunteer": "Волонтёр",
    "bed_sharing": "Общая кровать", "bed_partner": "Сосед по кровати",
    "status": "Статус заявки", "subscribed": "Подписан на канал",
    "resume_file_id": "Резюме (file_id)", "resume_text": "Резюме (текст)", "resume_url": "Резюме (ссылка)",
    "payment_status": "Статус оплаты", "payment_option": "Вариант оплаты",
    "receipt_file_id": "Чек (file_id)", "payment_due": "Срок оплаты", "paid_at": "Оплачено (когда)",
    "payment_plan_date": "Дата план. оплаты",
}


_CSV_INJECTION_PREFIXES = ("=", "+", "-", "@", "\t", "\r")


def _csv_safe(value):
    """Neutralize CSV/Excel formula injection (CWE-1236): prefix a single quote to any
    STRING cell that begins with a formula trigger so spreadsheet apps treat it as text.
    Non-string cells (int/None) pass through unchanged. Export-side only — never mutates
    stored data."""
    if isinstance(value, str) and value.startswith(_CSV_INJECTION_PREFIXES):
        return "'" + value
    return value


async def export_users_csv(*, city_scope=None):
    """Full audit dump — every users column (incl. phone & service fields), with readable
    RU headers. Unmapped columns keep their raw name so new columns still export.
    `city_scope=None` (default) exports everything, byte-identical to before Phase 07.2."""
    frag, city_params = _city_clause(city_scope)
    where = f" WHERE {frag}" if frag else ""
    async with aiosqlite.connect(config.DB_PATH) as db:
        async with db.execute(f'SELECT * FROM users{where}', tuple(city_params)) as cursor:
            raw = [description[0] for description in cursor.description]
            rows = await cursor.fetchall()
            headers = [CSV_HEADER_LABELS.get(h, h) for h in raw]
            rows = [tuple(_csv_safe(cell) for cell in row) for row in rows]
            return headers, rows


async def get_city_counts() -> list[tuple]:
    """One row per RAW `event_city` value present in `users` (including NULL and any
    unknown/garbage code) — `(event_city, total, pending, approved)`. Deliberately returns
    the raw column, never collapsed: db.py cannot import `cities` (import cycle — cities.py
    already imports database.db), so folding NULL/garbage into the default city is the
    CALLER's job via `cities.normalize_city`. The stats screen intentionally does NOT filter
    by the admin's selected city — it is a city-vs-city comparison, not a scoped view
    (07.2-CONTEXT.md decision)."""
    async with aiosqlite.connect(config.DB_PATH) as db:
        async with db.execute(
            "SELECT event_city, COUNT(*), "
            "SUM(CASE WHEN status = 'pending' THEN 1 ELSE 0 END), "
            "SUM(CASE WHEN status = 'approved' THEN 1 ELSE 0 END) "
            "FROM users GROUP BY event_city"
        ) as cursor:
            return await cursor.fetchall()


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

async def mark_reg_started(
    telegram_id: int,
    username: str | None,
    participant_type: str | None = None,
    event_city: str | None = None,
):
    started_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    async with aiosqlite.connect(config.DB_PATH) as db:
        await db.execute('''
            INSERT INTO reg_started (telegram_id, username, started_at, participant_type, event_city)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(telegram_id) DO UPDATE SET
                username=excluded.username,
                -- MD-03: do NOT reset started_at on re-entry. A mid-flow user re-sending /start
                -- (or advancing a step) must keep the ORIGINAL start time — otherwise every
                -- restart pushes started_at forward, the nudge cutoff is never crossed, and the
                -- dropout nudge is deferred indefinitely. A genuinely new attempt after
                -- completion is a fresh INSERT (the row was cleared), so it gets a fresh time.
                participant_type=COALESCE(excluded.participant_type, reg_started.participant_type),
                -- Phase 07.1 (CITY-01): same COALESCE semantics as participant_type — a bare
                -- repeat /start with no city arg must NOT clear an already-chosen city.
                event_city=COALESCE(excluded.event_city, reg_started.event_city)
        ''', (telegram_id, username, started_at, participant_type, event_city))
        await db.commit()


async def clear_reg_started(telegram_id: int):
    async with aiosqlite.connect(config.DB_PATH) as db:
        await db.execute("DELETE FROM reg_started WHERE telegram_id = ?", (telegram_id,))
        await db.commit()


# Phase 5 (D-02): read the track recorded at flow start, before finalize_registration clears
# the reg_started row — the source of truth for a bare repeat /start mid-flow.
async def get_reg_started_track(telegram_id: int) -> str | None:
    async with aiosqlite.connect(config.DB_PATH) as db:
        async with db.execute(
            "SELECT participant_type FROM reg_started WHERE telegram_id = ?", (telegram_id,)
        ) as cursor:
            row = await cursor.fetchone()
            return row[0] if row else None


# Phase 07.1 (CITY-01): read the event_city recorded at flow start — same read pattern as
# get_reg_started_track, for restoring an in-progress registration's city choice.
async def get_reg_started_city(telegram_id: int) -> str | None:
    async with aiosqlite.connect(config.DB_PATH) as db:
        async with db.execute(
            "SELECT event_city FROM reg_started WHERE telegram_id = ?", (telegram_id,)
        ) as cursor:
            row = await cursor.fetchone()
            return row[0] if row else None


# Phase 7 (07-04, SHORT-06): is there a live abandoned short-track registration right now?
# Used to gate the «Незавершённые» column merge in handlers.registration.incomplete_sheet_headers
# on the STATE of reg_started rows rather than on the live registration_mode setting — so a
# manager reverting the toggle on 2026-08-11 does not make the next 2h auto-sync
# (services/scheduler.py sync_incomplete_sheet_job) collapse already-answered promo fields
# back to "-" before the last abandoned promo delegate is cleared or finishes.
async def has_short_incomplete() -> bool:
    async with aiosqlite.connect(config.DB_PATH) as db:
        async with db.execute(
            "SELECT 1 FROM reg_started WHERE participant_type = 'short' LIMIT 1"
        ) as cursor:
            row = await cursor.fetchone()
            return bool(row)


# MD-02: a reg_started row is DELETEd on completion (finalize_registration), but that clear is
# fail-soft — a DB hiccup can leave a finished user in reg_started, where the dropout nudge,
# «Незавершённые» sheet, and broadcast segment would then wrongly treat them as a dropout.
# Defensively exclude anyone who already holds a NON-rejected users row (genuinely registered).
# Rejected users are KEPT: D-05a lets them fall through to re-register, so a reg_started row for
# a rejected user is a real in-progress attempt.
_INCOMPLETE_NOT_REGISTERED = (
    "NOT EXISTS (SELECT 1 FROM users u WHERE u.telegram_id = reg_started.telegram_id "
    "AND (u.status IS NULL OR u.status != 'rejected'))"
)


async def get_incomplete_user_ids() -> list[int]:
    async with aiosqlite.connect(config.DB_PATH) as db:
        async with db.execute(
            f"SELECT telegram_id FROM reg_started WHERE {_INCOMPLETE_NOT_REGISTERED}"
        ) as cursor:
            return [row[0] for row in await cursor.fetchall()]


async def get_incomplete_rows() -> list[tuple]:
    """Full dropout rows for the «Незавершённые» sheet tab: (telegram_id, username,
    started_at, last_step, partial_data). These users hit /start but never finished.
    Quick k4y: partial_data (JSON snapshot of already-answered fields) is now persisted
    alongside last_step — it is NULL for rows created before that column existed."""
    async with aiosqlite.connect(config.DB_PATH) as db:
        async with db.execute(
            "SELECT telegram_id, username, started_at, last_step, partial_data FROM reg_started "
            f"WHERE {_INCOMPLETE_NOT_REGISTERED} ORDER BY started_at"
        ) as cursor:
            return [tuple(row) for row in await cursor.fetchall()]


async def get_incomplete_rows_with_city() -> list[tuple]:
    """Same rows, filter, and ORDER BY as get_incomplete_rows, plus a sixth field
    (event_city) so the «Незавершённые» tab can be split per city (Phase 07.1, CITY-04).
    get_incomplete_rows() itself is UNTOUCHED -- existing tests rely on its 5-tuple shape."""
    async with aiosqlite.connect(config.DB_PATH) as db:
        async with db.execute(
            "SELECT telegram_id, username, started_at, last_step, partial_data, event_city "
            f"FROM reg_started WHERE {_INCOMPLETE_NOT_REGISTERED} ORDER BY started_at"
        ) as cursor:
            return [tuple(row) for row in await cursor.fetchall()]


async def set_reg_step(telegram_id: int, step_key: str, partial_json: str | None = None):
    """Stamp the question currently shown to a mid-registration user (dropout analytics),
    and optionally persist a JSON snapshot of already-answered fields (quick k4y). No-op
    if the reg_started row is gone (finished/cleared). Fail-soft at the call site.
    COALESCE keeps any previously-stored partial_data intact when called without a
    snapshot (e.g. the very first question) — it must never be reset to NULL."""
    async with aiosqlite.connect(config.DB_PATH) as db:
        await db.execute(
            "UPDATE reg_started SET last_step = ?, partial_data = COALESCE(?, partial_data) "
            "WHERE telegram_id = ?",
            (step_key, partial_json, telegram_id),
        )
        await db.commit()


async def get_dropout_step_stats() -> list[tuple]:
    """(last_step, count) over all incomplete registrations, most-abandoned first. last_step
    may be NULL for users who dropped before seeing any question."""
    async with aiosqlite.connect(config.DB_PATH) as db:
        async with db.execute(
            "SELECT last_step, COUNT(*) FROM reg_started "
            f"WHERE {_INCOMPLETE_NOT_REGISTERED} GROUP BY last_step ORDER BY COUNT(*) DESC"
        ) as cursor:
            return [tuple(row) for row in await cursor.fetchall()]


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


# ── Phase 07.2 Plan 01 (CITY-02): city scope clause builder ──────────────────
#
# `cities.py` imports `database.db` (registry accessors need `get_setting`/`set_setting`),
# so `database/db.py` must NEVER import `cities` — that would be an import cycle. The
# registry's knowledge (which codes exist, which is the default) is therefore handed to
# `_city_clause` BY VALUE as the `(code, exclude)` descriptor `cities.city_scope` builds;
# db.py stays the bottom layer and never learns what a "city" is.
def _city_clause(scope: tuple[str, tuple[str, ...]] | None, column: str = "event_city", *,
                  include_null: bool = False) -> tuple[str, list]:
    """Pure: turn a `cities.city_scope(...)` descriptor into a parameterized SQL fragment
    (no leading AND/WHERE — callers splice it in). `scope is None` -> `("", [])`, no
    filtering at all (this is what keeps module-off / no-scope byte-identical to today).
    Empty `exclude` -> equality (`event_city = ?`, or `(col IS NULL OR col = ?)` when
    `include_null=True` — Phase 09.1 (B): a task's NULL means "all cities", so a delegate's
    own-city fetch must catch NULL even though their own city is the equality branch);
    non-empty `exclude` -> the default-city shape (`event_city IS NULL OR event_city NOT IN
    (?, ...)`), one placeholder per excluded code (already catches NULL, `include_null` is a
    no-op here). `column` lets callers qualify the column for a JOIN (e.g. "t.event_city",
    "u.event_city") — it is ALWAYS one of this file's own literal call-site strings, never
    user/callback-derived (T-091-08); city codes never get interpolated into the SQL string,
    only the ? count does."""
    if scope is None:
        return "", []
    code, exclude = scope
    if not exclude:
        if include_null:
            return f"({column} IS NULL OR {column} = ?)", [code]
        return f"{column} = ?", [code]
    placeholders = ", ".join("?" for _ in exclude)
    return f"({column} IS NULL OR {column} NOT IN ({placeholders}))", list(exclude)


async def get_pending_users(limit: int = 1, offset: int = 0, *, city_scope=None) -> list[dict]:
    """Pending applications, oldest first (registration_date then telegram_id)."""
    frag, city_params = _city_clause(city_scope)
    extra = f" AND {frag}" if frag else ""
    async with aiosqlite.connect(config.DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            f"SELECT * FROM users WHERE status = 'pending'{extra} "
            "ORDER BY registration_date ASC, telegram_id ASC LIMIT ? OFFSET ?",
            (*city_params, limit, offset),
        ) as cursor:
            return [dict(row) for row in await cursor.fetchall()]


async def get_pending_count(*, city_scope=None) -> int:
    frag, city_params = _city_clause(city_scope)
    extra = f" AND {frag}" if frag else ""
    async with aiosqlite.connect(config.DB_PATH) as db:
        async with db.execute(
            f"SELECT COUNT(*) FROM users WHERE status = 'pending'{extra}", tuple(city_params)
        ) as cursor:
            row = await cursor.fetchone()
            return int(row[0]) if row and row[0] is not None else 0


async def approve_all_pending(*, city_scope=None) -> list[int]:
    """Flip every pending row to approved in one atomic statement; return the
    telegram_ids that flipped (each once). IN-07: requires sqlite >= 3.35 for
    RETURNING (bundled in CPython 3.10+, which the project already mandates) —
    there is no pre-3.35 fallback path in this function.

    `city_scope` narrows the WHERE of this SAME atomic UPDATE ... RETURNING (not a second
    query, not a post-filter on the returned ids) — a scoped call structurally cannot flip
    a row belonging to another city (T-072-03)."""
    frag, city_params = _city_clause(city_scope)
    extra = f" AND {frag}" if frag else ""
    async with aiosqlite.connect(config.DB_PATH) as db:
        async with db.execute(
            f"UPDATE users SET status = 'approved' WHERE status = 'pending'{extra} "
            "RETURNING telegram_id",
            tuple(city_params),
        ) as cursor:
            rows = await cursor.fetchall()
        await db.commit()
        return [row[0] for row in rows]


# ── Phase 3: scheduled-broadcast payload store (SCHED-01) ────────────────────

async def create_scheduled_broadcast(
    text: str | None,
    photo_file_id: str | None,
    filter_spec: str | None,
    scheduled_at: str,
    created_by: int,
) -> int:
    """Insert a pending scheduled broadcast; return its new id (the job's only arg)."""
    created_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    async with aiosqlite.connect(config.DB_PATH) as db:
        cursor = await db.execute(
            "INSERT INTO scheduled_broadcasts "
            "(text, photo_file_id, filter_spec, scheduled_at, status, created_by, created_at) "
            "VALUES (?, ?, ?, ?, 'pending', ?, ?)",
            (text, photo_file_id, filter_spec, scheduled_at, created_by, created_at),
        )
        await db.commit()
        return cursor.lastrowid


async def get_scheduled_broadcast(broadcast_id: int) -> dict | None:
    async with aiosqlite.connect(config.DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM scheduled_broadcasts WHERE id = ?", (broadcast_id,)
        ) as cursor:
            row = await cursor.fetchone()
            return dict(row) if row else None


async def mark_broadcast_sending(broadcast_id: int) -> int:
    """ME-02: atomically claim a pending broadcast for sending. Flips 'pending' → 'sending'
    and returns rowcount: 1 = this caller owns the send, 0 = already claimed/sent/cancelled
    (double-schedule race or a re-fire). A crash mid-send leaves the row 'sending' — never back
    to 'pending' — so neither a re-fire nor the ME-03 boot reconciliation (which re-arms only
    'pending' rows) can blast the whole audience a second time."""
    async with aiosqlite.connect(config.DB_PATH) as db:
        cursor = await db.execute(
            "UPDATE scheduled_broadcasts SET status = 'sending' "
            "WHERE id = ? AND status = 'pending'",
            (broadcast_id,),
        )
        await db.commit()
        return cursor.rowcount


async def mark_broadcast_sent(broadcast_id: int):
    async with aiosqlite.connect(config.DB_PATH) as db:
        await db.execute(
            "UPDATE scheduled_broadcasts SET status = 'sent' WHERE id = ?", (broadcast_id,)
        )
        await db.commit()


async def list_pending_broadcasts() -> list[dict]:
    async with aiosqlite.connect(config.DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM scheduled_broadcasts WHERE status = 'pending' ORDER BY scheduled_at"
        ) as cursor:
            return [dict(row) for row in await cursor.fetchall()]


async def cancel_scheduled_broadcast(broadcast_id: int):
    async with aiosqlite.connect(config.DB_PATH) as db:
        await db.execute(
            "UPDATE scheduled_broadcasts SET status = 'cancelled' WHERE id = ?", (broadcast_id,)
        )
        await db.commit()


# ── Phase 3: dropout-nudge scan/mark (SCHED-03) ──────────────────────────────

async def get_nudge_candidates(cutoff: str) -> list[int]:
    """Incomplete registrations older than cutoff that were never nudged.
    started_at is ISO ('%Y-%m-%d %H:%M:%S') so lexicographic `<` is chronological."""
    async with aiosqlite.connect(config.DB_PATH) as db:
        async with db.execute(
            "SELECT telegram_id FROM reg_started "
            f"WHERE started_at < ? AND nudged_at IS NULL AND {_INCOMPLETE_NOT_REGISTERED}",
            (cutoff,),
        ) as cursor:
            return [row[0] for row in await cursor.fetchall()]


async def mark_nudged(telegram_id: int):
    """Stamp nudged_at so a user is never nudged twice (one-shot dedup, D-14)."""
    nudged_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    async with aiosqlite.connect(config.DB_PATH) as db:
        await db.execute(
            "UPDATE reg_started SET nudged_at = ? WHERE telegram_id = ?",
            (nudged_at, telegram_id),
        )
        await db.commit()


# ── Phase 3: filtered-broadcast audience query (COMM-01/02/03) ───────────────

# Column whitelist — the ONLY place a column name is composed into SQL (Pitfall 5).
_FILTER_COLUMNS = {
    "city", "university", "status", "source", "payment_status",
    # Broadcast segmentation by more user attributes (all real users columns).
    "local_committee", "department", "aiesec_role", "education_status",
    "course", "study_field", "position", "attendance_format",
    "participant_type",  # Phase 5 (D-19, TRACK-06 SC#8)
    # Phase 07.2 (CITY-02) — event city as a broadcast-segment filter. MUST also be in
    # `handlers.admin._PICKER_FIELDS`, otherwise the field is silently dropped and the
    # manager broadcasts to the wrong segment while the screen says otherwise
    # (precedent: Phase 5 D-19). Handled by its own branch in `_build_filter_clause`,
    # not by the generic `{field} = ?` one — see there.
    "event_city",
}


def _build_filter_clause(filters: list[dict]) -> tuple[str, list]:
    """Pure: build a parameterized AND WHERE clause from a filter spec.

    Column names come only from `_FILTER_COLUMNS` (or the literal `registration_date`);
    values are NEVER interpolated — they bind as `?`. Non-whitelisted fields are dropped.

    `event_city` is the one field that is NOT a plain equality: the DEFAULT city is described
    by EXCLUSION of the other known cities, so it also catches `event_city IS NULL` (every
    application registered before the cities module existed) — same semantics as
    `cities.normalize_city` and the Sheets tabs. The list of "other known city codes" arrives
    in the filter dict itself under the `exclude` key, put there by the caller
    (`handlers/admin.py`, via `cities.city_scope`), because `database/db.py` may NEVER import
    `cities` — `cities.py` already imports this module, so that would be an import cycle.
    The `exclude` key must therefore also survive the `json.dumps`/`json.loads` round-trip a
    scheduled broadcast's filter spec goes through.
    """
    clauses: list[str] = []
    params: list = []
    for f in filters:
        field = f.get("field")
        if field == "registration_date":
            op = ">=" if f.get("op") == "after" else "<"
            clauses.append(f"registration_date {op} ?")
            params.append(f.get("value"))
        elif field == "event_city":
            # Must come BEFORE the generic `_FILTER_COLUMNS` branch below, which would emit a
            # plain `event_city = ?` and silently drop every NULL row from the default city.
            if not f.get("value"):
                # WR-01: НЕ «пропустить». Пропуск снимал условие целиком, и ME-04
                # (`if filters and not where`) спасал только когда отброшены ВСЕ фильтры.
                # Спека [{status: approved}, {event_city: ""}] давала `WHERE status = ?`,
                # т.е. рассылка уходила во все города, хотя сводка называла один. Эмитим
                # заведомо ложное условие — аудитория гарантированно пуста (fail closed),
                # что совпадает с наблюдаемым поведением ME-04.
                clauses.append("0")
                continue
            frag, city_params = _city_clause((f.get("value"), tuple(f.get("exclude") or ())))
            clauses.append(frag)
            params.extend(city_params)
        elif field in _FILTER_COLUMNS:
            clauses.append(f"{field} = ?")
            params.append(f.get("value"))
        # non-whitelisted field → silently skipped (never interpolated)
    where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
    return where, params


# NOT USABLE FOR `event_city` (Phase 07.2, CITY-02): this function returns raw column values
# and, by construction (`IS NOT NULL AND TRIM(...) != ''`), drops NULL rows — so the DEFAULT
# city, under which every pre-cities application still sits as NULL, would simply not appear
# in the list of offered values, and a city with no applications yet would be unofferable.
# The source of city values for the picker is the registry (`cities.CITIES`), resolved on the
# `handlers/admin.py` side; this module cannot import `cities` (import cycle).
async def get_distinct_filter_values(field: str) -> list[str]:
    """Distinct non-empty values present in the users table for a whitelisted filter column —
    feeds the broadcast value picker (buttons pulled from real data, no free-text typing).
    `registration_date` returns distinct calendar dates (YYYY-MM-DD). Field is validated against
    the same whitelist as _build_filter_clause, so the f-string column is never user-derived."""
    if field == "registration_date":
        sql = (
            "SELECT DISTINCT date(registration_date) AS v FROM users "
            "WHERE registration_date IS NOT NULL AND TRIM(registration_date) != '' ORDER BY v"
        )
    elif field in _FILTER_COLUMNS:
        sql = (
            f"SELECT DISTINCT {field} AS v FROM users "
            f"WHERE {field} IS NOT NULL AND TRIM({field}) != '' ORDER BY v"
        )
    else:
        return []
    async with aiosqlite.connect(config.DB_PATH) as db:
        async with db.execute(sql) as cursor:
            rows = await cursor.fetchall()
    return [str(r[0]) for r in rows if r[0] is not None and str(r[0]).strip()]


async def count_and_list_filtered(filters: list[dict]) -> list[int]:
    """Materialize the matched telegram_id list; the count preview is len(...)."""
    where, params = _build_filter_clause(filters)
    # ME-04: if the caller supplied filter(s) but every one was dropped (non-whitelisted field
    # / malformed spec), `where` degenerates to empty and the query would fan out to ALL users.
    # A filtered broadcast must NEVER silently blast the whole base — the "all users" broadcast
    # has its own dedicated path. Fail safe to an empty audience.
    if filters and not where:
        logger.warning(
            "count_and_list_filtered: %d filter(s) supplied but none produced a valid clause — "
            "returning empty audience (refusing to fan out to all users)", len(filters)
        )
        return []
    async with aiosqlite.connect(config.DB_PATH) as db:
        async with db.execute(
            f"SELECT telegram_id FROM users{where}", params
        ) as cursor:
            return [row[0] for row in await cursor.fetchall()]


# ── Phase 4: consent acceptances (CONS-01/02, D-02) ──────────────────────────

async def record_user_consent(user_id: int, consent_key: str):
    """Idempotent consent write — re-tapping «Принимаю» never raises (INSERT OR IGNORE)."""
    accepted_at = datetime.now().isoformat()
    async with aiosqlite.connect(config.DB_PATH) as db:
        await db.execute(
            "INSERT OR IGNORE INTO user_consents (user_id, consent_key, accepted_at) "
            "VALUES (?, ?, ?)",
            (user_id, consent_key, accepted_at),
        )
        await db.commit()


async def get_user_consents(user_id: int) -> list[str]:
    async with aiosqlite.connect(config.DB_PATH) as db:
        async with db.execute(
            "SELECT consent_key FROM user_consents WHERE user_id = ? ORDER BY accepted_at",
            (user_id,),
        ) as cursor:
            return [row[0] for row in await cursor.fetchall()]


# ── Phase 4: payment receipt queue + status (PAY-05, D-10/D-12) ──────────────

async def get_receipt_pending_users(limit: int = 50, offset: int = 0, *, city_scope=None) -> list[dict]:
    """Users awaiting receipt verification, oldest first (tinder queue source)."""
    frag, city_params = _city_clause(city_scope)
    extra = f" AND {frag}" if frag else ""
    async with aiosqlite.connect(config.DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT telegram_id, full_name, payment_option, receipt_file_id, payment_status "
            f"FROM users WHERE payment_status = 'receipt_sent'{extra} ORDER BY rowid LIMIT ? OFFSET ?",
            (*city_params, limit, offset),
        ) as cursor:
            return [dict(row) for row in await cursor.fetchall()]


async def get_receipt_pending_count(*, city_scope=None) -> int:
    frag, city_params = _city_clause(city_scope)
    extra = f" AND {frag}" if frag else ""
    async with aiosqlite.connect(config.DB_PATH) as db:
        async with db.execute(
            f"SELECT COUNT(*) FROM users WHERE payment_status = 'receipt_sent'{extra}",
            tuple(city_params),
        ) as cursor:
            row = await cursor.fetchone()
            return int(row[0]) if row and row[0] is not None else 0


async def update_payment_status(
    telegram_id: int, status: str, *, require_status: str | None = None, **kwargs
) -> int:
    """Transition one user's payment_status; returns cursor.rowcount.

    Confirm guard (STRIDE T-04-05-02): status='paid' only flips a row currently in
    'receipt_sent' — a second concurrent confirm matches 0 rows (rowcount=0 is the
    double-confirm signal the admin handler relies on).

    Reject guard (H-01): pass require_status='receipt_sent' to add the same conditional
    WHERE to a not_paid transition — so a stale/already-confirmed card tapped ❌ Отклонить
    cannot flip a 'paid' row back to 'not_paid' (rowcount=0 signals no-op to the handler).
    Callers that legitimately reset unconditionally (payment-option pick) omit it.

    Additive UPDATE only — never INSERT OR REPLACE."""
    sets = ["payment_status = ?"]
    params: list = [status]
    extras = dict(kwargs)
    if status == "paid" and "paid_at" not in extras:
        extras["paid_at"] = datetime.now().isoformat()
    for col in ("receipt_file_id", "paid_at", "payment_option", "payment_due"):
        if col in extras:
            sets.append(f"{col} = ?")
            params.append(extras[col])
    # status='paid' keeps its historical hard-coded guard unless the caller overrides it.
    guard = require_status if require_status is not None else ("receipt_sent" if status == "paid" else None)
    where = "telegram_id = ?"
    params.append(telegram_id)
    if guard is not None:
        where += " AND payment_status = ?"
        params.append(guard)
    async with aiosqlite.connect(config.DB_PATH) as db:
        cursor = await db.execute(
            f"UPDATE users SET {', '.join(sets)} WHERE {where}", params
        )
        await db.commit()
        return cursor.rowcount


async def set_payment_due(telegram_id: int, payment_due: str) -> None:
    """WR-03: persist the deadline a user owes payment by, WITHOUT touching payment_status.
    Lets the overdue sweep catch users who deferred from the option picker (payment_option NULL)."""
    async with aiosqlite.connect(config.DB_PATH) as db:
        await db.execute(
            "UPDATE users SET payment_due = ? WHERE telegram_id = ?",
            (payment_due, telegram_id),
        )
        await db.commit()


# ── Phase 8 (ROLE-02, D-11): staff roster accessors ─────────────────────────────────────

async def add_staff(telegram_id: int, role: str, added_by: int | None) -> bool:
    """Grant `role` to `telegram_id`. INSERT OR IGNORE against the composite PRIMARY KEY
    (telegram_id, role) makes re-adding an already-held role a no-op, not a duplicate row.
    Returns True iff this call actually inserted a new row."""
    async with aiosqlite.connect(config.DB_PATH) as db:
        cursor = await db.execute(
            "INSERT OR IGNORE INTO staff (telegram_id, role, added_by, added_at) VALUES (?, ?, ?, ?)",
            (telegram_id, role, added_by, datetime.utcnow().isoformat()),
        )
        await db.commit()
        return cursor.rowcount == 1


async def remove_staff(telegram_id: int, role: str) -> bool:
    """Revoke `role` from `telegram_id`. Returns True iff a row was actually removed."""
    async with aiosqlite.connect(config.DB_PATH) as db:
        cursor = await db.execute(
            "DELETE FROM staff WHERE telegram_id = ? AND role = ?",
            (telegram_id, role),
        )
        await db.commit()
        return cursor.rowcount == 1


async def get_staff_roles(telegram_id: int) -> list[str]:
    """All roles held by one person (empty list if they hold none)."""
    async with aiosqlite.connect(config.DB_PATH) as db:
        async with db.execute(
            "SELECT role FROM staff WHERE telegram_id = ?", (telegram_id,)
        ) as cursor:
            rows = await cursor.fetchall()
            return [row[0] for row in rows]


async def list_staff() -> list[dict]:
    """Full roster, oldest grant first -- feeds the "Роли и доступы" admin screen (08-02).
    `city` (Phase 09.1, C) is NULL for every pre-existing row -- "all cities", byte-identical
    to today's behavior for anyone who never gets a binding."""
    async with aiosqlite.connect(config.DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT telegram_id, role, added_by, added_at, city FROM staff ORDER BY added_at"
        ) as cursor:
            rows = await cursor.fetchall()
            return [dict(row) for row in rows]


# ── Phase 09.1 (C, ROLE-03): manager <-> city binding accessors ────────────────────────────

async def get_staff_city(telegram_id: int) -> str | None:
    """The city bound to this person, or None (unbound -- "all cities", same as every
    pre-09.1 record). Binding is per-person, not per-role -- any one of their role-rows
    carrying a non-NULL city is enough to answer (set_staff_city keeps them all in sync)."""
    async with aiosqlite.connect(config.DB_PATH) as db:
        async with db.execute(
            "SELECT city FROM staff WHERE telegram_id = ? AND city IS NOT NULL LIMIT 1",
            (telegram_id,),
        ) as cursor:
            row = await cursor.fetchone()
            return row[0] if row else None


async def set_staff_city(telegram_id: int, city: str | None) -> bool:
    """Bind (or clear, when `city` is None) this person's city across EVERY role-row they
    hold in one statement -- the binding is by telegram_id alone (CONTEXT.md C), not by
    (telegram_id, role). Returns True iff at least one row existed to update; a person with
    no staff row at all gets False and nothing is written."""
    async with aiosqlite.connect(config.DB_PATH) as db:
        cursor = await db.execute(
            "UPDATE staff SET city = ? WHERE telegram_id = ?", (city, telegram_id)
        )
        await db.commit()
        return cursor.rowcount > 0


async def get_staff_ids_by_role(role: str) -> list[int]:
    """Every telegram_id currently holding exactly this role -- feeds notification fan-out
    (D-13, wired in a later phase-8 plan)."""
    async with aiosqlite.connect(config.DB_PATH) as db:
        async with db.execute(
            "SELECT telegram_id FROM staff WHERE role = ?", (role,)
        ) as cursor:
            rows = await cursor.fetchall()
            return [row[0] for row in rows]


# ── Phase 8 (ROLE-01, D-13/D-14): delegate_questions accessors ─────────────────────────────

async def create_question(user_id: int, question_text: str) -> int:
    """One row per delegate question, created ONCE before the D-13 fan-out (never per
    recipient -- 08-RESEARCH Pitfall 6). Returns the new row's id, embedded in every fanned-
    out copy of the notification so any recipient's reply resolves to the same claim target."""
    async with aiosqlite.connect(config.DB_PATH) as db:
        cursor = await db.execute(
            "INSERT INTO delegate_questions (user_id, question_text, asked_at) VALUES (?, ?, ?)",
            (user_id, question_text, datetime.utcnow().isoformat()),
        )
        await db.commit()
        return cursor.lastrowid


async def get_question(question_id: int) -> dict | None:
    async with aiosqlite.connect(config.DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM delegate_questions WHERE id = ?", (question_id,)
        ) as cursor:
            row = await cursor.fetchone()
            return dict(row) if row else None


async def claim_question(question_id: int, admin_id: int, admin_name: str) -> bool:
    """Atomic single-row claim (D-14, same idiom as approve_user_atomic): True iff THIS call
    flipped the row (rowcount==1) -- a concurrent second claim on the same question_id
    returns False, and the loser reads answered_by_name back via get_question()."""
    async with aiosqlite.connect(config.DB_PATH) as db:
        cursor = await db.execute(
            "UPDATE delegate_questions SET answered_by = ?, answered_by_name = ?, "
            "answered_at = ? WHERE id = ? AND answered_by IS NULL",
            (admin_id, admin_name, datetime.utcnow().isoformat(), question_id),
        )
        await db.commit()
        return cursor.rowcount == 1


async def set_question_answer(question_id: int, answer_text: str):
    """Record the answer text AND stamp delivered_at together -- this is only ever called
    after `bot.send_message`/`send_copy` to the delegate has actually SUCCEEDED (T-08-33
    quick task). delivered_at, not answer_text, is the detector `get_stuck_questions()`
    relies on -- see the column's comment in init_db for why answer_text alone can't do it."""
    async with aiosqlite.connect(config.DB_PATH) as db:
        await db.execute(
            "UPDATE delegate_questions SET answer_text = ?, delivered_at = ? WHERE id = ?",
            (answer_text, datetime.utcnow().isoformat(), question_id),
        )
        await db.commit()


async def get_stuck_questions() -> list[dict]:
    """T-08-33 quick task, part D: rows that were claimed (answered_by set) but never
    successfully delivered (delivered_at still NULL) -- the admin "stuck questions" screen.
    Deliberately does NOT look at answer_text (see set_question_answer's docstring)."""
    async with aiosqlite.connect(config.DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM delegate_questions "
            "WHERE answered_by IS NOT NULL AND delivered_at IS NULL "
            "ORDER BY answered_at ASC"
        ) as cursor:
            rows = await cursor.fetchall()
            return [dict(row) for row in rows]


# ── Phase 9 (GAME-01/02/03): task model + submission queue ──────────────────────────────────
#
# GAME_CATEGORIES (D-06) — a single classification axis, no RESULT/INTERACTIVE/NETWORK track
# and no `participant_type` audience field (D-07); both deferred additions land as one
# `_ensure_column` later, not a storage rewrite. GAME_PROOF_TYPES (D-01/D-08) — the four
# confirmation shapes a task can require. Exported here (not duplicated in handlers) so
# handlers/admin.py and handlers/user_actions.py can never drift on the list of valid values.
GAME_CATEGORIES = ["Light", "Medium", "Hard", "Referral", "Special"]
GAME_PROOF_TYPES = ["photo", "pdf", "text", "link"]

# Phase 09.1 (A): the free-form submission's part storage kind vocabulary -- distinct from
# GAME_PROOF_TYPES ("pdf" narrows to "document": any file type is accepted now, not only PDF).
GAME_PART_KINDS = ["photo", "document", "text", "link"]

# Legacy single-column content_type -> new part `kind`, used ONLY by
# get_submission_parts_or_legacy to synthesize one part from a pre-migration row.
_LEGACY_KIND_MAP = {"photo": "photo", "pdf": "document", "text": "text", "link": "link"}


def parse_proof_types(raw: str | None) -> list[str]:
    """The ONE place that owns the storage format for a task's (possibly multiple)
    proof_type: a comma-separated string in the existing `game_tasks.proof_type` column
    (no migration needed -- a single old value parses as a one-element list). Unknown codes
    are dropped; order follows GAME_PROOF_TYPES, not the order codes appear in `raw`."""
    if not raw:
        return []
    codes = {segment.strip() for segment in raw.split(",") if segment.strip()}
    return [p for p in GAME_PROOF_TYPES if p in codes]


async def create_task(text: str, category: str, coins: int, proof_type: str,
                       deadline_at: str, created_by: int | None, *,
                       event_city: str | None = None) -> int:
    """`event_city` is kwarg-only (Phase 09.1 B) so every existing positional call site
    (including pre-09.1 tests) stays valid and keeps creating a NULL-city ("all cities")
    task unless a caller opts in."""
    created_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    async with aiosqlite.connect(config.DB_PATH) as db:
        cursor = await db.execute(
            "INSERT INTO game_tasks (text, category, coins, proof_type, deadline_at, "
            "created_by, created_at, event_city) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (text, category, coins, proof_type, deadline_at, created_by, created_at,
             event_city),
        )
        await db.commit()
        return cursor.lastrowid


async def get_task(task_id: int) -> dict | None:
    async with aiosqlite.connect(config.DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM game_tasks WHERE id = ?", (task_id,)
        ) as cursor:
            row = await cursor.fetchone()
            return dict(row) if row else None


async def list_active_tasks(*, city_scope=None, include_null: bool = True) -> list[dict]:
    """No deadline filter — A-05 (call 13.08): the deadline is soft, the bot keeps accepting
    submissions after it expires, so a past-deadline task must stay visible to a delegate or
    it becomes physically impossible to submit. Sorted by nearest deadline first.
    Phase 09.1 (B): `city_scope=None` (default) -> byte-identical to pre-09.1 (all tasks,
    no filter). `include_null=True` by default -- a task's NULL event_city means "all
    cities" (CONTEXT.md B), and the equality branch of `_city_clause` does NOT catch NULL on
    its own, so it must be asked for explicitly here."""
    frag, city_params = _city_clause(city_scope, "event_city", include_null=include_null)
    extra = f" WHERE {frag}" if frag else ""
    async with aiosqlite.connect(config.DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            f"SELECT * FROM game_tasks{extra} ORDER BY deadline_at ASC", tuple(city_params)
        ) as cursor:
            return [dict(row) for row in await cursor.fetchall()]


async def list_all_tasks(*, city_scope=None, include_null: bool = True) -> list[dict]:
    frag, city_params = _city_clause(city_scope, "event_city", include_null=include_null)
    extra = f" WHERE {frag}" if frag else ""
    async with aiosqlite.connect(config.DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            f"SELECT * FROM game_tasks{extra} ORDER BY created_at DESC", tuple(city_params)
        ) as cursor:
            return [dict(row) for row in await cursor.fetchall()]


async def create_submission(task_id: int, user_id: int, content_type: str, content: str,
                             submitted_at: str) -> int | None:
    """Returns the new row's id, or None if `idx_game_submissions_active` rejected a second
    non-rejected submission for this (task_id, user_id) pair — T-09-01, D-05. The caller
    (wave 3) treats None as "already submitted", never re-raises."""
    async with aiosqlite.connect(config.DB_PATH) as db:
        try:
            cursor = await db.execute(
                "INSERT INTO game_submissions (task_id, user_id, content_type, content, "
                "submitted_at) VALUES (?, ?, ?, ?, ?)",
                (task_id, user_id, content_type, content, submitted_at),
            )
            await db.commit()
            return cursor.lastrowid
        except aiosqlite.IntegrityError:
            return None


async def get_submission(submission_id: int) -> dict | None:
    async with aiosqlite.connect(config.DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM game_submissions WHERE id = ?", (submission_id,)
        ) as cursor:
            row = await cursor.fetchone()
            return dict(row) if row else None


async def get_active_submission(task_id: int, user_id: int) -> dict | None:
    """Most recent non-rejected submission for this pair, or None. Rejected submissions are
    invisible here on purpose — a fresh resubmission after rejection is a NEW row (D-05)."""
    async with aiosqlite.connect(config.DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM game_submissions WHERE task_id = ? AND user_id = ? "
            "AND status != 'rejected' ORDER BY id DESC LIMIT 1",
            (task_id, user_id),
        ) as cursor:
            row = await cursor.fetchone()
            return dict(row) if row else None


# ── Phase 09.1 (A): game_submission_parts accessors ──────────────────────────────────────

async def add_submission_part(submission_id: int, ord: int, kind: str, content: str | None,
                               caption: str | None = None) -> int:
    async with aiosqlite.connect(config.DB_PATH) as db:
        cursor = await db.execute(
            "INSERT INTO game_submission_parts (submission_id, ord, kind, content, caption) "
            "VALUES (?, ?, ?, ?, ?)",
            (submission_id, ord, kind, content, caption),
        )
        await db.commit()
        return cursor.lastrowid


async def list_submission_parts(submission_id: int) -> list[dict]:
    async with aiosqlite.connect(config.DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM game_submission_parts WHERE submission_id = ? "
            "ORDER BY ord ASC, id ASC",
            (submission_id,),
        ) as cursor:
            return [dict(row) for row in await cursor.fetchall()]


async def get_submission_parts_or_legacy(submission: dict) -> list[dict]:
    """Backward-compat read: a pre-migration submission has no game_submission_parts rows --
    synthesize exactly one part from its legacy content/content_type columns. A submission
    with real parts rows ignores the legacy columns entirely (CONTEXT.md A)."""
    parts = await list_submission_parts(submission["id"])
    if parts:
        return parts
    content = submission.get("content")
    if not content:
        return []
    kind = _LEGACY_KIND_MAP.get(submission.get("content_type"), "text")
    return [{"ord": 0, "kind": kind, "content": content, "caption": None}]


async def get_pending_submissions(limit: int = 1, offset: int = 0, *, city_scope=None) -> list[dict]:
    """Paginated moderation queue (CLAUDE.md: 1000+ submissions must never be one message per
    row), same LIMIT/OFFSET shape as get_pending_users. Joins game_tasks/users so the card
    (wave 4) needs zero extra queries — task_deadline_at lets the card flag "after deadline"
    per the soft-deadline decision (A-05, call 13.08) without a second lookup.
    Phase 09.1 (B): `city_scope` filters by u.event_city (the DELEGATE's city, same pattern
    as the applications/receipts queues in 07.2), NOT the task's city — a manager scoped to
    spb must see spb delegates' submissions regardless of which city the task itself was
    addressed to. No `include_null` -- for the default city, NULL already lands via the
    exclusion-shape branch of `_city_clause`, exactly like get_pending_users; passing
    include_null here would also surface delegates with no city to every non-default-city
    manager, which is not what CONTEXT.md's "тот же паттерн, что заявки/чеки в 07.2" asks
    for."""
    frag, city_params = _city_clause(city_scope, "u.event_city")
    extra = f" AND {frag}" if frag else ""
    async with aiosqlite.connect(config.DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT s.*, t.text AS task_text, t.category AS task_category, "
            "t.coins AS task_coins, t.proof_type AS task_proof_type, "
            "t.deadline_at AS task_deadline_at, t.event_city AS task_event_city, "
            "u.full_name AS user_full_name, u.username AS user_username, "
            "u.event_city AS user_event_city "
            "FROM game_submissions s "
            "JOIN game_tasks t ON t.id = s.task_id "
            "LEFT JOIN users u ON u.telegram_id = s.user_id "
            f"WHERE s.status = 'pending'{extra} "
            "ORDER BY s.submitted_at ASC, s.id ASC LIMIT ? OFFSET ?",
            (*city_params, limit, offset),
        ) as cursor:
            return [dict(row) for row in await cursor.fetchall()]


async def get_pending_submissions_count(*, city_scope=None) -> int:
    frag, city_params = _city_clause(city_scope, "u.event_city")
    extra = f" AND {frag}" if frag else ""
    async with aiosqlite.connect(config.DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT COUNT(*) FROM game_submissions s "
            "LEFT JOIN users u ON u.telegram_id = s.user_id "
            f"WHERE s.status = 'pending'{extra}",
            tuple(city_params),
        ) as cursor:
            row = await cursor.fetchone()
            return int(row[0]) if row and row[0] is not None else 0


async def claim_submission(submission_id: int, admin_id: int, status: str, *,
                            coins_awarded: int | None = None,
                            reject_reason: str | None = None) -> bool:
    """Atomic single-row claim (same idiom as approve_user_atomic/claim_question — T-08-27):
    True iff THIS call flipped the row (rowcount==1); a concurrent second claim on the same
    submission_id returns False and its coins_awarded/reject_reason never lands (T-09-02).
    Crediting coins via add_coins is NOT done here — the caller (wave 4) does it as a separate
    step, only after this returns True, same two-step shape as appr_approve -> approve_user."""
    reviewed_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    async with aiosqlite.connect(config.DB_PATH) as db:
        cursor = await db.execute(
            "UPDATE game_submissions SET status = ?, reviewed_by = ?, reviewed_at = ?, "
            "coins_awarded = ?, reject_reason = ? WHERE id = ? AND status = 'pending'",
            (status, admin_id, reviewed_at, coins_awarded, reject_reason, submission_id),
        )
        await db.commit()
        return cursor.rowcount == 1


async def list_all_submissions() -> list[dict]:
    """Full submission history (wave 5's "История сдач" sheet/list) — same join shape as
    get_pending_submissions, no status filter, oldest first. No `city_scope` here — Phase
    09.1 (B, CONTEXT.md "Уточнение (ночь 17→18.08…)"): the sheet tabs are whole-event
    exports rebuilt by a background debounce with no admin identity, unlike the live queue
    above; `user_event_city` is exposed so the sheet builder can add a "Город" COLUMN
    instead of filtering rows."""
    async with aiosqlite.connect(config.DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT s.*, t.text AS task_text, t.category AS task_category, "
            "t.coins AS task_coins, t.proof_type AS task_proof_type, "
            "t.deadline_at AS task_deadline_at, t.event_city AS task_event_city, "
            "u.full_name AS user_full_name, u.username AS user_username, "
            "u.event_city AS user_event_city "
            "FROM game_submissions s "
            "JOIN game_tasks t ON t.id = s.task_id "
            "LEFT JOIN users u ON u.telegram_id = s.user_id "
            "ORDER BY s.submitted_at ASC"
        ) as cursor:
            return [dict(row) for row in await cursor.fetchall()]


async def get_game_stats() -> dict:
    """Four aggregate reads for the stats screen (wave 6): distinct participants, counts by
    submission status, and an approved-only breakdown by task category."""
    async with aiosqlite.connect(config.DB_PATH) as db:
        async with db.execute(
            "SELECT COUNT(DISTINCT user_id) FROM game_submissions"
        ) as cursor:
            row = await cursor.fetchone()
            participants = int(row[0]) if row and row[0] is not None else 0

        stats = {"participants": participants, "pending": 0, "approved": 0, "rejected": 0}
        async with db.execute(
            "SELECT status, COUNT(*) FROM game_submissions GROUP BY status"
        ) as cursor:
            for status, count in await cursor.fetchall():
                if status in stats:
                    stats[status] = int(count)

        by_category: dict[str, int] = {}
        async with db.execute(
            "SELECT t.category, COUNT(*) FROM game_submissions s "
            "JOIN game_tasks t ON t.id = s.task_id "
            "WHERE s.status = 'approved' GROUP BY t.category"
        ) as cursor:
            for category, count in await cursor.fetchall():
                by_category[category] = int(count)
        stats["by_category"] = by_category

        return stats
