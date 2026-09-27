"""Снимок цифр для кейса «бот для мероприятий» (метрики M1–M12, .planning/CASE-METRICS.md).

Только чтение: база открывается URI `file:...?mode=ro` + `PRAGMA query_only`, в скрипте нет ни
одного INSERT/UPDATE/DELETE. Зависит только от стандартной библиотеки, поэтому запускается в
контейнере бота подачей через stdin, без копирования файла внутрь:

    docker exec -i youlead26-bot-1 python - /app/data/forum.db --season "YL 26/2" \\
        --label S0 < tools/case_snapshot.py > 260927-yl-S0.json
    ... --city spb --format md   # человекочитаемая сводка по-русски

Схемы стеков (YL, РилТолк, SkillUp, REC) расходятся по версиям бота, поэтому каждая таблица и
колонка проверяется через PRAGMA перед запросом: чего нет — метрика помечается
`available: false` с причиной, снимок не падает.

Сезон. `--season` не задан — берётся текущий `event_season` из настроек. Строки `users` без
сезона (легаси до 10.09) считаются текущим сезоном — ровно как на дашборде. Импортированные
делегаты прошлых сезонов (10.09: 482 человека «YL 26/1», tools/import_past_delegates.py)
лежат со СВОИМ сезоном, фильтр по сезону их и отсекает; их число видно в `other_seasons`.
Импортированный, который подал анкету заново, переписывается на текущий сезон (с
`prev_season`) — это честная заявка этого сезона, она в M1 остаётся.

Город. `--city <код>` — равенство `event_city`. Событие `start` в `reg_events` города ещё не
знает, поэтому для него (и для `nudged`) строки без города тоже входят в городской скоуп — так
же, как в таблице меток дашборда.
"""
from __future__ import annotations

import argparse
import json
import math
import sqlite3
import statistics
import sys
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

MSK = timezone(timedelta(hours=3))


# ── доступ к базе ─────────────────────────────────────────────────────────────────────────

@contextmanager
def open_ro(path: str):
    p = Path(path)
    if not p.is_file():
        raise FileNotFoundError(f"нет файла базы: {path}")
    conn = sqlite3.connect(p.resolve().as_uri() + "?mode=ro", uri=True)
    try:
        conn.execute("PRAGMA query_only = ON")
        yield conn
    finally:
        conn.close()


class _Schema:
    def __init__(self, conn):
        self.conn = conn
        self._cols: dict[str, set[str]] = {}

    def cols(self, table: str) -> set[str]:
        if table not in self._cols:
            row = self.conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (table,)
            ).fetchone()
            if row is None:
                self._cols[table] = set()
            else:
                self._cols[table] = {r[1] for r in self.conn.execute(f"PRAGMA table_info({table})")}
        return self._cols[table]

    def has(self, table: str, *columns: str) -> bool:
        cols = self.cols(table)
        return bool(cols) and all(c in cols for c in columns)


def _scalar(conn, sql, params=()):
    row = conn.execute(sql, params).fetchone()
    return row[0] if row else None


def _where(parts):
    return (" WHERE " + " AND ".join(parts)) if parts else ""


def _pct(part, whole):
    return round(part / whole * 100, 1) if whole else None


def _percentile(values, p):
    if not values:
        return None
    ordered = sorted(values)
    idx = max(0, math.ceil(p * len(ordered)) - 1)
    return round(ordered[idx], 1)


def _median(values):
    return round(statistics.median(values), 1) if values else None


def _unavailable(reason):
    return {"available": False, "reason": reason}


# ── скоуп: сезон + город ──────────────────────────────────────────────────────────────────

