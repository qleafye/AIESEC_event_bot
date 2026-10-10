"""Сброс статусов амбассадора, оставшихся после миграции: предпросмотр и применение.

Одна реализация для кнопки в админке («🤝 Амбассадоры → 🙋 Кандидаты и команда → 🧹 Сбросить
статусы») и для `tools/amb_status_reset.py`.

Режимы (флаги взаимоисключающие):
  SCOPE_PAST        все, у кого есть статус, а сезон строки не равен текущему сезону события;
  SCOPE_CANDIDATES  все кандидаты текущего сезона.

`preview` ничего не пишет. `apply` идёт через единственного писателя статуса
(`database.amb_status_db.set_status`) с проверкой прежнего статуса: если статус человека успел
смениться (менеджер нажал «Взять»), строка пропускается. Сообщений никому не шлёт, баллов не
начисляет и не снимает. Выданный пакет не трогает: место за человеком с выданным пакетом остаётся.
"""
from __future__ import annotations

from collections import Counter

SCOPE_PAST = "past-seasons"
SCOPE_CANDIDATES = "candidates-all"

STATUS_LABELS = {
    "candidate": "кандидат",
    "active": "амбассадор",
    "left": "вышел сам",
    "declined": "отказано",
}

NO_COLUMN = (
    "В базе нет статуса амбассадора. Сначала перезапустите бота на новой версии — "
    "при старте он сам добавит статус и перенесёт старые отметки. Потом запустите инструмент ещё раз."
)


NO_SEASON = (
    "Сезон события не задан, поэтому сбрасывать нельзя: без него бот не отличит прошлые сезоны от "
    "текущего и сбросил бы статусы у всех. Задайте «Сезон события» в настройках и откройте экран заново."
)


class SeasonNotSet(Exception):
    """Сезон события не задан — сброс запрещён (иначе под сброс попал бы и текущий сезон)."""


def ids_digest(rows: list[tuple]) -> str:
    """Короткий отпечаток списка людей из предпросмотра: кнопка «Сбросить» несёт его, и сброс
    идёт, только если состав за это время не изменился (одного числа мало: один ушёл, другой
    пришёл — число то же)."""
    import hashlib

    ids = sorted(int(r[0]) for r in rows)
    return hashlib.sha1(",".join(map(str, ids)).encode()).hexdigest()[:8]


async def has_status_column() -> bool:
    from database import db as _db

    async with _db._connect() as conn:
        async with conn.execute("PRAGMA table_info(users)") as cursor:
            return any(row[1] == "ambassador_status" for row in await cursor.fetchall())


async def collect(scope: str) -> dict:
    """Кого сбросим — только чтение. Возвращает сезон события и строки
    `(telegram_id, status, season, pack_at)`."""
    from database import db as _db

    season = ((await _db.get_setting("event_season")) or "").strip()
    if scope == SCOPE_CANDIDATES:
        where = "ambassador_status = 'candidate' AND COALESCE(season, '') = ?"
    elif scope == SCOPE_PAST:
        where = "ambassador_status IS NOT NULL AND COALESCE(season, '') != ?"
    else:
        raise ValueError(f"неизвестный режим: {scope!r}")
    async with _db._connect() as conn:
        async with conn.execute(
            "SELECT telegram_id, ambassador_status, COALESCE(season, ''), ambassador_pack_at "
            f"FROM users WHERE {where} ORDER BY telegram_id",
            (season,),
        ) as cursor:
            rows = [tuple(r) for r in await cursor.fetchall()]
    return {"season": season, "rows": rows}


def breakdown(rows: list[tuple]) -> list[str]:
    by_status = Counter(r[1] for r in rows)
    by_season = Counter(r[2] for r in rows)
    status_part = ", ".join(
        f"{STATUS_LABELS.get(s, s)} — {n}" for s, n in sorted(by_status.items())
    )
    season_part = ", ".join(
        (f"«{s}» — {n}" if s else f"без сезона — {n}") for s, n in sorted(by_season.items())
    )
    lines = [f"По статусу: {status_part}", f"По сезону: {season_part}"]
    no_season = sum(1 for r in rows if not r[2])
    if no_season:
        lines.append(
            f"Без сезона — {no_season}: старые записи, заведённые до того, как появились сезоны. "
            "Бот считает их прошлыми, поэтому они тоже будут сброшены."
        )
    with_pack = sum(1 for r in rows if r[3])
    if with_pack:
        lines.append(
            f"С выданным пакетом: {with_pack} — статус сбросится, место за ними останется."
        )
    return lines


async def preview(scope: str) -> dict:
    """Только чтение: `{season, rows}` — как `collect`."""
    return await collect(scope)


async def apply(scope: str, plan: dict | None = None) -> dict:
    """Сбрасывает статусы (по уже полученному `plan` из `preview` или по свежей выборке). Возвращает `{season, done: [row], skipped: [telegram_id]}`."""
    from database import amb_status_db
    from services.infra.timeutil import msk_now

    plan = plan if plan is not None else await collect(scope)
    if not (plan.get("season") or "").strip():
        raise SeasonNotSet(NO_SEASON)
    at = msk_now().strftime("%Y-%m-%d %H:%M:%S")
    done, skipped = [], []
    for row in plan["rows"]:
        tid, status = row[0], row[1]
        if await amb_status_db.set_status(tid, None, at=at, expect=(status,)):
            done.append(row)
        else:
            skipped.append(tid)
    return {"season": plan["season"], "done": done, "skipped": skipped}
