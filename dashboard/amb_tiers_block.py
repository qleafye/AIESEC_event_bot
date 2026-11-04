"""Блок «Ступени амбассадоров» дашборда — только числа, без персональных данных.

Отдельный модуль, а не ещё одна функция в `dashboard/queries.py` (тот уже за 2400 строк).
Дашборд — отдельный контейнер, копирует только каталог `dashboard/`: ни `database/`, ни
`services/` ему недоступны, поэтому SQL здесь самодостаточный и повторяет определения бота
(`database/amb_tiers_db.py`): приглашённый — `users.referrer_id = амбассадор`, сезон =
текущий `event_season`, не сам себя, не исключён менеджером; прошёл отбор — `status =
'approved'`, есть строка журнала зачётов без `excluded_at` (на старой схеме — без этого условия) и нет одобрения Mini App, ещё висящего в окне отмены. Паритет с ботом сверяет
`tests/test_amb_tiers_dashboard_backfill_su5.py`.

Ступеней 1–5 (`amb_tiers_count`), у любой может быть квота (`amb_tier{N}_quota_on`). Имена
ключей даёт корневой чистый модуль `amb_tier_keys` (в образе дашборда лежит рядом с
`shared/chat_score.py`), дефолты ниже повторяют реестр бота.

Скоуп страницы: город сужает круг АМБАССАДОРОВ (по их `event_city`); сезон задаёт сезон
приглашённых (по умолчанию — текущий). Выданные места квот и лист ожидания — общие на всё
событие, поэтому считаются без городского фильтра.
"""
from __future__ import annotations

import sqlite3

from shared.amb_tier_keys import MAX_TIERS, tier_key

_QUOTA_DEFAULT = 15
_DEFAULT_THRESHOLDS = {1: 1, 2: 3, 3: 7, 4: 10, 5: 15}


def _setting(conn, key: str) -> str | None:
    row = conn.execute("SELECT value FROM bot_settings WHERE key = ?", (key,)).fetchone()
    return row[0] if row is not None else None


# Образ дашборда не содержит `domain`. Без реестра городскими остаются только ключи,
# которые этот дашборд сам читает через `setting_for_city_scope`: четыре настройки ступеней
# и порог/квота/тумблер квоты ступеней 1–5 (`shared.amb_tier_keys.tier_key`). Иной ключ,
# включая `event_season`, без пакета — общее значение.
_DASHBOARD_CITY_KEYS = frozenset({
    "amb_qualified_program",
    "amb_tiers_count",
    "amb_team_selection_enabled",
    "amb_slots_limit",
    *(
        tier_key(n, kind)
        for n in range(1, MAX_TIERS + 1)
        for kind in ("threshold", "quota", "quota_on")
    ),
})


def _schema_per_city(key: str) -> bool:
    """`SETTINGS_SCHEMA[key]["per_city"] is True`. Статический импорт `domain` в дашборд
    нельзя: образ содержит только `dashboard/` и `shared/`, а реестр тянет `database.db`.
    Если пакета нет — городской только ключ из `_DASHBOARD_CITY_KEYS`."""
    try:
        import importlib
        schema = importlib.import_module("domain.settings.schema").SETTINGS_SCHEMA
    except ImportError:
        return key in _DASHBOARD_CITY_KEYS
    return bool(schema.get(key, {}).get("per_city"))


def setting_for_city_scope(conn, key: str, scope) -> str | None:
    """Лестница городского чтения на этом соединении. Общее значение, если модуль городов
    не `on`, у ключа нет `per_city`, город пустой или `*`, либо оверрайд пустой."""
    general = _setting(conn, key)
    if _setting(conn, "event_city_enabled") != "on":
        return general
    if not _schema_per_city(key):
        return general
    city = getattr(scope, "city", None) if scope is not None else None
    if not city or city == "*":
        return general
    override = _setting(conn, f"{key}__city__{city}")
    if override is None or str(override).strip() == "":
        return general
    return override


def _int_setting(conn, key: str, default: int, *, allow_zero: bool = False) -> int:
    try:
        value = int(_setting(conn, key) or default)
    except ValueError:
        return default
    if value < 0 or (value == 0 and not allow_zero):
        return default
    return value


def _int_setting_scope(conn, key: str, scope, default: int, *, allow_zero: bool = False) -> int:
    try:
        value = int(setting_for_city_scope(conn, key, scope) or default)
    except ValueError:
        return default
    if value < 0 or (value == 0 and not allow_zero):
        return default
    return value


