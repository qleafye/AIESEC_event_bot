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

        # Phase 07.1 migrations (CITY-01) — additive, idempotent; NO backfill. ~590 rows are
        # accumulated PAST data (only ~100 are live current-event applications); writing
        # "Москва" into old rows would fabricate a fact in storage. NULL means "registered
        # before cities existed" and must stay distinguishable from an explicit Moscow pick.
        # "Москва" is substituted ONLY on read, exclusively via cities.normalize_city — no
        # reader may write that default back into the DB.
        await _ensure_column(db, "users", "event_city", "TEXT")
        await _ensure_column(db, "reg_started", "event_city", "TEXT")

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


async def export_users_csv():
    """Full audit dump — every users column (incl. phone & service fields), with readable
    RU headers. Unmapped columns keep their raw name so new columns still export."""
    async with aiosqlite.connect(config.DB_PATH) as db:
        async with db.execute('SELECT * FROM users') as cursor:
            raw = [description[0] for description in cursor.description]
            rows = await cursor.fetchall()
            headers = [CSV_HEADER_LABELS.get(h, h) for h in raw]
            rows = [tuple(_csv_safe(cell) for cell in row) for row in rows]
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
def _city_clause(scope: tuple[str, tuple[str, ...]] | None) -> tuple[str, list]:
    """Pure: turn a `cities.city_scope(...)` descriptor into a parameterized SQL fragment
    (no leading AND/WHERE — callers splice it in). `scope is None` -> `("", [])`, no
    filtering at all (this is what keeps module-off / no-scope byte-identical to today).
    Empty `exclude` -> equality (`event_city = ?`); non-empty `exclude` -> the default-city
    shape (`event_city IS NULL OR event_city NOT IN (?, ...)`), one placeholder per excluded
    code. City codes never get interpolated into the SQL string — only the ? count does."""
    if scope is None:
        return "", []
    code, exclude = scope
    if not exclude:
        return "event_city = ?", [code]
    placeholders = ", ".join("?" for _ in exclude)
    return f"(event_city IS NULL OR event_city NOT IN ({placeholders}))", list(exclude)


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
}


def _build_filter_clause(filters: list[dict]) -> tuple[str, list]:
    """Pure: build a parameterized AND WHERE clause from a filter spec.

    Column names come only from `_FILTER_COLUMNS` (or the literal `registration_date`);
    values are NEVER interpolated — they bind as `?`. Non-whitelisted fields are dropped.
    """
    clauses: list[str] = []
    params: list = []
    for f in filters:
        field = f.get("field")
        if field == "registration_date":
            op = ">=" if f.get("op") == "after" else "<"
            clauses.append(f"registration_date {op} ?")
            params.append(f.get("value"))
        elif field in _FILTER_COLUMNS:
            clauses.append(f"{field} = ?")
            params.append(f.get("value"))
        # non-whitelisted field → silently skipped (never interpolated)
    where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
    return where, params


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