class Scope:
    def __init__(self, sc: _Schema, season: str | None, city: str | None):
        self.sc = sc
        self.notes: list[str] = []
        current = None
        if sc.has("bot_settings", "key", "value"):
            current = _scalar(sc.conn, "SELECT value FROM bot_settings WHERE key = 'event_season'")
            current = (current or "").strip() or None
        self.current_season = current
        self.season = season or current
        self.city = city

    def _season_part(self, table, alias):
        if not self.season:
            return None, []
        if not self.sc.has(table, "season"):
            return None, []
        col = f"{alias}season"
        if self.season == self.current_season:
            return f"({col} IS NULL OR {col} = ?)", [self.season]
        return f"{col} = ?", [self.season]

    def users(self, alias=""):
        """Фрагменты WHERE для `users` (alias — «u.» при JOIN)."""
        parts, params = [], []
        frag, p = self._season_part("users", alias)
        if frag:
            parts.append(frag)
            params += p
        if self.city and self.sc.has("users", "event_city"):
            parts.append(f"{alias}event_city = ?")
            params.append(self.city)
        return parts, params

    def users_ids_sql(self):
        parts, params = self.users()
        return f"SELECT telegram_id FROM users{_where(parts)}", params

    def events(self, relaxed_city: bool):
        parts, params = [], []
        frag, p = self._season_part("reg_events", "")
        if frag:
            parts.append(frag)
            params += p
        if self.city and self.sc.has("reg_events", "event_city"):
            if relaxed_city:
                parts.append("(event_city = ? OR event_city IS NULL)")
            else:
                parts.append("event_city = ?")
            params.append(self.city)
        return parts, params


# ── метрики ───────────────────────────────────────────────────────────────────────────────

def m1_applications(sc: _Schema, scope: Scope):
    if not sc.has("users", "telegram_id"):
        return _unavailable("нет таблицы users")
    conn = sc.conn
    parts, params = scope.users()
    total = _scalar(conn, f"SELECT COUNT(*) FROM users{_where(parts)}", params) or 0
    out = {"available": True, "total": total}
    notes = []
    if sc.has("users", "status"):
        rows = conn.execute(
            f"SELECT COALESCE(status, 'unknown'), COUNT(*) FROM users{_where(parts)} "
            "GROUP BY 1 ORDER BY 2 DESC", params
        ).fetchall()
        out["by_status"] = {r[0]: r[1] for r in rows}
    if sc.has("users", "season"):
        if scope.season:
            other_parts = ["season IS NOT NULL", "season != ?"]
            rows = conn.execute(
                f"SELECT season, COUNT(*) FROM users{_where(other_parts)} GROUP BY season ORDER BY season",
                (scope.season,),
            ).fetchall()
            out["other_seasons"] = {r[0]: r[1] for r in rows}
            if scope.season == scope.current_season:
                null_parts, null_params = ["season IS NULL"], []
                if scope.city and sc.has("users", "event_city"):
                    null_parts.append("event_city = ?")
                    null_params.append(scope.city)
                out["without_season_counted"] = _scalar(
                    conn, f"SELECT COUNT(*) FROM users{_where(null_parts)}", null_params
                ) or 0
            notes.append(
                "прошлые сезоны (в т.ч. импорт 10.09) отсечены по users.season — см. other_seasons"
            )
        else:
            notes.append("сезон не задан и event_season пуст — посчитаны все строки users")
        if sc.has("users", "prev_season"):
            out["returning"] = _scalar(
                conn,
                f"SELECT COUNT(*) FROM users{_where(parts + ['prev_season IS NOT NULL', TRIM_NE('prev_season')])}",
                params,
            ) or 0
    else:
        notes.append("в users нет колонки season — импорт прошлых сезонов не отделить, посчитаны все")
    if scope.city and not sc.has("users", "event_city"):
        notes.append("в users нет event_city — фильтр города не применён")
    out["note"] = "; ".join(notes)
    return out


def TRIM_NE(col):
    return f"TRIM({col}) != ''"


