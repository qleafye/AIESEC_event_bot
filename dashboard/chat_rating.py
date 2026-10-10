"""Живой рейтинг чата делегатов для страницы «Чат» (квик 260927).

Источник — таблицы живого учёта бота: chat_messages (без текста, только длина), chat_reactions,
chat_usernames, chat_admins, chat_bot_state. Формула — общий корневой модуль shared/chat_score.py
(тот же, что у тула по экспорту tools/chat_export_stats.py), поэтому балл по экспорту и балл
на дашборде считаются одинаково — это держит тест паритета tests/test_chat_rating_parity_260927.py.

Только чтение (соединение mode=ro), ничего не пишет и не шлёт. Подпись участника — @ник из
Telegram, иначе ник из анкеты, иначе имя из Telegram (first_name, без фамилии), иначе «без
ника»; голый telegram_id, ФИО и контакты сюда не попадают (D-17).

Кого показывать: по формуле — по умолчанию только людей с анкетой сезона страницы и города
чата («Только с анкетой»), по правилам города — всех участников чата (СПб считает всех);
команда не показывается никогда.

Модуль не импортирует ни бота, ни services/database: образ дашборда их не содержит.
"""
from __future__ import annotations

import os
import logging
import sqlite3
from datetime import date, datetime, timedelta

import shared.chat_score as chat_score
from shared.chat_score import ChatRecord

log = logging.getLogger(__name__)

PERIODS = [
    ("all", "Всё время"),
    ("7d", "7 дней"),
    ("week", "Эта неделя"),
    ("prev_week", "Прошлая неделя"),
]
_PERIOD_CODES = {code for code, _label in PERIODS}
DEFAULT_PERIOD = "all"
TOP_FORMULA = 50

_TS_FORMAT = "%Y-%m-%d %H:%M:%S"
_BOT_ADMIN_STATUSES = ("administrator", "creator")
# Служебный аккаунт Telegram (автопересылки связанного канала) — не участник рейтинга.
SERVICE_USER_ID = 777000


def normalize_period(period) -> str:
    """Закрытый набор: всё незнакомое -> «всё время». Из параметра ничего не строится в SQL."""
    return period if period in _PERIOD_CODES else DEFAULT_PERIOD


def period_bounds(period, today: date) -> tuple:
    """(since, until) включительно; (None, None) — без границ."""
    period = normalize_period(period)
    if period == "7d":
        return today - timedelta(days=6), today
    monday = today - timedelta(days=today.weekday())
    if period == "week":
        return monday, monday + timedelta(days=6)
    if period == "prev_week":
        return monday - timedelta(days=7), monday - timedelta(days=1)
    return None, None


def _rows(conn, sql: str, params=()) -> list:
    """Старая БД без таблиц рейтинга — не ошибка страницы, а пустой результат."""
    try:
        return conn.execute(sql, params).fetchall()
    except sqlite3.OperationalError as e:
        if "no such table" in str(e) or "no such column" in str(e):
            return []
        raise


def _settings(conn, keys) -> dict:
    keys = list(keys)
    if not keys:
        return {}
    marks = ",".join("?" for _ in keys)
    return {
        row[0]: row[1]
        for row in _rows(conn, f"SELECT key, value FROM bot_settings WHERE key IN ({marks})", keys)
    }


# Роль, которая командой в чате НЕ считается: волонтёр форума держит только право чек-ина,
# её выдают по ссылке-приглашению и нередко делегатам.
_NOT_TEAM_ROLES = ("volunteer",)


def team_ids(conn, chat_id: int, admin_ids) -> set:
    """Команда = суперадмины (ADMIN_IDS) + сотрудники бота с любой ролью, кроме волонтёра
    форума + админы этой группы с правами модерации (бот хранит в chat_admins только их:
    владелец или право удалять сообщения / ограничивать участников)."""
    team = {int(x) for x in (admin_ids or ())}
    marks = ",".join("?" for _ in _NOT_TEAM_ROLES)
    team.update(row[0] for row in _rows(
        conn, f"SELECT DISTINCT telegram_id FROM staff WHERE role NOT IN ({marks})", _NOT_TEAM_ROLES,
    ))
    team.update(
        row[0] for row in _rows(
            conn, "SELECT telegram_id FROM chat_admins WHERE chat_id = ?", (chat_id,),
        )
    )
    return team


