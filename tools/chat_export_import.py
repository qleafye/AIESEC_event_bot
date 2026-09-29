"""Импорт экспорта Telegram Desktop в историю живого рейтинга чата (chat_messages / chat_reactions).

Зачем. Живой учёт пишет журнал только с момента, когда бот стал его вести; всё, что было в
чате раньше, есть только в ручном экспорте (Telegram Desktop → чат → ⋮ → «Экспорт истории
чата» → Machine-readable JSON). Этот тул переносит такой result.json в те же таблицы, из
которых считает дашборд, — и рейтинг города начинается с первого дня чата, а не с выката.

Запуск — внутри контейнера бота, СНАЧАЛА пробный прогон (он ничего не пишет):
    docker compose exec bot python tools/chat_export_import.py /app/data/result.json --db /app/data/forum.db
    docker compose exec bot python tools/chat_export_import.py /app/data/result.json --db /app/data/forum.db --apply

Флаги:
    --chat-id -100…  чат в терминах Bot API, если id из экспорта не тот (по умолчанию -100<id>)
    --apply          записать (без него — только отчёт, что было бы записано)
    --force          писать в чат, который не привязан ни к одному городу

Правила — те же, что у живого учёта (handlers/group_chat.py) и тула статистики:
- текст НЕ пишется, только длина; у пересланного чужого текста длина 0;
- ответ на корень темы (сервисное сообщение) — не ответ; ответ на пост канала — цель без
  автора-человека; сообщения анонимного админа (от имени самой группы) пропускаются;
- пост канала — is_channel_post=1, автор = отрицательный id канала (-100<id>);
- реакции: известные дарители (recent) — строки chat_reactions, остаток count − известных —
  reactions_extra (Telegram хранит неполный список дарителей); кастомный эмодзи без
  document_id получает ключ «export:<позиция>»;
- время — Москва по date_unixtime; поле date в экспорте — локальное время того, кто выгружал,
  поэтому сообщение без date_unixtime пропускается (их число — в отчёте);
- срок хранения (chat_rating_retention_days, по умолчанию 180 дней): строки старше него
  суточная чистка бота удалила бы сразу — пробный прогон пишет, сколько таких, --apply их
  не записывает;
- ников в экспорте нет: у авторов и дарителей реакций сохраняется имя (первое слово поля
  from) в chat_usernames — только если бот ещё не знает его сам; живые ники не трогаются.
Повторный запуск ничего не добавляет: INSERT OR IGNORE по (чат, id сообщения); живые строки с
тем же id не трогаются (и их реакции тоже). Разрыв между датой экспорта и началом живого учёта
не заполняется — выгрузите экспорт прямо перед импортом, перекрытие безопасно.

Только stdlib; настройки бота читаются из bot_settings напрямую (реестр бота тут не нужен).
Коды выхода: 0 — готово (или пробный прогон); 1 — плохой вход, нет таблиц или отказ.
"""
import argparse
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

_REPO_ROOT = str(Path(__file__).resolve().parent.parent)
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from core import chat_score  # noqa: E402 — после бутстрапа sys.path; только stdlib
from tools.chat_export_stats import (  # noqa: E402 — после бутстрапа sys.path
    ExportError,
    _has_media,
    _text_length,
    load_export,
)

# Москва без перехода на летнее время с 2014 года — фиксированный UTC+3 не требует tzdata.
_MSK = timezone(timedelta(hours=3))
_TS_FORMAT = "%Y-%m-%d %H:%M:%S"
_PER_CITY_PREFIX = "delegate_chat_id__city__"


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


def build_arg_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        description="Импорт экспорта Telegram Desktop (result.json) в историю рейтинга чата.",
    )
    ap.add_argument("export", help="путь к result.json (Machine-readable JSON)")
    ap.add_argument("--db", default="data/forum.db", help="forum.db бота, по умолчанию data/forum.db")
    ap.add_argument("--chat-id", type=int, default=None, help="id чата в Bot API (-100…)")
    ap.add_argument("--apply", action="store_true", help="записать; без флага — пробный прогон")
    ap.add_argument("--force", action="store_true", help="писать в чат, не привязанный к городу")
    return ap


def _err(text: str) -> int:
    print(text, file=sys.stderr)
    return 1