def m2_cities(sc: _Schema, scope: Scope):
    out = {"available": True}
    conn = sc.conn
    if sc.has("cities", "code"):
        if "enabled" in sc.cols("cities"):
            rows = conn.execute(
                "SELECT code FROM cities WHERE enabled = 1 ORDER BY code"
            ).fetchall()
        else:
            rows = conn.execute("SELECT code FROM cities ORDER BY code").fetchall()
        out["cities_enabled"] = [r[0] for r in rows]
    else:
        out["cities_enabled"] = None
    if sc.has("users", "event_city"):
        parts, params = scope.users()
        rows = conn.execute(
            f"SELECT COALESCE(NULLIF(TRIM(event_city), ''), 'без города'), COUNT(*) FROM users"
            f"{_where(parts)} GROUP BY 1 ORDER BY 2 DESC", params
        ).fetchall()
        out["by_event_city"] = {r[0]: r[1] for r in rows}
    else:
        out["by_event_city"] = None
    if out["cities_enabled"] is None and out["by_event_city"] is None:
        return _unavailable("нет ни таблицы cities, ни users.event_city")
    return out


def _distinct_event(conn, scope: Scope, event: str):
    parts, params = scope.events(relaxed_city=event in ("start", "nudged"))
    return _scalar(
        conn,
        f"SELECT COUNT(DISTINCT telegram_id) FROM reg_events{_where(parts + ['event = ?'])}",
        params + [event],
    ) or 0


def m3_funnel(sc: _Schema, scope: Scope):
    if not sc.has("reg_events", "telegram_id", "event", "ts"):
        return _unavailable("нет журнала reg_events")
    conn = sc.conn
    parts, params = scope.events(relaxed_city=True)
    since = _scalar(conn, f"SELECT MIN(ts) FROM reg_events{_where(parts)}", params)
    start = _distinct_event(conn, scope, "start")
    started = _distinct_event(conn, scope, "form_started")
    completed = _distinct_event(conn, scope, "form_completed")
    return {
        "available": True,
        "since": since,
        "start": start,
        "form_started": started,
        "form_completed": completed,
        "conversion_pct": _pct(completed, start),
    }


def m4_nudges(sc: _Schema, scope: Scope):
    if not sc.has("reg_events", "telegram_id", "event", "ts"):
        return _unavailable("нет журнала reg_events")
    conn = sc.conn
    n_parts, n_params = scope.events(relaxed_city=True)
    nudged = _distinct_event(conn, scope, "nudged")
    since = _scalar(
        conn, f"SELECT MIN(ts) FROM reg_events{_where(n_parts + ['event = ?'])}", n_params + ["nudged"]
    )
    completed_after = _scalar(
        conn,
        "SELECT COUNT(*) FROM (SELECT telegram_id, MIN(ts) AS first_nudge FROM reg_events"
        f"{_where(n_parts + ['event = ?'])} GROUP BY telegram_id) n "
        "WHERE EXISTS (SELECT 1 FROM reg_events c WHERE c.telegram_id = n.telegram_id "
        "AND c.event = 'form_completed' AND c.ts > n.first_nudge)",
        n_params + ["nudged"],
    ) or 0
    out = {
        "available": True,
        "nudged": nudged,
        "nudged_since": since,
        "completed_after_nudge": completed_after,
        "caught_pct": _pct(completed_after, nudged),
    }
    if sc.has("reg_started", "telegram_id"):
        rs_parts, rs_params = [], []
        if scope.city and "event_city" in sc.cols("reg_started"):
            rs_parts.append("event_city = ?")
            rs_params.append(scope.city)
        out["pending_total"] = _scalar(
            conn, f"SELECT COUNT(*) FROM reg_started{_where(rs_parts)}", rs_params
        ) or 0
        if "nudged_at" in sc.cols("reg_started"):
            out["pending_nudged"] = _scalar(
                conn,
                f"SELECT COUNT(*) FROM reg_started{_where(rs_parts + ['nudged_at IS NOT NULL'])}",
                rs_params,
            ) or 0
    if not nudged:
        out["note"] = "событий nudged нет — журнал пишет их только с выката этой доработки"
    return out


