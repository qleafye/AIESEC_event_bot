"""Живой рейтинг чата делегатов для страницы «Чат» (квик 260927).

Источник — таблицы живого учёта бота: chat_messages (без текста, только длина), chat_reactions,
chat_usernames, chat_admins, chat_bot_state. Формула — общий корневой модуль chat_score.py
(тот же, что у тула по экспорту tools/chat_export_stats.py), поэтому балл по экспорту и балл
на дашборде считаются одинаково — это держит тест паритета tests/test_chat_rating_parity_260927.py.

Только чтение (соединение mode=ro), ничего не пишет и не шлёт. Подпись участника — @ник из
Telegram, иначе ник из анкеты, иначе id; ФИО и контакты сюда не попадают (D-17).

Модуль не импортирует ни бота, ни services/database: образ дашборда их не содержит.
"""
from __future__ import annotations

import sqlite3
from datetime import date, datetime, timedelta

import chat_score
from chat_score import ChatRecord

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


def team_ids(conn, chat_id: int, admin_ids) -> set:
    """Команда = сотрудники бота (staff) + суперадмины (ADMIN_IDS) + админы этой группы."""
    team = {int(x) for x in (admin_ids or ())}
    team.update(row[0] for row in _rows(conn, "SELECT DISTINCT telegram_id FROM staff"))
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


def load_records(conn, chat_id: int, until: date | None = None) -> list:
    """Все сообщения чата до конца дня `until` как ChatRecord. Начало окна применяет
    aggregate_records: ответ в окне на старое сообщение всё равно засчитывает резонанс его
    автору. Реакции: строки chat_reactions (дарители известны) + reactions_extra (импорт
    экспорта: посчитаны Telegram, дарители неизвестны)."""
    reactions: dict = {}
    for row in _rows(
        conn,
        "SELECT message_id, COUNT(*), GROUP_CONCAT(telegram_id) FROM chat_reactions "
        "WHERE chat_id = ? GROUP BY message_id",
        (chat_id,),
    ):
        givers = tuple(int(x) for x in str(row[2] or "").split(",") if x.strip())
        reactions[row[0]] = (int(row[1] or 0), givers)

    sql = (
        "SELECT message_id, telegram_id, ts, kind, text_len, reply_to_message_id, "
        "reply_to_author_id, is_channel_post, reactions_extra FROM chat_messages WHERE chat_id = ?"
    )
    params: list = [chat_id]
    if until is not None:
        sql += " AND ts <= ?"
        params.append(f"{until.isoformat()} 23:59:59")
    sql += " ORDER BY ts, message_id"

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


def display_names(conn, ids) -> dict:
    """@ник из Telegram -> ник из анкеты (users.username) -> id. Только ники, никогда ФИО."""
    ids = list(dict.fromkeys(int(i) for i in ids))
    out = {i: str(i) for i in ids}
    if not ids:
        return out
    marks = ",".join("?" for _ in ids)
    for row in _rows(conn, f"SELECT telegram_id, username FROM users WHERE telegram_id IN ({marks})", ids):
        nick = _nick(row[1])
        if nick:
            out[row[0]] = f"@{nick}"
    for row in _rows(
        conn, f"SELECT telegram_id, username FROM chat_usernames WHERE telegram_id IN ({marks})", ids,
    ):
        nick = _nick(row[1])
        if nick:
            out[row[0]] = f"@{nick}"
    return out


def bot_admin(conn, chat_id: int):
    """True/False — бот админ группы или нет; None — состояние ещё не записано."""
    rows = _rows(conn, "SELECT bot_status FROM chat_bot_state WHERE chat_id = ?", (chat_id,))
    if not rows or rows[0][0] is None:
        return None
    return rows[0][0] in _BOT_ADMIN_STATUSES


def retention_days(raw: dict) -> int:
    value = chat_score._parse_non_negative(raw.get(chat_score.RETENTION_KEY))
    return int(value) if value is not None and int(value) > 0 else chat_score.DEFAULT_RETENTION_DAYS


def chat_rating(conn, chat: dict, *, period, admin_ids, now: datetime) -> dict:
    """Рейтинг по формуле активности за период. Команда режется ПОСЛЕ расчёта (тот же
    порядок, что у тула по экспорту): её ответы и реакции делегатам уже учтены."""
    chat_id = chat["chat_id"]
    period = normalize_period(period)
    since, until = period_bounds(period, now.date())
    raw = _settings(
        conn,
        [*chat_score.SETTING_KEYS.values(), chat_score.BURST_GAP_KEY, chat_score.RETENTION_KEY],
    )
    weights, burst_gap = chat_score.weights_from_settings(raw)

    records = load_records(conn, chat_id, until=until)
    aggs = chat_score.aggregate_records(records, burst_gap=burst_gap, since=since, until=until)
    scores = chat_score.score_authors(aggs, weights)
    team = team_ids(conn, chat_id, admin_ids)

    kept = [aid for aid in aggs if aid not in team and aid > 0]
    names = display_names(conn, kept)
    rows = []
    for aid in kept:
        sc = scores[aid]
        agg = aggs[aid]
        rows.append({
            "telegram_id": aid,
            "display_name": names.get(aid, str(aid)),
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
    }