def _parse_ts(value) -> datetime | None:
    try:
        return datetime.strptime(str(value), _TS_FORMAT)
    except (TypeError, ValueError):
        return None


def load_records(conn, chat_id: int, until: date | None = None, *,
                 since: date | None = None) -> list:
    """Сообщения чата с начала дня `since` до конца дня `until` как ChatRecord; обе границы —
    в SQL (индекс chat_id+ts), реакции — только к сообщениям этого окна. Окно формулы по
    периоду применяет aggregate_records: ответ в окне на старое сообщение несёт автора цели
    в своей же строке (reply_to_author_id), поэтому старые сообщения грузить не нужно.
    Реакции: строки chat_reactions (дарители известны) + reactions_extra (импорт экспорта:
    посчитаны Telegram, дарители неизвестны)."""
    bounds = ""
    params: list = [chat_id]
    if since is not None:
        bounds += " AND ts >= ?"
        params.append(f"{since.isoformat()} 00:00:00")
    if until is not None:
        bounds += " AND ts <= ?"
        params.append(f"{until.isoformat()} 23:59:59")

    reactions: dict = {}
    reaction_sql = (
        "SELECT message_id, COUNT(*), GROUP_CONCAT(telegram_id) FROM chat_reactions "
        "WHERE chat_id = ?"
    )
    reaction_params: list = [chat_id]
    if bounds:
        reaction_sql += (
            " AND message_id IN (SELECT message_id FROM chat_messages WHERE chat_id = ?"
            f"{bounds})"
        )
        reaction_params += params
    for row in _rows(conn, reaction_sql + " GROUP BY message_id", reaction_params):
        givers = tuple(int(x) for x in str(row[2] or "").split(",") if x.strip())
        reactions[row[0]] = (int(row[1] or 0), givers)

    sql = (
        "SELECT message_id, telegram_id, ts, kind, text_len, reply_to_message_id, "
        "reply_to_author_id, is_channel_post, reactions_extra FROM chat_messages WHERE chat_id = ?"
        f"{bounds} ORDER BY ts, message_id"
    )

    records = []
    for row in _rows(conn, sql, params):
        ts = _parse_ts(row[2])
        if ts is None:
            continue
        count, givers = reactions.get(row[0], (0, ()))
        kind = row[3] or ""
        records.append(ChatRecord(
            message_id=row[0],
            author_id=row[1],
            ts=ts,
            text_len=int(row[4] or 0),
            has_media=kind in ("media", "sticker"),
            is_sticker=kind == "sticker",
            reply_to_message_id=row[5],
            reply_to_author_id=row[6],
            reactions_received=count + int(row[8] or 0),
            reaction_giver_ids=givers,
            is_channel_post=bool(row[7]),
        ))
    return records


def _nick(raw) -> str:
    value = str(raw or "").strip().lstrip("@")
    return "" if not value or value == "-" else value


NO_NICK = "без ника"


def display_names(conn, ids) -> dict:
    """@ник из Telegram -> ник из анкеты (users.username) -> имя из Telegram (first_name) ->
    «без ника». Никогда не ФИО и никогда не голый id."""
    ids = list(dict.fromkeys(int(i) for i in ids))
    out = {i: NO_NICK for i in ids}
    if not ids:
        return out
    marks = ",".join("?" for _ in ids)
    columns = {row[1] for row in _rows(conn, "PRAGMA table_info(chat_usernames)")}
    has_first = "first_name" in columns
    tg_rows = _rows(
        conn,
        f"SELECT telegram_id, username, {'first_name' if has_first else 'NULL'} "
        f"FROM chat_usernames WHERE telegram_id IN ({marks})",
        ids,
    )
    for row in tg_rows:
        name = str(row[2] or "").strip()
        if name:
            out[row[0]] = name
    for row in _rows(conn, f"SELECT telegram_id, username FROM users WHERE telegram_id IN ({marks})", ids):
        nick = _nick(row[1])
        if nick:
            out[row[0]] = f"@{nick}"
    for row in tg_rows:
        nick = _nick(row[1])
        if nick:
            out[row[0]] = f"@{nick}"
    return out