def m5_latency(sc: _Schema, scope: Scope):
    if not sc.has("application_decisions", "telegram_id", "decided_at"):
        return _unavailable("нет таблицы application_decisions")
    if not sc.has("users", "registration_date"):
        return _unavailable("в users нет registration_date")
    parts, params = scope.users(alias="u.")
    dec_parts = ["d.decided_at IS NOT NULL"]
    if "undone_at" in sc.cols("application_decisions"):
        dec_parts.append("d.undone_at IS NULL")
    parts = parts + dec_parts + [
        "u.registration_date IS NOT NULL",
        "TRIM(u.registration_date) != ''",
        "julianday(d.decided_at) >= julianday(u.registration_date)",
    ]
    rows = sc.conn.execute(
        "SELECT (julianday(MIN(d.decided_at)) - julianday(u.registration_date)) * 24.0 "
        "FROM users u JOIN application_decisions d ON d.telegram_id = u.telegram_id"
        f"{_where(parts)} GROUP BY u.telegram_id",
        params,
    ).fetchall()
    hours = [r[0] for r in rows if r[0] is not None]
    return {
        "available": True,
        "decided": len(hours),
        "median_hours": _median(hours),
        "p90_hours": _percentile(hours, 0.9),
        "note": "от подачи анкеты до ПЕРВОГО неотменённого решения",
    }


def m6_moderation(sc: _Schema, scope: Scope):
    if not sc.has("application_decisions", "telegram_id", "decision"):
        return _unavailable("нет таблицы application_decisions")
    conn = sc.conn
    ids_sql, ids_params = scope.users_ids_sql()
    parts = [f"telegram_id IN ({ids_sql})"]
    if "undone_at" in sc.cols("application_decisions"):
        parts.append("undone_at IS NULL")
    rows = conn.execute(
        f"SELECT decision, COUNT(*) FROM application_decisions{_where(parts)} GROUP BY decision",
        ids_params,
    ).fetchall()
    by_decision = {r[0]: r[1] for r in rows}
    out = {"available": True, "decisions": sum(by_decision.values()), "by_decision": by_decision}
    if sc.has("auto_reject_log", "telegram_id"):
        ar_where = _where([f"telegram_id IN ({ids_sql})"])
        out["auto_reject_rows"] = _scalar(conn, f"SELECT COUNT(*) FROM auto_reject_log{ar_where}", ids_params) or 0
        out["auto_reject_people"] = _scalar(
            conn, f"SELECT COUNT(DISTINCT telegram_id) FROM auto_reject_log{ar_where}", ids_params
        ) or 0
        if "returned_to_moderation_at" in sc.cols("auto_reject_log"):
            out["auto_reject_returned"] = _scalar(
                conn,
                f"SELECT COUNT(*) FROM auto_reject_log{_where([f'telegram_id IN ({ids_sql})', 'returned_to_moderation_at IS NOT NULL'])}",
                ids_params,
            ) or 0
    else:
        out["auto_reject_people"] = None
    return out


def m7_payments(sc: _Schema, scope: Scope):
    has_receipt = sc.has("users", "receipt_file_id")
    has_status = sc.has("users", "payment_status")
    if not (has_receipt or has_status):
        return _unavailable("в users нет полей оплаты")
    conn = sc.conn
    parts, params = scope.users()
    out = {"available": True}
    out["receipts"] = _scalar(
        conn,
        f"SELECT COUNT(*) FROM users{_where(parts + ['receipt_file_id IS NOT NULL', TRIM_NE('receipt_file_id')])}",
        params,
    ) or 0 if has_receipt else 0
    if has_status:
        rows = conn.execute(
            f"SELECT COALESCE(payment_status, 'not_paid'), COUNT(*) FROM users{_where(parts)} GROUP BY 1",
            params,
        ).fetchall()
        out["payment_status"] = {r[0]: r[1] for r in rows}
    else:
        out["payment_status"] = {}
    real = {k: v for k, v in out["payment_status"].items() if k != "not_paid"}
    out["has_payment"] = bool(out["receipts"] or real)
    return out


