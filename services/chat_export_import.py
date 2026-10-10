"""Импорт экспорта Telegram Desktop в историю живого рейтинга чата (chat_messages / chat_reactions):
разбор файла, предпросмотр и запись. Одна реализация для кнопки «📥 Загрузить историю чата» на экране
«🏆 Рейтинг чата» и для `tools/chat_export_import.py`.

Живой учёт пишет журнал только с момента, когда бот стал его вести; всё, что было в чате раньше,
есть только в ручном экспорте (Telegram Desktop → чат → ⋮ → «Экспорт истории чата» →
Machine-readable JSON). Этот модуль переносит такой result.json в те же таблицы, из которых
считает дашборд.

Правила — те же, что у живого учёта (handlers/group_chat.py) и тула статистики:
- текст НЕ пишется, только длина; у пересланного чужого текста длина 0;
- ответ на корень темы (сервисное сообщение) — не ответ; ответ на пост канала — цель без
  автора-человека; сообщения анонимного админа (от имени самой группы) пропускаются;
- пост канала — is_channel_post=1, автор = отрицательный id канала (-100<id>);
- реакции: известные дарители (recent) — строки chat_reactions, остаток count − известных —
  reactions_extra; кастомный эмодзи без document_id получает ключ «export:<позиция>»;
- время — Москва по date_unixtime; сообщение без date_unixtime пропускается (их число — в отчёте);
- срок хранения (chat_rating_retention_days, по умолчанию 180 дней): строки старше него суточная
  чистка бота удалила бы сразу — они не записываются, их число — в отчёте;
- ников в экспорте нет: у авторов и дарителей реакций сохраняется имя (первое слово поля from) в
  chat_usernames — только если бот ещё не знает его сам; живые ники не трогаются.
Повторная запись ничего не добавляет: INSERT OR IGNORE по (чат, id сообщения); живые строки с тем
же id не трогаются (и их реакции тоже).

Только stdlib (+ chat_score и tools.chat_export_stats); настройки бота читаются из bot_settings
напрямую. Все функции синхронные: бот зовёт их через asyncio.to_thread.
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone

import chat_score
from tools.chat_export_stats import (
    ExportError,
    _has_media,
    _text_length,
    parse_export,
)

__all__ = ["ExportError", "parse_export"]

# Москва без перехода на летнее время с 2014 года — фиксированный UTC+3 не требует tzdata.
_MSK = timezone(timedelta(hours=3))
_TS_FORMAT = "%Y-%m-%d %H:%M:%S"
_PER_CITY_PREFIX = "delegate_chat_id__city__"
_BATCH = 1000  # сообщений в одной транзакции записи


def _now() -> datetime:
    """Сейчас по Москве, наивное (как ts в chat_messages). Отдельной функцией — для тестов."""
    return datetime.now(_MSK).replace(tzinfo=None)


def _msk_ts(raw: dict) -> str | None:
    """Время по Москве из date_unixtime (UTC-секунды). Одного поля date мало: это локальное
    время того, кто выгружал, — такое сообщение пропускается."""
    unix = raw.get("date_unixtime")
    if unix is None:
        return None
    try:
        return datetime.fromtimestamp(int(unix), tz=_MSK).strftime(_TS_FORMAT)
    except (TypeError, ValueError, OSError):
        return None


def _first_name(raw_from) -> str | None:
    """Имя из поля from экспорта (там «Имя Фамилия») — только первое слово, без фамилии."""
    if not isinstance(raw_from, str):
        return None
    parts = raw_from.split()
    return parts[0][:64] if parts else None


def _peer(raw_from_id):
    """("user", id) | ("channel", id) | (None, None)."""
    if not isinstance(raw_from_id, str):
        return None, None
    for prefix in ("user", "channel"):
        if raw_from_id.startswith(prefix):
            try:
                return prefix, int(raw_from_id[len(prefix):])
            except ValueError:
                return None, None
    return None, None


def _kind(raw: dict) -> str:
    """Тот же словарь видов, что у живого учёта: text / media / sticker / other."""
    if raw.get("media_type") in ("sticker", "animation"):
        return "sticker"
    if _has_media(raw):
        return "media"
    if raw.get("text") not in (None, "", []):
        return "text"
    return "other"


def _reaction_key(reaction: dict, position: int) -> str:
    """Ключ как у живого учёта. Без опознавательного знака (экспорт кладёт кастомный эмодзи с
    пустым document_id) — ключ по позиции в списке реакций сообщения: иначе две разные
    реакции одного человека слиплись бы в одну строку и «отдача» разошлась с тулом."""
    rtype = reaction.get("type")
    if rtype == "emoji" and reaction.get("emoji"):
        return str(reaction["emoji"])
    if rtype == "custom_emoji" and reaction.get("document_id"):
        return f"custom:{reaction['document_id']}"
    if rtype == "paid":
        return "paid"
    return f"export:{position}"


def build_rows(data: dict, chat_id: int, *, stats: dict | None = None):
    """-> (messages, reactions): строки для chat_messages и chat_reactions. Текста в них нет.
    `stats` (если передан) получает: no_unixtime — сколько сообщений пропущено без
    date_unixtime; names — {telegram_id: имя} авторов и дарителей реакций."""
    stats = stats if stats is not None else {}
    stats.setdefault("no_unixtime", 0)
    names: dict = stats.setdefault("names", {})
    raw_messages = [m for m in data["messages"] if isinstance(m, dict)]
    export_chat = data.get("id")

    # Индекс целей ответа по ВСЕМУ экспорту: id -> (тип, автор-человек или None).
    index: dict = {}
    for raw in raw_messages:
        try:
            mid = int(raw.get("id"))
        except (TypeError, ValueError):
            continue
        mtype = raw.get("type") or "message"
        kind, peer_id = _peer(raw.get("from_id"))
        index[mid] = (mtype, peer_id if (mtype == "message" and kind == "user") else None)

    messages, reactions = [], []
    for raw in raw_messages:
        if (raw.get("type") or "message") != "message":
            continue
        try:
            mid = int(raw.get("id"))
        except (TypeError, ValueError):
            continue
        ts = _msk_ts(raw)
        if ts is None:
            stats["no_unixtime"] += 1
            continue
        peer_kind, peer_id = _peer(raw.get("from_id"))
        if peer_kind is None:
            continue
        is_channel = peer_kind == "channel"
        if is_channel and export_chat is not None and str(peer_id) == str(export_chat):
            continue  # анонимный админ пишет от имени группы — живой учёт такие не видит
        author = int(f"-100{peer_id}") if is_channel else peer_id
        if not is_channel and (name := _first_name(raw.get("from"))):
            names[author] = name

        if is_channel or not raw.get("forwarded_from"):
            text_len = _text_length(raw.get("text"))
        else:
            text_len = 0

        reply_mid = reply_author = None
        target_raw = raw.get("reply_to_message_id")
        if target_raw is not None and not is_channel:
            try:
                target = int(target_raw)
            except (TypeError, ValueError):
                target = None
            if target is not None:
                t_type, t_author = index.get(target, ("message", None))
                if t_type == "message":  # сервисное (корень темы) — не ответ
                    reply_mid = target
                    reply_author = t_author

        total, givers = 0, []
        for position, reaction in enumerate(raw.get("reactions") or []):
            if not isinstance(reaction, dict):
                continue
            count = reaction.get("count")
            if isinstance(count, int):
                total += count
            key = _reaction_key(reaction, position)
            for entry in reaction.get("recent") or []:
                if not isinstance(entry, dict):
                    continue
                g_kind, g_id = _peer(entry.get("from_id"))
                if g_kind == "user" and (g_id, key) not in givers:
                    givers.append((g_id, key))
                if g_kind == "user" and (name := _first_name(entry.get("from"))):
                    names.setdefault(g_id, name)
        for g_id, key in givers:
            reactions.append((chat_id, mid, g_id, key, ts))

        messages.append({
            "chat_id": chat_id, "message_id": mid, "telegram_id": author, "ts": ts,
            "kind": _kind(raw), "text_len": text_len,
            "reply_to_message_id": reply_mid, "reply_to_author_id": reply_author,
            "is_channel_post": 1 if is_channel else 0,
            "reactions_extra": max(0, total - len(givers)),
        })
    return messages, reactions


def retention_days(conn) -> int:
    """Срок хранения истории чата из bot_settings (тот же разбор, что у бота и дашборда)."""
    row = conn.execute(
        "SELECT value FROM bot_settings WHERE key = ?", (chat_score.RETENTION_KEY,),
    ).fetchone()
    value = chat_score._parse_non_negative(row[0] if row else None)
    return int(value) if value is not None and int(value) > 0 else chat_score.DEFAULT_RETENTION_DAYS


def bound_city(conn, chat_id: int):
    """(True, код города | None для общего чата) или (False, None) — чат не привязан."""
    rows = conn.execute(
        "SELECT key, value FROM bot_settings WHERE key LIKE 'delegate_chat_id%'"
    ).fetchall()
    for key, value in rows:
        if str(value or "").strip() != str(chat_id):
            continue
        if key == "delegate_chat_id":
            return True, None
        if key.startswith(_PER_CITY_PREFIX):
            return True, key[len(_PER_CITY_PREFIX):]
    return False, None


def make_plan(conn, data: dict, chat_id: int) -> dict:
    """Предпросмотр: только чтение. Что в файле, что из этого уже есть в базе и что добавится.

    `to_add` — сообщения, которых в базе ещё нет и которые не старше срока хранения (именно их и
    запишет `apply_plan`)."""
    is_bound, city = bound_city(conn, chat_id)
    stats: dict = {}
    messages, reactions = build_rows(data, chat_id, stats=stats)
    keep_days = retention_days(conn)
    cutoff = (_now() - timedelta(days=keep_days)).strftime(_TS_FORMAT)
    too_old = [m for m in messages if m["ts"] < cutoff]
    existing = {
        r[0] for r in conn.execute(
            "SELECT message_id FROM chat_messages WHERE chat_id = ?", (chat_id,),
        )
    }
    days = sorted(m["ts"][:10] for m in messages)
    to_add = [m for m in messages if m["message_id"] not in existing and m["ts"] >= cutoff]
    add_days = sorted(m["ts"][:10] for m in to_add)
    return {
        "chat_id": chat_id, "export_name": data.get("name"), "export_id": data.get("id"),
        "is_bound": is_bound, "city": city,
        "messages": messages, "reactions": reactions, "stats": stats,
        "keep_days": keep_days, "cutoff": cutoff, "too_old": len(too_old),
        "days": (days[0], days[-1]) if days else None,
        "already": sum(1 for m in messages if m["message_id"] in existing),
        "to_add": len(to_add),
        "to_add_authors": len({m["telegram_id"] for m in to_add}),
        "to_add_days": (add_days[0], add_days[-1]) if add_days else None,
    }


def apply_plan(conn, plan: dict) -> tuple[int, int]:
    """Записывает ровно то, что показал `make_plan`: (новых сообщений, новых реакций).

    Пачками по `_BATCH` сообщений, каждая — своя транзакция вместе с реакциями и именами её
    сообщений: одна длинная транзакция на весь файл держит блокировку записи, и живой учёт чата
    (handlers/group_chat.py) ловил бы «database is locked». Сбой посередине оставляет целые
    пачки; повторный вызов (запись по id сообщения) доделывает остальное и ничего не задваивает."""
    messages, reactions = plan["messages"], plan["reactions"]
    cutoff, names = plan["cutoff"], plan["stats"]["names"]
    added_reactions = 0
    has_first_name = "first_name" in {
        r[1] for r in conn.execute("PRAGMA table_info(chat_usernames)")
    }
    now_ts = _now().strftime(_TS_FORMAT)
    by_mid: dict = {}
    for row in reactions:
        by_mid.setdefault(row[1], []).append(row)
    fresh = [m for m in messages if m["ts"] >= cutoff]  # старше срока хранения — суточная чистка удалила бы сразу
    inserted = set()
    for start in range(0, len(fresh), _BATCH):
        with conn:
            batch_inserted = set()
            for m in fresh[start:start + _BATCH]:
                cur = conn.execute(
                    "INSERT OR IGNORE INTO chat_messages (chat_id, message_id, telegram_id, ts, "
                    "kind, text_len, reply_to_message_id, reply_to_author_id, is_channel_post, "
                    "reactions_extra, source) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'export')",
                    (m["chat_id"], m["message_id"], m["telegram_id"], m["ts"], m["kind"],
                     m["text_len"], m["reply_to_message_id"], m["reply_to_author_id"],
                     m["is_channel_post"], m["reactions_extra"]),
                )
                if cur.rowcount == 1:
                    batch_inserted.add(m["message_id"])
            people = {m["telegram_id"] for m in fresh[start:start + _BATCH]
                      if m["message_id"] in batch_inserted}
            for mid in batch_inserted:  # у живой строки свои реакции — их не смешиваем
                for row in by_mid.get(mid, ()):
                    cur = conn.execute(
                        "INSERT OR IGNORE INTO chat_reactions (chat_id, message_id, telegram_id, "
                        "reaction, ts) VALUES (?, ?, ?, ?, ?)", row,
                    )
                    added_reactions += cur.rowcount
                    people.add(row[2])
            if has_first_name:
                for tid in sorted(p for p in people if p in names):
                    conn.execute(
                        "INSERT INTO chat_usernames (telegram_id, username, first_name, updated_at) "
                        "VALUES (?, NULL, ?, ?) ON CONFLICT(telegram_id) DO UPDATE SET "
                        "first_name = COALESCE(chat_usernames.first_name, excluded.first_name)",
                        (tid, names[tid], now_ts),
                    )
            inserted |= batch_inserted
    return len(inserted), added_reactions