def registered_ids(conn, city, season=None) -> set:
    """Кто подал анкету в сезоне страницы (по умолчанию — текущем) и городе чата — фильтр
    «Только с анкетой»."""
    scope, params = _users_scope_sql(conn, city, season)
    return {row[0] for row in _rows(conn, f"SELECT telegram_id FROM users WHERE {scope}", params)}


def bot_admin(conn, chat_id: int):
    """True/False — бот админ группы или нет; None — состояние ещё не записано."""
    rows = _rows(conn, "SELECT bot_status FROM chat_bot_state WHERE chat_id = ?", (chat_id,))
    if not rows or rows[0][0] is None:
        return None
    return rows[0][0] in _BOT_ADMIN_STATUSES


def retention_days(raw: dict) -> int:
    value = chat_score._parse_non_negative(raw.get(chat_score.RETENTION_KEY))
    return int(value) if value is not None and int(value) > 0 else chat_score.DEFAULT_RETENTION_DAYS


def _retention_floor(raw: dict, now: datetime) -> date:
    """Первый день, который ещё хранится: «Всё время» = не старше срока хранения, даже если
    суточная чистка ещё не прошла (или импорт положил старые строки)."""
    return now.date() - timedelta(days=retention_days(raw))


def _window(period, now: datetime, bounds) -> tuple:
    """Окно расчёта: явные `bounds` (since, until) важнее кода периода. Нужны еженедельному
    посту в чат (services/chat_rating_post.py): «с начала» там — до конца завершённой недели,
    а не до «сейчас», которого у дашборда нет в списке периодов."""
    if bounds is not None:
        return bounds
    return period_bounds(period, now.date())


def chat_rating(conn, chat: dict, *, period, admin_ids, now: datetime,
                registered_only: bool | None = None, season: str | None = None,
                bounds=None) -> dict:
    """Рейтинг по формуле активности за период. Команда режется ПОСЛЕ расчёта (тот же
    порядок, что у тула по экспорту): её ответы и реакции делегатам уже учтены.
    `registered_only` (по умолчанию — да): только люди с анкетой сезона `season` и города чата;
    фильтр тоже после расчёта — ответы участников без анкеты делегатам засчитываются."""
    if registered_only is None:
        registered_only = True
    chat_id = chat["chat_id"]
    period = normalize_period(period)
    since, until = _window(period, now, bounds)
    raw = _settings(
        conn,
        [*chat_score.SETTING_KEYS.values(), chat_score.BURST_GAP_KEY, chat_score.RETENTION_KEY],
    )
    weights, burst_gap = chat_score.weights_from_settings(raw)

    floor = _retention_floor(raw, now)
    records = load_records(conn, chat_id, until=until, since=max(since or floor, floor))
    aggs = chat_score.aggregate_records(records, burst_gap=burst_gap, since=since, until=until)
    scores = chat_score.score_authors(aggs, weights)
    team = team_ids(conn, chat_id, admin_ids)

    kept = [aid for aid in aggs if aid not in team and aid > 0 and aid != SERVICE_USER_ID]
    if registered_only:
        registered = registered_ids(conn, chat.get("city"), season)
        kept = [aid for aid in kept if aid in registered]
    names = display_names(conn, kept)
    rows = []
    for aid in kept:
        sc = scores[aid]
        agg = aggs[aid]
        rows.append({
            "telegram_id": aid,
            "display_name": names.get(aid, NO_NICK),
            "score": round(sc["score"], 1),
            "volume": round(sc["volume"], 1),
            "resonance": round(sc["resonance"], 1),
            "regularity": round(sc["regularity"], 1),
            "giving": round(sc["giving"], 1),
            "messages": agg.messages,
            "active_days": agg.active_days,
            "_exact": sc["score"],
        })
    rows.sort(key=lambda r: (-r["_exact"], -r["messages"], r["display_name"]))
    rows = rows[:TOP_FORMULA]
    for place, row in enumerate(rows, start=1):
        row["place"] = place
        del row["_exact"]

    return {
        "rows": rows,
        "period": period,
        "periods": PERIODS,
        "weights": weights,
        "formula_text": chat_score.describe_formula(weights),
        "retention_days": retention_days(raw),
        "bot_admin": bot_admin(conn, chat_id),
        "team_count": len(team & aggs.keys()),
        "registered_only": registered_only,
    }