def m8_broadcasts(sc: _Schema, scope: Scope):
    has_bd = sc.has("broadcast_deliveries", "broadcast_id", "chat_id")
    has_sbd = sc.has("scheduled_broadcast_deliveries", "broadcast_id", "status")
    if not (has_bd or has_sbd):
        return _unavailable("нет журналов доставки рассылок")
    conn = sc.conn
    out = {"available": True, "note": "по всему стеку, без фильтра сезона и города"}
    messages = recipient = broadcasts = 0
    if has_bd:
        messages = _scalar(conn, "SELECT COUNT(*) FROM broadcast_deliveries") or 0
        recipient = _scalar(
            conn, "SELECT COUNT(*) FROM (SELECT DISTINCT broadcast_id, chat_id FROM broadcast_deliveries)"
        ) or 0
        broadcasts = _scalar(conn, "SELECT COUNT(DISTINCT broadcast_id) FROM broadcast_deliveries") or 0
    scheduled_ok = 0
    if has_sbd:
        # Отложенные рассылки с квика 260915 сами пишут доставки в broadcast_deliveries
        # (через scheduled_broadcasts.log_broadcast_id) — их не считаем второй раз.
        if sc.has("scheduled_broadcasts", "id", "log_broadcast_id"):
            scheduled_ok = _scalar(
                conn,
                "SELECT COUNT(*) FROM scheduled_broadcast_deliveries d "
                "JOIN scheduled_broadcasts s ON s.id = d.broadcast_id "
                "WHERE d.status = 'ok' AND s.log_broadcast_id IS NULL",
            ) or 0
        else:
            scheduled_ok = _scalar(
                conn, "SELECT COUNT(*) FROM scheduled_broadcast_deliveries WHERE status = 'ok'"
            ) or 0
    out.update({
        "messages": messages,
        "recipient_deliveries": recipient,
        "broadcasts": broadcasts,
        "scheduled_unlogged_ok": scheduled_ok,
        "total_deliveries": recipient + scheduled_ok,
    })
    return out


def _checkin_day_expr(sc):
    return "day" if "day" in sc.cols("checkins") else "substr(scanned_at, 1, 10)"


def m9_arrivals(sc: _Schema, scope: Scope):
    if not sc.has("checkins", "telegram_id", "point"):
        return _unavailable("нет таблицы checkins")
    conn = sc.conn
    ids_sql, ids_params = scope.users_ids_sql()
    day = _checkin_day_expr(sc)
    base = ["c.point = 'entry'", f"c.telegram_id IN ({ids_sql})"]
    arrived = _scalar(
        conn, f"SELECT COUNT(DISTINCT c.telegram_id) FROM checkins c{_where(base)}", ids_params
    ) or 0
    approved = None
    if sc.has("users", "status"):
        parts, params = scope.users()
        approved = _scalar(
            conn, f"SELECT COUNT(*) FROM users{_where(parts + ['status = ?'])}", params + ["approved"]
        ) or 0
    by_day = {
        r[0]: r[1] for r in conn.execute(
            f"SELECT c.{day}, COUNT(DISTINCT c.telegram_id) FROM checkins c{_where(base)} "
            "GROUP BY 1 ORDER BY 1", ids_params
        ).fetchall()
    }
    by_city = None
    if sc.has("users", "event_city"):
        by_city = {
            r[0]: r[1] for r in conn.execute(
                "SELECT COALESCE(NULLIF(TRIM(u.event_city), ''), 'без города'), COUNT(DISTINCT c.telegram_id) "
                f"FROM checkins c JOIN users u ON u.telegram_id = c.telegram_id{_where(base)} "
                "GROUP BY 1 ORDER BY 2 DESC", ids_params
            ).fetchall()
        }
    return {
        "available": True,
        "arrived": arrived,
        "approved": approved,
        "arrived_pct": _pct(arrived, approved),
        "by_day": by_day,
        "by_city": by_city,
    }


