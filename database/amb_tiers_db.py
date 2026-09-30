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
- прошёл отбор (qualified) — есть строка журнала `referral_credits` без `excluded_at`, `status =
  'approved'` прямо сейчас И нет «живого» одобрения в
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
           SUM(CASE WHEN u.status = 'approved' AND EXISTS (
                   SELECT 1 FROM referral_credits rc
                   WHERE rc.invitee_id = u.telegram_id AND rc.excluded_at IS NULL
               ) AND NOT EXISTS (
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
                          quotas: "dict[int, int | None] | int | None" = None, *,
                          o2o_quota: int | None = None, o2o_tier: int = 2,
                          season: str | None = None) -> list[dict]:
    """Записывает достигнутые ступени и возвращает ТОЛЬКО новые: `[{"tier", "o2o_status"}]`.

    `quotas` — `{ступень: квота}` для ступеней с включённой квотой (на остальных квоты нет и
    статус `None`). Прежний вызов с числом четвёртым аргументом или `o2o_quota=` читается как
    `{o2o_tier: квота}`. Колонка `o2o_status` осталась под прежним именем, но значит «статус
    квоты этой ступени»: 'granted' (место за человеком) или 'waitlist' (сверх квоты).

    Одна транзакция `BEGIN IMMEDIATE`: второй процесс (бот и веб) ждёт, пока первый не
    закоммитит, и видит его строки до своего подсчёта квоты — последнее место не достанется
    двоим. Уникальность держит PRIMARY KEY (telegram_id, tier): `INSERT OR IGNORE` +
    `rowcount == 1`, проверки «а не выдавали ли уже» в Python нет.

    Порядок раздачи = порядок успешной записи строки ступени: у живых путей это порядок, в
    котором одобрения довели амбассадоров до порога; бэкафилл зовёт функцию последовательно,
    упорядочив амбассадоров по `users.approved_at`. Сверх квоты — 'waitlist'.

    `season` задан — ступени, снятые менеджером вручную (`amb_tier_revocations`), и все старшие
    не выдаются: это общая нижняя точка всех путей автовыдачи. Проверка внутри той же
    транзакции, что и запись, — гонки со снятием нет."""
    if isinstance(quotas, int) and not isinstance(quotas, bool):
        quotas = {int(o2o_tier): int(quotas)}
    elif quotas is None:
        quotas = {int(o2o_tier): int(o2o_quota)} if o2o_quota is not None else {}
    limits = {int(t): q for t, q in quotas.items() if q is not None}
    new_rows: list[dict] = []
    async with _db._connect() as conn:
        await conn.execute("BEGIN IMMEDIATE")
        try:
            floor = await _revoked_floor(conn, int(telegram_id), season) if season else None
            for tier in sorted({int(t) for t in tiers}):
                if floor is not None and tier >= floor:
                    continue
                status = None
                if tier in limits:
                    async with conn.execute(
                        "SELECT COUNT(*) FROM ambassador_tiers "
                        "WHERE tier = ? AND o2o_status = 'granted'",
                        (tier,),
                    ) as cursor:
                        granted = (await cursor.fetchone())[0]
                    status = "granted" if granted < int(limits[tier]) else "waitlist"
                cursor = await conn.execute(
                    "INSERT OR IGNORE INTO ambassador_tiers "
                    "(telegram_id, tier, reached_at, o2o_status, notified_at) "
                    "VALUES (?, ?, ?, ?, NULL)",
                    (int(telegram_id), tier, reached_at, status),
                )
                if cursor.rowcount == 1:
                    new_rows.append({"tier": tier, "o2o_status": status})
            await conn.commit()
        except Exception:
            await conn.rollback()
            raise
    return new_rows


async def _revoked_floor(conn, telegram_id: int, season: str) -> int | None:
    """Младшая из снятых вручную ступеней человека в сезоне (`None` — снятых нет)."""
    async with conn.execute(
        "SELECT MIN(tier) FROM amb_tier_revocations WHERE telegram_id = ? AND season = ?",
        (int(telegram_id), season),
    ) as cursor:
        value = (await cursor.fetchone())[0]
    return int(value) if value is not None else None


async def revoked_floor(telegram_id: int, season: str) -> int | None:
    """Ступень, с которой автовыдача человеку закрыта (снятая вручную и все старшие)."""
    async with _db._connect() as conn:
        return await _revoked_floor(conn, telegram_id, season)


async def revoked_floors(season: str) -> dict[int, int]:
    """`{telegram_id: младшая снятая ступень}` за сезон — для предпросмотра бэкафилла."""
    async with _db._connect() as conn:
        async with conn.execute(
            "SELECT telegram_id, MIN(tier) FROM amb_tier_revocations WHERE season = ? "
            "GROUP BY telegram_id", (season,),
        ) as cursor:
            return {int(t): int(n) for t, n in await cursor.fetchall()}


async def list_revocations(season: str) -> list[dict]:
    """Снятые вручную ступени сезона: по времени снятия, новые сверху."""
    async with _db._connect() as conn:
        conn.row_factory = aiosqlite.Row
        async with conn.execute(
            "SELECT * FROM amb_tier_revocations WHERE season = ? "
            "ORDER BY revoked_at DESC, telegram_id, tier", (season,),
        ) as cursor:
            return [dict(r) for r in await cursor.fetchall()]


async def remove_revocation(telegram_id: int, tier: int, season: str) -> bool:
    """Снимает метку «снята вручную». `False` — метки уже нет (повторное нажатие)."""
    async with _db._connect() as conn:
        cursor = await conn.execute(
            "DELETE FROM amb_tier_revocations WHERE telegram_id = ? AND tier = ? AND season = ?",
            (int(telegram_id), int(tier), season),
        )
        await conn.commit()
        return cursor.rowcount == 1


async def revoke_tier_sticky(telegram_id: int, tier: int, season: str, *, by: int | None,
                             at: str) -> dict | None:
    """Ручное снятие одной транзакцией: удаляет строку ступени и пишет метку, которая не даёт
    автоматике выдать её снова. `None` — такой ступени у человека нет (повторное нажатие)."""
    async with _db._connect() as conn:
        conn.row_factory = aiosqlite.Row
        await conn.execute("BEGIN IMMEDIATE")
        try:
            async with conn.execute(
                "SELECT * FROM ambassador_tiers WHERE telegram_id = ? AND tier = ?",
                (int(telegram_id), int(tier)),
            ) as cursor:
                row = await cursor.fetchone()
            if row is None:
                await conn.rollback()
                return None
            await conn.execute(
                "DELETE FROM ambassador_tiers WHERE telegram_id = ? AND tier = ?",
                (int(telegram_id), int(tier)),
            )
            await conn.execute(
                "INSERT OR REPLACE INTO amb_tier_revocations "
                "(telegram_id, tier, season, revoked_at, revoked_by) VALUES (?, ?, ?, ?, ?)",
                (int(telegram_id), int(tier), season, at, by),
            )
            await conn.commit()
        except Exception:
            await conn.rollback()
            raise
    return dict(row)


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


async def quota_summary() -> dict[int, dict]:
    """`{ступень: {"granted", "waitlist"}}` по ступеням, где хоть раз разыгрывалась квота."""
    async with _db._connect() as conn:
        async with conn.execute(
            "SELECT tier, o2o_status, COUNT(*) FROM ambassador_tiers "
            "WHERE o2o_status IN ('granted', 'waitlist') GROUP BY tier, o2o_status"
        ) as cursor:
            rows = await cursor.fetchall()
    result: dict[int, dict] = {}
    for tier, status, count in rows:
        result.setdefault(int(tier), {"granted": 0, "waitlist": 0})[status] = int(count)
    return result


async def delete_tier_row(telegram_id: int, tier: int) -> dict | None:
    """Удаляет строку ступени амбассадора и отдаёт её (`None` — такой строки уже нет: повторное
    нажатие). Выданное место квоты освобождается само: квота считается по строкам `granted`."""
    async with _db._connect() as conn:
        conn.row_factory = aiosqlite.Row
        await conn.execute("BEGIN IMMEDIATE")
        try:
            async with conn.execute(
                "SELECT * FROM ambassador_tiers WHERE telegram_id = ? AND tier = ?",
                (int(telegram_id), int(tier)),
            ) as cursor:
                row = await cursor.fetchone()
            if row is None:
                await conn.rollback()
                return None
            cursor = await conn.execute(
                "DELETE FROM ambassador_tiers WHERE telegram_id = ? AND tier = ?",
                (int(telegram_id), int(tier)),
            )
            won = cursor.rowcount == 1
            await conn.commit()
        except Exception:
            await conn.rollback()
            raise
    return dict(row) if won else None


async def first_waitlisted(tier: int) -> dict | None:
    """Первый в листе ожидания ступени: самый ранний `reached_at` (при равенстве — меньший id)."""
    async with _db._connect() as conn:
        conn.row_factory = aiosqlite.Row
        async with conn.execute(
            "SELECT * FROM ambassador_tiers WHERE tier = ? AND o2o_status = 'waitlist' "
            "ORDER BY reached_at, telegram_id LIMIT 1",
            (int(tier),),
        ) as cursor:
            row = await cursor.fetchone()
    return dict(row) if row else None


async def promote_first_waitlisted(tier: int, quota: int, *, at: str) -> int | None:
    """Отдаёт свободное место ступени первому из листа ожидания и возвращает его id.
    `None` — места нет или лист пуст. `BEGIN IMMEDIATE` + условный `UPDATE ... WHERE
    o2o_status = 'waitlist'` + `rowcount`: два менеджера за одно место дадут одно повышение.
    `notified_at` сбрасывается — человеку уйдёт текст ступени (ставит вызывающий)."""
    async with _db._connect() as conn:
        await conn.execute("BEGIN IMMEDIATE")
        try:
            async with conn.execute(
                "SELECT COUNT(*) FROM ambassador_tiers WHERE tier = ? AND o2o_status = 'granted'",
                (int(tier),),
            ) as cursor:
                granted = (await cursor.fetchone())[0]
            if granted >= int(quota):
                await conn.rollback()
                return None
            async with conn.execute(
                "SELECT telegram_id FROM ambassador_tiers WHERE tier = ? "
                "AND o2o_status = 'waitlist' ORDER BY reached_at, telegram_id LIMIT 1",
                (int(tier),),
            ) as cursor:
                row = await cursor.fetchone()
            if row is None:
                await conn.rollback()
                return None
            cursor = await conn.execute(
                "UPDATE ambassador_tiers SET o2o_status = 'granted', notified_at = NULL "
                "WHERE telegram_id = ? AND tier = ? "
                "AND o2o_status = 'waitlist'",
                (int(row[0]), int(tier)),
            )
            won = cursor.rowcount == 1
            await conn.commit()
        except Exception:
            await conn.rollback()
            raise
    return int(row[0]) if won else None


async def waitlist_count(tier: int) -> int:
    async with _db._connect() as conn:
        async with conn.execute(
            "SELECT COUNT(*) FROM ambassador_tiers WHERE tier = ? AND o2o_status = 'waitlist'",
            (int(tier),),
        ) as cursor:
            return int((await cursor.fetchone())[0])


async def tiers_summary(season: str | None = None) -> dict[int, dict]:
    """`{ступень: {"reached", "granted", "waitlist"}}` по ступеням, которые кто-то получил:
    достигли всего, место выдано, ждут места. `season` не используется — `ambassador_tiers`
    привязана к событию самой базой (один стек = одно событие); параметр оставлен для
    единообразия с остальными счётчиками модуля."""
    async with _db._connect() as conn:
        async with conn.execute(
            "SELECT tier, COUNT(*), "
            "SUM(CASE WHEN o2o_status = 'granted' THEN 1 ELSE 0 END), "
            "SUM(CASE WHEN o2o_status = 'waitlist' THEN 1 ELSE 0 END) "
            "FROM ambassador_tiers GROUP BY tier"
        ) as cursor:
            rows = await cursor.fetchall()
    return {
        int(tier): {"reached": int(total), "granted": int(granted or 0), "waitlist": int(wait or 0)}
        for tier, total, granted, wait in rows
    }


async def stale_unnotified(older_than: str) -> list[dict]:
    """Ступени без отметки `notified_at`, достигнутые раньше `older_than` (строка в формате
    `YYYY-MM-DD HH:MM:SS`): уведомление потерялось или ещё в очереди."""
    async with _db._connect() as conn:
        conn.row_factory = aiosqlite.Row
        async with conn.execute(
            "SELECT * FROM ambassador_tiers WHERE notified_at IS NULL AND reached_at < ? "
            "ORDER BY telegram_id, tier",
            (older_than,),
        ) as cursor:
            rows = await cursor.fetchall()
    return [dict(r) for r in rows]


async def active_ambassadors_with_invitees(season: str) -> list[int]:
    """Действующие амбассадоры, у которых есть хотя бы один приглашённый текущего сезона."""
    async with _db._connect() as conn:
        async with conn.execute(
            "SELECT u.telegram_id FROM users u WHERE u.is_ambassador = 1 AND EXISTS ("
            "  SELECT 1 FROM users i WHERE i.referrer_id = u.telegram_id "
            "  AND i.telegram_id != u.telegram_id AND COALESCE(i.season, '') = ?) "
            "ORDER BY u.telegram_id",
            ((season or "").strip(),),
        ) as cursor:
            rows = await cursor.fetchall()
    return [int(r[0]) for r in rows]


async def has_pending_tier_event(kind: str, telegram_id: int, tier: int) -> bool:
    """Есть ли в `miniapp_outbox` необработанное событие этого вида про (амбассадор, ступень)."""
    import json

    async with _db._connect() as conn:
        async with conn.execute(
            "SELECT payload FROM miniapp_outbox WHERE kind = ? AND processed_at IS NULL",
            (kind,),
        ) as cursor:
            rows = await cursor.fetchall()
    for (raw,) in rows:
        try:
            payload = json.loads(raw)
        except (TypeError, ValueError):
            continue
        if int(payload.get("telegram_id") or 0) == int(telegram_id)                 and int(payload.get("tier") or 0) == int(tier):
            return True
    return False


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


async def qualified_approval_times(season: str) -> dict[int, list[str]]:
    """`{амбассадор: [approved_at прошедших отбор, по возрастанию]}` — порядок раздачи квоты
    разборов резюме у разового бэкафилла (кто раньше набрал порог, тот раньше в очереди).
    Те же условия, что у `referral_counts`; пустой `approved_at` (легаси) — в конец."""
    async with _db._connect() as conn:
        async with conn.execute(
            "SELECT u.referrer_id, COALESCE(u.approved_at, '') FROM users u "
            "WHERE u.referrer_id IS NOT NULL AND u.telegram_id != u.referrer_id "
            "AND COALESCE(u.season, '') = ? AND u.status = 'approved' "
            "AND u.telegram_id NOT IN (SELECT invitee_id FROM ambassador_exclusions) "
            "AND EXISTS (SELECT 1 FROM referral_credits rc "
            "  WHERE rc.invitee_id = u.telegram_id AND rc.excluded_at IS NULL) "
            "AND NOT EXISTS (SELECT 1 FROM application_decisions d "
            "  WHERE d.telegram_id = u.telegram_id AND d.decision = 'approved' "
            "  AND d.effects_sent_at IS NULL AND d.undone_at IS NULL)",
            ((season or "").strip(),),
        ) as cursor:
            rows = await cursor.fetchall()
    result: dict[int, list[str]] = {}
    for rid, approved_at in rows:
        result.setdefault(int(rid), []).append(approved_at or "9999")
    for times in result.values():
        times.sort()
    return result


# ── Заморозка прежних дефолтов ступеней (миграция user_version = 4) ──────────────────────────

_TIER_FREEZE_MIGRATION_USER_VERSION = 4


async def freeze_legacy_tier_defaults(db: aiosqlite.Connection) -> None:
    """Одноразово по `PRAGMA user_version`. Дефолты реестра ступеней стали нейтральными, а стек
    СкиллАп жил на прежних (1/3/7, квота 15, тексты и дедлайн). Если на стеке программа ступеней
    уже включена или сохранён любой ключ ступеней, прежние значения записываются явно для тех
    ключей, которых в `bot_settings` ещё нет (`INSERT OR IGNORE` — сохранённое не перетирается),
    плюс включается квота второй ступени. Стек без программы ничего не получает; сам
    `user_version` поднимается всегда, повторный старт — no-op."""
    import logging

    logger = logging.getLogger(__name__)
    async with db.execute("PRAGMA user_version") as cursor:
        row = await cursor.fetchone()
    if (row[0] if row else 0) >= _TIER_FREEZE_MIGRATION_USER_VERSION:
        return
    written = 0
    async with db.execute(
        "SELECT 1 FROM bot_settings WHERE (key = 'amb_qualified_program' AND value = 'on') "
        "OR key LIKE 'amb!_tier%' ESCAPE '!' OR key LIKE 'amb!_o2o%' ESCAPE '!' "
        "OR key = 'amb_count_deadline' LIMIT 1"
    ) as cursor:
        has_program = await cursor.fetchone() is not None
    if has_program:
        from reg_presets import SKILLUP_TIER_SETTINGS

        for key, value in SKILLUP_TIER_SETTINGS.items():
            cursor = await db.execute(
                "INSERT OR IGNORE INTO bot_settings (key, value) VALUES (?, ?)", (key, value),
            )
            written += cursor.rowcount
    await db.execute(f"PRAGMA user_version = {_TIER_FREEZE_MIGRATION_USER_VERSION}")
    logger.info("freeze_legacy_tier_defaults: записано ключей %s", written)