def main(argv=None) -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass
    args = build_arg_parser().parse_args(argv)

    try:
        data = load_export(args.export)
    except ExportError as e:
        return _err(str(e))

    chat_id = args.chat_id
    if chat_id is None:
        try:
            chat_id = int(f"-100{int(data.get('id'))}")
        except (TypeError, ValueError):
            return _err("В экспорте нет id чата — укажите его явно: --chat-id -100…")

    if not Path(args.db).is_file():
        return _err(f"Не нашёл базу «{args.db}». Укажите путь к forum.db бота: --db …")
    try:
        conn = sqlite3.connect(args.db, timeout=30)
    except sqlite3.Error as e:
        return _err(f"Не открыл базу «{args.db}»: {e}")
    try:
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if not {"chat_messages", "chat_reactions", "bot_settings"} <= tables:
            return _err(
                "В базе нет таблиц chat_messages / chat_reactions — это старая версия бота. "
                "Обновите и перезапустите бота (таблицы создаются при старте), потом повторите."
            )

        is_bound, city = bound_city(conn, chat_id)
        stats: dict = {}
        messages, reactions = build_rows(data, chat_id, stats=stats)
        keep_days = retention_days(conn)
        cutoff = (_now() - timedelta(days=keep_days)).strftime(_TS_FORMAT)
        too_old = [m for m in messages if m["ts"] < cutoff]
        days = sorted(m["ts"][:10] for m in messages)
        existing = {
            r[0] for r in conn.execute(
                "SELECT message_id FROM chat_messages WHERE chat_id = ?", (chat_id,),
            )
        }
        new_ids = {m["message_id"] for m in messages} - existing

        print(f"Чат: {data.get('name') or 'без названия'} → chat_id {chat_id}")
        if is_bound:
            print(f"Привязан к городу: {city if city else 'общий чат (без города)'}")
        else:
            print("Чат не привязан ни к одному городу.")
        print(f"Период: {days[0]} — {days[-1]}" if days else "Период: сообщений нет")
        print(f"Сообщений в экспорте: {len(messages)} (уже есть в базе: {len(messages) - len(new_ids)})")
        print(f"Из них ответов людям/постам: {sum(1 for m in messages if m['reply_to_message_id'])}")
        print(f"Постов канала: {sum(m['is_channel_post'] for m in messages)}")
        print(f"Реакций с известным автором: {len(reactions)}; "
              f"без автора (в reactions_extra): {sum(m['reactions_extra'] for m in messages)}")
        if stats["no_unixtime"]:
            print(f"Пропущено сообщений без date_unixtime: {stats['no_unixtime']} (поле date — "
                  "локальное время того, кто выгружал; выгрузите экспорт заново в Telegram Desktop)")
        if too_old:
            print(f"Сообщений старше срока хранения ({keep_days} дн.): {len(too_old)} — бот удалил бы "
                  "их первой же суточной чисткой, поэтому при --apply они не записываются. "
                  "Нужна история длиннее — увеличьте «Сколько дней хранить историю чата» в боте "
                  "и повторите импорт.")
        print("Ников в экспорте нет: у тех, кого бот ещё не видел в чате, на дашборде будет "
              "имя из Telegram (первое слово), ник появится, когда человек напишет в чат.")

        if not is_bound and not args.force:
            return _err(
                f"Чат {chat_id} не привязан ни к одному городу в настройках бота — скорее всего, "
                "id не тот. Проверьте --chat-id (чат в боте: «🔧 Управление» → чат делегатов) "
                "или добавьте --force, если импорт нужен именно сюда."
            )
        if not args.apply:
            print("Пробный прогон: ничего не записано. Чтобы записать — повторите с --apply.")
            return 0

        added_messages = added_reactions = 0
        has_first_name = "first_name" in {
            r[1] for r in conn.execute("PRAGMA table_info(chat_usernames)")
        }
        now_ts = _now().strftime(_TS_FORMAT)
        with conn:  # одна транзакция: либо всё, либо ничего
            inserted = set()
            for m in messages:
                if m["ts"] < cutoff:
                    continue  # старше срока хранения — суточная чистка удалила бы сразу
                cur = conn.execute(
                    "INSERT OR IGNORE INTO chat_messages (chat_id, message_id, telegram_id, ts, "
                    "kind, text_len, reply_to_message_id, reply_to_author_id, is_channel_post, "
                    "reactions_extra, source) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'export')",
                    (m["chat_id"], m["message_id"], m["telegram_id"], m["ts"], m["kind"],
                     m["text_len"], m["reply_to_message_id"], m["reply_to_author_id"],
                     m["is_channel_post"], m["reactions_extra"]),
                )
                if cur.rowcount == 1:
                    inserted.add(m["message_id"])
            added_messages = len(inserted)
            for row in reactions:
                if row[1] not in inserted:
                    continue  # у живой строки свои реакции — не смешиваем
                cur = conn.execute(
                    "INSERT OR IGNORE INTO chat_reactions (chat_id, message_id, telegram_id, "
                    "reaction, ts) VALUES (?, ?, ?, ?, ?)", row,
                )
                added_reactions += cur.rowcount
            if has_first_name:
                people = {m["telegram_id"] for m in messages if m["message_id"] in inserted}
                people |= {row[2] for row in reactions if row[1] in inserted}
                for tid in sorted(p for p in people if p in stats["names"]):
                    conn.execute(
                        "INSERT INTO chat_usernames (telegram_id, username, first_name, updated_at) "
                        "VALUES (?, NULL, ?, ?) ON CONFLICT(telegram_id) DO UPDATE SET "
                        "first_name = COALESCE(chat_usernames.first_name, excluded.first_name)",
                        (tid, stats["names"][tid], now_ts),
                    )
        print(f"Записано. Новых сообщений: {added_messages}, новых реакций: {added_reactions}.")
        if too_old:
            print(f"Старше срока хранения не записаны: {len(too_old)}.")
        return 0
    except sqlite3.Error as e:
        return _err(f"Ошибка базы: {e}. Ничего не записано.")
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