def m10_sessions(sc: _Schema, scope: Scope):
    if not sc.has("checkins", "telegram_id", "point"):
        return _unavailable("нет таблицы checkins")
    conn = sc.conn
    ids_sql, ids_params = scope.users_ids_sql()
    day = _checkin_day_expr(sc)
    base = ["point LIKE 'session:%'", f"telegram_id IN ({ids_sql})"]
    marks = _scalar(conn, f"SELECT COUNT(*) FROM checkins{_where(base)}", ids_params) or 0
    delegates = _scalar(
        conn, f"SELECT COUNT(DISTINCT telegram_id) FROM checkins{_where(base)}", ids_params
    ) or 0
    by_day = {
        r[0]: r[1] for r in conn.execute(
            f"SELECT {day}, COUNT(*) FROM checkins{_where(base)} GROUP BY 1 ORDER BY 1", ids_params
        ).fetchall()
    }
    return {
        "available": True,
        "marks": marks,
        "delegates": delegates,
        "avg_per_delegate": round(marks / delegates, 2) if delegates else None,
        "by_day": by_day,
    }


def _minutes_between(conn, table, parts, params, start_col, end_col):
    rows = conn.execute(
        f"SELECT (julianday({end_col}) - julianday({start_col})) * 1440.0 FROM {table}"
        f"{_where(parts + [f'{end_col} IS NOT NULL', f'{start_col} IS NOT NULL'])}",
        params,
    ).fetchall()
    return [r[0] for r in rows if r[0] is not None and r[0] >= 0]


def m11_sos(sc: _Schema, scope: Scope):
    if not sc.has("sos_reports", "id", "created_at"):
        return _unavailable("нет таблицы sos_reports")
    conn = sc.conn
    cols = sc.cols("sos_reports")
    parts, params = [], []
    if scope.city and "city" in cols:
        parts.append("city = ?")
        params.append(scope.city)
    out = {
        "available": True,
        "total": _scalar(conn, f"SELECT COUNT(*) FROM sos_reports{_where(parts)}", params) or 0,
        "note": "по городу SOS (без фильтра сезона)",
    }
    if "category" in cols:
        out["by_category"] = {
            r[0]: r[1] for r in conn.execute(
                f"SELECT COALESCE(category, 'без категории'), COUNT(*) FROM sos_reports{_where(parts)} "
                "GROUP BY 1 ORDER BY 2 DESC", params
            ).fetchall()
        }
    if "claimed_at" in cols:
        claim = _minutes_between(conn, "sos_reports", parts, params, "created_at", "claimed_at")
        out["claimed"] = len(claim)
        out["median_minutes_to_claim"] = _median(claim)
        out["p90_minutes_to_claim"] = _percentile(claim, 0.9)
    if "resolved_at" in cols:
        res = _minutes_between(conn, "sos_reports", parts, params, "created_at", "resolved_at")
        out["resolved"] = len(res)
        out["median_minutes_to_resolve"] = _median(res)
    return out


def m12_ambassadors(sc: _Schema, scope: Scope):
    has_flag = sc.has("users", "is_ambassador")
    has_credits = sc.has("referral_credits", "invitee_id", "referrer_id")
    if not (has_flag or has_credits):
        return _unavailable("нет ни users.is_ambassador, ни referral_credits")
    conn = sc.conn
    parts, params = scope.users()
    out = {"available": True}
    out["ambassadors"] = _scalar(
        conn, f"SELECT COUNT(*) FROM users{_where(parts + ['is_ambassador = 1'])}", params
    ) or 0 if has_flag else None
    if has_credits:
        ids_sql, ids_params = scope.users_ids_sql()
        cr_where = _where([f"invitee_id IN ({ids_sql})"])
        out["referral_credits"] = _scalar(
            conn, f"SELECT COUNT(*) FROM referral_credits{cr_where}", ids_params
        ) or 0
        out["referrers"] = _scalar(
            conn, f"SELECT COUNT(DISTINCT referrer_id) FROM referral_credits{cr_where}", ids_params
        ) or 0
    if sc.has("users", "referrer_id", "status"):
        out["referred_approved"] = _scalar(
            conn,
            f"SELECT COUNT(*) FROM users{_where(parts + ['referrer_id IS NOT NULL', 'status = ?'])}",
            params + ["approved"],
        ) or 0
    return out


