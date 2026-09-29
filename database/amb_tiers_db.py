"""Слой БД квалифицированной амбассадорки СкиллАп: подсчёт приглашённых и ступени.

Отдельный модуль, а не ещё один блок в `database/db.py`: тот уже больше 12 тысяч строк.
Схема (`ambassador_tiers`, `ambassador_exclusions`) по-прежнему создаётся в
`database.db.init_db` — схемой владеет только бот, здесь миграций нет.

Соединение берётся как `_db._connect()` через АТРИБУТ модуля, а не `from database.db import
_connect`: тесты подменяют `config.DB_PATH`/атрибуты `database.db`, и прямой импорт функции
закрепил бы старую ссылку (тот же приём, что у `settings_audit.py`).

Определения (решения владельца):
- приглашённый — `users.referrer_id = амбассадор`, сезон = текущий `event_season`, сам себя
  не приглашал (`telegram_id != referrer_id`), не исключён менеджером (`ambassador_exclusions`);
- прошёл отбор (qualified) — `status = 'approved'` прямо сейчас И нет «живого» одобрения в
  Mini App, у которого ещё не прошло окно отмены (строка `application_decisions` с
  `decision = 'approved'`, `effects_sent_at IS NULL`, `undone_at IS NULL`). Иначе одобрение
  другого приглашённого того же амбассадора засчитало бы ещё отменяемое решение, а ступень
  не снимается никогда;
- дошёл (arrived) — есть строка `checkins` с `point = CHECKIN_ENTRY_POINT`.
"""
from __future__ import annotations

import aiosqlite

from database import db as _db

_COUNTS_SELECT = """
    SELECT u.referrer_id AS referrer_id,
           COUNT(*) AS total,
           SUM(CASE WHEN u.status = 'pending' THEN 1 ELSE 0 END) AS pending,
           SUM(CASE WHEN u.status = 'approved' AND NOT EXISTS (
                   SELECT 1 FROM application_decisions d
                   WHERE d.telegram_id = u.telegram_id AND d.decision = 'approved'
                     AND d.effects_sent_at IS NULL AND d.undone_at IS NULL
               ) THEN 1 ELSE 0 END) AS qualified,
           SUM(CASE WHEN EXISTS (
                   SELECT 1 FROM checkins c
                   WHERE c.telegram_id = u.telegram_id AND c.point = ?
               ) THEN 1 ELSE 0 END) AS arrived
    FROM users u
    WHERE u.referrer_id IS NOT NULL
      AND u.telegram_id != u.referrer_id
      AND COALESCE(u.season, '') = ?
      AND u.telegram_id NOT IN (SELECT invitee_id FROM ambassador_exclusions)
"""

_ZERO = {"total": 0, "pending": 0, "qualified": 0, "arrived": 0}


def _counts_from_row(row) -> dict:
    return {
        "total": int(row["total"] or 0),
        "pending": int(row["pending"] or 0),
        "qualified": int(row["qualified"] or 0),
        "arrived": int(row["arrived"] or 0),
    }


async def referral_counts(referrer_id: int, season: str) -> dict:
    """`{"total", "pending", "qualified", "arrived"}` по приглашённым одного амбассадора.
    `season` — текущий `event_season` (пустая строка = сезон не настроен, тогда считаются
    приглашённые с пустым сезоном)."""
    async with _db._connect() as conn:
        conn.row_factory = aiosqlite.Row
        async with conn.execute(
            _COUNTS_SELECT + " AND u.referrer_id = ? GROUP BY u.referrer_id",
            (_db.CHECKIN_ENTRY_POINT, (season or "").strip(), int(referrer_id)),
        ) as cursor:
            row = await cursor.fetchone()
    return _counts_from_row(row) if row else dict(_ZERO)