# ---------------------------------------------------------------------------
# Режим «По правилам города» (пресет СПб «коины»)
# ---------------------------------------------------------------------------

_PER_CITY_SEP = "__city__"
_ALL_CITIES = "*"
_CHAT_PRESENT_STATUSES = ("creator", "administrator", "member")
TOP_RULES = 100

# Колонки таблицы по правилам: ключ счётчика, поле баллов (None — только счётчик), подпись и
# сумма правила, от которой зависит видимость колонки.
_RULE_COLUMNS = (
    ("comments", "comment_points", "Комментарии", ("comment_points", "valuable_points")),
    ("valuable", None, "Ценные", ("valuable_points",)),
    ("referrals", "referral_points", "Друзья", ("referral_points",)),
    ("social", "social_points", "Соцсети", ("social_points",)),
    ("checkins", "checkin_points", "Очно", ("checkin_points",)),
)


def _city_setting(conn, key: str, city) -> str | None:
    """Копия лестницы cities.get_setting_for_city только на чтение: значение города (если
    непустое и не «*»), иначе общее, иначе None (дефолт берёт вызывающий). Дашборд не
    импортирует cities.py — образ его не содержит."""
    keys = [key]
    if city and city != _ALL_CITIES:
        keys.insert(0, f"{key}{_PER_CITY_SEP}{city}")
    values = _settings(conn, keys)
    for k in keys:
        raw = values.get(k)
        if raw is not None and str(raw).strip() and str(raw).strip() != _ALL_CITIES:
            return str(raw)
    return None


def chat_mode(conn, chat: dict) -> str:
    """«formula» | «rules» для города чата; незнакомое значение — формула (дефолт)."""
    raw = (_city_setting(conn, chat_score.MODE_KEY, chat.get("city")) or "").strip()
    return raw if raw in chat_score.MODES else chat_score.MODES[0]


def _social_task_ids(raw) -> set:
    """Список id заданий по одному на строку; «0» — маркер «у города пусто»."""
    out = set()
    for piece in str(raw or "").replace(",", "\n").split():
        try:
            value = int(piece)
        except ValueError:
            continue
        if value > 0:
            out.add(value)
    return out


def _day(value) -> date | None:
    try:
        return date.fromisoformat(str(value)[:10])
    except (TypeError, ValueError):
        return None


def _date_map(rows) -> dict:
    out: dict = {}
    for pid, raw_day in rows:
        day = _day(raw_day)
        if pid is None or day is None:
            continue
        out.setdefault(int(pid), []).append(day)
    return out


def _default_city_code(conn) -> str:
    """Копия dashboard.queries._default_city_code на кортежах (соединение вызывающего может быть
    без row_factory)."""
    configured = os.environ.get("EVENT_CITY_DEFAULT", "msk")
    if _rows(conn, "SELECT code FROM cities WHERE code = ?", (configured,)):
        return configured
    rows = _rows(conn, "SELECT code FROM cities ORDER BY sort_order ASC, code ASC LIMIT 1")
    return rows[0][0] if rows else "msk"