METRICS = (
    ("M1", "Заявок подано", m1_applications),
    ("M2", "Городов / событий", m2_cities),
    ("M3", "Воронка «нажал /start → подал анкету»", m3_funnel),
    ("M4", "Брошенных анкет догнали напоминалки", m4_nudges),
    ("M5", "Время до решения по заявке", m5_latency),
    ("M6", "Решений модерации / автоотказов", m6_moderation),
    ("M7", "Чеков через модерацию", m7_payments),
    ("M8", "Доставлено рассылками", m8_broadcasts),
    ("M9", "Форум: пришли", m9_arrivals),
    ("M10", "Форум: отметок на сессиях", m10_sessions),
    ("M11", "Форум: SOS-обращения", m11_sos),
    ("M12", "Амбассадоры и приведённые", m12_ambassadors),
)


def collect(conn, season: str | None = None, city: str | None = None, label: str | None = None) -> dict:
    sc = _Schema(conn)
    scope = Scope(sc, season, city)
    data = {
        "meta": {
            "generated_at": datetime.now(MSK).strftime("%Y-%m-%d %H:%M:%S МСК"),
            "season": scope.season,
            "current_event_season": scope.current_season,
            "city": city,
            "label": label,
        }
    }
    for key, _title, fn in METRICS:
        try:
            data[key] = fn(sc, scope)
        except sqlite3.Error as e:
            data[key] = _unavailable(f"ошибка запроса: {e}")
    return data


# ── человекочитаемая сводка ───────────────────────────────────────────────────────────────

_LABELS = {
    "approved": "одобрено", "pending": "ожидают", "rejected": "отказ", "unknown": "без статуса",
}
_PAYMENT_LABELS = {
    "not_paid": "не оплачено", "receipt_sent": "чек на проверке", "paid": "оплачено",
    "overdue": "просрочено",
}


def _fmt_map(d, labels=None):
    if not d:
        return "—"
    labels = labels or {}
    return ", ".join(f"{labels.get(k, k)}: {v}" for k, v in d.items())


def _fmt(v, suffix=""):
    return "—" if v is None else f"{v}{suffix}"