def amb_tiers_block(conn, scope) -> "dict | None":
    """`None` — программа выключена или в базе ещё нет таблиц ступеней (старая БД).
    Иначе `{"active", "qualified_total", "o2o_granted", "o2o_quota", "waitlist", "tiers"}`:
    `tiers` — по ступеням `{"tier", "threshold", "reached", "quota", "granted", "waitlist"}`
    (`quota` — `None`, если квота ступени выключена); `o2o_*` — сводка по первой ступени с
    квотой (старые потребители), без квот — по ступени 2."""
    # Ленивый импорт: queries.py импортирует этот модуль, круг замыкаем при вызове.
    from dashboard.queries import _city_sql

    try:
        if (setting_for_city_scope(conn, "amb_qualified_program", scope) or "off") != "on":
            return None
        count = max(1, min(MAX_TIERS, _int_setting_scope(conn, "amb_tiers_count", scope, 3)))
        cfg = []
        for n in range(1, count + 1):
            quota_on = (setting_for_city_scope(conn, tier_key(n, "quota_on"), scope) or "off") == "on"
            cfg.append({
                "tier": n,
                "threshold": _int_setting_scope(conn, tier_key(n, "threshold"), scope, _DEFAULT_THRESHOLDS[n]),
                "quota": (
                    _int_setting_scope(conn, tier_key(n, "quota"), scope, _QUOTA_DEFAULT, allow_zero=True)
                    if quota_on else None
                ),
            })

        season = scope.season if getattr(scope, "season", None) else (_setting(conn, "event_season") or "")
        city_frag, city_params = _city_sql(conn, getattr(scope, "city", None))
        amb_where = "is_ambassador = 1" + (f" AND {city_frag}" if city_frag else "")

        journal = ""
        if any(r[1] == "excluded_at" for r in conn.execute("PRAGMA table_info(referral_credits)")):
            journal = (
                "AND EXISTS (SELECT 1 FROM referral_credits rc WHERE rc.invitee_id = u.telegram_id "
                "  AND rc.excluded_at IS NULL) "
            )
        rows = conn.execute(
            "SELECT u.referrer_id AS rid, COUNT(*) AS qualified FROM users u "
            f"WHERE u.referrer_id IN (SELECT telegram_id FROM users WHERE {amb_where}) "
            "AND u.telegram_id != u.referrer_id "
            "AND COALESCE(u.season, '') = ? "
            "AND u.telegram_id NOT IN (SELECT invitee_id FROM ambassador_exclusions) "
            "AND u.status = 'approved' "
            + journal +
            "AND NOT EXISTS (SELECT 1 FROM application_decisions d "
            "  WHERE d.telegram_id = u.telegram_id AND d.decision = 'approved' "
            "  AND d.effects_sent_at IS NULL AND d.undone_at IS NULL) "
            "GROUP BY u.referrer_id",
            (*city_params, (season or "").strip()),
        ).fetchall()
        per_tier: dict[int, dict] = {}
        for tier, status, n in conn.execute(
            "SELECT tier, o2o_status, COUNT(*) FROM ambassador_tiers "
            "WHERE o2o_status IN ('granted', 'waitlist') GROUP BY tier, o2o_status"
        ).fetchall():
            per_tier.setdefault(int(tier), {})[status] = int(n)
    except sqlite3.OperationalError:
        return None

    tiers = []
    for c in cfg:
        counts = per_tier.get(c["tier"], {})
        tiers.append({
            **c,
            "reached": sum(1 for r in rows if int(r[1]) >= c["threshold"]),
            "granted": int(counts.get("granted", 0)),
            "waitlist": int(counts.get("waitlist", 0)),
        })
    primary = next((t for t in tiers if t["quota"] is not None), None)
    if primary is None:
        primary = next((t for t in tiers if t["tier"] == 2), None) or {
            "quota": _QUOTA_DEFAULT, "granted": 0, "waitlist": 0,
        }
    return {
        "active": sum(1 for r in rows if int(r[1]) > 0),
        "qualified_total": sum(int(r[1]) for r in rows),
        "o2o_granted": primary["granted"],
        "o2o_quota": primary["quota"] if primary["quota"] is not None else _QUOTA_DEFAULT,
        "waitlist": primary["waitlist"],
        "tiers": tiers,
    }
