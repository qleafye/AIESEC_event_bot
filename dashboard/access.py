"""Phase 15 Plan 04 (STAT-02, D-09/D-10): пересверка права `stats` и городской скоуп.

Копия логики `handlers/admin_caps.py::resolve_capabilities` (СОЗНАТЕЛЬНОЕ дублирование, не
импорт — оригинал тянет `aiogram`, дашборду недоступен, см. 15-CONTEXT.md `<interfaces>`).
При изменении модели прав в боте (новая capability, новая роль, новый дефолт роли) — правь
ОБА места; сторожевой тест в `tests/test_dashboard_auth.py` сравнивает `ALL_CAPABILITIES` и
дефолты ролей этого модуля с `handlers.admin_caps`/`settings_schema.SETTINGS_SCHEMA` и падает
на дрейфе.

D-09 (без кэша, RESEARCH Pitfall 4): каждый вызов читает `staff`/`bot_settings` заново из
переданного `conn` (`dashboard.db.read_conn`) — то же правило "no-cache-by-design", что и в
оригинале (`handlers/admin_caps.py:9-11`). НЕ добавлять сюда декоратор мемоизации, модульный
словарь-кэш прав или что-либо, переживающее один вызов (грепом это ловит
tests/test_dashboard_auth.py — потому здесь нет и самого слова-триггера для этого грепа).

Заголовки, которые ставит прокси перед origin (см. `dashboard.auth.client_ip`), здесь НЕ
используются и не должны: решение о доступе и городской скоуп определяются ИСКЛЮЧИТЕЛЬНО
`telegram_id` из подписанной сессии (D-03/D-09) — такие заголовки управляемы извне, за
Cloudflare Tunnel общий адрес у всех, кто пришёл через один edge-узел.
"""
from __future__ import annotations

from dashboard.queries import Scope

# D-06 в handlers/admin_caps.py: ровно семь capability, в этом порядке.
ALL_CAPABILITIES = [
    "moderate_reg",
    "moderate_receipts",
    "moderate_game",
    "broadcast",
    "settings",
    "stats",
    "checkin",
]

# Дефолты `role_caps_<role>` — дублируют `settings_schema.SETTINGS_SCHEMA["role_caps_<role>"]
# ["default"]`. Известные роли ограничены этим словарём (чужое имя роли в `staff` игнорируется,
# как и в оригинале).
_ROLE_DEFAULT_CAPS: dict[str, list[str]] = {
    "reg_manager": ["moderate_reg", "moderate_receipts"],
    "game_manager": ["moderate_game"],
}


def _role_caps_key(role: str) -> str:
    return f"role_caps_{role}"


def _role_enabled_key(role: str) -> str:
    return f"role_{role}_enabled"


def _get_setting(conn, key: str) -> str | None:
    row = conn.execute("SELECT value FROM bot_settings WHERE key = ?", (key,)).fetchone()
    return row["value"] if row is not None else None


def _role_enabled(conn, role: str) -> bool:
    """Дефолт всех `role_<role>_enabled` в реестре — `"on"` (см. settings_schema.py); значит
    отсутствующая в `bot_settings` строка тоже означает «включена», как и в боте."""
    raw = _get_setting(conn, _role_enabled_key(role))
    return raw != "off"


def _parse_caps_list(raw: str | None, default: list[str]) -> list[str]:
    """Тот же разбор, что `settings_schema._parse_setting` для `type: "list"`: перевод строки
    ИЛИ «;»-разделитель (mobile Enter=send trap — CLAUDE.md), пустое сырое значение — дефолт
    роли. Сентинел пустого набора («—», `handlers/admin_roles.py::_CAPS_EMPTY_SENTINEL`) и
    любой мусорный токен отдельно не обрабатываются — они просто не входят в
    `ALL_CAPABILITIES`, и вызывающий `resolve_capabilities` отфильтровывает их тем же
    `if cap in ALL_CAPABILITIES`, что и оригинал."""
    if not raw:
        return list(default)
    items = [
        segment.strip()
        for line in raw.splitlines()
        for segment in line.split(";")
        if segment.strip()
    ]
    return items if items else list(default)


def _get_staff_roles(conn, telegram_id: int) -> list[str]:
    rows = conn.execute(
        "SELECT role FROM staff WHERE telegram_id = ?", (telegram_id,)
    ).fetchall()
    return [row["role"] for row in rows]


def resolve_capabilities(conn, telegram_id: int, admin_ids) -> set[str]:
    """Точная копия `handlers.admin_caps.resolve_capabilities` для синхронного read-only
    `sqlite3`-подключения: bootstrap-суперадмин -> весь набор БЕЗ обращения к `staff` (пустая
    или битая `staff` не запирает администратора), иначе — объединение прав включённых ролей
    (D-08)."""
    if telegram_id in admin_ids:
        return set(ALL_CAPABILITIES)

    caps: set[str] = set()
    for role in _get_staff_roles(conn, telegram_id):
        if role not in _ROLE_DEFAULT_CAPS:
            continue  # T-08-02 аналог: неизвестная/устаревшая роль в staff игнорируется
        if not _role_enabled(conn, role):
            continue  # роль выключена целиком -> ноль прав от неё
        raw = _get_setting(conn, _role_caps_key(role))
        role_caps = _parse_caps_list(raw, _ROLE_DEFAULT_CAPS[role])
        caps.update(cap for cap in role_caps if cap in ALL_CAPABILITIES)
    return caps


def has_stats(conn, telegram_id: int, admin_ids) -> bool:
    """D-09: пересверка на КАЖДЫЙ вызов, без кэша — зовётся каждым защищённым маршрутом.
    Решение принимается только по `telegram_id` из подписанной сессии."""
    return "stats" in resolve_capabilities(conn, telegram_id, admin_ids)


def staff_city(conn, telegram_id: int) -> str | None:
    """Привязка к городу этого человека, или `None` (все города) — тот же запрос, что
    `database.db.get_staff_city`, но по read-only `sqlite3`-подключению дашборда."""
    row = conn.execute(
        "SELECT city FROM staff WHERE telegram_id = ? AND city IS NOT NULL LIMIT 1",
        (telegram_id,),
    ).fetchone()
    return row["city"] if row is not None else None


def viewer_scope(conn, telegram_id: int, admin_ids, requested_city: str | None) -> Scope:
    """D-10: есть привязка `staff.city` -> город ВСЕГДА её, запрошенный в URL другой город
    молча игнорируется (не 403 — просто своя вкладка, как в боте). Привязки нет -> уважается
    `requested_city` (пусто = все города). Суперадминство НЕ освобождает от привязки — тот же
    человек может одновременно быть суперадмином и менеджером конкретного города (в боте это
    так же: `get_staff_city` не смотрит на ADMIN_IDS)."""
    bound = staff_city(conn, telegram_id)
    if bound:
        return Scope(city=bound)
    return Scope(city=requested_city or None)