def _users_scope_sql(conn, city, season) -> tuple[str, list]:
    """Условие на users «делегат этого сезона и города чата» — та же логика, что у страницы:
    сезон страницы, иначе текущий (users.season пуст или равен event_season; сезон не задан —
    любой); город по умолчанию — исключением прочих известных кодов (ловит пустой город)."""
    parts: list[str] = []
    params: list = []
    if season:
        parts.append("season = ?")
        params.append(season)
    else:
        current = (_settings(conn, ["event_season"]).get("event_season") or "").strip()
        if current:
            parts.append("(season IS NULL OR TRIM(season) = '' OR season = ?)")
            params.append(current)
    if city and city != _ALL_CITIES:
        default = _default_city_code(conn)
        others = [r[0] for r in _rows(conn, "SELECT code FROM cities WHERE code != ?", (default,))]
        if city == default and others:
            marks = ",".join("?" for _ in others)
            parts.append(f"(event_city IS NULL OR event_city NOT IN ({marks}))")
            params.extend(others)
        else:
            parts.append("event_city = ?")
            params.append(city)
    return (" AND ".join(parts) or "1 = 1"), params


def _table_cols(conn, table: str) -> set:
    try:
        return {r[1] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()}
    except sqlite3.Error:
        return set()


def _referral_dates(conn) -> dict:
    """Приведённые берутся из журнала зачётов (строка у любого пригласившего, исключённые
    отсеяны), а приглашённый проверяется живым JOIN по users: вернули в ожидание, и он выпал.
    Дата — users.approved_at, а не credited_at: у строк бэкафилла credited_at — день бэкафилла.
    Пока журнал сезона неполный или схема старая, читается прежний запрос по users."""
    season = (_settings(conn, ["event_season"]).get("event_season") or "").strip()
    season_sql = ""
    params: list = []
    if season:
        season_sql = " AND (u.season IS NULL OR TRIM(u.season) = '' OR u.season = ?)"
        params.append(season)
    base_users = ("u.status = 'approved' AND u.referrer_id IS NOT NULL "
                  "AND u.approved_at IS NOT NULL AND u.referrer_id != u.telegram_id")
    journal_ok = "excluded_at" in _table_cols(conn, "referral_credits")
    if journal_ok:
        missing = _rows(
            conn,
            f"SELECT 1 FROM users u WHERE {base_users}{season_sql} AND NOT EXISTS "
            "(SELECT 1 FROM referral_credits rc WHERE rc.invitee_id = u.telegram_id) LIMIT 1",
            params,
        )
        if missing:
            journal_ok = False
            log.warning("chat_rating: журнал зачётов сезона неполный — приведённые по users")
    if journal_ok:
        return _date_map(_rows(
            conn,
            "SELECT rc.referrer_id, u.approved_at FROM referral_credits rc "
            "JOIN users u ON u.telegram_id = rc.invitee_id "
            f"WHERE {base_users} AND rc.excluded_at IS NULL "
            f"AND rc.referrer_id != rc.invitee_id{season_sql}",
            params,
        ))
    excl = ""
    if _table_cols(conn, "ambassador_exclusions"):
        excl = " AND u.telegram_id NOT IN (SELECT invitee_id FROM ambassador_exclusions)"
    return _date_map(_rows(
        conn,
        f"SELECT u.referrer_id, u.approved_at FROM users u WHERE {base_users}{season_sql}{excl}",
        params,
    ))


def _social_dates(conn, task_ids: set, city=None, season=None) -> dict:
    """Одобренные задания, выбранные для правила, — только у делегатов сезона и города чата."""
    if not task_ids:
        return {}
    ids = sorted(task_ids)
    marks = ",".join("?" for _ in ids)
    scope, scope_params = _users_scope_sql(conn, city, season)
    return _date_map(_rows(
        conn,
        f"SELECT user_id, reviewed_at FROM game_submissions WHERE status = 'approved' "
        f"AND reviewed_at IS NOT NULL AND task_id IN ({marks}) "
        f"AND user_id IN (SELECT telegram_id FROM users WHERE {scope})",
        [*ids, *scope_params],
    ))