async def referral_counts_bulk(referrer_ids: list[int] | None, season: str) -> dict[int, dict]:
    """То же одним запросом на много амбассадоров (CSV, бэкафилл). `None` — все, у кого есть
    хоть один приглашённый. Амбассадор без приглашённых в ответ не попадает — вызывающий
    подставляет нули сам."""
    sql = _COUNTS_SELECT
    params: list = [_db.CHECKIN_ENTRY_POINT, (season or "").strip()]
    if referrer_ids is not None:
        ids = [int(r) for r in referrer_ids]
        if not ids:
            return {}
        sql += f" AND u.referrer_id IN ({','.join('?' for _ in ids)})"
        params.extend(ids)
    sql += " GROUP BY u.referrer_id"
    async with _db._connect() as conn:
        conn.row_factory = aiosqlite.Row
        async with conn.execute(sql, params) as cursor:
            rows = await cursor.fetchall()
    return {int(r["referrer_id"]): _counts_from_row(r) for r in rows}


async def claim_new_tiers(telegram_id: int, tiers: list[int], reached_at: str,
                          o2o_quota: int, o2o_tier: int = 2) -> list[dict]:
    """Записывает достигнутые ступени и возвращает ТОЛЬКО новые: `[{"tier", "o2o_status"}]`.

    Одна транзакция `BEGIN IMMEDIATE`: второй процесс (бот и веб) ждёт, пока первый не
    закоммитит, и видит его строки до своего подсчёта квоты — 16-й слот O2O при квоте 15 не
    выдаётся. Уникальность держит PRIMARY KEY (telegram_id, tier): `INSERT OR IGNORE` +
    `rowcount == 1`, проверки «а не выдавали ли уже» в Python нет.

    Порядок раздачи O2O = порядок успешной записи строки ступени `o2o_tier`: у живых путей
    это порядок, в котором одобрения довели амбассадоров до порога; бэкафилл зовёт функцию
    последовательно, упорядочив амбассадоров по `users.approved_at`. Сверх квоты — 'waitlist'."""
    new_rows: list[dict] = []
    async with _db._connect() as conn:
        await conn.execute("BEGIN IMMEDIATE")
        try:
            for tier in sorted({int(t) for t in tiers}):
                o2o_status = None
                if tier == o2o_tier:
                    async with conn.execute(
                        "SELECT COUNT(*) FROM ambassador_tiers "
                        "WHERE tier = ? AND o2o_status = 'granted'",
                        (o2o_tier,),
                    ) as cursor:
                        granted = (await cursor.fetchone())[0]
                    o2o_status = "granted" if granted < int(o2o_quota) else "waitlist"
                cursor = await conn.execute(
                    "INSERT OR IGNORE INTO ambassador_tiers "
                    "(telegram_id, tier, reached_at, o2o_status, notified_at) "
                    "VALUES (?, ?, ?, ?, NULL)",
                    (int(telegram_id), tier, reached_at, o2o_status),
                )
                if cursor.rowcount == 1:
                    new_rows.append({"tier": tier, "o2o_status": o2o_status})
            await conn.commit()
        except Exception:
            await conn.rollback()
            raise
    return new_rows


async def mark_tiers_notified(telegram_id: int, tiers: list[int], at: str) -> None:
    """Проставляет `notified_at` без отправки: младшие ступени, перекрытые старшей в том же
    одобрении, и тихий бэкафилл без уведомлений."""
    tiers = [int(t) for t in tiers]
    if not tiers:
        return
    async with _db._connect() as conn:
        await conn.execute(
            f"UPDATE ambassador_tiers SET notified_at = ? WHERE telegram_id = ? "
            f"AND tier IN ({','.join('?' for _ in tiers)}) AND notified_at IS NULL",
            (at, int(telegram_id), *tiers),
        )
        await conn.commit()


async def claim_tier_notification(telegram_id: int, tier: int, at: str) -> dict | None:
    """Атомарно «забирает» право отправить уведомление: ставит `notified_at`, если он был
    пустым, и возвращает строку ступени. Повторный разбор того же события — `None`."""
    async with _db._connect() as conn:
        conn.row_factory = aiosqlite.Row
        cursor = await conn.execute(
            "UPDATE ambassador_tiers SET notified_at = ? "
            "WHERE telegram_id = ? AND tier = ? AND notified_at IS NULL",
            (at, int(telegram_id), int(tier)),
        )
        won = cursor.rowcount == 1
        await conn.commit()
        if not won:
            return None
        async with conn.execute(
            "SELECT * FROM ambassador_tiers WHERE telegram_id = ? AND tier = ?",
            (int(telegram_id), int(tier)),
        ) as cur:
            row = await cur.fetchone()
    return dict(row) if row else None


