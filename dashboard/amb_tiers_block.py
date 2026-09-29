"""Блок «Ступени амбассадоров» дашборда — только числа, без персональных данных.

Отдельный модуль, а не ещё одна функция в `dashboard/queries.py` (тот уже за 2400 строк).
Дашборд — отдельный контейнер, копирует только каталог `dashboard/`: ни `database/`, ни
`services/` ему недоступны, поэтому SQL здесь самодостаточный и повторяет определения бота
(`database/amb_tiers_db.py`): приглашённый — `users.referrer_id = амбассадор`, сезон =
текущий `event_season`, не сам себя, не исключён менеджером; прошёл отбор — `status =
'approved'` и нет одобрения Mini App, ещё висящего в окне отмены. Паритет с ботом сверяет
`tests/test_amb_tiers_dashboard_backfill_su5.py`.

Скоуп страницы: город сужает круг АМБАССАДОРОВ (по их `event_city`); сезон задаёт сезон
приглашённых (по умолчанию — текущий). Слоты разбора резюме и лист ожидания — общие на всё
событие (квота одна), поэтому считаются без городского фильтра.
"""
from __future__ import annotations

import sqlite3

_QUOTA_DEFAULT = 15


def _setting(conn, key: str) -> str | None:
    row = conn.execute("SELECT value FROM bot_settings WHERE key = ?", (key,)).fetchone()
    return row[0] if row is not None else None


def amb_tiers_block(conn, scope) -> "dict | None":
    """`None` — программа выключена или в базе ещё нет таблиц ступеней (старая БД).
    Иначе `{"active", "qualified_total", "o2o_granted", "o2o_quota", "waitlist"}`."""
    # Ленивый импорт: queries.py импортирует этот модуль, круг замыкаем при вызове.
    from dashboard.queries import _city_sql

    try:
        if (_setting(conn, "amb_qualified_program") or "off") != "on":
            return None
        try:
            quota = int(_setting(conn, "amb_o2o_quota") or _QUOTA_DEFAULT)
        except ValueError:
            quota = _QUOTA_DEFAULT
        if quota <= 0:
            quota = _QUOTA_DEFAULT

        season = scope.season if getattr(scope, "season", None) else (_setting(conn, "event_season") or "")
        city_frag, city_params = _city_sql(conn, getattr(scope, "city", None))
        amb_where = "is_ambassador = 1" + (f" AND {city_frag}" if city_frag else "")

        rows = conn.execute(
            "SELECT u.referrer_id AS rid, COUNT(*) AS qualified FROM users u "
            f"WHERE u.referrer_id IN (SELECT telegram_id FROM users WHERE {amb_where}) "
            "AND u.telegram_id != u.referrer_id "
            "AND COALESCE(u.season, '') = ? "
            "AND u.telegram_id NOT IN (SELECT invitee_id FROM ambassador_exclusions) "
            "AND u.status = 'approved' "
            "AND NOT EXISTS (SELECT 1 FROM application_decisions d "
            "  WHERE d.telegram_id = u.telegram_id AND d.decision = 'approved' "
            "  AND d.effects_sent_at IS NULL AND d.undone_at IS NULL) "
            "GROUP BY u.referrer_id",
            (*city_params, (season or "").strip()),
        ).fetchall()
        o2o = dict(conn.execute(
            "SELECT o2o_status, COUNT(*) FROM ambassador_tiers "
            "WHERE o2o_status IN ('granted', 'waitlist') GROUP BY o2o_status"
        ).fetchall())
    except sqlite3.OperationalError:
        return None

    return {
        "active": sum(1 for r in rows if int(r[1]) > 0),
        "qualified_total": sum(int(r[1]) for r in rows),
        "o2o_granted": int(o2o.get("granted", 0)),
        "o2o_quota": quota,
        "waitlist": int(o2o.get("waitlist", 0)),
    }