def _checkin_days(conn, city=None, season=None) -> dict:
    """Дни форума с отметкой на входе — только у делегатов сезона и города чата; отметки на
    сессиях отдельно не считаются."""
    scope, scope_params = _users_scope_sql(conn, city, season)
    return _date_map(_rows(
        conn,
        "SELECT telegram_id, day FROM checkins WHERE point = 'entry' "
        f"AND telegram_id IN (SELECT telegram_id FROM users WHERE {scope})",
        scope_params,
    ))


def _chat_participants(conn, chat_id: int, records) -> set:
    marks = ",".join("?" for _ in _CHAT_PRESENT_STATUSES)
    people = {
        row[0] for row in _rows(
            conn,
            f"SELECT telegram_id FROM chat_members WHERE chat_id = ? AND status IN ({marks})",
            (chat_id, *_CHAT_PRESENT_STATUSES),
        )
    }
    people.update(r.author_id for r in records if not r.is_channel_post and r.author_id is not None)
    return people


def rules_rating(conn, chat: dict, *, period, admin_ids, now: datetime,
                 season: str | None = None, registered_only: bool | None = None,
                 bounds=None) -> dict:
    """Таблица по правилам города за период — только расчёт для публикации итогов: коины не
    начисляются, делегатам ничего не уходит. Посты могут быть старше окна, поэтому журнал
    читается за весь срок хранения (до конца окна), а окно применяет score_city_rules."""
    chat_id = chat["chat_id"]
    city = chat.get("city")
    period = normalize_period(period)
    since, until = _window(period, now, bounds)
    if registered_only is None:
        registered_only = False  # СПб считает всех, кто в чате

    rules_raw = {
        key: value for key in chat_score.RULE_SETTING_KEYS.values()
        if (value := _city_setting(conn, key, city)) is not None
    }
    rules = chat_score.rules_from_settings(rules_raw)
    currency = (_city_setting(conn, chat_score.CURRENCY_KEY, city) or "").strip() \
        or chat_score.DEFAULT_CURRENCY
    task_ids = _social_task_ids(_city_setting(conn, chat_score.SOCIAL_TASKS_KEY, city))

    # Посты могут быть старше окна (и прошлые комментарии нужны для недельной разницы),
    # поэтому журнал читается от границы срока хранения, а не от начала периода.
    raw = _settings(conn, [chat_score.RETENTION_KEY])
    records = load_records(conn, chat_id, until=until, since=_retention_floor(raw, now))
    team = team_ids(conn, chat_id, admin_ids)
    scored = chat_score.score_city_rules(
        records, team_ids=team, rules=rules,
        referral_dates=_referral_dates(conn),
        social_dates=_social_dates(conn, task_ids, city, season),
        checkin_days=_checkin_days(conn, city, season),
        since=since, until=until,
    )

    columns = []
    for key, points_key, label, amount_keys in _RULE_COLUMNS:
        if not any(rules[a] > 0 for a in amount_keys):
            continue
        if key == "social" and not task_ids:
            continue
        columns.append({"key": key, "points_key": points_key, "label": label})

    participants = _chat_participants(conn, chat_id, records) - team
    kept = [pid for pid in scored if pid in participants and pid > 0 and pid != SERVICE_USER_ID]
    if registered_only:
        registered = registered_ids(conn, city, season)
        kept = [pid for pid in kept if pid in registered]
    names = display_names(conn, kept)
    rows = []
    for pid in kept:
        row = dict(scored[pid])
        row["telegram_id"] = pid
        row["display_name"] = names.get(pid, NO_NICK)
        rows.append(row)
    rows.sort(key=lambda r: (-r["total"], r["display_name"]))
    rows = rows[:TOP_RULES]
    for place, row in enumerate(rows, start=1):
        row["place"] = place

    return {
        "rows": rows,
        "period": period,
        "periods": PERIODS,
        "currency": currency,
        "rules": rules,
        "rules_text": chat_score.describe_rules(rules, currency),
        "columns": columns,
        "bot_admin": bot_admin(conn, chat_id),
        "retention_days": retention_days(raw),
        "registered_only": registered_only,
    }