async def release_tier_notification(telegram_id: int, tier: int) -> None:
    """Снимает `notified_at` после временного сбоя отправки — очередь сделает ретрай."""
    async with _db._connect() as conn:
        await conn.execute(
            "UPDATE ambassador_tiers SET notified_at = NULL WHERE telegram_id = ? AND tier = ?",
            (int(telegram_id), int(tier)),
        )
        await conn.commit()


async def list_tiers(telegram_id: int | None = None) -> list[dict]:
    """Строки ступеней (одного амбассадора или все) по возрастанию ступени и времени."""
    sql = "SELECT * FROM ambassador_tiers"
    params: tuple = ()
    if telegram_id is not None:
        sql += " WHERE telegram_id = ?"
        params = (int(telegram_id),)
    sql += " ORDER BY telegram_id, tier"
    async with _db._connect() as conn:
        conn.row_factory = aiosqlite.Row
        async with conn.execute(sql, params) as cursor:
            rows = await cursor.fetchall()
    return [dict(r) for r in rows]


async def o2o_summary() -> dict:
    """`{"granted", "waitlist"}` — сколько слотов O2O выдано и сколько человек в листе ожидания."""
    async with _db._connect() as conn:
        async with conn.execute(
            "SELECT o2o_status, COUNT(*) FROM ambassador_tiers "
            "WHERE o2o_status IN ('granted', 'waitlist') GROUP BY o2o_status"
        ) as cursor:
            rows = await cursor.fetchall()
    result = {"granted": 0, "waitlist": 0}
    for status, count in rows:
        result[status] = int(count)
    return result


async def referral_coin_ordinals(user_id: int) -> dict[int, int]:
    """`{coins.id: N}` — порядковый номер каждой строки начисления за приглашённого
    (`source = 'referral'`) среди таких строк пользователя, по возрастанию `coins.id`, с 1.
    Номер не зависит от страницы истории и поверхности (бот/Mini App)."""
    async with _db._connect() as conn:
        async with conn.execute(
            "SELECT id FROM coins WHERE user_id = ? AND source = 'referral' ORDER BY id",
            (int(user_id),),
        ) as cursor:
            rows = await cursor.fetchall()
    return {int(row[0]): n for n, row in enumerate(rows, start=1)}


# ── Админка «🎓 Ступени амбассадоров»: выгрузка и ручное исключение ────────────────────────

_O2O_LABELS = {"granted": "выдан", "waitlist": "лист ожидания"}

AMB_CSV_HEADERS = [
    "telegram_id", "@username", "Дата вступления", "Всего по ссылке", "На рассмотрении",
    "Прошли отбор", "Дошли", "Текущая ступень", "Дата ступени", "Разбор резюме (O2O)",
]


async def export_ambassador_tiers_csv(season: str) -> tuple[list[str], list[list]]:
    """`(headers, rows)` выгрузки «Амбассадоры: прогресс и ступени».

    Строка — это АМБАССАДОР (сейчас `is_ambassador = 1` или уже получил ступень): ни одного
    поля приглашённых в файл не идёт, только числа по ним. Счётчики — те же, что у ступеней
    (`referral_counts_bulk`, текущий сезон). Сортировка по ТЗ: больше дошедших выше, при
    равенстве — больше прошедших отбор, затем раньше достигнутая текущая ступень."""
    async with _db._connect() as conn:
        conn.row_factory = aiosqlite.Row
        async with conn.execute(
            "SELECT telegram_id, username, ambassador_since FROM users "
            "WHERE is_ambassador = 1 "
            "OR telegram_id IN (SELECT telegram_id FROM ambassador_tiers)"
        ) as cursor:
            people = [dict(r) for r in await cursor.fetchall()]
        async with conn.execute(
            "SELECT telegram_id, tier, reached_at, o2o_status FROM ambassador_tiers"
        ) as cursor:
            tier_rows = [dict(r) for r in await cursor.fetchall()]
    counts = await referral_counts_bulk([p["telegram_id"] for p in people], season)

    top: dict[int, dict] = {}
    o2o: dict[int, str | None] = {}
    for row in tier_rows:
        tid = int(row["telegram_id"])
        if tid not in top or int(row["tier"]) > int(top[tid]["tier"]):
            top[tid] = row
        if int(row["tier"]) == 2:
            o2o[tid] = row["o2o_status"]

    records = []
    for person in people:
        tid = int(person["telegram_id"])
        c = counts.get(tid, _ZERO)
        best = top.get(tid)
        records.append((c, best, person))
    records.sort(key=lambda rec: (
        -rec[0]["arrived"], -rec[0]["qualified"],
        rec[1]["reached_at"] if rec[1] else "9999",
        int(rec[2]["telegram_id"]),
    ))

    rows = []
    for c, best, person in records:
        tid = int(person["telegram_id"])
        username = (person.get("username") or "").strip().lstrip("@")
        rows.append([
            tid,
            # Без «@» в ячейке: ведущая «@» — триггер формулы, `_csv_safe` приписал бы к ней
            # апостроф, и в Excel было бы видно «'@name». Заголовок колонки говорит, что это.
            _db._csv_safe(username),
            _db._csv_safe(person.get("ambassador_since") or ""),
            c["total"], c["pending"], c["qualified"], c["arrived"],
            int(best["tier"]) if best else 0,
            _db._csv_safe(best["reached_at"] if best else ""),
            _db._csv_safe(_O2O_LABELS.get(o2o.get(tid) or "", "—")),
        ])
    return list(AMB_CSV_HEADERS), rows