def _md_lines(key, m):
    if key == "M1":
        lines = [f"Всего: **{m['total']}** ({_fmt_map(m.get('by_status'), _LABELS)})"]
        if "other_seasons" in m:
            lines.append(f"Прошлые сезоны в базе (не в счёте): {_fmt_map(m['other_seasons'])}")
        if m.get("without_season_counted"):
            lines.append(f"В том числе без метки сезона (легаси, считаются текущим): {m['without_season_counted']}")
        if "returning" in m:
            lines.append(f"Повторных (были в прошлом сезоне): {m['returning']}")
        return lines
    if key == "M2":
        enabled = m.get("cities_enabled")
        return [
            f"Включённых городов: {len(enabled) if enabled is not None else '—'}"
            + (f" ({', '.join(enabled)})" if enabled else ""),
            f"Заявки по городам: {_fmt_map(m.get('by_event_city'))}",
        ]
    if key == "M3":
        return [
            f"С {_fmt(m['since'])}: /start — {m['start']}, начали анкету — {m['form_started']}, "
            f"подали — {m['form_completed']} (конверсия {_fmt(m['conversion_pct'], ' %')})",
        ]
    if key == "M4":
        lines = [
            f"Напомнили: {m['nudged']} (с {_fmt(m.get('nudged_since'))}), подали после напоминания: "
            f"{m['completed_after_nudge']} ({_fmt(m.get('caught_pct'), ' %')})",
        ]
        if "pending_total" in m:
            lines.append(
                f"Сейчас не подали: {m['pending_total']}, из них уже получили напоминание: "
                f"{_fmt(m.get('pending_nudged'))}"
            )
        return lines
    if key == "M5":
        return [
            f"Решений: {m['decided']}; медиана — {_fmt(m['median_hours'], ' ч')}, "
            f"90 % заявок — не дольше {_fmt(m['p90_hours'], ' ч')}",
        ]
    if key == "M6":
        return [
            f"Решений: **{m['decisions']}** ({_fmt_map(m['by_decision'], _LABELS)})",
            f"Автоотказ: {_fmt(m.get('auto_reject_people'))} чел."
            + (f", возвращено на модерацию: {m['auto_reject_returned']}" if "auto_reject_returned" in m else ""),
        ]
    if key == "M7":
        if not m["has_payment"]:
            return ["нет оплаты — чеков и оплат в этом стеке не было"]
        return [f"Чеков: **{m['receipts']}**; статусы оплаты: {_fmt_map(m['payment_status'], _PAYMENT_LABELS)}"]
    if key == "M8":
        return [
            f"Доставок (получатель × рассылка): **{m['total_deliveries']}**; сообщений: {m['messages']}; "
            f"рассылок в журнале: {m['broadcasts']}",
        ]
    if key == "M9":
        return [
            f"Пришли: **{m['arrived']}** из одобренных {_fmt(m['approved'])} ({_fmt(m['arrived_pct'], ' %')})",
            f"По дням: {_fmt_map(m['by_day'])}",
            f"По городам: {_fmt_map(m.get('by_city'))}",
        ]
    if key == "M10":
        return [
            f"Отметок на сессиях: **{m['marks']}**, делегатов: {m['delegates']}, "
            f"в среднем на делегата: {_fmt(m['avg_per_delegate'])}",
        ]
    if key == "M11":
        lines = [f"Обращений: **{m['total']}** ({_fmt_map(m.get('by_category'))})"]
        if "claimed" in m:
            lines.append(
                f"Взяли в работу: {m['claimed']}, медиана до ответа — {_fmt(m['median_minutes_to_claim'], ' мин')}"
            )
        if "resolved" in m:
            lines.append(f"Решено: {m['resolved']}, медиана до решения — {_fmt(m['median_minutes_to_resolve'], ' мин')}")
        return lines
    if key == "M12":
        return [
            f"Амбассадоров: **{_fmt(m.get('ambassadors'))}**; начислений за одобренных приглашённых: "
            f"{_fmt(m.get('referral_credits'))} (от {_fmt(m.get('referrers'))} амбассадоров)",
            f"Одобренных с пригласившим: {_fmt(m.get('referred_approved'))}",
        ]
    return [json.dumps(m, ensure_ascii=False)]


def render_md(data: dict) -> str:
    meta = data["meta"]
    head = f"# Снимок для кейса{' ' + meta['label'] if meta.get('label') else ''}"
    out = [
        head,
        "",
        f"Сезон: {_fmt(meta.get('season'))} · город: {meta.get('city') or 'все'} · снят {meta['generated_at']}",
        "",
    ]
    for key, title, _fn in METRICS:
        m = data.get(key) or _unavailable("не посчитано")
        out.append(f"## {key}. {title}")
        if not m.get("available"):
            out.append(f"_нет данных: {m.get('reason')}_")
        else:
            out.extend(f"- {line}" for line in _md_lines(key, m))
            if m.get("note"):
                out.append(f"- _{m['note']}_")
        out.append("")
    return "\n".join(out)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Read-only снимок цифр кейса (M1–M12) по БД стека")
    ap.add_argument("db", help="путь к forum.db (в контейнере: /app/data/forum.db)")
    ap.add_argument("--season", help="сезон, например «YL 26/2»; по умолчанию — event_season из настроек")
    ap.add_argument("--city", help="код города мероприятия (msk, spb, …); по умолчанию — все")
    ap.add_argument("--label", help="метка среза (S0, S1, …)")
    ap.add_argument("--format", choices=("json", "md"), default="json")
    args = ap.parse_args(argv)
    try:
        with open_ro(args.db) as conn:
            data = collect(conn, season=args.season, city=args.city, label=args.label)
    except (FileNotFoundError, sqlite3.Error) as e:
        print(f"Не удалось открыть базу: {e}", file=sys.stderr)
        return 2
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except (AttributeError, ValueError):
        pass
    if args.format == "md":
        print(render_md(data))
    else:
        print(json.dumps(data, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