async def exclude_invitee(invitee_id: int, reason: str, excluded_by: int | None, at: str) -> bool:
    """Исключает приглашённого из зачёта. `False` — уже был исключён (повторное нажатие)."""
    async with _db._connect() as conn:
        cursor = await conn.execute(
            "INSERT OR IGNORE INTO ambassador_exclusions "
            "(invitee_id, reason, excluded_by, excluded_at) VALUES (?, ?, ?, ?)",
            (int(invitee_id), reason, excluded_by, at),
        )
        won = cursor.rowcount == 1
        await conn.commit()
    return won


async def get_exclusion(invitee_id: int) -> dict | None:
    async with _db._connect() as conn:
        conn.row_factory = aiosqlite.Row
        async with conn.execute(
            "SELECT * FROM ambassador_exclusions WHERE invitee_id = ?", (int(invitee_id),)
        ) as cursor:
            row = await cursor.fetchone()
    return dict(row) if row else None


async def unexclude_invitee(invitee_id: int) -> dict | None:
    """Возвращает приглашённого в зачёт; отдаёт удалённую строку (`None` — его уже вернули)."""
    async with _db._connect() as conn:
        conn.row_factory = aiosqlite.Row
        async with conn.execute(
            "SELECT * FROM ambassador_exclusions WHERE invitee_id = ?", (int(invitee_id),)
        ) as cursor:
            row = await cursor.fetchone()
        if row is None:
            return None
        cursor = await conn.execute(
            "DELETE FROM ambassador_exclusions WHERE invitee_id = ?", (int(invitee_id),)
        )
        won = cursor.rowcount == 1
        await conn.commit()
    return dict(row) if won else None


async def count_exclusions() -> int:
    async with _db._connect() as conn:
        async with conn.execute("SELECT COUNT(*) FROM ambassador_exclusions") as cursor:
            return int((await cursor.fetchone())[0])


async def list_exclusions(limit: int = 10, offset: int = 0) -> list[dict]:
    """Исключённые для экрана МЕНЕДЖЕРА (ему имена видны): свежие сверху, с именем
    приглашённого и пригласившего."""
    async with _db._connect() as conn:
        conn.row_factory = aiosqlite.Row
        async with conn.execute(
            "SELECT e.invitee_id, e.reason, e.excluded_by, e.excluded_at, "
            "u.full_name AS invitee_name, u.username AS invitee_username, "
            "u.referrer_id AS referrer_id, r.full_name AS referrer_name "
            "FROM ambassador_exclusions e "
            "LEFT JOIN users u ON u.telegram_id = e.invitee_id "
            "LEFT JOIN users r ON r.telegram_id = u.referrer_id "
            "ORDER BY e.excluded_at DESC, e.invitee_id DESC LIMIT ? OFFSET ?",
            (int(limit), int(offset)),
        ) as cursor:
            rows = await cursor.fetchall()
    return [dict(r) for r in rows]
